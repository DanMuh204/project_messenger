"""JSON logs and bounded cleanup for all three application processes."""

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                **getattr(record, "fields", {}),
            },
            ensure_ascii=False,
            default=str,
        )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # The application logs sanitized error types; AMQP URLs never enter our logs.
    for name in ("aio_pika", "aiormq"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def log(event: str, **fields) -> None:
    logging.getLogger("messenger").info(event, extra={"fields": fields})


async def close_resource(resource) -> None:
    if resource is None:
        return
    try:
        async with asyncio.timeout(8):
            await resource.close()
    except Exception as exc:
        log("cleanup_failed", error_type=type(exc).__name__)


def run_worker(main) -> None:
    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, task.cancel)
        try:
            await main()
        except asyncio.CancelledError:
            log("worker_stopped")

    configure_logging()
    asyncio.run(run())
