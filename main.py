#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import struct
from dataclasses import dataclass
from typing import Optional

from PyQt5.QtCore import Qt, pyqtSignal, QObject, QDateTime, QTimer
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QSpinBox, QMessageBox,
    QGroupBox, QFormLayout, QFileDialog, QProgressBar, QCheckBox
)
from PyQt5.QtNetwork import QTcpServer, QTcpSocket, QHostAddress, QAbstractSocket, QNetworkInterface

# -------------------- 协议常量 --------------------
MAGIC = b"PC"      # Protocol Chat
VERSION = 1
HEADER_LEN = 8      # 2(MAGIC) + 1(V) + 1(TYPE) + 4(LEN)

T_TEXT       = 0x01  # 文本（utf-8）
T_FILE_META  = 0x10  # 文件元信息（json: name,size）
T_FILE_CHUNK = 0x11  # 文件数据分片
T_FILE_END   = 0x12  # 文件结束

CHUNK_SIZE = 32 * 1024  # 32KB 分片


def now_ts():
    return QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")


def get_local_ipv4_list():
    addrs = []
    for addr in QNetworkInterface.allAddresses():
        if addr.protocol() == QAbstractSocket.IPv4Protocol and not addr.isLoopback():
            addrs.append(addr.toString())
    # 去重并保持顺序
    seen = set(); result = []
    for a in addrs:
        if a not in seen:
            seen.add(a); result.append(a)
    return result or ["127.0.0.1"]


@dataclass
class IncomingFile:
    name: str
    size: int
    received: int = 0
    fp: Optional[object] = None
    path: Optional[str] = None


class Peer(QObject):
    """封装 P2P 连接：服务端/客户端 + 自定义帧协议（文本/文件）。"""

    # 文本与连接相关
    message_received = pyqtSignal(str)
    connected = pyqtSignal(str)
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    listening = pyqtSignal(int)

    # 文件相关
    file_incoming = pyqtSignal(str, int)          # (name, size)
    file_progress = pyqtSignal(int, int)          # (received, total)
    file_saved = pyqtSignal(str)                  # path

    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: Optional[QTcpServer] = None
        self.socket: Optional[QTcpSocket] = None
        self._rxbuf = bytearray()
        self._incoming: Optional[IncomingFile] = None
        self._download_dir = os.path.join(os.getcwd(), "downloads")
        os.makedirs(self._download_dir, exist_ok=True)

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
            incoming.disconnectFromHost(); incoming.deleteLater(); return
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
            sent = 0
            with open(file_path, 'rb') as fp:
                while True:
                    chunk = fp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    if not self._send_frame(T_FILE_CHUNK, chunk):
                        return False
                    sent += len(chunk)
                    # 让事件循环处理 UI
                    QApplication.processEvents()
            return self._send_frame(T_FILE_END, b"")
        except Exception as e:
            self.error.emit(f"发送文件失败：{e}")
            return False

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
                name = meta.get('name') or 'recv.bin'
                size = int(meta.get('size') or 0)
                save_path = self._unique_path(os.path.join(self._download_dir, name))
                fp = open(save_path, 'wb')
                self._incoming = IncomingFile(name=name, size=size, received=0, fp=fp, path=save_path)
                self.file_incoming.emit(name, size)
                self.file_progress.emit(0, size)
            except Exception as e:
                self.error.emit(f"接收文件元信息失败：{e}")
        elif typ == T_FILE_CHUNK:
            inc = self._incoming
            if not inc or not inc.fp:
                # 未声明文件就收到分片，忽略
                return
            inc.fp.write(payload)
            inc.received += len(payload)
            self.file_progress.emit(inc.received, inc.size)
        elif typ == T_FILE_END:
            inc = self._incoming
            if inc and inc.fp:
                inc.fp.flush(); inc.fp.close()
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
            except Exception:
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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("P2P Chat v2 (PyQt5, IP直连)")
        self.setObjectName("RootWindow")
        self.resize(820, 600)

        self.peer = Peer(self)
        self.log_fp = None  # 聊天日志文件句柄

        self._build_ui()
        self._wire_signals()
        self.setAcceptDrops(True)

    # -------------------- UI 构建 --------------------
    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # 连接控制区
        ctrl_group = QGroupBox("连接")
        ctrl_form = QFormLayout()
        ctrl_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        ctrl_group.setLayout(ctrl_form)

        self.ip_edit = QLineEdit(); self.ip_edit.setPlaceholderText("对方IP，例如 192.168.1.100")
        self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535); self.port_spin.setValue(5000)

        ip_port_row = QHBoxLayout()
        ip_port_row.addWidget(self.ip_edit, 3)
        ip_port_row.addWidget(self.port_spin, 1)
        ip_port_widget = QWidget(); ip_port_widget.setLayout(ip_port_row)

        self.btn_listen = QPushButton("开启主机"); self.btn_listen.setObjectName("secondaryButton")
        self.btn_connect = QPushButton("连接主机"); self.btn_connect.setObjectName("primaryButton")
        self.btn_disconnect = QPushButton("断开"); self.btn_disconnect.setObjectName("dangerButton")
        self.btn_disconnect.setEnabled(False)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_listen)
        btn_row.addWidget(self.btn_connect)
        btn_row.addWidget(self.btn_disconnect)
        btn_row_widget = QWidget(); btn_row_widget.setLayout(btn_row)

        self.local_ip_label = QLabel("本机可用IP：" + ", ".join(get_local_ipv4_list()))
        self.status_label = QLabel("状态：未连接"); self.status_label.setObjectName("statusLabel")

        # 下载目录与日志
        dl_row = QHBoxLayout()
        self.download_edit = QLineEdit(os.path.join(os.getcwd(), "downloads"))
        self.btn_browse = QPushButton("选择…"); self.btn_browse.setObjectName("ghostButton")
        dl_row.addWidget(self.download_edit, 1)
        dl_row.addWidget(self.btn_browse)
        dl_widget = QWidget(); dl_widget.setLayout(dl_row)

        self.chk_log = QCheckBox("保存聊天日志到文件")
        self.btn_choose_log = QPushButton("选择日志文件…"); self.btn_choose_log.setEnabled(False); self.btn_choose_log.setObjectName("ghostButton")

        log_row = QHBoxLayout(); log_row.addWidget(self.chk_log); log_row.addWidget(self.btn_choose_log)
        log_widget = QWidget(); log_widget.setLayout(log_row)

        ctrl_form.addRow("对方IP / 端口：", ip_port_widget)
        ctrl_form.addRow("操作：", btn_row_widget)
        ctrl_form.addRow("下载目录：", dl_widget)
        ctrl_form.addRow(log_widget)
        ctrl_form.addRow(self.local_ip_label)
        ctrl_form.addRow(self.status_label)

        # 聊天区
        self.chat_view = QTextEdit(); self.chat_view.setReadOnly(True); self.chat_view.setPlaceholderText("聊天记录…（可拖拽文件到窗口发送）")

        input_row = QHBoxLayout()
        self.input_edit = QLineEdit(); self.input_edit.setPlaceholderText("输入消息，回车发送…")
        self.btn_send = QPushButton("发送"); self.btn_send.setEnabled(False); self.btn_send.setObjectName("primaryButton")
        self.btn_send_file = QPushButton("发送文件…"); self.btn_send_file.setEnabled(False); self.btn_send_file.setObjectName("secondaryButton")
        input_row.addWidget(self.input_edit, 1)
        input_row.addWidget(self.btn_send)
        input_row.addWidget(self.btn_send_file)
        input_row_widget = QWidget(); input_row_widget.setLayout(input_row)

        # 进度
        prog_row = QHBoxLayout()
        self.prog_label = QLabel("文件传输：-")
        self.prog = QProgressBar(); self.prog.setRange(0, 100); self.prog.setValue(0)
        prog_row.addWidget(self.prog_label, 2); prog_row.addWidget(self.prog, 3)
        prog_widget = QWidget(); prog_widget.setLayout(prog_row)

        root.addWidget(ctrl_group)
        root.addWidget(self.chat_view, 1)
        root.addWidget(input_row_widget)
        root.addWidget(prog_widget)

    # -------------------- 信号绑定 --------------------
    def _wire_signals(self):
        self.btn_listen.clicked.connect(self.on_listen)
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_disconnect.clicked.connect(self.on_disconnect)
        self.btn_send.clicked.connect(self.on_send_text)
        self.input_edit.returnPressed.connect(self.on_send_text)
        self.btn_send_file.clicked.connect(self.on_send_file)
        self.btn_browse.clicked.connect(self.on_choose_download_dir)
        self.chk_log.toggled.connect(self.on_toggle_log)
        self.btn_choose_log.clicked.connect(self.on_choose_log_file)

        self.peer.message_received.connect(self.on_message_received)
        self.peer.connected.connect(self.on_connected)
        self.peer.disconnected.connect(self.on_disconnected)
        self.peer.error.connect(self.on_error)
        self.peer.listening.connect(self.on_listening)

        self.peer.file_incoming.connect(self.on_file_incoming)
        self.peer.file_progress.connect(self.on_file_progress)
        self.peer.file_saved.connect(self.on_file_saved)

    # -------------------- 事件处理 --------------------
    def on_listen(self):
        port = int(self.port_spin.value())
        if self.peer.start_listening(port):
            self.append_sys(f"正在监听 0.0.0.0:{port}，等待对方连接…")
            self.btn_disconnect.setEnabled(True)
        else:
            self.append_sys("监听失败")

    def on_connect(self):
        host = self.ip_edit.text().strip(); port = int(self.port_spin.value())
        if not host:
            QMessageBox.warning(self, "提示", "请先输入对方IP地址"); return
        self.append_sys(f"正在连接 {host}:{port} …")
        self.peer.connect_to(host, port)

    def on_disconnect(self):
        self.peer.close_socket(); self.peer.stop_listening()
        self.append_sys("已请求断开/停止监听")
        self.set_connected_ui(False)

    def on_send_text(self):
        text = self.input_edit.text().strip()
        if not text:
            return
        ok = self.peer.send_text(text)
        if ok:
            self.append_me(text)
            self.input_edit.clear()
            self._log_line(f"[我 {now_ts()}] {text}")
        else:
            self.append_sys("发送失败：尚未连接")

    def on_send_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if not path:
            return
        self.append_sys(f"开始发送文件：{os.path.basename(path)} ({self._human_size(os.path.getsize(path))})")
        ok = self.peer.send_file(path)
        if not ok:
            self.append_sys("发送文件失败")

    def on_message_received(self, text: str):
        self.chat_view.append(f"<b style='color:#1565c0'>对方</b> <span style='color:#999'>[{now_ts()}]</span>：{self._escape(text)}")
        self._log_line(f"[对方 {now_ts()}] {text}")

    def on_connected(self, peer_addr: str):
        self.append_sys(f"已连接：{peer_addr}")
        self.set_connected_ui(True)

    def on_disconnected(self):
        self.append_sys("连接已断开")
        self.set_connected_ui(False)

    def on_error(self, err: str):
        self.append_sys(f"<span style='color:#c62828'>错误</span>：{self._escape(err)}")

    def on_listening(self, port: int):
        self.status_label.setText(f"状态：监听中 (端口 {port})")

    # 文件信号
    def on_file_incoming(self, name: str, size: int):
        self.append_sys(f"接收文件：{name}，大小 {self._human_size(size)}")
        self.prog_label.setText(f"接收 {name}")
        self.prog.setValue(0)

    def on_file_progress(self, received: int, total: int):
        pct = int(received * 100 / total) if total else 0
        self.prog.setValue(pct)

    def on_file_saved(self, path: str):
        self.append_sys(f"文件已保存：{path}")
        self.prog_label.setText("文件传输：完成")

    # 下载与日志设置
    def on_choose_download_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录", self.download_edit.text() or os.getcwd())
        if d:
            self.download_edit.setText(d)
            self.peer.set_download_dir(d)

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
                self.append_sys(f"日志记录到：{path}")
            except Exception as e:
                self.append_sys(f"无法打开日志文件：{e}")
                self.chk_log.setChecked(False)

    def _close_log(self):
        if self.log_fp:
            try:
                self.log_fp.close()
            except Exception:
                pass
            self.log_fp = None

    # -------------------- UI 辅助 --------------------
    def set_connected_ui(self, connected: bool):
        self.btn_send.setEnabled(connected)
        self.btn_send_file.setEnabled(connected)
        self.btn_disconnect.setEnabled(True)
        self.status_label.setText("状态：已连接" if connected else "状态：未连接/未监听")

    def append_me(self, text: str):
        self.chat_view.append(f"<b style='color:#2e7d32'>我</b> <span style='color:#999'>[{now_ts()}]</span>：{self._escape(text)}")

    def append_sys(self, text: str):
        self.chat_view.append(f"<i style='color:#757575'>[系统 {now_ts()}] {self._escape(text)}</i>")

    def _log_line(self, line: str):
        if self.log_fp:
            try:
                self.log_fp.write(line + "\n"); self.log_fp.flush()
            except Exception:
                pass

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

    # 拖拽发送文件
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        if not urls:
            return
        for u in urls:
            path = u.toLocalFile()
            if path and os.path.isfile(path):
                self.append_sys(f"开始发送文件：{os.path.basename(path)} ({self._human_size(os.path.getsize(path))})")
                ok = self.peer.send_file(path)
                if not ok:
                    self.append_sys("发送文件失败")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("P2P Chat v2")
    app.setOrganizationName("Example")
    # 应用外部 QSS 主题，如果文件存在
    qss_path = os.path.join(os.path.dirname(__file__), "style.qss")
    try:
        with open(qss_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except Exception:
        # 找不到或读取失败时，继续使用默认样式
        pass

    win = MainWindow()
    win.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
