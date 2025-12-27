# PyQt-P2P-Chat

This project is a learning project that aims to learn how to use PyQt5 to build a desktop chatting app which can be used to chat and transfer files.

## Features

- **P2P 直连模式**：点对点直接连接，无需服务器
- **服务器模式**：通过中转服务器进行聊天
- **聊天室功能**：支持多用户聊天室
- **私聊功能**：支持用户间私聊
- **文件传输**：支持文件发送和接收
- **聊天日志**：支持保存聊天记录到文件

## How to run

Only PyQt5 is needed for this project.

```bash
pip install PyQt5
```

## Usage

### 启动客户端

```bash
python main.py
```

客户端提供两种模式：
1. **P2P 直连**：输入对方IP地址直接连接
2. **服务器模式**：连接到服务器，支持聊天室和私聊

### 启动服务器

在服务器上运行：

```bash
python server.py
```

服务器命令：
- `start [host] [port]` - 启动服务器（默认: 0.0.0.0 5000）
- `stop` - 停止服务器
- `status` - 查看状态
- `quit/exit` - 退出

### 服务器模式使用说明

1. 启动服务器
2. 在客户端"服务器模式"标签页输入：
   - 服务器地址和端口
   - 你的昵称
   - 房间ID（默认: default）
3. 点击"连接服务器"
4. 开始聊天：
   - 直接发送消息为群聊
   - `@用户名 消息内容` 为私聊
   - 可查看在线用户列表
   - 支持文件传输
