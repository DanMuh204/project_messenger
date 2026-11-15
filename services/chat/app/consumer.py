"""Log message.created v1, then acknowledge. Redelivery is permitted."""

import asyncio

from pydantic import ValidationError

from .broker import Broker
from .config import BrokerSettings
from .models import MessageCreated
from .runtime import close_resource, log, run_worker


async def process_message(incoming) -> None:
    try:
        event = MessageCreated.model_validate_json(incoming.body)
    except (ValidationError, ValueError):
        log("consumer_rejected", reason="invalid_event")
        await incoming.reject(requeue=False)
        return
    log(
        "consumer_processed", event_id=event.event_id, message_id=event.message.message_id,
        dialog_id=event.message.dialog_id, seq=event.message.seq,
        redelivered=incoming.redelivered,
    )
    await incoming.ack()


async def main() -> None:
    settings = BrokerSettings.from_env()
    while True:
        broker = None
        try:
            broker = await Broker.connect(settings, "consumer")
            await broker.channel.set_qos(prefetch_count=10)
            log("consumer_started")
            async with broker.queue.iterator(no_ack=False) as iterator:
                async for incoming in iterator:
                    await process_message(incoming)
        except Exception as exc:
            log("consumer_waiting", error_type=type(exc).__name__)
        finally:
            # On processing/ack failure, closing requeues unacknowledged messages.
            await close_resource(broker)
        await asyncio.sleep(2)


if __name__ == "__main__":
    run_worker(main)
