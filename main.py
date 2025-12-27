#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import struct
from dataclasses import dataclass
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal, QObject, QDateTime, QTimer, QSize, QSettings
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QSpinBox, QMessageBox,
    QGroupBox, QFormLayout, QFileDialog, QProgressBar, QCheckBox,
    QTabWidget, QListWidget, QSplitter, QSlider, QComboBox, QColorDialog, QDialog
)
from PyQt5.QtNetwork import QTcpServer, QTcpSocket, QHostAddress, QAbstractSocket, QNetworkInterface
from PyQt5.QtGui import QPixmap, QPainter, QColor, QFont, QLinearGradient, QBrush

# -------------------- 协议常量 --------------------
MAGIC = b"PC"      # Protocol Chat
VERSION = 2
HEADER_LEN = 8      # 2(MAGIC) + 1(V) + 1(TYPE) + 4(LEN)

# P2P 协议
T_TEXT       = 0x01  # 文本（utf-8）
T_FILE_META  = 0x10  # 文件元信息（json: name,size）
T_FILE_CHUNK = 0x11  # 文件数据分片
T_FILE_END   = 0x12  # 文件结束

# 服务器协议类型
S_REGISTER = 0x20      # 注册/登录 (json: username)
S_JOIN_ROOM = 0x21     # 加入房间 (json: room_id)
S_LEAVE_ROOM = 0x22    # 离开房间
S_CHAT_PUBLIC = 0x23   # 公共聊天 (json: message, room_id)
S_CHAT_PRIVATE = 0x24  # 私聊 (json: message, to_user)
S_USER_LIST = 0x25     # 获取用户列表 (json: room_id)
S_HEARTBEAT = 0x26     # 心跳
S_FILE_META = 0x30     # 文件元信息 (json: name, size, room_id/target_user, is_private)
S_FILE_CHUNK = 0x31    # 文件数据
S_FILE_END = 0x32      # 文件结束

# 服务器->客户端类型
SC_LOGIN_SUCCESS = 0x40     # 登录成功 (json: user_id, username)
SC_USER_JOINED = 0x41       # 用户加入 (json: user_id, username, room_id)
SC_USER_LEFT = 0x42         # 用户离开 (json: user_id, username)
SC_CHAT_MESSAGE = 0x43      # 聊天消息 (json: from_user, from_username, message, room_id, is_private)
SC_USER_LIST = 0x44         # 用户列表 (json: users)
SC_ERROR = 0x45             # 错误 (json: message)

CHUNK_SIZE = 32 * 1024  # 32KB 分片


def now_ts():
    return QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")


def get_local_ipv4_list():
    addrs = []
    for addr in QNetworkInterface.allAddresses():
        if addr.protocol() == QAbstractSocket.IPv4Protocol and not addr.isLoopback():
            addrs.append(addr.toString())
    # 去重并保持顺序
    seen = set()
    result = []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result or ["127.0.0.1"]


@dataclass
class IncomingFile:
    name: str
    size: int
    received: int = 0
    fp: Optional[object] = None
    path: Optional[str] = None


class ServerPeer(QObject):
    """封装服务器连接模式。"""

    message_received = pyqtSignal(str, str, bool)  # (message, from_user, is_private)
    connected = pyqtSignal(str)
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    login_success = pyqtSignal(str, str)  # (user_id, username)
    user_joined = pyqtSignal(str, str, str)  # (user_id, username, room_id)
    user_left = pyqtSignal(str, str)  # (user_id, username)
    user_list_received = pyqtSignal(list)  # (users)

    file_incoming = pyqtSignal(str, int, str, bool)  # (name, size, from_user, is_private)
    file_progress = pyqtSignal(int, int)
    file_saved = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.socket: Optional[QTcpSocket] = None
        self._rxbuf = bytearray()
        self._incoming: Optional[IncomingFile] = None
        self._download_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(self._download_dir, exist_ok=True)
        self._sending_fp = None
        self._send_timer = QTimer(self)
        self._send_timer.timeout.connect(self._send_next_chunk)
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._send_heartbeat)
        self._user_id = ""
        self._username = ""
        self._room_id = "default"

    def connect_to_server(self, host: str, port: int, username: str):
        self._username = username
        self.close_socket()
        sock = QTcpSocket(self)
        self.socket = sock
        sock.readyRead.connect(self._on_ready_read)
        sock.disconnected.connect(self._on_disconnected)
        sock.errorOccurred.connect(lambda _err: self.error.emit(sock.errorString()))
        sock.connected.connect(lambda: self._register())
        sock.connectToHost(host, port)

    def _register(self):
        data = json.dumps({"username": self._username})
        self._send_frame(S_REGISTER, data.encode('utf-8'))

    def join_room(self, room_id: str):
        self._room_id = room_id
        data = json.dumps({"room_id": room_id})
        self._send_frame(S_JOIN_ROOM, data.encode('utf-8'))

    def send_public_message(self, message: str):
        data = json.dumps({"message": message, "room_id": self._room_id})
        self._send_frame(S_CHAT_PUBLIC, data.encode('utf-8'))

    def send_private_message(self, to_user: str, message: str):
        data = json.dumps({"message": message, "to_user": to_user})
        self._send_frame(S_CHAT_PRIVATE, data.encode('utf-8'))

    def request_user_list(self, room_id: str = None):
        data = json.dumps({"room_id": room_id or self._room_id})
        self._send_frame(S_USER_LIST, data.encode('utf-8'))

    def send_file(self, file_path: str, to_user: str = None, is_private: bool = False):
        if not self.socket or self.socket.state() != QAbstractSocket.ConnectedState:
            return False
        try:
            size = os.path.getsize(file_path)
            name = os.path.basename(file_path)
            meta = json.dumps({
                "name": name,
                "size": size,
                "room_id": self._room_id if not is_private else "",
                "target_user": to_user if is_private else "",
                "is_private": is_private
            }).encode('utf-8')
            if not self._send_frame(S_FILE_META, meta):
                return False
            self._sending_fp = open(file_path, 'rb')
            self._send_remaining = size
            self._send_timer.start(10)
            return True
        except OSError as e:
            self.error.emit(f"发送文件失败：{e}")
            if self._sending_fp:
                self._sending_fp.close()
                self._sending_fp = None
            return False

    def _send_heartbeat(self):
        if self.socket and self.socket.state() == QAbstractSocket.ConnectedState:
            self._send_frame(S_HEARTBEAT, b"")

    def close_socket(self):
        if self.socket:
            try:
                if self.socket.state() == QAbstractSocket.ConnectedState:
                    self.socket.disconnectFromHost()
            finally:
                self.socket.deleteLater()
                self.socket = None
        if self._sending_fp:
            try:
                self._sending_fp.close()
            except OSError:
                pass
            self._sending_fp = None
        self._send_timer.stop()
        self._heartbeat_timer.stop()
        self._rxbuf.clear()
        self._cleanup_incoming()

    def _on_ready_read(self):
        if not self.socket:
            return
        try:
            self._rxbuf.extend(bytes(self.socket.readAll()))
            while True:
                if len(self._rxbuf) < HEADER_LEN:
                    return
                if self._rxbuf[0:2] != MAGIC:
                    self._rxbuf.pop(0)
                    continue
                ver = self._rxbuf[2]
                typ = self._rxbuf[3]
                length = struct.unpack('>I', self._rxbuf[4:8])[0]
                if len(self._rxbuf) < HEADER_LEN + length:
                    return
                payload = bytes(self._rxbuf[8:8+length])
                del self._rxbuf[:8+length]
                self._handle_frame(ver, typ, payload)
        except Exception as e:
            self.error.emit(f"接收解析错误：{e}")

    def _handle_frame(self, ver: int, typ: int, payload: bytes):
        if ver != VERSION:
            self.error.emit("协议版本不匹配")
            return

        if typ == SC_LOGIN_SUCCESS:
            data = json.loads(payload.decode('utf-8'))
            self.login_success.emit(data.get('user_id', ''), data.get('username', ''))
        elif typ == SC_USER_JOINED:
            data = json.loads(payload.decode('utf-8'))
            self.user_joined.emit(data.get('user_id', ''), data.get('username', ''), data.get('room_id', ''))
        elif typ == SC_USER_LEFT:
            data = json.loads(payload.decode('utf-8'))
            self.user_left.emit(data.get('user_id', ''), data.get('username', ''))
        elif typ == SC_CHAT_MESSAGE:
            data = json.loads(payload.decode('utf-8'))
            self.message_received.emit(data.get('message', ''), data.get('from_user', ''), data.get('is_private', False))
        elif typ == SC_USER_LIST:
            data = json.loads(payload.decode('utf-8'))
            self.user_list_received.emit(data.get('users', []))
        elif typ == SC_ERROR:
            data = json.loads(payload.decode('utf-8'))
            self.error.emit(data.get('message', ''))
        elif typ == S_FILE_META:
            data = json.loads(payload.decode('utf-8'))
            try:
                name = data.get('name', '')
                size = int(data.get('size', 0))
                save_path = self._unique_path(os.path.join(self._download_dir, name))
                fp = open(save_path, 'wb')
                self._incoming = IncomingFile(name=name, size=size, received=0, fp=fp, path=save_path)
                self.file_incoming.emit(name, size, '', False)
                self.file_progress.emit(0, size)
            except (json.JSONDecodeError, OSError) as e:
                self.error.emit(f"接收文件元信息失败：{e}")
        elif typ == S_FILE_CHUNK:
            inc = self._incoming
            if not inc or not inc.fp:
                return
            try:
                inc.fp.write(payload)
                inc.received += len(payload)
                self.file_progress.emit(inc.received, inc.size)
            except OSError:
                pass
        elif typ == S_FILE_END:
            inc = self._incoming
            if inc and inc.fp:
                try:
                    inc.fp.flush()
                    inc.fp.close()
                    self.file_saved.emit(inc.path or "")
                except OSError:
                    self.file_saved.emit(inc.path or "")
            self._incoming = None

    def _unique_path(self, path: str) -> str:
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while True:
            trial = f"{base} ({i}){ext}"
            if not os.path.exists(trial):
                return trial
            i += 1

    def _cleanup_incoming(self):
        if self._incoming and self._incoming.fp:
            try:
                self._incoming.fp.close()
            except OSError:
                pass
        self._incoming = None

    def _send_heartbeat(self):
        if self.socket and self.socket.state() == QAbstractSocket.ConnectedState:
            self._send_frame(S_HEARTBEAT, b"")

    def close_socket(self):
        if self.socket:
            try:
                if self.socket.state() == QAbstractSocket.ConnectedState:
                    self.socket.disconnectFromHost()
            finally:
                self.socket.deleteLater()
                self.socket = None
        if self._sending_fp:
            try:
                self._sending_fp.close()
            except OSError:
                pass
            self._sending_fp = None
        self._send_timer.stop()
        self._heartbeat_timer.stop()
        self._rxbuf.clear()
        self._cleanup_incoming()

    def _on_disconnected(self):
        self.disconnected.emit()
        self.close_socket()

    def _send_frame(self, typ: int, payload: bytes) -> bool:
        if not self.socket:
            return False
        try:
            header = MAGIC + bytes([VERSION, typ]) + struct.pack('>I', len(payload))
            n1 = self.socket.write(header)
            n2 = self.socket.write(payload)
            self.socket.flush()
            return n1 == len(header) and n2 == len(payload)
        except Exception as e:
            self.error.emit(f"发送失败：{e}")
            return False
        try:
            size = os.path.getsize(file_path)
            name = os.path.basename(file_path)
            meta = json.dumps({
                "name": name,
                "size": size,
                "room_id": self._room_id if not is_private else "",
                "target_user": to_user if is_private else "",
                "is_private": is_private
            }).encode('utf-8')
            if not self._send_frame(S_FILE_META, meta):
                return False
            self._sending_fp = open(file_path, 'rb')
            self._send_remaining = size
            self._send_timer.start(10)
            return True
        except OSError as e:
            self.error.emit(f"发送文件失败：{e}")
            if self._sending_fp:
                self._sending_fp.close()
                self._sending_fp = None
            return False

    def _send_heartbeat(self):
        if self.socket and self.socket.state() == QAbstractSocket.ConnectedState:
            self._send_frame(S_HEARTBEAT, b"")

    def close_socket(self):
        if self.socket:
            try:
                if self.socket.state() == QAbstractSocket.ConnectedState:
                    self.socket.disconnectFromHost()
            finally:
                self.socket.deleteLater()
                self.socket = None
        if self._sending_fp:
            try:
                self._sending_fp.close()
            except OSError:
                pass
            self._sending_fp = None
        self._send_timer.stop()
        self._heartbeat_timer.stop()
        self._rxbuf.clear()
        self._cleanup_incoming()

    def _on_ready_read(self):
        if not self.socket:
            return
        try:
            self._rxbuf.extend(bytes(self.socket.readAll()))
            while True:
                if len(self._rxbuf) < HEADER_LEN:
                    return
                if self._rxbuf[0:2] != MAGIC:
                    self._rxbuf.pop(0)
                    continue
                ver = self._rxbuf[2]
                typ = self._rxbuf[3]
                length = struct.unpack('>I', self._rxbuf[4:8])[0]
                if len(self._rxbuf) < HEADER_LEN + length:
                    return
                payload = bytes(self._rxbuf[8:8+length])
                del self._rxbuf[:8+length]
                self._handle_frame(ver, typ, payload)
        except Exception as e:
            self.error.emit(f"接收解析错误：{e}")

    def _handle_frame(self, ver: int, typ: int, payload: bytes):
        if ver != VERSION:
            self.error.emit("协议版本不匹配")
            return

        if typ == SC_LOGIN_SUCCESS:
            data = json.loads(payload.decode('utf-8'))
            self._user_id = data.get('user_id', '')
            username = data.get('username', '')
            self.login_success.emit(self._user_id, username)
            self._heartbeat_timer.start(30000)
        elif typ == SC_USER_JOINED:
            data = json.loads(payload.decode('utf-8'))
            self.user_joined.emit(data.get('user_id', ''), data.get('username', ''), data.get('room_id', ''))
        elif typ == SC_USER_LEFT:
            data = json.loads(payload.decode('utf-8'))
            self.user_left.emit(data.get('user_id', ''), data.get('username', ''))
        elif typ == SC_CHAT_MESSAGE:
            data = json.loads(payload.decode('utf-8'))
            from_user = data.get('from_user', '')
            from_username = data.get('from_username', '')
            is_private = data.get('is_private', False)
            message = data.get('message', '')
            display_msg = f"<b>{from_username}</b>" if is_private else f"<b>{from_username}</b>"
            self.message_received.emit(message, display_msg, is_private)
        elif typ == SC_USER_LIST:
            data = json.loads(payload.decode('utf-8'))
            users = data.get('users', [])
            self.user_list_received.emit(users)
        elif typ == SC_ERROR:
            data = json.loads(payload.decode('utf-8'))
            self.error.emit(data.get('message', '未知错误'))
        elif typ == S_FILE_META:
            data = json.loads(payload.decode('utf-8'))
            safe_name = os.path.basename(data.get('name', '')) or 'recv.bin'
            size = int(data.get('size', 0))
            from_user = data.get('from_user', '')
            is_private = data.get('is_private', False)
            save_path = self._unique_path(os.path.join(self._download_dir, safe_name))
            fp = open(save_path, 'wb')
            self._incoming = IncomingFile(name=safe_name, size=size, received=0, fp=fp, path=save_path)
            self.file_incoming.emit(safe_name, size, from_user, is_private)
            self.file_progress.emit(0, size)
        elif typ == S_FILE_CHUNK:
            inc = self._incoming
            if not inc or not inc.fp:
                return
            try:
                inc.fp.write(payload)
                inc.received += len(payload)
                self.file_progress.emit(inc.received, inc.size)
            except OSError as e:
                self.error.emit(f"文件写入失败：{e}")
                if inc.fp:
                    try:
                        inc.fp.close()
                    except OSError:
                        pass
                self._incoming = None
        elif typ == S_FILE_END:
            inc = self._incoming
            if inc and inc.fp:
                try:
                    inc.fp.flush()
                    inc.fp.close()
                    self.file_saved.emit(inc.path or "")
                except OSError:
                    self.file_saved.emit(inc.path or "")
            self._incoming = None

    def _send_next_chunk(self):
        if not self._sending_fp:
            self._send_timer.stop()
            return
        try:
            chunk = self._sending_fp.read(CHUNK_SIZE)
            if not chunk:
                self._sending_fp.close()
                self._sending_fp = None
                self._send_timer.stop()
                self._send_frame(S_FILE_END, b"")
                return
            if not self._send_frame(S_FILE_CHUNK, chunk):
                self._send_timer.stop()
                self._sending_fp.close()
                self._sending_fp = None
        except OSError as e:
            self._send_timer.stop()
            if self._sending_fp:
                self._sending_fp.close()
                self._sending_fp = None
            self.error.emit(f"发送文件失败：{e}")

    def _on_disconnected(self):
        self.disconnected.emit()
        self.close_socket()

    def _unique_path(self, path: str) -> str:
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while True:
            trial = f"{base} ({i}){ext}"
            if not os.path.exists(trial):
                return trial
            i += 1

    def _cleanup_incoming(self):
        if self._incoming and self._incoming.fp:
            try:
                self._incoming.fp.close()
            except OSError:
                pass
        self._incoming = None

    def _send_frame(self, typ: int, payload: bytes) -> bool:
        if not self.socket:
            return False
        try:
            header = MAGIC + bytes([VERSION, typ]) + struct.pack('>I', len(payload))
            n1 = self.socket.write(header)
            n2 = self.socket.write(payload)
            self.socket.flush()
            return n1 == len(header) and n2 == len(payload)
        except Exception as e:
            self.error.emit(f"发送失败：{e}")
            return False


class Peer(QObject):
    """封装 P2P 连接：服务端/客户端 + 自定义帧协议（文本/文件）。"""

    message_received = pyqtSignal(str)
    connected = pyqtSignal(str)
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    listening = pyqtSignal(int)

    file_incoming = pyqtSignal(str, int)
    file_progress = pyqtSignal(int, int)
    file_saved = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: Optional[QTcpServer] = None
        self.socket: Optional[QTcpSocket] = None
        self._rxbuf = bytearray()
        self._incoming: Optional[IncomingFile] = None
        self._download_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(self._download_dir, exist_ok=True)
        self._sending_fp = None
        self._send_timer = QTimer(self)
        self._send_timer.timeout.connect(self._send_next_chunk)

    # -------------------- 作为服务端 --------------------
    def start_listening(self, port: int) -> bool:
        self.stop_listening()
        self.server = QTcpServer(self)
        ok = self.server.listen(QHostAddress.Any, port)
        if not ok:
            self.error.emit(f"监听失败：{self.server.errorString()}")
            self.server = None
            return False
        self.server.newConnection.connect(self._on_new_connection)
        self.listening.emit(self.server.serverPort())
        return True

    def stop_listening(self):
        if self.server:
            self.server.close()
            self.server.deleteLater()
            self.server = None

    def _on_new_connection(self):
        if not self.server:
            return
        incoming = self.server.nextPendingConnection()
        if self.socket and self.socket.state() == QAbstractSocket.ConnectedState:
            incoming.disconnectFromHost()
            incoming.deleteLater()
            return
        self._attach_socket(incoming)

    # -------------------- 作为客户端 --------------------
    def connect_to(self, host: str, port: int):
        self.close_socket()
        sock = QTcpSocket(self)
        self._attach_socket(sock)
        sock.connectToHost(host, port)

    # -------------------- 公共：socket 绑定/关闭 --------------------
    def _attach_socket(self, sock: QTcpSocket):
        self.socket = sock
        sock.readyRead.connect(self._on_ready_read)
        sock.disconnected.connect(self._on_disconnected)
        sock.errorOccurred.connect(lambda _err: self.error.emit(sock.errorString()))
        if sock.state() == QAbstractSocket.ConnectedState:
            self.connected.emit(f"{sock.peerAddress().toString()}:{sock.peerPort()}")
        else:
            sock.connected.connect(lambda: self.connected.emit(
                f"{sock.peerAddress().toString()}:{sock.peerPort()}"))

    def close_socket(self):
        if self.socket:
            try:
                if self.socket.state() == QAbstractSocket.ConnectedState:
                    self.socket.disconnectFromHost()
            finally:
                self.socket.deleteLater()
                self.socket = None
        if self._sending_fp:
            try:
                self._sending_fp.close()
            except OSError:
                pass
            self._sending_fp = None
        self._send_timer.stop()
        self._rxbuf.clear()
        self._cleanup_incoming()

    def set_download_dir(self, path: str):
        if path and os.path.isdir(path):
            self._download_dir = path

    # -------------------- 发送 API --------------------
    def send_text(self, text: str) -> bool:
        if not self._ensure_conn():
            return False
        payload = text.encode('utf-8')
        return self._send_frame(T_TEXT, payload)

    def send_file(self, file_path: str) -> bool:
        if not self._ensure_conn():
            return False
        try:
            size = os.path.getsize(file_path)
            name = os.path.basename(file_path)
            meta = json.dumps({"name": name, "size": size}).encode('utf-8')
            if not self._send_frame(T_FILE_META, meta):
                return False
            self._sending_fp = open(file_path, 'rb')
            self._send_remaining = size
            self._send_timer.start(10)
            return True
        except OSError as e:
            self.error.emit(f"发送文件失败：{e}")
            if self._sending_fp:
                self._sending_fp.close()
                self._sending_fp = None
            return False

    def _send_next_chunk(self):
        if not self._sending_fp:
            self._send_timer.stop()
            return
        try:
            chunk = self._sending_fp.read(CHUNK_SIZE)
            if not chunk:
                self._sending_fp.close()
                self._sending_fp = None
                self._send_timer.stop()
                self._send_frame(T_FILE_END, b"")
                return
            if not self._send_frame(T_FILE_CHUNK, chunk):
                self._send_timer.stop()
                self._sending_fp.close()
                self._sending_fp = None
        except OSError as e:
            self._send_timer.stop()
            if self._sending_fp:
                self._sending_fp.close()
                self._sending_fp = None
            self.error.emit(f"发送文件失败：{e}")

    def _ensure_conn(self) -> bool:
        if not self.socket or self.socket.state() != QAbstractSocket.ConnectedState:
            self.error.emit("尚未连接对方")
            return False
        return True

    # -------------------- 接收处理 --------------------
    def _on_ready_read(self):
        if not self.socket:
            return
        try:
            self._rxbuf.extend(bytes(self.socket.readAll()))
            while True:
                if len(self._rxbuf) < HEADER_LEN:
                    return
                if self._rxbuf[0:2] != MAGIC:
                    # resync：丢弃首字节
                    self._rxbuf.pop(0)
                    continue
                ver = self._rxbuf[2]
                typ = self._rxbuf[3]
                length = struct.unpack('>I', self._rxbuf[4:8])[0]
                if len(self._rxbuf) < HEADER_LEN + length:
                    return
                payload = bytes(self._rxbuf[8:8+length])
                del self._rxbuf[:8+length]
                self._handle_frame(ver, typ, payload)
        except Exception as e:
            self.error.emit(f"接收解析错误：{e}")

    def _handle_frame(self, ver: int, typ: int, payload: bytes):
        if ver != VERSION:
            self.error.emit("协议版本不匹配")
            return
        if typ == T_TEXT:
            try:
                text = payload.decode('utf-8')
            except Exception:
                text = "[无法解码的文本]"
            self.message_received.emit(text)
        elif typ == T_FILE_META:
            try:
                meta = json.loads(payload.decode('utf-8'))
                safe_name = os.path.basename(meta.get('name', '')) or 'recv.bin'
                size = int(meta.get('size') or 0)
                save_path = self._unique_path(os.path.join(self._download_dir, safe_name))
                fp = open(save_path, 'wb')
                self._incoming = IncomingFile(name=safe_name, size=size, received=0, fp=fp, path=save_path)
                self.file_incoming.emit(safe_name, size)
                self.file_progress.emit(0, size)
            except (json.JSONDecodeError, OSError) as e:
                self.error.emit(f"接收文件元信息失败：{e}")
        elif typ == T_FILE_CHUNK:
            inc = self._incoming
            if not inc or not inc.fp:
                return
            try:
                inc.fp.write(payload)
                inc.received += len(payload)
                self.file_progress.emit(inc.received, inc.size)
            except OSError as e:
                self.error.emit(f"文件写入失败：{e}")
                if inc.fp:
                    try:
                        inc.fp.close()
                    except OSError:
                        pass
                self._incoming = None
        elif typ == T_FILE_END:
            inc = self._incoming
            if inc and inc.fp:
                try:
                    inc.fp.flush()
                    inc.fp.close()
                    self.file_saved.emit(inc.path or "")
                except OSError:
                    self.file_saved.emit(inc.path or "")
            self._incoming = None
        else:
            self.error.emit(f"未知帧类型：{typ}")

    def _unique_path(self, path: str) -> str:
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while True:
            trial = f"{base} ({i}){ext}"
            if not os.path.exists(trial):
                return trial
            i += 1

    def _cleanup_incoming(self):
        if self._incoming and self._incoming.fp:
            try:
                self._incoming.fp.close()
            except OSError:
                pass
        self._incoming = None

    def _on_disconnected(self):
        self.disconnected.emit()
        self.close_socket()

    # -------------------- 发帧底层 --------------------
    def _send_frame(self, typ: int, payload: bytes) -> bool:
        if not self.socket:
            return False
        try:
            header = MAGIC + bytes([VERSION, typ]) + struct.pack('>I', len(payload))
            n1 = self.socket.write(header)
            n2 = self.socket.write(payload)
            self.socket.flush()
            return n1 == len(header) and n2 == len(payload)
        except Exception as e:
            self.error.emit(f"发送失败：{e}")
            return False


class ThemeManager(QObject):
    theme_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_theme = "light"
        self._themes = {
            "light": "style.qss",
            "dark": "dark.qss"
        }
        self.settings = QSettings("P2PChat", "Settings")
        self._load_settings()
        self._background_image = None
        self._background_image_path = ""
        self._background_opacity = 1.0
        self._blur_radius = 0

    def _load_settings(self):
        self._current_theme = self.settings.value("theme", "light", type=str)
        self._background_opacity = self.settings.value("background_opacity", 1.0, type=float)
        self._blur_radius = self.settings.value("blur_radius", 0, type=int)
        self._background_image_path = self.settings.value("background_image_path", "", type=str)
        if self._background_image_path and os.path.exists(self._background_image_path):
            self._background_image = QPixmap(self._background_image_path)

    def _save_settings(self):
        self.settings.setValue("theme", self._current_theme)
        self.settings.setValue("background_opacity", self._background_opacity)
        self.settings.setValue("background_image_path", self._background_image_path)
        self.settings.setValue("blur_radius", self._blur_radius)

    def get_theme_path(self):
        return self._themes.get(self._current_theme, "style.qss")

    def set_theme(self, theme_name: str):
        if theme_name in self._themes:
            self._current_theme = theme_name
            self._save_settings()
            self.theme_changed.emit(theme_name)

    def get_current_theme(self):
        return self._current_theme

    def set_background_image(self, path: str):
        if path and os.path.exists(path):
            self._background_image = QPixmap(path)
            self._background_image_path = path
            self._save_settings()
        else:
            self._background_image = None
            self._background_image_path = ""
            self._save_settings()

    def get_background_image(self):
        return self._background_image

    def set_background_opacity(self, opacity: float):
        self._background_opacity = max(0.1, min(1.0, opacity))
        self._save_settings()

    def get_background_opacity(self):
        return self._background_opacity




class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("💬 P2P 聊天室")
        self.setObjectName("RootWindow")
        self.resize(1150, 800)
        self.setMinimumSize(900, 650)

        self.theme_manager = ThemeManager(self)
        self.peer = Peer(self)
        self.server_peer = ServerPeer(self)
        self.log_fp = None
        self._mode = "p2p"  # p2p or server
        self._current_room = "default"
        self._users = {}

        self._build_ui()
        self._apply_theme()
        self._wire_signals()
        self.setAcceptDrops(True)

    def closeEvent(self, event):
        self._close_log()
        super().closeEvent(event)

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top_bar = QHBoxLayout()
        self.theme_btn = QPushButton("🎨 主题")
        self.theme_btn.setObjectName("secondaryButton")
        self.theme_btn.setFixedWidth(100)
        self.theme_btn.clicked.connect(self._open_theme_settings)

        top_bar.addWidget(self.theme_btn)
        top_bar.addStretch()
        top_bar_widget = QWidget()
        top_bar_widget.setLayout(top_bar)

        self.tabs = QTabWidget()
        self.p2p_tab = self._build_p2p_tab()
        self.server_tab = self._build_server_tab()
        self.tabs.addTab(self.p2p_tab, "P2P 直连")
        self.tabs.addTab(self.server_tab, "服务器模式")

        root.addWidget(top_bar_widget)
        root.addWidget(self.tabs)

    def _build_p2p_tab(self):
        widget = QWidget()
        root = QVBoxLayout(widget)

        ctrl_group = QGroupBox("P2P 连接")
        ctrl_form = QFormLayout()
        ctrl_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        ctrl_group.setLayout(ctrl_form)

        self.ip_edit = QLineEdit()
        self.ip_edit.setPlaceholderText("对方IP，例如 192.168.1.100")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(5000)

        ip_port_row = QHBoxLayout()
        ip_port_row.addWidget(self.ip_edit, 3)
        ip_port_row.addWidget(self.port_spin, 1)
        ip_port_widget = QWidget()
        ip_port_widget.setLayout(ip_port_row)

        self.btn_listen = QPushButton("开启主机")
        self.btn_listen.setObjectName("secondaryButton")
        self.btn_connect = QPushButton("连接主机")
        self.btn_connect.setObjectName("primaryButton")
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.setObjectName("dangerButton")
        self.btn_disconnect.setEnabled(False)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_listen)
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_disconnect)
        btn_row_widget = QWidget()
        btn_row_widget.setLayout(btn_row)

        self.local_ip_label = QLabel("本机可用IP：" + ", ".join(get_local_ipv4_list()))
        self.status_label = QLabel("状态：未连接")
        self.status_label.setObjectName("statusLabel")

        ctrl_form.addRow("对方IP / 端口：", ip_port_widget)
        ctrl_form.addRow("操作：", btn_row_widget)
        ctrl_form.addRow(self.local_ip_label)
        ctrl_form.addRow(self.status_label)

        self.p2p_chat_view = QTextEdit()
        self.p2p_chat_view.setReadOnly(True)
        self.p2p_chat_view.setPlaceholderText("聊天记录…（可拖拽文件到窗口发送）")

        input_row = QHBoxLayout()
        self.p2p_input_edit = QLineEdit()
        self.p2p_input_edit.setPlaceholderText("输入消息，按 Enter 发送…")
        self.p2p_btn_send = QPushButton("发送")
        self.p2p_btn_send.setEnabled(False)
        self.p2p_btn_send.setObjectName("primaryButton")
        self.p2p_btn_send_file = QPushButton("发送文件…")
        self.p2p_btn_send_file.setEnabled(False)
        self.p2p_btn_send_file.setObjectName("secondaryButton")
        input_row.addWidget(self.p2p_input_edit, 1)
        input_row.addWidget(self.p2p_btn_send)
        input_row.addWidget(self.p2p_btn_send_file)
        input_row_widget = QWidget()
        input_row_widget.setLayout(input_row)

        prog_row = QHBoxLayout()
        self.p2p_prog_label = QLabel("文件传输：-")
        self.p2p_prog = QProgressBar()
        self.p2p_prog.setRange(0, 100)
        self.p2p_prog.setValue(0)
        prog_row.addWidget(self.p2p_prog_label, 2)
        prog_row.addWidget(self.p2p_prog, 3)
        prog_widget = QWidget()
        prog_widget.setLayout(prog_row)

        root.addWidget(ctrl_group)
        root.addWidget(self.p2p_chat_view, 1)
        root.addWidget(input_row_widget)
        root.addWidget(prog_widget)

        return widget

    def _build_server_tab(self):
        widget = QWidget()
        root = QHBoxLayout(widget)

        left_panel = QSplitter(Qt.Vertical)

        ctrl_group = QGroupBox("服务器连接")
        ctrl_form = QFormLayout()
        ctrl_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        ctrl_group.setLayout(ctrl_form)

        self.server_host_edit = QLineEdit()
        self.server_host_edit.setPlaceholderText("服务器IP，例如 192.168.1.1")
        self.server_port_spin = QSpinBox()
        self.server_port_spin.setRange(1, 65535)
        self.server_port_spin.setValue(5000)

        host_port_row = QHBoxLayout()
        host_port_row.addWidget(self.server_host_edit, 3)
        host_port_row.addWidget(self.server_port_spin, 1)
        host_port_widget = QWidget()
        host_port_widget.setLayout(host_port_row)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("你的昵称")

        self.room_id_edit = QLineEdit()
        self.room_id_edit.setPlaceholderText("房间ID (默认: default)")
        self.room_id_edit.setText("default")

        self.btn_server_connect = QPushButton("连接服务器")
        self.btn_server_connect.setObjectName("primaryButton")
        self.btn_server_disconnect = QPushButton("断开")
        self.btn_server_disconnect.setObjectName("dangerButton")
        self.btn_server_disconnect.setEnabled(False)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_server_connect)
        btn_row.addWidget(self.btn_server_disconnect)
        btn_row_widget = QWidget()
        btn_row_widget.setLayout(btn_row)

        self.server_status_label = QLabel("状态：未连接")
        self.server_status_label.setObjectName("statusLabel")

        ctrl_form.addRow("服务器地址：", host_port_widget)
        ctrl_form.addRow("昵称：", self.username_edit)
        ctrl_form.addRow("房间ID：", self.room_id_edit)
        ctrl_form.addRow(btn_row_widget)
        ctrl_form.addRow(self.server_status_label)

        users_group = QGroupBox("在线用户")
        users_layout = QVBoxLayout()
        self.users_list = QListWidget()
        users_layout.addWidget(self.users_list)
        users_group.setLayout(users_layout)

        settings_group = QGroupBox("设置")
        settings_layout = QFormLayout()

        dl_row = QHBoxLayout()
        self.download_edit = QLineEdit(os.path.join(os.getcwd(), "downloads"))
        self.btn_browse = QPushButton("选择…")
        self.btn_browse.setObjectName("ghostButton")
        dl_row.addWidget(self.download_edit, 1)
        dl_row.addWidget(self.btn_browse)
        dl_widget = QWidget()
        dl_widget.setLayout(dl_row)

        self.chk_log = QCheckBox("保存聊天日志到文件")
        self.btn_choose_log = QPushButton("选择日志文件…")
        self.btn_choose_log.setEnabled(False)
        self.btn_choose_log.setObjectName("ghostButton")

        log_row = QHBoxLayout()
        log_row.addWidget(self.chk_log)
        log_row.addWidget(self.btn_choose_log)
        log_widget = QWidget()
        log_widget.setLayout(log_row)

        settings_layout.addRow("下载目录：", dl_widget)
        settings_layout.addRow(log_widget)
        settings_group.setLayout(settings_layout)

        left_panel.addWidget(ctrl_group)
        left_panel.addWidget(users_group)
        left_panel.addWidget(settings_group)
        left_panel.setStretchFactor(0, 2)
        left_panel.setStretchFactor(1, 2)
        left_panel.setStretchFactor(2, 1)

        right_panel = QVBoxLayout()
        self.server_chat_view = QTextEdit()
        self.server_chat_view.setReadOnly(True)
        self.server_chat_view.setPlaceholderText("聊天记录…")

        input_row = QHBoxLayout()
        self.server_input_edit = QLineEdit()
        self.server_input_edit.setPlaceholderText("输入消息，按 Enter 发送 (@用户名 私聊)…")
        self.server_btn_send = QPushButton("发送")
        self.server_btn_send.setEnabled(False)
        self.server_btn_send.setObjectName("primaryButton")
        self.server_btn_send_file = QPushButton("发送文件…")
        self.server_btn_send_file.setEnabled(False)
        self.server_btn_send_file.setObjectName("secondaryButton")
        input_row.addWidget(self.server_input_edit, 1)
        input_row.addWidget(self.server_btn_send)
        input_row.addWidget(self.server_btn_send_file)
        input_row_widget = QWidget()
        input_row_widget.setLayout(input_row)

        server_prog_row = QHBoxLayout()
        self.server_prog_label = QLabel("文件传输：-")
        self.server_prog = QProgressBar()
        self.server_prog.setRange(0, 100)
        self.server_prog.setValue(0)
        server_prog_row.addWidget(self.server_prog_label, 2)
        server_prog_row.addWidget(self.server_prog, 3)
        server_prog_widget = QWidget()
        server_prog_widget.setLayout(server_prog_row)

        right_panel.addWidget(self.server_chat_view, 1)
        right_panel.addWidget(input_row_widget)
        right_panel.addWidget(server_prog_widget)

        right_panel_widget = QWidget()
        right_panel_widget.setLayout(right_panel)

        root.addWidget(left_panel, 1)
        root.addWidget(right_panel_widget, 4)

        return widget

    def _wire_signals(self):
        # P2P 信号
        self.btn_listen.clicked.connect(self.on_listen)
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.p2p_btn_send.clicked.connect(self.on_send_text)
        self.p2p_input_edit.returnPressed.connect(self.on_send_text)
        self.p2p_btn_send_file.clicked.connect(self.on_send_file)

        self.peer.message_received.connect(self.on_message_received)
        self.peer.connected.connect(self.on_connected)
        self.peer.disconnected.connect(self.on_disconnected)
        self.peer.error.connect(self.on_error)
        self.peer.listening.connect(self.on_listening)

        self.peer.file_incoming.connect(self.on_file_incoming)
        self.peer.file_progress.connect(self.on_file_progress)
        self.peer.file_saved.connect(self.on_file_saved)

        # Server 信号
        self.btn_server_connect.clicked.connect(self.on_server_connect)
        self.btn_server_disconnect.clicked.connect(self.on_server_disconnect)
        self.server_btn_send.clicked.connect(self.on_server_send)
        self.server_input_edit.returnPressed.connect(self.on_server_send)
        self.server_btn_send_file.clicked.connect(self.on_server_send_file)

        self.server_peer.connected.connect(self.on_server_connected)
        self.server_peer.disconnected.connect(self.on_server_disconnected)
        self.server_peer.error.connect(self.on_error)
        self.server_peer.login_success.connect(self.on_server_login_success)
        self.server_peer.message_received.connect(self.on_server_message)
        self.server_peer.user_joined.connect(self.on_server_user_joined)
        self.server_peer.user_left.connect(self.on_server_user_left)
        self.server_peer.user_list_received.connect(self.on_server_user_list)
        self.server_peer.file_incoming.connect(self.on_server_file_incoming)
        self.server_peer.file_progress.connect(self.on_server_file_progress)
        self.server_peer.file_saved.connect(self.on_server_file_saved)

        # 全局设置
        self.btn_browse.clicked.connect(self.on_choose_download_dir)
        self.chk_log.toggled.connect(self.on_toggle_log)
        self.btn_choose_log.clicked.connect(self.on_choose_log_file)

        self.theme_manager.theme_changed.connect(self._on_theme_changed)

    def _get_message_style(self, theme: str):
        if theme == "dark":
            return {
                "my_bg": "#5dade2",
                "other_bg": "#16213e",
                "other_border": "#0f3460",
                "other_name": "#5dade2",
                "other_text": "#e0e0e0",
                "sys_bg": "#0f3460",
                "sys_text": "#95a5a6",
                "error_bg": "#721c24",
                "file_bg": "#856404",
                "file_border": "#9e6a03"
            }
        return {
            "my_bg": "#3498db",
            "other_bg": "#ffffff",
            "other_border": "#e2e8f0",
            "other_name": "#3498db",
            "other_text": "#2c3e50",
            "sys_bg": "#e8ecef",
            "sys_text": "#7f8c8d",
            "error_bg": "#f8d7da",
            "file_bg": "#856404",
            "file_border": "#ffc107"
        }

    def on_server_connect(self):
        host = self.server_host_edit.text().strip()
        port = self.server_port_spin.value()
        username = self.username_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "提示", "请输入服务器地址")
            return
        if not username:
            QMessageBox.warning(self, "提示", "请输入昵称")
            return
        self.server_status_label.setText("状态：连接中…")
        self.server_peer.connect_to_server(host, port, username)

    def on_server_disconnect(self):
        self.server_peer.close_socket()
        self.server_status_label.setText("状态：未连接")
        self.server_btn_send.setEnabled(False)
        self.server_btn_send_file.setEnabled(False)
        self.users_list.clear()
        self._users.clear()

    def on_server_send(self):
        text = self.server_input_edit.text().strip()
        if not text:
            return
        if text.startswith('@'):
            parts = text[1:].split(' ', 1)
            if len(parts) == 2:
                target_username = parts[0].strip()
                message = parts[1].strip()
                target_user_id = self._get_user_id_by_name(target_username)
                if target_user_id:
                    self.server_peer.send_private_message(target_user_id, message)
                    html = self._create_private_message_html(target_username, message)
                    self.server_chat_view.append(html)
                    self.server_input_edit.clear()
                    return
        self.server_peer.send_public_message(text)
        html = self._create_my_message_html(text)
        self.server_chat_view.append(html)
        self.server_input_edit.clear()

    def on_server_send_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if not path:
            return
        self.server_peer.send_file(path)

    def on_server_message(self, message: str, from_user: str, is_private: bool):
        if is_private:
            html = self._create_private_received_html(from_user, message)
        else:
            html = self._create_other_message_html(from_user, message)
        self.server_chat_view.append(html)

    def on_server_user_joined(self, user_id: str, username: str, room_id: str):
        self._users[user_id] = username
        self._refresh_user_list()
        html = self._create_system_message_html(f"{username} 加入了房间 {room_id}")
        self.server_chat_view.append(html)

    def on_server_user_left(self, user_id: str, username: str):
        if user_id in self._users:
            del self._users[user_id]
        self._refresh_user_list()
        html = self._create_system_message_html(f"{username} 离开了房间")
        self.server_chat_view.append(html)

    def on_error(self, err: str):
        active_tab = self.tabs.currentIndex()
        error_html = self._create_error_message_html(err)
        if active_tab == 0:
            self.p2p_chat_view.append(error_html)
        else:
            self.server_chat_view.append(error_html)

    def on_server_user_list(self, users: list):
        self._users = {u['user_id']: u['username'] for u in users}
        self._refresh_user_list()

    def _append_system_message(self, chat_view: QTextEdit, message: str):
        html = self._create_system_message_html(message)
        chat_view.append(html)

    def on_server_file_incoming(self, name: str, size: int, from_user: str, is_private: bool):
        username = self._users.get(from_user, from_user)
        html = self._create_file_message_html(f"📁 接收来自 {username} 的文件：{name} ({self._human_size(size)})")
        self.server_chat_view.append(html)
        self.server_prog_label.setText(f"接收 {name}")
        self.server_prog.setValue(0)

    def on_server_file_progress(self, received: int, total: int):
        pct = int(received * 100 / total) if total else 0
        self.server_prog.setValue(pct)

    def on_server_file_saved(self, path: str):
        html = self._create_file_message_html(f"✅ 文件已保存：{path}")
        self.server_chat_view.append(html)
        self.server_prog_label.setText("文件传输：完成")

    def on_server_connected(self):
        self._append_system_message(self.server_chat_view, "已连接到服务器")

    def on_server_disconnected(self):
        self._append_system_message(self.server_chat_view, "已断开与服务器的连接")

    def on_server_login_success(self):
        self._append_system_message(self.server_chat_view, "登录成功")
        html = f"""
        <div class="file-message" style="background: #d4edda; border-color: #28a745; color: #155724;">✅ 文件已保存：{path}</div>
        """
        self.server_chat_view.append(html)
        self.server_prog_label.setText("文件传输：完成")

    def _get_user_id_by_name(self, username: str) -> str:
        for uid, name in self._users.items():
            if name == username:
                return uid
        return ""

    def _refresh_user_list(self):
        self.users_list.clear()
        for username in sorted(self._users.values()):
            item_text = f"  {username}"
            self.users_list.addItem(item_text)

    # -------------------- 下载与日志设置 --------------------
    def on_choose_download_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录", self.download_edit.text() or os.getcwd())
        if d:
            self.download_edit.setText(d)
            self.peer.set_download_dir(d)
            self.server_peer._download_dir = d

    def on_toggle_log(self, checked: bool):
        self.btn_choose_log.setEnabled(checked)
        if checked:
            self.on_choose_log_file()
        else:
            self._close_log()

    def on_choose_log_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "选择/创建日志文件", os.path.join(os.getcwd(), "chat.log"))
        if path:
            try:
                self._close_log()
                self.log_fp = open(path, 'a', encoding='utf-8')
                self.server_chat_view.append(f"<i>[系统] 日志记录到：{path}</i>")
            except Exception as e:
                self.server_chat_view.append(f"<i>[错误] 无法打开日志文件：{e}</i>")
                self.chk_log.setChecked(False)

    def _close_log(self):
        if self.log_fp:
            try:
                self.log_fp.close()
            except Exception:
                pass
            self.log_fp = None

    def _log_line(self, line: str):
        if self.log_fp:
            try:
                self.log_fp.write(line + "\n")
                self.log_fp.flush()
            except Exception:
                pass

    # -------------------- P2P 事件处理 --------------------
    def on_listen(self):
        port = int(self.port_spin.value())
        if self.peer.start_listening(port):
            self.p2p_append_sys(f"正在监听 0.0.0.0:{port}，等待对方连接…")
            self.btn_disconnect.setEnabled(True)
        else:
            self.p2p_append_sys("监听失败")

    def on_connect(self):
        host = self.ip_edit.text().strip()
        port = int(self.port_spin.value())
        if not host:
            QMessageBox.warning(self, "提示", "请先输入对方IP地址")
            return
        self.p2p_append_sys(f"正在连接 {host}:{port} …")
        self.peer.connect_to(host, port)

    def on_disconnect(self):
        self.peer.close_socket()
        self.peer.stop_listening()
        self.p2p_append_sys("已请求断开/停止监听")
        self.set_p2p_connected_ui(False)

    def on_send_text(self):
        text = self.p2p_input_edit.text().strip()
        if not text:
            return
        ok = self.peer.send_text(text)
        if ok:
            self.p2p_append_me(text)
            self.p2p_input_edit.clear()
            self._log_line(f"[我 {now_ts()}] {text}")
        else:
            self.p2p_append_sys("发送失败：尚未连接")

    def on_send_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if not path:
            return
        html = self._create_file_message_html(f"📁 开始发送文件：{os.path.basename(path)} ({self._human_size(os.path.getsize(path))})")
        self.p2p_chat_view.append(html)
        self.peer.send_file(path)

    def on_file_incoming(self, name: str, size: int):
        html = self._create_file_message_html(f"📁 接收文件：{name}，大小 {self._human_size(size)}")
        self.p2p_chat_view.append(html)
        self.p2p_prog_label.setText(f"接收 {name}")
        self.p2p_prog.setValue(0)

    def on_file_progress(self, received: int, total: int):
        pct = int(received * 100 / total) if total else 0
        self.p2p_prog.setValue(pct)

    def on_file_saved(self, path: str):
        html = self._create_file_message_html(f"✅ 文件已保存：{path}")
        self.p2p_chat_view.append(html)
        self.p2p_prog_label.setText("文件传输：完成")

    def on_message_received(self, message: str):
        html = self._create_other_message_html("对方", message)
        self.p2p_chat_view.append(html)

    def on_connected(self):
        self.p2p_append_sys("已连接")
        self.set_p2p_connected_ui(True)

    def on_disconnected(self):
        self.p2p_append_sys("对方已断开连接")
        self.set_p2p_connected_ui(False)

    def on_listening(self):
        self.p2p_append_sys("正在监听中…")

    # -------------------- UI 辅助 --------------------
    def set_p2p_connected_ui(self, connected: bool):
        self.p2p_btn_send.setEnabled(connected)
        self.p2p_btn_send_file.setEnabled(connected)
        self.btn_disconnect.setEnabled(True)
        self.status_label.setText("状态：已连接" if connected else "状态：未连接/未监听")

    def p2p_append_me(self, text: str):
        html = self._create_my_message_html(text)
        self.p2p_chat_view.append(html)

    def p2p_append_sys(self, text: str):
        html = self._create_system_message_html(text)
        self.p2p_chat_view.append(html)

    @staticmethod
    def _escape(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _human_size(n: int) -> str:
        units = ["B","KB","MB","GB","TB"]
        f = float(n); i = 0
        while f >= 1024 and i < len(units)-1:
            f /= 1024.0; i += 1
        return f"{f:.2f} {units[i]}"

    def _get_theme_colors(self):
        if self.theme_manager.get_current_theme() == "dark":
            return {
                'my_msg_bg': '#2e7d32',
                'my_msg_text': '#ffffff',
                'other_msg_bg': '#1e1e1e',
                'other_msg_text': '#e0e0e0',
                'private_msg_bg': '#2e7d32',
                'private_msg_border': '#1b5e20',
                'sender_color': '#4caf50',
                'time_color': '#757575',
                'system_bg': '#262626',
                'system_text': '#9e9e9e',
                'file_bg': '#263238',
                'file_border': '#37474f',
                'error_bg': '#4a2c2c',
                'error_border': '#b71c1c',
                'error_text': '#ff8a80'
            }
        else:
            return {
                'my_msg_bg': '#07c160',
                'my_msg_text': '#ffffff',
                'other_msg_bg': '#ffffff',
                'other_msg_text': '#1a1a1a',
                'private_msg_bg': '#07c160',
                'private_msg_border': '#06a850',
                'sender_color': '#07c160',
                'time_color': '#999999',
                'system_bg': '#f5f5f5',
                'system_text': '#999999',
                'file_bg': '#fff8f0',
                'file_border': '#ffe0b2',
                'error_bg': '#fff3f0',
                'error_border': '#ffccbc',
                'error_text': '#d32f2f'
            }

    def _create_my_message_html(self, text: str, time: str = None):
        colors = self._get_theme_colors()
        t = time if time else now_ts()
        return f'''<div style="margin: 8px 16px 8px 16px; text-align: right; clear: both;"><div style="display: inline-block; max-width: 70%; padding: 10px 14px; background: {colors['my_msg_bg']}; color: {colors['my_msg_text']}; border-radius: 6px 6px 0 6px; box-shadow: 0 1px 2px rgba(0,0,0,0.1); word-wrap: break-word; vertical-align: top;"><div style="font-size: 15px; line-height: 1.6; white-space: pre-wrap;">{self._escape(text)}</div><div style="font-size: 10px; margin-top: 4px; text-align: right; opacity: 0.7;">{t}</div></div></div>'''

    def _create_other_message_html(self, sender: str, text: str, time: str = None):
        colors = self._get_theme_colors()
        t = time if time else now_ts()
        return f'''<div style="margin: 8px 16px; text-align: left; clear: both;"><div style="display: inline-block; max-width: 70%; padding: 10px 14px; background: {colors['other_msg_bg']}; color: {colors['other_msg_text']}; border-radius: 6px 6px 6px 0; box-shadow: 0 1px 2px rgba(0,0,0,0.1); word-wrap: break-word; vertical-align: top; border: 1px solid rgba(0,0,0,0.06);"><div style="font-weight: 600; font-size: 13px; color: {colors['sender_color']}; margin-bottom: 4px;">{self._escape(sender)}</div><div style="font-size: 15px; line-height: 1.6; white-space: pre-wrap;">{self._escape(text)}</div><div style="font-size: 10px; margin-top: 4px; opacity: 0.6;">{t}</div></div></div>'''

    def _create_private_message_html(self, target: str, text: str, time: str = None):
        colors = self._get_theme_colors()
        t = time if time else now_ts()
        return f'''<div style="margin: 8px 16px 8px 16px; text-align: right; clear: both;"><div style="display: inline-block; max-width: 70%; padding: 10px 14px; background: {colors['private_msg_bg']}; color: #ffffff; border-radius: 6px 6px 0 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.15); word-wrap: break-word; vertical-align: top; border-left: 3px solid {colors['private_msg_border']};"><div style="font-weight: 600; font-size: 12px; margin-bottom: 3px; opacity: 0.9;">🔒 私聊给 {self._escape(target)}</div><div style="font-size: 15px; line-height: 1.6; white-space: pre-wrap;">{self._escape(text)}</div><div style="font-size: 10px; margin-top: 4px; text-align: right; opacity: 0.7;">{t}</div></div></div>'''

    def _create_private_received_html(self, sender: str, text: str, time: str = None):
        colors = self._get_theme_colors()
        t = time if time else now_ts()
        return f'''<div style="margin: 8px 16px; text-align: left; clear: both;"><div style="display: inline-block; max-width: 70%; padding: 10px 14px; background: {colors['private_msg_bg']}; color: #ffffff; border-radius: 6px 6px 6px 0; box-shadow: 0 1px 3px rgba(0,0,0,0.15); word-wrap: break-word; vertical-align: top; border-right: 3px solid {colors['private_msg_border']};"><div style="font-weight: 600; font-size: 12px; margin-bottom: 3px; opacity: 0.9;">🔒 {self._escape(sender)} (私聊)</div><div style="font-size: 15px; line-height: 1.6; white-space: pre-wrap;">{self._escape(text)}</div><div style="font-size: 10px; margin-top: 4px; opacity: 0.7;">{t}</div></div></div>'''

    def _create_system_message_html(self, text: str):
        colors = self._get_theme_colors()
        return f'''<div style="background: rgba(0,0,0,0.05); color: {colors['system_text']}; padding: 6px 18px; border-radius: 12px; font-size: 12px; text-align: center; margin: 12px auto 12px auto; max-width: 70%; font-weight: 400; opacity: 0.8;">{self._escape(text)}</div>'''

    def _create_file_message_html(self, text: str):
        colors = self._get_theme_colors()
        return f'''<div style="background: {colors['file_bg']}; border: 1px solid {colors['file_border']}; border-radius: 8px; padding: 10px 14px; margin: 6px 16px; font-size: 13px; color: {colors['other_msg_text']}; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">{self._escape(text)}</div>'''

    def _create_error_message_html(self, text: str):
        colors = self._get_theme_colors()
        return f'''<div style="background: {colors['error_bg']}; border: 1px solid {colors['error_border']}; border-radius: 8px; padding: 10px 14px; margin: 6px 16px; font-size: 13px; color: {colors['error_text']}; box-shadow: 0 1px 2px rgba(0,0,0,0.1);">❌ {self._escape(text)}</div>'''

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if not urls:
            return
        active_tab = self.tabs.currentIndex()
        for u in urls:
            path = u.toLocalFile()
            if path and os.path.isfile(path):
                file_html = self._create_file_message_html(f"📁 开始发送文件：{os.path.basename(path)} ({self._human_size(os.path.getsize(path))})")
                if active_tab == 0:
                    self.p2p_chat_view.append(file_html)
                    self.peer.send_file(path)
                else:
                    self.server_chat_view.append(file_html)
                    self.server_peer.send_file(path)

    # -------------------- 主题设置 --------------------
    def _apply_theme(self):
        qss_path = os.path.join(os.path.dirname(__file__), self.theme_manager.get_theme_path())
        try:
            with open(qss_path, "r", encoding="utf-8") as f:
                qss_content = f.read()
                self.setStyleSheet(qss_content)
        except Exception as e:
            pass

    def _on_theme_changed(self, theme_name: str):
        self._apply_theme()
        self.theme_btn.setText(f"🎨 {theme_name.title()}")

    def _update_background(self):
        bg_image = self.theme_manager.get_background_image()
        opacity = self.theme_manager.get_background_opacity()

        # Apply background to entire window
        if bg_image and not bg_image.isNull():
            scaled_bg = bg_image.scaled(self.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            palette = self.palette()
            palette.setBrush(self.backgroundRole(), QBrush(scaled_bg))
            self.setPalette(palette)
            self.setAutoFillBackground(True)
        else:
            self.setAutoFillBackground(False)

        # Apply blur and opacity to text views
        for view in [self.p2p_chat_view, self.server_chat_view]:
            view.setGraphicsEffect(None)
            if bg_image and not bg_image.isNull():
                # Semi-transparent background with blur for text views
                from PyQt5.QtWidgets import QGraphicsBlurEffect
                blur_effect = QGraphicsBlurEffect()
                blur_effect.setBlurRadius(5)
                view.setGraphicsEffect(blur_effect)

    def _open_theme_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("主题设置")
        dialog.setFixedWidth(400)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(15)

        theme_group = QGroupBox("主题")
        theme_layout = QVBoxLayout()

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["light", "dark"])
        self.theme_combo.setCurrentText(self.theme_manager.get_current_theme())
        self.theme_combo.currentTextChanged.connect(self._on_theme_combo_changed)
        theme_layout.addWidget(QLabel("选择主题："))
        theme_layout.addWidget(self.theme_combo)
        theme_group.setLayout(theme_layout)

        bg_group = QGroupBox("背景设置")
        bg_layout = QFormLayout()

        bg_row = QHBoxLayout()
        self.bg_path_edit = QLineEdit()
        self.bg_path_edit.setReadOnly(True)
        bg_image = self.theme_manager.get_background_image()
        if bg_image:
            self.bg_path_edit.setText("已设置背景图片")

        self.btn_select_bg = QPushButton("选择图片")
        self.btn_select_bg.clicked.connect(self._on_select_background)
        self.btn_clear_bg = QPushButton("清除")
        self.btn_clear_bg.clicked.connect(self._on_clear_background)
        bg_row.addWidget(self.bg_path_edit, 1)
        bg_row.addWidget(self.btn_select_bg)
        bg_row.addWidget(self.btn_clear_bg)

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(10, 100)
        self.opacity_slider.setValue(int(self.theme_manager.get_background_opacity() * 100))
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)

        self.opacity_label = QLabel(f"{int(self.theme_manager.get_background_opacity() * 100)}%")

        bg_layout.addRow("背景图片：", bg_row)
        bg_layout.addRow("不透明度：", self.opacity_label)
        bg_layout.addRow(self.opacity_slider)
        bg_group.setLayout(bg_layout)

        buttons = QHBoxLayout()
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(dialog.accept)
        buttons.addStretch()
        buttons.addWidget(btn_close)

        layout.addWidget(theme_group)
        layout.addWidget(bg_group)
        layout.addStretch()
        layout.addLayout(buttons)

        dialog.exec_()

    def _on_theme_combo_changed(self, theme: str):
        self.theme_manager.set_theme(theme)

    def _on_select_background(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择背景图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif)"
        )
        if path:
            self.theme_manager.set_background_image(path)
            self.bg_path_edit.setText(os.path.basename(path))
        self._update_background()

    def _on_clear_background(self):
        self.theme_manager.set_background_image("")
        self.bg_path_edit.setText("")
        self._update_background()

    def _on_opacity_changed(self, value: int):
        self.theme_manager.set_background_opacity(value / 100.0)
        self.opacity_label.setText(f"{value}%")
        self._update_background()




def main():
    app = QApplication(sys.argv)
    app.setApplicationName("P2P Chat v2")
    app.setOrganizationName("P2PChat")

    win = MainWindow()
    win.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
