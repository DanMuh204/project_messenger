from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from .auth import current_user
from .errors import AppError
from .models import MAX_SEQ, HistoryPage, Message, MessageCreate
from .runtime import log

router = APIRouter(prefix="/dialogs", tags=["messages"])
User = Annotated[UUID, Depends(current_user)]


@router.post("/{dialog_id}/messages", response_model=Message, status_code=201)
async def send_message(
    dialog_id: UUID, data: MessageCreate, request: Request, response: Response, user: User,
):
    message, created = await request.app.state.storage.create_message(dialog_id, user, data)
    response.status_code = 201 if created else 200
    log(
        "message_saved" if created else "message_replayed",
        message_id=message.message_id, dialog_id=dialog_id, seq=message.seq,
    )
    return message


@router.get("/{dialog_id}/messages", response_model=HistoryPage)
async def read_history(
    dialog_id: UUID, request: Request, response: Response, user: User,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_seq: Annotated[int | None, Query(ge=1, le=MAX_SEQ)] = None,
    after_seq: Annotated[int | None, Query(ge=0, le=MAX_SEQ)] = None,
):
    if before_seq is not None and after_seq is not None:
        raise AppError(422, "validation_error", "Use only one pagination cursor")
    storage, cache = request.app.state.storage, request.app.state.cache
    dialog = await storage.get_dialog(dialog_id, user)  # Authorization before Redis.
    snapshot_seq = dialog["last_seq"]
    first_page = before_seq is None and after_seq is None
    state = "BYPASS"
    if first_page:
        cached, state = await cache.get(dialog_id, snapshot_seq, limit)
        if cached is not None:
            response.headers["X-Cache"] = state
            return cached
    page = await storage.history(dialog_id, snapshot_seq, limit, before_seq, after_seq)
    if first_page and state == "MISS":
        if not await cache.put(dialog_id, snapshot_seq, limit, page):
            state = "BYPASS"
    response.headers["X-Cache"] = state
    return page
