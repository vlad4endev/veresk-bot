#!/usr/bin/env python3
"""Локальный запуск админки: статика /admin/ + API /api/admin/ (без Telegram-бота)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from admin_api import on_admin_startup, setup_admin_routes
from bot_metrics import init_bot_metrics
from mailing_db import init_mailing_db, upsert_customer, upsert_customer_event, count_customers

ROOT = Path(__file__).resolve().parent
ADMIN_DIR = ROOT / "adminapp"
HOST = "127.0.0.1"
PORT = 3005

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("admin_local")


async def _seed_demo() -> None:
    if await count_customers() > 0:
        return
    c1 = await upsert_customer(
        posiflora_id="local-demo-1",
        name="Мария Волкова",
        phone="9162341122",
        segment="regular",
        notes="Локальный демо-клиент",
    )
    c2 = await upsert_customer(
        posiflora_id="local-demo-2",
        name="Игорь Петров",
        phone="9037802215",
        segment="new",
    )
    from datetime import date, timedelta

    today = date.today()
    await upsert_customer_event(
        customer_id=c1,
        posiflora_event_id="local-ev-1",
        title="День рождения",
        date_from=today.isoformat(),
        kind="bday",
    )
    await upsert_customer_event(
        customer_id=c2,
        posiflora_event_id="local-ev-2",
        title="День рождения",
        date_from=(today + timedelta(days=2)).isoformat(),
        kind="bday",
    )
    logger.info("Добавлены демо-клиенты для локального просмотра")


async def main() -> None:
    await init_mailing_db()
    await init_bot_metrics()
    await _seed_demo()

    from senders.telegram_userbot import is_telethon_installed
    from senders.session_keepalive import start_telegram_session_keepalive

    if not is_telethon_installed():
        logger.warning(
            "Telethon не установлен — подключение Telegram-номеров недоступно. "
            "Выполните: .venv/bin/pip install telethon==1.36.0 и перезапустите админку."
        )
    else:
        start_telegram_session_keepalive()

    app = web.Application()
    app["redis"] = None
    app["bot"] = None
    setup_admin_routes(app)
    app.on_startup.append(on_admin_startup)

    async def redirect_root(_request: web.Request) -> web.Response:
        raise web.HTTPFound("/admin/")

    async def admin_index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(ADMIN_DIR / "index.html")

    app.router.add_get("/", redirect_root)
    app.router.add_get("/admin", redirect_root)
    app.router.add_get("/admin/", admin_index)
    app.router.add_static("/admin/", path=str(ADMIN_DIR), name="admin")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    logger.info("Админка: http://%s:%s/admin/", HOST, PORT)
    logger.info("Вход: логин ADMIN_USERNAME, пароль ADMIN_PASSWORD из .env")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
