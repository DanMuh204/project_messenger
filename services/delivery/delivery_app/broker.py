import asyncio
import logging
import os
import aio_pika
from .events import process


async def consume(connections):
    # Explicit reconnect cycle: readiness follows the subscription, not just TCP.
    # Closing AMQP or its channel invalidates all WS sessions and triggers history recovery.
    while True:
        connection = None
        try:
            connection = await aio_pika.connect(
                host=os.getenv("RABBITMQ_HOST", "rabbitmq"),
                port=int(os.getenv("RABBITMQ_PORT", "5672")),
                login=os.getenv("RABBITMQ_USER", "messenger"),
                password=os.environ["RABBITMQ_PASSWORD"],
                virtualhost=os.getenv("RABBITMQ_VHOST", "messenger"),
                heartbeat=15,
                timeout=3,
                client_properties={"connection_name": "messenger-delivery"},
            )

            async def lost(*args, **kwargs):
                await connections.offline()

            connection.close_callbacks.add(lost)
            channel = await connection.channel()
            channel.close_callbacks.add(lost)
            await channel.set_qos(prefetch_count=50)
            exchange = await channel.declare_exchange(
                "messenger.events", aio_pika.ExchangeType.TOPIC, durable=True
            )
            queue = await channel.declare_queue(
                "delivery.websocket",
                durable=True,
                arguments={"x-queue-type": "classic"},
            )
            await queue.bind(exchange, routing_key="message.created")
            async with queue.iterator(exclusive=True) as iterator:
                connections.ready = True
                logging.getLogger("delivery").info("consumer_ready")
                async for incoming in iterator:
                    await process(incoming, connections)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger("delivery").warning(
                "consumer_unavailable %s", type(exc).__name__
            )
        finally:
            await connections.offline()
            if connection:
                try:
                    async with asyncio.timeout(5):
                        await connection.close()
                except Exception:
                    pass
        await asyncio.sleep(2)
