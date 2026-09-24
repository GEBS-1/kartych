from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.max_client import MaxAPIError, MaxClient
from app.config import Settings, get_settings
from app.db.redis import create_redis, ping_redis
from app.db.session import create_all as create_tables
from app.db.session import create_engine, ping_db
from app.logging import setup_logging
from app.web.pages import router as pages_router
from app.webhooks.router import router as webhook_router
from app.workers.subscription_watchdog import check_subscriptions, run_subscription_watchdog

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent


async def _optional(coro, label: str) -> Any:
    try:
        return await coro
    except Exception as exc:
        log.warning("%s unavailable: %s", label, exc)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    setup_logging(settings.log_level)
    app.state.bot = None
    app.state.webhook_status = {"ok": False, "action": "starting"}
    app.state.db_ok = False
    app.state.redis_ok = False

    if settings.app_env == "test":
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import StaticPool

        from app.db.session import create_session_factory, seed_demo_shops

        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        await create_tables(engine)
        factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = factory
        app.state.max_client = MaxClient(settings)
        app.state.redis = None
        app.state.bot = {"user_id": 1, "username": "cupcard_bot"}
        app.state.webhook_status = {"ok": True, "action": "test"}
        app.state.db_ok = True
        app.state.redis_ok = False
        async with factory() as session:
            await seed_demo_shops(session)
        yield
        await app.state.max_client.aclose()
        await engine.dispose()
        return

    engine = create_engine(settings)
    app.state.engine = engine
    await _optional(create_tables(engine), "create_tables")
    from app.db.session import create_session_factory, seed_demo_shops

    factory = create_session_factory(engine)
    app.state.session_factory = factory
    async with factory() as session:
        await _optional(seed_demo_shops(session), "seed_shops")
    redis = create_redis(settings) if settings.redis_url else None
    app.state.redis = redis
    client = MaxClient(settings)
    app.state.max_client = client

    app.state.db_ok = bool(await _optional(ping_db(engine), "database"))
    app.state.redis_ok = (
        bool(await _optional(ping_redis(redis), "redis")) if redis is not None else False
    )

    if settings.max_bot_token:
        try:
            app.state.bot = await client.get_me()
        except Exception as exc:
            log.warning("GET /me TLS failed (%s), retry without verify", exc)
            await client.aclose()
            import httpx

            client = MaxClient(
                settings,
                client=httpx.AsyncClient(
                    base_url=settings.max_api_base.rstrip("/"),
                    timeout=httpx.Timeout(15.0),
                    headers={
                        "Authorization": settings.max_bot_token.strip(),
                        "Content-Type": "application/json",
                    },
                    verify=False,
                ),
            )
            app.state.max_client = client
            try:
                app.state.bot = await client.get_me()
            except MaxAPIError as exc2:
                log.error("GET /me failed: %s body=%s", exc2, exc2.body)
        if app.state.bot:
            log.info(
                "MAX bot: @%s id=%s", app.state.bot.get("username"), app.state.bot.get("user_id")
            )
        if settings.subscribe_on_startup and settings.public_base_url.startswith("https://"):
            await check_subscriptions(app)
        elif settings.subscribe_on_startup:
            log.warning("PUBLIC_BASE_URL без HTTPS — подписку webhook не регистрирую")
    else:
        log.warning("MAX_BOT_TOKEN пустой — бот в режиме каркаса")

    watchdog = asyncio.create_task(run_subscription_watchdog(app), name="subscription-watchdog")
    try:
        yield
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass
        await client.aclose()
        if redis is not None:
            await redis.aclose()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Картыч",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def protect_session(request, call_next):
        from fastapi.responses import JSONResponse

        # SameSite=None is needed for MAX, so reject cross-origin browser writes.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), settings.public_base_url.rstrip("/")}
            if origin and origin not in allowed:
                return JSONResponse({"detail": "Недопустимый источник запроса"}, status_code=403)
        response = await call_next(request)
        if not request.url.path.startswith(("/static/", "/favicon")):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(webhook_router)
    app.include_router(pages_router)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(ROOT / "static" / "favicon.ico", media_type="image/x-icon")

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        bot = getattr(app.state, "bot", None) or {}
        return {
            "ok": True,
            "env": settings.app_env,
            "bot": {
                "username": bot.get("username") or settings.max_bot_username,
                "user_id": bot.get("user_id"),
            },
            "webhook": getattr(app.state, "webhook_status", {}),
            "postgres": bool(getattr(app.state, "db_ok", False)),
            "redis": bool(getattr(app.state, "redis_ok", False)),
            "miniapp_url": settings.miniapp_url,
        }

    return app


app = create_app()
