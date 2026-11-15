import time
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .models import Credentials
from .security import digest, new_refresh, unauthorized

router = APIRouter()
bearer = HTTPBearer(auto_error=False)
COOKIE = "messenger_refresh"


def csrf(request: Request):
    if (
        request.headers.get("origin") != request.app.state.settings.origin
        or request.headers.get("x-csrf-protection") != "1"
        or request.headers.get("content-type", "").split(";")[0].strip()
        != "application/json"
    ):
        raise HTTPException(403, "Invalid request origin or CSRF protection")


def user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    if not credentials:
        raise unauthorized()
    return request.app.state.security.verify(credentials.credentials)


def public(row):
    return {k: row[k] for k in ("user_id", "username", "created_at")}


def cookie(response, raw, settings, seconds):
    response.set_cookie(
        COOKIE,
        raw,
        max_age=max(0, int(seconds)),
        httponly=True,
        secure=settings.secure_cookie,
        samesite="strict",
        path="/auth",
    )
    response.headers["Cache-Control"] = "no-store"


def tokens(request, user, family):
    return {
        "access_token": request.app.state.security.access(user, family),
        "token_type": "Bearer",
        "expires_in": request.app.state.settings.access_seconds,
    }


@router.post("/auth/register", status_code=201, dependencies=[Depends(csrf)])
async def register(data: Credentials, request: Request):
    row = await request.app.state.storage.register(
        data.username, await request.app.state.security.hash_password(data.password)
    )
    if not row:
        raise HTTPException(409, "Username already exists")
    return public(row)


@router.post("/auth/login", dependencies=[Depends(csrf)])
async def login(data: Credentials, request: Request, response: Response):
    s = request.app.state
    row = await s.storage.find(data.username)
    valid = await s.security.check_password(
        row["password_hash"] if row else None, data.password
    )
    if not row or not valid:
        raise unauthorized()
    raw = new_refresh()
    family = await s.storage.login(
        row["user_id"], digest(raw), s.settings.refresh_seconds
    )
    cookie(response, raw, s.settings, s.settings.refresh_seconds)
    return {**tokens(request, row["user_id"], family), "user": public(row)}


@router.post("/auth/refresh", dependencies=[Depends(csrf)])
async def refresh(request: Request, response: Response):
    raw = request.cookies.get(COOKIE, "")
    if not 32 <= len(raw) <= 128:
        raise unauthorized()
    new = new_refresh()
    row = await request.app.state.storage.rotate(digest(raw), digest(new))
    if row is None:
        raise unauthorized()
    user, family, expires = row
    cookie(response, new, request.app.state.settings, expires.timestamp() - time.time())
    return tokens(request, user, family)


@router.post("/auth/logout", status_code=204, dependencies=[Depends(csrf)])
async def logout(request: Request, response: Response):
    raw = request.cookies.get(COOKIE, "")
    if 32 <= len(raw) <= 128:
        await request.app.state.storage.logout(digest(raw))
    response.delete_cookie(
        COOKIE,
        path="/auth",
        httponly=True,
        secure=request.app.state.settings.secure_cookie,
        samesite="strict",
    )


@router.get("/auth/me")
async def me(request: Request, user=Depends(user_id)):
    rows = await request.app.state.storage.profiles([user])
    if not rows:
        raise unauthorized()
    return public(rows[0])


@router.get("/users/{username}")
async def find(username: str, request: Request, user=Depends(user_id)):
    row = await request.app.state.storage.find(username.lower())
    if row is None:
        raise HTTPException(404, "User not found")
    return {"user_id": row["user_id"], "username": row["username"]}
