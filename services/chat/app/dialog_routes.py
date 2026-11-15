import base64
import json
from datetime import datetime
from uuid import UUID
from typing import Annotated
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from .routes import User
from .errors import AppError

router = APIRouter(tags=["dialogs"])


class DialogCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    peer_user_id: UUID


def cursor_encode(row):
    return base64.urlsafe_b64encode(
        json.dumps([row["created_at"].isoformat(), str(row["dialog_id"])]).encode()
    ).decode()


def cursor_decode(cursor):
    if cursor is None:
        return None
    try:
        date, ident = json.loads(
            base64.b64decode(cursor, altchars=b"-_", validate=True)
        )
        if not isinstance(date, str) or not isinstance(ident, str):
            raise ValueError()
        stamp = datetime.fromisoformat(date)
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp, UUID(ident)
    except (ValueError, TypeError, UnicodeError):
        raise AppError(422, "invalid_cursor", "Invalid dialog cursor") from None


async def enrich(request, rows, user):
    peers = [
        r["participant_b"] if r["participant_a"] == user else r["participant_a"]
        for r in rows
    ]
    profiles = (
        await request.app.state.auth_client.resolve(list(set(peers))) if peers else {}
    )
    return [
        {**dict(row), "peer_user_id": peer, "peer_username": profiles.get(peer)}
        for row, peer in zip(rows, peers)
    ]


@router.post("/dialogs", status_code=201)
async def create(data: DialogCreate, request: Request, response: Response, user: User):
    peer = data.peer_user_id
    if peer == user:
        raise AppError(422, "self_dialog", "Choose another user")
    storage = request.app.state.storage
    row = await storage.find_pair(user, peer)
    if row:
        response.status_code = 200
    else:
        profiles = await request.app.state.auth_client.resolve([peer], required=True)
        if peer not in profiles:
            raise AppError(404, "user_not_found", "User not found")
        row, created = await storage.create_dialog(user, peer)
        response.status_code = 201 if created else 200
    return (await enrich(request, [row], user))[0]


@router.get("/dialogs")
async def listing(
    request: Request,
    user: User,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
):
    rows = await request.app.state.storage.list_dialogs(
        user, limit + 1, cursor_decode(cursor)
    )
    more = len(rows) > limit
    return {
        "items": await enrich(request, rows[:limit], user),
        "has_more": more,
        "next_cursor": cursor_encode(rows[limit - 1]) if more else None,
    }


@router.get("/dialogs/{dialog_id}")
async def detail(dialog_id: UUID, request: Request, user: User):
    row = await request.app.state.storage.get_dialog(dialog_id, user)
    return (await enrich(request, [row], user))[0]
