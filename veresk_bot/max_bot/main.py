"""
Точка входа MAX-бота Veresk.

Запуск:  python -m max_bot.main   (из папки veresk_bot)
Docker:  сервис max_bot в docker-compose.yml

Режимы:
1) MAX_WEBHOOK_URL задан — обновления принимает bot-сервис (/api/max/webhook).
   Этот процесс только держит heartbeat (long polling при webhook недоступен).
2) Иначе — long polling GET /updates + уведомление SSE-хаба админки.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from max_bot.api import DEFAULT_API_BASE, MaxAPIError, MaxBotAPI, poll_updates_forever  # noqa: E402
from max_bot.storage import init_max_db  # noqa: E402
from max_bot.survey import SurveyBot  # noqa: E402
from max_bot.webhook_runtime import florist_chat_id, webhook_url  # noqa: E402
from senders.max_bot import get_max_bot_token  # noqa: E402

logger = logging.getLogger(__name__)

UPDATE_TYPES = ["message_created", "message_callback", "bot_started"]
_TOKEN_WAIT_SEC = 10


def _setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_dir = "/app/logs" if os.path.isdir("/app") else str(Path(__file__).resolve().parent.parent / "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                os.path.join(log_dir, "max_bot.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _hub_notify_url() -> str:
    return (
        os.getenv("MAX_HUB_NOTIFY_URL", "").strip()
        or "http://bot:3005/api/internal/max/event"
    )


def _hub_secret() -> str:
    return (
        os.getenv("MAX_WEBHOOK_SECRET", "").strip()
        or get_max_bot_token()
        or ""
    )


async def _notify_admin_hub(update: dict) -> None:
    """Прокинуть событие в SSE-хаб процесса bot (админка)."""
    url = _hub_notify_url()
    secret = _hub_secret()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Max-Internal-Secret"] = secret
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, json={"update": update}, headers=headers
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.debug("hub notify HTTP %s: %s", resp.status, text[:200])
    except Exception:
        logger.debug("hub notify failed", exc_info=True)


async def _webhook_idle_loop() -> None:
    """Webhook-режим: анкета на bot-сервисе, здесь только heartbeat."""
    from bot_metrics import PLATFORM_MAX, touch_bot_heartbeat

    logger.info(
        "MAX webhook mode: обновления → %s (long polling отключён)",
        webhook_url(),
    )
    while True:
        await touch_bot_heartbeat(PLATFORM_MAX)
        await asyncio.sleep(30)


async def _wait_for_token() -> str:
    """Ждём токен из панели (Настройки → MAX) или .env — без crash-loop."""
    warned = False
    while True:
        token = get_max_bot_token()
        if token:
            if warned:
                logger.info("Токен MAX получен — запускаю бота")
            return token
        if not warned:
            logger.warning(
                "Токен MAX-бота не задан. Создайте бота у @MasterBot, "
                "затем укажите токен в админке: Настройки → MAX "
                "(или MAX_BOT_TOKEN в .env). Жду…"
            )
            warned = True
        await asyncio.sleep(_TOKEN_WAIT_SEC)


async def main() -> None:
    _setup_logging()

    token = await _wait_for_token()

    api_base = os.getenv("MAX_API_BASE", DEFAULT_API_BASE)
    api = MaxBotAPI(token, base_url=api_base)

    try:
        me = await api.get_me()
    except MaxAPIError as exc:
        logger.error(
            "Токен MAX неверный или API недоступен (%s). "
            "Проверьте токен у @MasterBot и домен %s.",
            exc,
            api_base,
        )
        await api.close()
        raise SystemExit(1) from None

    logger.info(
        "MAX-бот авторизован: %s (id=%s)",
        me.get("username") or me.get("name") or "?",
        me.get("user_id"),
    )

    await init_max_db()

    from bot_metrics import PLATFORM_MAX, init_bot_metrics, touch_bot_heartbeat

    await init_bot_metrics()

    wh = webhook_url()
    if wh:
        # Подписка регистрируется в bot.on_startup; здесь не поллим.
        try:
            await _webhook_idle_loop()
        finally:
            await api.close()
        return

    # Posiflora — тот же механизм, что и у Telegram-бота
    try:
        from posiflora import start_token_refresher, warmup_token

        await warmup_token()
        start_token_refresher()
    except Exception:
        logger.exception("Posiflora недоступна — анкеты будут сохраняться только локально")

    # florist_chat_id читается из панели/env при каждой анкете (см. SurveyBot)
    bot = SurveyBot(api, florist_chat_id=florist_chat_id())

    async def _max_heartbeat_loop() -> None:
        while True:
            await touch_bot_heartbeat(PLATFORM_MAX)
            await asyncio.sleep(30)

    async def _on_update(update: dict) -> None:
        await touch_bot_heartbeat(PLATFORM_MAX)
        await bot.handle_update(update)
        await _notify_admin_hub(update)

    asyncio.create_task(_max_heartbeat_loop())

    logger.info(
        "🔄 MAX long polling запущен (%s), florist_chat_id=%s",
        api_base,
        florist_chat_id() or "выкл",
    )
    try:
        await poll_updates_forever(api, _on_update, types=UPDATE_TYPES)
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
