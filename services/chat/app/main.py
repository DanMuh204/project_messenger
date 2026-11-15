from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from .auth_client import AuthClient
from .dialog_routes import router as dialog_router
from .auth import TokenVerifier
from .cache import HistoryCache
from .config import AuthSettings, CacheSettings, DatabaseSettings
from .errors import AppError
from .routes import router
from .runtime import close_resource, configure_logging, log
from .storage import DATABASE_UNAVAILABLE, Storage


WEB_ROOT = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_logging()
    application.state.verifier = TokenVerifier(AuthSettings.from_env())
    storage = await Storage.connect(DatabaseSettings.from_env())
    cache = None
    auth_client = None
    try:
        auth_client = AuthClient()
        application.state.auth_client = auth_client
        cache = HistoryCache.connect(CacheSettings.from_env())
        application.state.storage = storage
        application.state.cache = cache
        log("postgres_connected")
        log("redis_connected" if await cache.ping() else "redis_degraded")
        yield
    finally:
        await close_resource(auth_client)
        await close_resource(cache)
        await close_resource(storage)


def create_app() -> FastAPI:
    application = FastAPI(title="Messenger Chat Service", version="0.4.0", lifespan=lifespan)
    application.include_router(router)
    application.include_router(dialog_router)
    application.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")

    @application.get("/", include_in_schema=False)
    async def web_chat() -> FileResponse:
        return FileResponse(
            WEB_ROOT / "index.html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                    "object-src 'none'; frame-ancestors 'none'; form-action 'self'"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            {"code": exc.code, "message": exc.message}, status_code=exc.status, headers=headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Validation details can contain input values; do not echo them or tokens.
        return JSONResponse(
            {"code": "validation_error", "message": "Invalid request parameters or body"},
            status_code=422,
        )

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(
            {"code": f"http_{exc.status_code}", "message": str(exc.detail)},
            status_code=exc.status_code, headers=exc.headers,
        )

    async def database_unavailable(request: Request, exc: Exception):
        log("database_unavailable", error_type=type(exc).__name__)
        return JSONResponse(
            {"code": "database_unavailable", "message": "Database temporarily unavailable"},
            status_code=503,
        )

    for error_type in DATABASE_UNAVAILABLE:
        application.add_exception_handler(error_type, database_unavailable)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        log("internal_error", error_type=type(exc).__name__)
        return JSONResponse(
            {"code": "internal_error", "message": "Internal server error"}, status_code=500,
        )

    @application.get("/hello")
    async def hello() -> dict[str, str]:
        return {"service": "chat", "message": "Hello, world!"}

    @application.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    async def health_ready(request: Request) -> dict[str, str]:
        await request.app.state.storage.ping()
        return {"status": "ready"}

    return application


app = create_app()
