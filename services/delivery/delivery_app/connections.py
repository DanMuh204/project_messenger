import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(eq=False)
class Connection:
    socket: Any
    user_id: UUID
    queue: asyncio.Queue
    closed: bool = False
    sender_task: asyncio.Task | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self, code=1013):
        if self.closed:
            return
        self.closed = True
        # Cancel a stalled send before acquiring its lock to close the transport.
        if self.sender_task and self.sender_task is not asyncio.current_task():
            self.sender_task.cancel()
        try:
            async with asyncio.timeout(2):
                async with self.send_lock:
                    await self.socket.close(code=code)
        except Exception:
            pass

    async def send(self):
        self.sender_task = asyncio.current_task()
        while not self.closed:
            data = await self.queue.get()
            async with self.send_lock:
                if self.closed:
                    return
                async with asyncio.timeout(5):
                    await self.socket.send_json(data)


class Connections:
    def __init__(self, settings):
        self.settings = settings
        self.users = {}
        self.ready = False
        self.accepted = 0  # Includes unauthenticated handshakes.

    def add(self, socket, user, expires):
        if not self.ready:
            raise RuntimeError("Broker not ready")
        connection = Connection(socket, user, asyncio.Queue(self.settings.queue_size))
        connection.queue.put_nowait(
            {"type": "ready", "user_id": str(user), "expires_at": expires}
        )
        self.users.setdefault(user, set()).add(connection)
        return connection

    def remove(self, connection):
        group = self.users.get(connection.user_id)
        if group:
            group.discard(connection)
            if not group:
                self.users.pop(connection.user_id, None)

    async def broadcast(self, event):
        payload = {
            "type": "message.created",
            "event_id": str(event.event_id),
            "message": event.message.model_dump(mode="json"),
        }
        slow = []
        for user in event.participant_ids:
            for connection in list(self.users.get(user, ())):
                if connection.closed:
                    continue
                try:
                    connection.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    self.remove(connection)
                    slow.append(connection.close())
        if slow:
            await asyncio.gather(*slow)

    async def offline(self):
        self.ready = False
        clients = [c for group in self.users.values() for c in group]
        self.users.clear()
        await asyncio.gather(*(c.close(1013) for c in clients))
