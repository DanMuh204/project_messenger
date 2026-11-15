from uuid import UUID

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.asyncio.retry import Retry

from .config import CacheSettings
from .models import HistoryPage
from .runtime import log

CACHE_UNAVAILABLE = (RedisError, OSError, TimeoutError)


def history_key(dialog_id: UUID, seq: int) -> str:
    return f"history:{dialog_id}:v:{seq}"


class HistoryCache:
    def __init__(self, client: Redis, ttl: int = 60):
        self.client = client
        self.ttl = ttl

    @classmethod
    def connect(cls, settings: CacheSettings):
        return cls(Redis.from_url(
            settings.url, decode_responses=True,
            socket_connect_timeout=settings.timeout,
            socket_timeout=settings.timeout,
            retry=Retry(NoBackoff(), 0),
        ), settings.ttl)

    async def close(self) -> None:
        await self.client.aclose()

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except CACHE_UNAVAILABLE:
            return False

    async def get(self, dialog_id: UUID, seq: int, limit: int):
        try:
            raw = await self.client.hget(history_key(dialog_id, seq), str(limit))
            if raw is not None:
                try:
                    page = HistoryPage.model_validate_json(raw)
                    if any(item.dialog_id != dialog_id or item.seq > seq for item in page.items):
                        raise ValueError("Cache snapshot mismatch")
                    log("cache_hit", dialog_id=dialog_id, seq=seq, limit=limit)
                    return page, "HIT"
                except (ValidationError, ValueError):
                    log("cache_invalid_payload", dialog_id=dialog_id, seq=seq)
            log("cache_miss", dialog_id=dialog_id, seq=seq, limit=limit)
            return None, "MISS"
        except CACHE_UNAVAILABLE as exc:
            log("cache_bypass", dialog_id=dialog_id, error_type=type(exc).__name__)
            return None, "BYPASS"

    async def put(self, dialog_id: UUID, seq: int, limit: int, page: HistoryPage) -> bool:
        try:
            async with self.client.pipeline(transaction=True) as pipe:
                pipe.hset(history_key(dialog_id, seq), str(limit), page.model_dump_json())
                pipe.expire(history_key(dialog_id, seq), self.ttl)
                await pipe.execute()
            return True
        except CACHE_UNAVAILABLE as exc:
            log("cache_bypass", dialog_id=dialog_id, error_type=type(exc).__name__)
            return False

    async def invalidate_previous(self, dialog_id: UUID, seq: int) -> None:
        # Deliberately propagate failures: outbox waits until invalidation succeeds.
        await self.client.delete(history_key(dialog_id, seq - 1))
        log("cache_invalidated", dialog_id=dialog_id, version=seq - 1)
