#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import json
import struct
import time
from typing import Dict, Set
from dataclasses import dataclass
from collections import defaultdict

import uuid
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QTimer, QMetaObject, Q_ARG, pyqtSlot
from PyQt5.QtNetwork import QTcpServer, QTcpSocket, QHostAddress, QAbstractSocket
from PyQt5.QtWidgets import QApplication

# -------------------- 协议常量 --------------------
MAGIC = b"PC"      # Protocol Chat
VERSION = 2
HEADER_LEN = 8      # 2(MAGIC) + 1(V) + 1(TYPE) + 4(LEN)

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

CHUNK_SIZE = 32 * 1024


@dataclass
class User:
    user_id: str
    username: str
    socket: QTcpSocket
    room_id: str = "default"
    last_heartbeat: float = 0.0


class ChatServer(QObject):
    log_message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: QTcpServer = None
        self.users: Dict[str, User] = {}
        self.room_users: Dict[str, Set[str]] = defaultdict(set)
        self._next_user_id = 1
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._check_heartbeats)
        self._heartbeat_timer.start(30000)

    def start(self, host: str, port: int) -> bool:
        self.server = QTcpServer(self)
        ok = self.server.listen(QHostAddress(host), port)
        if not ok:
            self.log_message.emit(f"启动失败：{self.server.errorString()}")
            return False
        self.server.newConnection.connect(self._on_new_connection)
        self.log_message.emit(f"服务器启动，监听 {host}:{port}")
        return True

    def stop(self):
        if self.server:
            self.server.close()
            self.log_message.emit("服务器已停止")
        for user in self.users.values():
            user.socket.disconnectFromHost()
        self.users.clear()
        self.room_users.clear()

    def _on_new_connection(self):
        socket = self.server.nextPendingConnection()
        socket.readyRead.connect(lambda: self._on_ready_read(socket))
        socket.disconnected.connect(lambda: self._on_disconnected(socket))
        socket.errorOccurred.connect(lambda err: self._on_socket_error(socket, err))
        self.log_message.emit(f"新连接：{socket.peerAddress().toString()}:{socket.peerPort()}")

    def _on_ready_read(self, socket: QTcpSocket):
        try:
            while socket.bytesAvailable() > 0:
                data = socket.readAll()
                self._handle_data(socket, bytes(data))
        except Exception as e:
            self.log_message.emit(f"处理数据错误：{e}")

    def _on_disconnected(self, socket: QTcpSocket):
        user = self._find_user_by_socket(socket)
        if user:
            self._remove_user(user)
        socket.deleteLater()

    def _on_socket_error(self, socket: QTcpSocket, err):
        self.log_message.emit(f"Socket错误：{socket.errorString()}")

    def _handle_data(self, socket: QTcpSocket, data: bytes):
        buf = bytearray(data)
        while len(buf) >= HEADER_LEN:
            if buf[0:2] != MAGIC:
                buf.pop(0)
                continue
            ver = buf[2]
            typ = buf[3]
            length = struct.unpack('>I', buf[4:8])[0]
            if len(buf) < HEADER_LEN + length:
                break
            payload = bytes(buf[8:8+length])
            del buf[:8+length]
            self._handle_frame(socket, ver, typ, payload)

    def _handle_frame(self, socket: QTcpSocket, ver: int, typ: int, payload: bytes):
        if ver != VERSION:
            self._send_error(socket, "协议版本不匹配")
            return

        user = self._find_user_by_socket(socket)

        if typ == S_REGISTER:
            self._handle_register(socket, payload)
        elif typ == S_JOIN_ROOM:
            if user:
                self._handle_join_room(user, payload)
        elif typ == S_LEAVE_ROOM:
            if user:
                self._handle_leave_room(user)
        elif typ == S_CHAT_PUBLIC:
            if user:
                self._handle_chat_public(user, payload)
        elif typ == S_CHAT_PRIVATE:
            if user:
                self._handle_chat_private(user, payload)
        elif typ == S_USER_LIST:
            if user:
                self._handle_user_list(user, payload)
        elif typ == S_HEARTBEAT:
            if user:
                user.last_heartbeat = time.time()
        elif typ == S_FILE_META:
            if user:
                self._handle_file_meta(user, payload)
        elif typ == S_FILE_CHUNK:
            if user:
                self._handle_file_chunk(user, payload)
        elif typ == S_FILE_END:
            if user:
                self._handle_file_end(user, payload)

    def _handle_register(self, socket: QTcpSocket, payload: bytes):
        try:
            data = json.loads(payload.decode('utf-8'))
            username = data.get('username', '').strip()
            if not username:
                self._send_error(socket, "用户名不能为空")
                return
            user_id = str(uuid.uuid4())[:8]
            user = User(user_id=user_id, username=username, socket=socket, last_heartbeat=time.time())
            self.users[user_id] = user
            self.room_users["default"].add(user_id)
            response = json.dumps({"user_id": user_id, "username": username})
            self._send_frame(socket, SC_LOGIN_SUCCESS, response.encode('utf-8'))
            self.log_message.emit(f"用户注册：{username} ({user_id})")
            self._broadcast_user_joined(user, "default")
        except json.JSONDecodeError as e:
            self._send_error(socket, f"数据格式错误：{e}")

    def _handle_join_room(self, user: User, payload: bytes):
        try:
            data = json.loads(payload.decode('utf-8'))
            new_room = data.get('room_id', 'default').strip()
            if new_room != user.room_id:
                old_room = user.room_id
                self.room_users[old_room].discard(user.user_id)
                user.room_id = new_room
                self.room_users[new_room].add(user.user_id)
                self.log_message.emit(f"{user.username} 加入房间：{new_room}")
        except json.JSONDecodeError:
            pass

    def _handle_leave_room(self, user: User):
        self.room_users[user.room_id].discard(user.user_id)
        user.room_id = ""
        self.log_message.emit(f"{user.username} 离开房间")

    def _handle_chat_public(self, user: User, payload: bytes):
        try:
            data = json.loads(payload.decode('utf-8'))
            message = data.get('message', '')
            room_id = data.get('room_id', user.room_id)
            if message:
                msg_data = json.dumps({
                    "from_user": user.user_id,
                    "from_username": user.username,
                    "message": message,
                    "room_id": room_id,
                    "is_private": False
                })
                self._broadcast_to_room(room_id, SC_CHAT_MESSAGE, msg_data.encode('utf-8'), exclude_user=user.user_id)
        except json.JSONDecodeError:
            pass

    def _handle_chat_private(self, user: User, payload: bytes):
        try:
            data = json.loads(payload.decode('utf-8'))
            message = data.get('message', '')
            to_user = data.get('to_user', '')
            if message and to_user and to_user in self.users:
                target = self.users[to_user]
                msg_data = json.dumps({
                    "from_user": user.user_id,
                    "from_username": user.username,
                    "message": message,
                    "room_id": "",
                    "is_private": True
                })
                self._send_frame(target.socket, SC_CHAT_MESSAGE, msg_data.encode('utf-8'))
        except json.JSONDecodeError:
            pass

    def _handle_user_list(self, user: User, payload: bytes):
        try:
            data = json.loads(payload.decode('utf-8'))
            room_id = data.get('room_id', user.room_id)
            users = []
            for uid in self.room_users.get(room_id, set()):
                if uid in self.users:
                    u = self.users[uid]
                    users.append({"user_id": u.user_id, "username": u.username})
            response = json.dumps({"room_id": room_id, "users": users})
            self._send_frame(user.socket, SC_USER_LIST, response.encode('utf-8'))
        except json.JSONDecodeError:
            pass

    def _handle_file_meta(self, user: User, payload: bytes):
        try:
            data = json.loads(payload.decode('utf-8'))
            is_private = data.get('is_private', False)
            if is_private:
                target_user = data.get('target_user', '')
                if target_user in self.users:
                    meta = {
                        "name": data.get('name', ''),
                        "size": data.get('size', 0),
                        "from_user": user.user_id,
                        "from_username": user.username,
                        "is_private": True
                    }
                    self._send_frame(self.users[target_user].socket, S_FILE_META, json.dumps(meta).encode('utf-8'))
                    user._file_target = target_user
            else:
                room_id = data.get('room_id', user.room_id)
                meta = {
                    "name": data.get('name', ''),
                    "size": data.get('size', 0),
                    "from_user": user.user_id,
                    "from_username": user.username,
                    "room_id": room_id,
                    "is_private": False
                }
                self._broadcast_to_room(room_id, S_FILE_META, json.dumps(meta).encode('utf-8'), exclude_user=user.user_id)
                user._file_target = None
        except json.JSONDecodeError:
            pass

    def _handle_file_chunk(self, user: User, payload: bytes):
        target = user._file_target
        if target and target in self.users:
            self._send_frame(self.users[target].socket, S_FILE_CHUNK, payload)
        else:
            self._broadcast_to_room(user.room_id, S_FILE_CHUNK, payload, exclude_user=user.user_id)

    def _handle_file_end(self, user: User, payload: bytes):
        target = user._file_target
        if target and target in self.users:
            self._send_frame(self.users[target].socket, S_FILE_END, payload)
        else:
            self._broadcast_to_room(user.room_id, S_FILE_END, payload, exclude_user=user.user_id)

    def _remove_user(self, user: User):
        if user.user_id in self.users:
            del self.users[user.user_id]
        self.room_users[user.room_id].discard(user.user_id)
        self._broadcast_user_left(user, user.room_id)
        self.log_message.emit(f"用户断开：{user.username}")

    def _broadcast_user_joined(self, user: User, room_id: str):
        data = json.dumps({"user_id": user.user_id, "username": user.username, "room_id": room_id})
        self._broadcast_to_room(room_id, SC_USER_JOINED, data.encode('utf-8'))

    def _broadcast_user_left(self, user: User, room_id: str):
        data = json.dumps({"user_id": user.user_id, "username": user.username, "room_id": room_id})
        self._broadcast_to_room(room_id, SC_USER_LEFT, data.encode('utf-8'))

    def _broadcast_to_room(self, room_id: str, typ: int, payload: bytes, exclude_user: str = None):
        for uid in self.room_users.get(room_id, set()):
            if exclude_user and uid == exclude_user:
                continue
            if uid in self.users:
                self._send_frame(self.users[uid].socket, typ, payload)

    def _check_heartbeats(self):
        now = time.time()
        timeout_users = []
        for user_id, user in self.users.items():
            if now - user.last_heartbeat > 90:
                timeout_users.append(user)
        for user in timeout_users:
            self.log_message.emit(f"心跳超时：{user.username}")
            user.socket.disconnectFromHost()

    def _find_user_by_socket(self, socket: QTcpSocket) -> User:
        for user in self.users.values():
            if user.socket == socket:
                return user
        return None

    def _send_frame(self, socket: QTcpSocket, typ: int, payload: bytes):
        header = MAGIC + bytes([VERSION, typ]) + struct.pack('>I', len(payload))
        socket.write(header)
        socket.write(payload)
        socket.flush()

    def _send_error(self, socket: QTcpSocket, message: str):
        data = json.dumps({"message": message})
        self._send_frame(socket, SC_ERROR, data.encode('utf-8'))


import threading

# ... existing code ...

class ServerConsole:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.server = ChatServer()
        self.server.log_message.connect(self._on_log)
        print("=" * 50)
        print("  P2P Chat Server v2")
        print("=" * 50)
        print("命令:")
        print("  start [host] [port]  - 启动服务器 (默认: 0.0.0.0 5000)")
        print("  stop                 - 停止服务器")
        print("  status               - 查看状态")
        print("  quit/exit            - 退出")
        print("=" * 50)
        
        # 使用线程处理输入，避免阻塞 Qt 事件循环
        self.input_thread = threading.Thread(target=self._input_loop, daemon=True)
        self.input_thread.start()
        
        sys.exit(self.app.exec_())

    def _input_loop(self):
        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                cmd = line.strip()
                if cmd:
                    # 将命令处理放回主线程执行，保证 Qt 对象线程安全
                    QMetaObject.invokeMethod(self, "_handle_command", 
                                          Qt.QueuedConnection, 
                                          Q_ARG(str, cmd))
            except Exception as e:
                print(f"输入错误: {e}")
                break

    @pyqtSlot(str)
    def _handle_command(self, cmd: str):
        parts = cmd.split()
        if not parts:
            return
        op = parts[0].lower()

        if op == "start":
            host = parts[1] if len(parts) > 1 else "0.0.0.0"
            port = int(parts[2]) if len(parts) > 2 else 5000
            self.server.start(host, port)
        elif op == "stop":
            self.server.stop()
        elif op == "status":
            if self.server.server and self.server.server.isListening():
                print(f"服务器运行中，监听端口：{self.server.server.serverPort()}")
                print(f"在线用户数：{len(self.server.users)}")
                for user in self.server.users.values():
                    print(f"  - {user.username} ({user.user_id}) in {user.room_id}")
            else:
                print("服务器未运行")
        elif op in ["quit", "exit"]:
            self.quit()

    def _on_log(self, message: str):
        print(f"[{time.strftime('%H:%M:%S')}] {message}")

    def quit(self):
        self.server.stop()
        QApplication.quit()


if __name__ == "__main__":
    ServerConsole()
