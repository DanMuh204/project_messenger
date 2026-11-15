"""Parameterized SQL. Message and outbox insert share one transaction."""

import asyncio
from uuid import UUID, uuid4

import asyncpg

from .config import DatabaseSettings
from .errors import dialog_not_found, idempotency_conflict
from .models import HistoryPage, Message, MessageCreate, MessageCreated

MESSAGE_COLUMNS = "message_id, dialog_id, sender_id, seq, text, created_at"
DATABASE_UNAVAILABLE = (
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    asyncpg.QueryCanceledError,
    asyncpg.InterfaceError,
    TimeoutError,
    OSError,
)


def message_from_row(row) -> Message:
    return Message.model_validate({key: row[key] for key in Message.model_fields})


def existing_result(row, dialog_id: UUID, data: MessageCreate) -> tuple[Message, bool]:
    if row["dialog_id"] != dialog_id or row["text"] != data.text:
        raise idempotency_conflict()
    return message_from_row(row), False


class Storage:
    def __init__(self, pool: asyncpg.Pool, timeout: float = 3.0):
        self.pool = pool
        self.timeout = timeout

    @classmethod
    async def connect(cls, settings: DatabaseSettings):
        pool = await asyncpg.create_pool(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.user,
            password=settings.password,
            min_size=1,
            max_size=settings.pool_size,
            timeout=settings.timeout,
            command_timeout=settings.timeout,
            server_settings={"application_name": "messenger-chat", "timezone": "UTC"},
        )
        return cls(pool, settings.timeout)

    async def close(self) -> None:
        try:
            async with asyncio.timeout(5):
                await self.pool.close()
        except TimeoutError:
            self.pool.terminate()

    async def ping(self) -> None:
        async with self.pool.acquire(timeout=self.timeout) as connection:
            await connection.execute("SELECT dialog_id, last_seq FROM dialogs WHERE false")
            await connection.execute("SELECT message_id FROM messages WHERE false")
            await connection.execute("SELECT event_id FROM outbox_events WHERE false")

    async def get_dialog(self, dialog_id: UUID, user_id: UUID):
        async with self.pool.acquire(timeout=self.timeout) as connection:
            row = await connection.fetchrow(
                """SELECT dialog_id, participant_a, participant_b, last_seq, created_at
                   FROM dialogs WHERE dialog_id = $1
                   AND (participant_a = $2 OR participant_b = $2)""",
                dialog_id, user_id,
            )
        if row is None:
            raise dialog_not_found()
        return row

    async def find_pair(self, user, peer):
        a, b = sorted((user, peer))
        async with self.pool.acquire(timeout=self.timeout) as c:
            return await c.fetchrow('SELECT * FROM dialogs WHERE participant_a=$1 AND participant_b=$2', a, b)

    async def create_dialog(self, user, peer):
        a, b = sorted((user, peer))
        async with self.pool.acquire(timeout=self.timeout) as c:
            row = await c.fetchrow('INSERT INTO dialogs(dialog_id,participant_a,participant_b) VALUES($1,$2,$3) '
                                  'ON CONFLICT(participant_a,participant_b) DO NOTHING RETURNING *', uuid4(), a, b)
            if row is not None:
                return row, True
            row = await c.fetchrow('SELECT * FROM dialogs WHERE participant_a=$1 AND participant_b=$2', a, b)
            return row, False

    async def list_dialogs(self, user, limit, cursor):
        args = [user, limit]
        condition = ''
        if cursor:
            condition = ' AND (created_at,dialog_id) < ($3,$4)'
            args.extend(cursor)
        async with self.pool.acquire(timeout=self.timeout) as c:
            return await c.fetch('SELECT * FROM dialogs WHERE (participant_a=$1 OR participant_b=$1)' + condition +
                                 ' ORDER BY created_at DESC, dialog_id DESC LIMIT $2', *args)

    async def create_message(
        self, dialog_id: UUID, sender_id: UUID, data: MessageCreate,
    ) -> tuple[Message, bool]:
        async with self.pool.acquire(timeout=self.timeout) as connection:
            try:
                async with connection.transaction():
                    dialog = await connection.fetchrow(
                        """SELECT participant_a, participant_b FROM dialogs
                           WHERE dialog_id = $1 AND (participant_a = $2 OR participant_b = $2)
                           FOR UPDATE""",
                        dialog_id, sender_id,
                    )
                    if dialog is None:
                        raise dialog_not_found()
                    previous = await connection.fetchrow(
                        f"SELECT {MESSAGE_COLUMNS} FROM messages "
                        "WHERE sender_id = $1 AND client_message_id = $2",
                        sender_id, data.client_message_id,
                    )
                    if previous is not None:
                        return existing_result(previous, dialog_id, data)
                    seq = await connection.fetchval(
                        "UPDATE dialogs SET last_seq = last_seq + 1 "
                        "WHERE dialog_id = $1 RETURNING last_seq", dialog_id,
                    )
                    row = await connection.fetchrow(
                        "INSERT INTO messages "
                        "(message_id, dialog_id, sender_id, client_message_id, seq, text) "
                        f"VALUES ($1, $2, $3, $4, $5, $6) RETURNING {MESSAGE_COLUMNS}",
                        uuid4(), dialog_id, sender_id, data.client_message_id, seq, data.text,
                    )
                    message = message_from_row(row)
                    event = MessageCreated(
                        type="message.created", version=1,
                        event_id=uuid4(), occurred_at=message.created_at,
                        participant_ids=(dialog["participant_a"], dialog["participant_b"]),
                        message=message,
                    )
                    await connection.execute(
                        "INSERT INTO outbox_events (event_id, message_id, event_type, payload) "
                        "VALUES ($1, $2, $3, $4::jsonb)",
                        event.event_id, message.message_id, event.type, event.model_dump_json(),
                    )
                # Commit has completed before the caller sends HTTP 201.
                return message, True
            except asyncpg.UniqueViolationError as exc:
                if exc.constraint_name != "messages_idempotency":
                    raise
                # The failed transaction has rolled back, including last_seq.
                previous = await connection.fetchrow(
                    f"SELECT {MESSAGE_COLUMNS} FROM messages "
                    "WHERE sender_id = $1 AND client_message_id = $2",
                    sender_id, data.client_message_id,
                )
                if previous is None:
                    raise
                return existing_result(previous, dialog_id, data)

    async def history(
        self, dialog_id: UUID, snapshot_seq: int, limit: int,
        before_seq: int | None = None, after_seq: int | None = None,
    ) -> HistoryPage:
        # Only these static fragments enter SQL. All request values are parameters.
        condition, direction, cursor = "", "DESC", None
        if before_seq is not None:
            condition, cursor = " AND seq < $4", before_seq
        elif after_seq is not None:
            condition, direction, cursor = " AND seq > $4", "ASC", after_seq
        params = [dialog_id, snapshot_seq, limit + 1]
        if cursor is not None:
            params.append(cursor)
        async with self.pool.acquire(timeout=self.timeout) as connection:
            rows = await connection.fetch(
                f"SELECT {MESSAGE_COLUMNS} FROM messages "
                f"WHERE dialog_id = $1 AND seq <= $2{condition} ORDER BY seq {direction} LIMIT $3",
                *params,
            )
        has_more = len(rows) > limit
        items = [message_from_row(row) for row in rows[:limit]]
        return HistoryPage(
            items=items,
            has_more=has_more,
            next_cursor=items[-1].seq if has_more else None,
        )

    async def next_event(self):
        async with self.pool.acquire(timeout=self.timeout) as connection:
            return await connection.fetchrow(
                """SELECT id, event_id, payload, attempts FROM outbox_events
                   WHERE published_at IS NULL AND next_attempt_at <= now()
                   ORDER BY id LIMIT 1""",
            )

    async def mark_published(self, row_id: int) -> None:
        async with self.pool.acquire(timeout=self.timeout) as connection:
            await connection.execute(
                "UPDATE outbox_events SET published_at = now(), last_error = NULL "
                "WHERE id = $1 AND published_at IS NULL", row_id,
            )

    async def defer_event(self, row_id: int, attempts: int, error_type: str) -> None:
        delay = min(2 ** min(attempts, 4), 10)
        async with self.pool.acquire(timeout=self.timeout) as connection:
            await connection.execute(
                """UPDATE outbox_events SET attempts = attempts + 1, last_error = $2,
                   next_attempt_at = now() + ($3::double precision * interval '1 second')
                   WHERE id = $1 AND published_at IS NULL""",
                row_id, error_type[:100], delay,
            )
