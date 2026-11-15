import asyncio
import os
from uuid import uuid4
import asyncpg


class Storage:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def connect(cls):
        pool = await asyncpg.create_pool(
            host=os.getenv("PGHOST", "postgres"),
            port=int(os.getenv("PGPORT", "5432")),
            database=os.getenv("PGDATABASE", "auth_db"),
            user=os.getenv("PGUSER", "auth_app"),
            password=os.environ["PGPASSWORD"],
            min_size=1,
            max_size=5,
            timeout=3,
            command_timeout=3,
        )
        return cls(pool)

    async def close(self):
        try:
            async with asyncio.timeout(5):
                await self.pool.close()
        except TimeoutError:
            self.pool.terminate()

    async def ready(self):
        async with self.pool.acquire(timeout=3) as c:
            await c.fetchval("SELECT count(*) FROM users WHERE false")
            await c.fetchval("SELECT count(*) FROM refresh_sessions WHERE false")

    async def register(self, username, password_hash):
        async with self.pool.acquire(timeout=3) as c:
            return await c.fetchrow(
                "INSERT INTO users(user_id, username, password_hash) VALUES($1,$2,$3) "
                "ON CONFLICT(username) DO NOTHING RETURNING user_id, username, created_at",
                uuid4(),
                username,
                password_hash,
            )

    async def find(self, username):
        async with self.pool.acquire(timeout=3) as c:
            return await c.fetchrow("SELECT * FROM users WHERE username=$1", username)

    async def profiles(self, ids):
        async with self.pool.acquire(timeout=3) as c:
            return await c.fetch(
                "SELECT user_id, username, created_at FROM users WHERE user_id=ANY($1::uuid[])",
                ids,
            )

    async def login(self, user_id, token_hash, seconds):
        family = uuid4()
        async with self.pool.acquire(timeout=3) as c:
            await c.execute(
                "INSERT INTO refresh_sessions(token_id,family_id,user_id,token_hash,expires_at) "
                "VALUES($1,$2,$3,$4,now()+$5 * interval '1 second')",
                uuid4(),
                family,
                user_id,
                token_hash,
                seconds,
            )
        return family

    async def rotate(self, old_hash, new_hash):
        # A family lock serializes rotation/replay/logout, including different generations.
        result = None
        async with self.pool.acquire(timeout=3) as c, c.transaction():
            family = await c.fetchval(
                "SELECT family_id FROM refresh_sessions WHERE token_hash=$1", old_hash
            )
            if family is None:
                return None
            await c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(family)
            )
            row = await c.fetchrow(
                "SELECT *, expires_at>now() AS valid FROM refresh_sessions "
                "WHERE token_hash=$1 FOR UPDATE",
                old_hash,
            )
            if row["used_at"] is not None:
                await c.execute(
                    "UPDATE refresh_sessions SET revoked_at=coalesce(revoked_at,now()) WHERE family_id=$1",
                    family,
                )
            elif row["revoked_at"] is None and row["valid"]:
                await c.execute(
                    "UPDATE refresh_sessions SET used_at=now() WHERE token_hash=$1",
                    old_hash,
                )
                await c.execute(
                    "INSERT INTO refresh_sessions(token_id,family_id,user_id,token_hash,expires_at) "
                    "VALUES($1,$2,$3,$4,$5)",
                    uuid4(),
                    family,
                    row["user_id"],
                    new_hash,
                    row["expires_at"],
                )
                result = (row["user_id"], family, row["expires_at"])
        # Return after COMMIT, including family revocation on replay.
        return result

    async def logout(self, token_hash):
        async with self.pool.acquire(timeout=3) as c, c.transaction():
            family = await c.fetchval(
                "SELECT family_id FROM refresh_sessions WHERE token_hash=$1", token_hash
            )
            if family:
                await c.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(family)
                )
                await c.execute(
                    "UPDATE refresh_sessions SET revoked_at=coalesce(revoked_at,now()) WHERE family_id=$1",
                    family,
                )
