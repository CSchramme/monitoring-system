"""Application factory. Run with: uvicorn app.main:create_app --factory"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import editor, metrics_router, protected, router, user_admin, viewer
from .auth import LoginThrottle
from .config import Settings
from .db import Database
from .notifier import Notifier
from .scheduler import Scheduler
from .webpush import WebPush
from .userstore import MariaDBUserStore, SqliteUserStore, UserConflict, UserStore, UserStoreError

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
AGENT_DIR = BASE_DIR.parent / "agent"


def create_user_store(settings: Settings, db: Database) -> UserStore:
    if not settings.user_db_host:
        return SqliteUserStore(db)
    missing = [
        name for name, value in (
            ("MONITOR_USER_DB_NAME", settings.user_db_name),
            ("MONITOR_USER_DB_USER", settings.user_db_user),
        ) if not value
    ]
    if missing:
        raise RuntimeError(f"Für die Benutzer-Datenbank fehlen: {', '.join(missing)}")
    return MariaDBUserStore(
        host=settings.user_db_host,
        port=settings.user_db_port,
        user=settings.user_db_user,
        password=settings.user_db_password,
        database=settings.user_db_name,
        user_table=settings.user_db_table,
    )


def create_app(settings: Settings | None = None, start_scheduler: bool = True) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings or Settings()
    db = Database(settings.db_path)
    notifier = Notifier(db, settings.public_url)
    users = create_user_store(settings, db)
    webpush = WebPush(db, settings.push_subject, users)
    notifier.webpush = webpush

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = Scheduler(db, notifier, settings.max_concurrent_checks, settings.retention_days, users)
        app.state.scheduler = scheduler
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await notifier.wait_idle()

    app = FastAPI(title="Monitoring", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.notifier = notifier
    app.state.users = users
    app.state.webpush = webpush
    app.state.throttle = LoginThrottle()

    @app.exception_handler(UserStoreError)
    async def user_store_unavailable(request: Request, exc: UserStoreError):
        logging.getLogger("monitoring").error("User database error: %s", exc)
        return JSONResponse({"detail": f"Benutzer-Datenbank nicht erreichbar: {exc}"}, status_code=503)

    @app.exception_handler(UserConflict)
    async def user_conflict(request: Request, exc: UserConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/docs"):
            response.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith("/api/"):
            # UI assets are tiny; always revalidate so updates show up immediately.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.include_router(router)
    app.include_router(protected)
    app.include_router(viewer)
    app.include_router(editor)
    app.include_router(user_admin)
    app.include_router(metrics_router)
    app.mount("/agent", StaticFiles(directory=AGENT_DIR), name="agent")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
