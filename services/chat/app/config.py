"""Environment settings. Each process reads only its own dependencies."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path


def required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Required environment variable is missing: {name}")
    return value


def number(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def integer(name: str, default: int, maximum: int = 65535) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return value


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    timeout: float = 3.0
    pool_size: int = 5

    @classmethod
    def from_env(cls, *, worker: bool = False):
        return cls(
            host=os.getenv("PGHOST", "postgres"),
            port=integer("PGPORT", 5432),
            database=os.getenv("PGDATABASE", "chat_db"),
            user=os.getenv("PGUSER", "chat_app"),
            password=required("PGPASSWORD"),
            timeout=number("DEPENDENCY_TIMEOUT_SECONDS", 3),
            pool_size=1 if worker else integer("PGPOOL_MAX_SIZE", 5, 100),
        )


@dataclass(frozen=True)
class CacheSettings:
    url: str = field(repr=False)
    ttl: int = 60
    timeout: float = 3.0

    @classmethod
    def from_env(cls):
        return cls(
            url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
            ttl=integer("CACHE_TTL_SECONDS", 60, 86400),
            timeout=number("DEPENDENCY_TIMEOUT_SECONDS", 3),
        )


@dataclass(frozen=True)
class BrokerSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    vhost: str = "messenger"
    timeout: float = 3.0
    confirm_timeout: float = 5.0

    @classmethod
    def from_env(cls):
        return cls(
            host=os.getenv("RABBITMQ_HOST", "rabbitmq"),
            port=integer("RABBITMQ_PORT", 5672),
            user=os.getenv("RABBITMQ_USER", "messenger"),
            password=required("RABBITMQ_PASSWORD"),
            vhost=os.getenv("RABBITMQ_VHOST", "messenger"),
            timeout=number("DEPENDENCY_TIMEOUT_SECONDS", 3),
            confirm_timeout=number("PUBLISH_CONFIRM_TIMEOUT_SECONDS", 5),
        )


@dataclass(frozen=True)
class AuthSettings:
    public_key_file: Path
    issuer: str = "messenger-auth"
    audience: str = "messenger-api"

    @classmethod
    def from_env(cls):
        return cls(
            public_key_file=Path(required("JWT_PUBLIC_KEY_FILE")),
            issuer=os.getenv("JWT_ISSUER", "messenger-auth"),
            audience=os.getenv("JWT_AUDIENCE", "messenger-api"),
        )
