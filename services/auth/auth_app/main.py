from contextlib import asynccontextmanager
import logging
import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .config import Settings
from .security import Security
from .storage import Storage
from .routes import router
from .internal_routes import router as internal_router


@asynccontextmanager
async def lifespan(app):
    app.state.settings = Settings.from_env()
    app.state.security = Security(app.state.settings)
    app.state.storage = await Storage.connect()
    try:
        await app.state.storage.ready()
        yield
    finally:
        await app.state.storage.close()


def create_app():
    app = FastAPI(title="Messenger Auth", version="0.4.0", lifespan=lifespan)
    app.include_router(router)
    app.include_router(internal_router)

    @app.middleware("http")
    async def no_store(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"detail": "Invalid request fields"}, status_code=422)

    async def unavailable(request, exc):
        logging.getLogger("auth").warning("database_unavailable %s", type(exc).__name__)
        return JSONResponse(
            {"detail": "Database temporarily unavailable"}, status_code=503
        )

    for cls in (
        asyncpg.PostgresConnectionError,
        asyncpg.CannotConnectNowError,
        asyncpg.InterfaceError,
        asyncpg.QueryCanceledError,
        asyncpg.TooManyConnectionsError,
        TimeoutError,
        OSError,
    ):
        app.add_exception_handler(cls, unavailable)

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        await request.app.state.storage.ready()
        return {"status": "ready"}

    return app


app = create_app()
