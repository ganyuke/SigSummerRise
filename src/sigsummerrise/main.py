from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from sigsummerrise.bot import Bot
from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.responses import init_responses
from sigsummerrise.web import mount_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sigsummerrise")


def require_runtime_settings(settings: Settings) -> None:
    if not settings.db_key:
        raise SystemExit("DB_KEY is required")
    if not settings.signal_group_id.strip():
        raise SystemExit("SIGNAL_GROUP_ID is required")


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    start_bot: bool = False,
) -> FastAPI:
    settings = settings or Settings()
    database = db or Database(settings.db_path, settings.db_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.init()
        app.state.settings = settings
        app.state.db = database
        task = None
        bot = None
        if start_bot:
            bot = Bot(settings, database)
            app.state.bot = bot
            task = asyncio.create_task(bot.run())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if bot is not None:
                await bot.signal.aclose()

    application = FastAPI(
        title="SigSummerRise",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    database.init()
    application.state.settings = settings
    application.state.db = database
    mount_routes(application)
    return application


def main() -> None:
    settings = Settings()
    require_runtime_settings(settings)
    init_responses(settings.responses_path)
    log.info("starting web server on %s:%s", settings.bind_host, settings.bind_port)
    uvicorn.run(
        create_app(settings=settings, start_bot=True),
        host=settings.bind_host,
        port=settings.bind_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
