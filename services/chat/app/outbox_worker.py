"""One publisher replica. External I/O never holds the message transaction."""

import asyncio

from .broker import Broker
from .cache import CACHE_UNAVAILABLE, HistoryCache
from .config import BrokerSettings, CacheSettings, DatabaseSettings
from .models import MessageCreated
from .runtime import close_resource, log, run_worker
from .storage import Storage


async def publish_record(storage: Storage, cache: HistoryCache, broker: Broker, row) -> None:
    payload = row["payload"]  # asyncpg decodes JSONB to str by default.
    event = MessageCreated.model_validate_json(payload)
    try:
        async with asyncio.timeout(0.5):
            await cache.invalidate_previous(event.message.dialog_id, event.message.seq)
    except CACHE_UNAVAILABLE as exc:
        # last_seq versioning + TTL keep reads correct even if old keys survive.
        log("cache_invalidation_deferred", error_type=type(exc).__name__)

    await broker.publish(payload, event)
    await storage.mark_published(row["id"])
    log(
        "event_published", event_id=event.event_id, message_id=event.message.message_id,
        dialog_id=event.message.dialog_id, seq=event.message.seq,
    )


async def main() -> None:
    database_settings = DatabaseSettings.from_env(worker=True)
    broker_settings = BrokerSettings.from_env()
    cache = HistoryCache.connect(CacheSettings.from_env())
    storage = broker = None
    try:
        while True:
            try:
                if storage is None:
                    storage = await Storage.connect(database_settings)
                if broker is None:
                    broker = await Broker.connect(broker_settings, "outbox")
                    log("outbox_started")
                row = await storage.next_event()
                if row is None:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    await publish_record(storage, cache, broker, row)
                except Exception as exc:
                    await storage.defer_event(row["id"], row["attempts"], type(exc).__name__)
                    log("outbox_retry", event_id=row["event_id"], error_type=type(exc).__name__)
                    # Reopening also repairs a closed channel or missing binding.
                    await close_resource(broker)
                    broker = None
            except Exception as exc:
                log("outbox_waiting", error_type=type(exc).__name__)
                await close_resource(broker)
                await close_resource(storage)
                broker = storage = None
                await asyncio.sleep(2)
    finally:
        await close_resource(broker)
        await close_resource(cache)
        await close_resource(storage)


if __name__ == "__main__":
    run_worker(main)
