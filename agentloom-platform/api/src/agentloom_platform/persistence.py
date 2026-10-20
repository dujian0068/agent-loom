"""Platform records only. LangGraph owns its checkpoint tables."""

import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


def uid():
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Space(Base):
    __tablename__ = "spaces"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20))


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(24))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    draft: Mapped[dict] = mapped_column(JSON)
    release_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Release(Base):
    __tablename__ = "releases"
    __table_args__ = (UniqueConstraint("app_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    draft_revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class ReleaseJob(Base):
    __tablename__ = "release_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    status: Mapped[str] = mapped_column(String(30), default="prepared")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ResourceVersion(Base):
    __tablename__ = "resource_versions"
    __table_args__ = (UniqueConstraint("resource_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="ready")
    spec: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Secret(Base):
    __tablename__ = "secrets"
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    app_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(100), default="新会话")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"))
    thread_id: Mapped[str] = mapped_column(String(32), unique=True, default=uid)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    host_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cleanup_state: Mapped[str] = mapped_column(String(24), default="pending")
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    deadline_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Input(Base):
    __tablename__ = "inputs"
    __table_args__ = (UniqueConstraint("session_id", "client_message_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    client_message_id: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="submitting")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class RunEvent(Base):
    __tablename__ = "run_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Operation(Base):
    __tablename__ = "operations"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Receipt(Base):
    __tablename__ = "request_receipts"
    actor_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSON)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    space_id: Mapped[str] = mapped_column(String(32), index=True)
    actor_id: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {},
        )
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def sqlite_setup(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def session(self, write=False):
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                if write:
                    # Short metadata transactions only; never hold this lock across model or tool calls.
                    if self.engine.dialect.name == "sqlite":
                        session.execute(text("BEGIN IMMEDIATE"))
                    else:
                        session.execute(text("SELECT pg_advisory_xact_lock(781205319)"))
                yield session
                if write:
                    session.commit()
            except BaseException:
                session.rollback()
                raise
