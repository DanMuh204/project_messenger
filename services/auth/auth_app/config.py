import os
from dataclasses import dataclass
from pathlib import Path


def secret(name):
    value = Path(os.environ[name]).read_text().strip()
    if len(value) < 32:
        raise ValueError(f"{name} must contain at least 32 characters")
    return value


@dataclass(frozen=True)
class Settings:
    private_key: Path
    public_key: Path
    internal_secret: str
    origin: str = "http://127.0.0.1:8000"
    secure_cookie: bool = False
    issuer: str = "messenger-auth"
    audience: str = "messenger-api"
    access_seconds: int = 900
    refresh_seconds: int = 604800

    @classmethod
    def from_env(cls):
        ttl = int(os.getenv("ACCESS_TOKEN_SECONDS", "900"))
        refresh = int(os.getenv("REFRESH_TOKEN_SECONDS", "604800"))
        if not 2 <= ttl <= 3600 or not 60 <= refresh <= 2592000:
            raise ValueError("Invalid token lifetime")
        origin = os.getenv("APP_ORIGIN", "http://127.0.0.1:8000").rstrip("/")
        secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
        if not secure and origin != "http://127.0.0.1:8000":
            raise ValueError(
                "Insecure cookies are restricted to the local development origin"
            )
        return cls(
            Path(os.environ["JWT_PRIVATE_KEY_FILE"]),
            Path(os.environ["JWT_PUBLIC_KEY_FILE"]),
            secret("INTERNAL_AUTH_SECRET_FILE"),
            origin,
            secure,
            os.getenv("JWT_ISSUER", "messenger-auth"),
            os.getenv("JWT_AUDIENCE", "messenger-api"),
            ttl,
            refresh,
        )
