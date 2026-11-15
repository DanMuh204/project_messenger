import os
from pathlib import Path
from uuid import UUID
import httpx
from .errors import AppError


class AuthClient:
    def __init__(self):
        self.secret = Path(os.environ["INTERNAL_AUTH_SECRET_FILE"]).read_text().strip()
        self.client = httpx.AsyncClient(
            base_url=os.getenv("AUTH_BASE_URL", "http://auth:8000"),
            timeout=2,
            headers={"Authorization": "Bearer " + self.secret},
            follow_redirects=False,
        )

    async def resolve(self, ids, *, required=False):
        try:
            r = await self.client.post(
                "/internal/users/resolve", json={"user_ids": [str(i) for i in ids]}
            )
            r.raise_for_status()
            return {
                UUID(item["user_id"]): item["username"] for item in r.json()["items"]
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            if required:
                raise AppError(
                    503, "auth_unavailable", "User service temporarily unavailable"
                ) from None
            return {}

    async def close(self):
        await self.client.aclose()
