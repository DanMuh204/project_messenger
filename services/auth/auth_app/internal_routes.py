import secrets
from fastapi import APIRouter, HTTPException, Request
from .models import ResolveUsers

router = APIRouter()


@router.post("/internal/users/resolve")
async def resolve(data: ResolveUsers, request: Request):
    given = request.headers.get("authorization", "")
    if not secrets.compare_digest(
        given, "Bearer " + request.app.state.settings.internal_secret
    ):
        raise HTTPException(401, "Service authentication required")
    rows = await request.app.state.storage.profiles(data.user_ids)
    return {
        "items": [
            {"user_id": row["user_id"], "username": row["username"]} for row in rows
        ]
    }
