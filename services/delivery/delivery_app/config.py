import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    public_key: Path
    origin: str = "http://127.0.0.1:8000"
    issuer: str = "messenger-auth"
    audience: str = "messenger-api"
    auth_timeout: float = 5
    queue_size: int = 100
    max_connections: int = 1000
    heartbeat_seconds: float = 20
    receive_timeout: float = 60

    @classmethod
    def from_env(cls):
        return cls(
            Path(os.environ["JWT_PUBLIC_KEY_FILE"]),
            os.getenv("APP_ORIGIN", "http://127.0.0.1:8000"),
            os.getenv("JWT_ISSUER", "messenger-auth"),
            os.getenv("JWT_AUDIENCE", "messenger-api"),
        )
