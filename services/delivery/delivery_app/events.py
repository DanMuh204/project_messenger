import logging
from pydantic import ValidationError
from .models import MessageCreated


async def process(incoming, connections):
    try:
        event = MessageCreated.model_validate_json(incoming.body)
    except (ValidationError, ValueError):
        logging.getLogger("delivery").warning("invalid_event")
        await incoming.reject(requeue=False)
        return
    try:
        await connections.broadcast(event)
    except Exception:
        await incoming.reject(requeue=True)
        raise
    await incoming.ack()
    logging.getLogger("delivery").info(
        "event_dispatched event_id=%s message_id=%s",
        event.event_id,
        event.message.message_id,
    )
