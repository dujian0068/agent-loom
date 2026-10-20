import os
import socket
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/platform.db"
    checkpoint_url: str = "sqlite:///./data/checkpoints.db"
    data_dir: Path = Path("./data")
    secret_key: str = ""
    cookie_secure: bool = False
    allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    session_hours: int = 12
    allow_test_model: bool = False
    poll_seconds: float = 0.5
    worker_host_id: str = socket.gethostname()

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.secret_key:
            raise RuntimeError("PLATFORM_SECRET_KEY is required; generate it with the init command.")
        from cryptography.fernet import Fernet

        Fernet(self.secret_key.encode())
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        os.environ.setdefault("LANGSMITH_TRACING", "false")
