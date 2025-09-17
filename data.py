# -*- coding: utf-8 -*-
"""
data.py — 在线数据库访问层（MySQL / PyMySQL 版）

连接串示例：
    mysql+pymysql://user:pass@host:3306/dbname?charset=utf8mb4

依赖：
    pip install SQLAlchemy bcrypt PyMySQL
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, ForeignKey, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship
from sqlalchemy.dialects.mysql import LONGTEXT  # 使用 LONGTEXT 存聊天内容
import bcrypt

Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    # MySQL 没有带时区的 DATETIME，统一存 UTC 的 naive 时间
    created_at = Column(DateTime, default=lambda: datetime.utcnow(), nullable=False)

    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    peer = Column(String(128), default="", nullable=False)     # 对端，如 "192.168.1.2:5000"
    direction = Column(String(8), nullable=False)              # 'sent' 或 'recv'
    content = Column(LONGTEXT, nullable=False)                 # 采用 LONGTEXT 以容纳较长消息
    created_at = Column(DateTime, default=lambda: datetime.utcnow(), nullable=False)

    user = relationship("User", back_populates="messages")

# 按用户+时间倒序的索引，便于查询最近消息
Index("idx_messages_user_time", Message.user_id, Message.created_at.desc())


class Database:
    """
    用法：
        db = Database("mysql+pymysql://user:pass@host:3306/db?charset=utf8mb4")
        db.create_tables()
        db.create_user("alice", "pwd")
        user = db.verify_user("alice", "pwd")
        db.save_message(user["id"], "1.2.3.4:5000", "sent", "hello")
    """
    def __init__(self, url: str):
        if not url:
            raise ValueError("数据库URL为空")
        # MySQL 推荐设置：
        # - pool_pre_ping 防止空闲连接失效
        # - pool_recycle 避免 “MySQL server has gone away”
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            future=True,
        )
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, autocommit=False, expire_on_commit=False)
        self._session: Optional[Session] = None

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = self.SessionLocal()
        return self._session

    def create_tables(self):
        Base.metadata.create_all(self.engine)

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None
        self.engine.dispose()

    # ---- 账号 ----
    def create_user(self, username: str, password: str) -> int:
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("用户名或密码不能为空")
        if self.session.query(User).filter_by(username=username).first():
            raise ValueError("用户名已存在")
        pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        u = User(username=username, password_hash=pw_hash)
        self.session.add(u)
        self.session.commit()
        return u.id

    def verify_user(self, username: str, password: str) -> Optional[Dict]:
        u = self.session.query(User).filter_by(username=username).first()
        if not u:
            return None
        ok = bcrypt.checkpw(password.encode("utf-8"), u.password_hash.encode("utf-8"))
        if not ok:
            return None
        return {"id": u.id, "username": u.username}

    # ---- 聊天记录 ----
    def save_message(self, user_id: int, peer: str, direction: str, content: str):
        if direction not in ("sent", "recv"):
            raise ValueError("direction 必须为 'sent' 或 'recv'")
        m = Message(
            user_id=int(user_id),
            peer=peer or "",
            direction=direction,
            content=content,
            created_at=datetime.utcnow(),
        )
        self.session.add(m)
        self.session.commit()
