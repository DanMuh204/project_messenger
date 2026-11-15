import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
import time
import jwt
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect
from .auth import Verifier
from .broker import consume
from .config import Settings
from .connections import Connections


@asynccontextmanager
async def lifespan(app):
    logging.basicConfig(level=logging.INFO)
    for name in ("aio_pika", "aiormq"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.verifier = Verifier(settings)
    app.state.connections = Connections(settings)
    task = asyncio.create_task(consume(app.state.connections))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def read_json(ws):
    raw = await ws.receive_text()
    if len(raw.encode()) > 8192:
        await ws.close(1009)
        raise WebSocketDisconnect(1009)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Object required")
    return data


async def receive(connection, settings):
    while not connection.closed:
        async with asyncio.timeout(settings.receive_timeout):
            data = await read_json(connection.socket)
        if data.get("type") == "ping" and set(data) == {"type"}:
            connection.queue.put_nowait({"type": "pong"})
        elif data.get("type") != "pong" or set(data) != {"type"}:
            await connection.close(1008)
            return


async def heartbeat(connection, settings):
    while not connection.closed:
        await asyncio.sleep(settings.heartbeat_seconds)
        connection.queue.put_nowait({"type": "ping"})


async def expire(connection, expires):
    await asyncio.sleep(max(0, expires - time.time()))
    await connection.close(4001)


def create_app():
    app = FastAPI(title="Messenger Delivery", version="0.4.0", lifespan=lifespan)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        ok = app.state.connections.ready
        return JSONResponse(
            {"status": "ready" if ok else "waiting_for_broker"},
            status_code=200 if ok else 503,
        )

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        settings, manager = app.state.settings, app.state.connections
        if ws.headers.get("origin") != settings.origin:
            await ws.close(1008)
            return
        if manager.accepted >= settings.max_connections or not manager.ready:
            await ws.close(1013)
            return
        manager.accepted += 1
        connection = None
        tasks = []
        try:
            await ws.accept()
            async with asyncio.timeout(settings.auth_timeout):
                data = await read_json(ws)
                if (
                    set(data) != {"type", "access_token"}
                    or data["type"] != "auth"
                    or not isinstance(data["access_token"], str)
                ):
                    raise ValueError("Authentication required")
                user, expires = app.state.verifier.verify(data["access_token"])
            connection = manager.add(ws, user, expires)
            tasks = [
                asyncio.create_task(coro)
                for coro in (
                    connection.send(),
                    receive(connection, settings),
                    heartbeat(connection, settings),
                    expire(connection, expires),
                )
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if not task.cancelled():
                    task.result()
        except jwt.ExpiredSignatureError:
            await ws.close(4001)
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            if connection:
                await connection.close(1008)
            else:
                await ws.close(1008)
        except (WebSocketDisconnect, TimeoutError, RuntimeError, asyncio.QueueFull):
            if connection:
                await connection.close(1013)
            else:
                with suppress(Exception):
                    await ws.close(1008)
        finally:
            manager.accepted -= 1
            if connection:
                manager.remove(connection)
                await connection.close(1000)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    return app


app = create_app()
