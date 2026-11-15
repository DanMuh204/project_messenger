"""Shared RabbitMQ topology and confirmed publication."""

import asyncio

import aio_pika
from pamqp.commands import Basic

from .config import BrokerSettings
from .models import MessageCreated
from .runtime import close_resource

EXCHANGE = "messenger.events"
ROUTING_KEY = "message.created"
QUEUE = "delivery.message-created"


class Broker:
    def __init__(self, connection, channel, exchange, queue, settings: BrokerSettings):
        self.connection = connection
        self.channel = channel
        self.exchange = exchange
        self.queue = queue
        self.settings = settings

    @classmethod
    async def connect(cls, settings: BrokerSettings, process: str):
        connection = None
        try:
            connection = await aio_pika.connect_robust(
                host=settings.host, port=settings.port, login=settings.user,
                password=settings.password, virtualhost=settings.vhost,
                timeout=settings.timeout, fail_fast=True, reconnect_interval=2,
                heartbeat=15, client_properties={"connection_name": f"messenger-{process}"},
            )
            async with asyncio.timeout(settings.timeout):
                channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
                exchange = await channel.declare_exchange(
                    EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True, auto_delete=False,
                )
                queue = await channel.declare_queue(
                    QUEUE, durable=True, exclusive=False, auto_delete=False,
                    arguments={"x-queue-type": "classic"},
                )
                await queue.bind(exchange, routing_key=ROUTING_KEY)
            return cls(connection, channel, exchange, queue, settings)
        except BaseException:
            await close_resource(connection)
            raise

    async def publish(self, payload: str, event: MessageCreated) -> None:
        async with asyncio.timeout(self.settings.confirm_timeout):
            await self.channel.ready()
            confirmation = await self.exchange.publish(
                aio_pika.Message(
                    body=payload.encode("utf-8"),
                    content_type="application/json",
                    content_encoding="utf-8",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=str(event.event_id),
                    type=event.type,
                    timestamp=event.occurred_at,
                ),
                routing_key=ROUTING_KEY, mandatory=True,
                timeout=self.settings.confirm_timeout,
            )
            if not isinstance(confirmation, Basic.Ack):
                raise RuntimeError("Publication was not positively confirmed")

    async def close(self) -> None:
        await self.connection.close()
