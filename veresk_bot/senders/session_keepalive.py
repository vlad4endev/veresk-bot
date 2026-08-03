"""
Фоновое продление / проверка Telegram userbot-сессий.

Периодически подключается к каждому аккаунту (get_me), чтобы:
- сессия оставалась «живой» и не засыпала без трафика;
- вовремя пометить unavailable, если Telegram отозвал авторизацию;
- обычный пользователь в админке видел понятный статус, а не тихий сбой рассылок.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from mailing_db import list_send_accounts, update_send_account
from senders.telegram_userbot import check_telegram_session, is_telethon_configured

logger = logging.getLogger(__name__)

# Интервал между полными проходами (секунды). По умолчанию 30 минут.
KEEPALIVE_INTERVAL_SEC = max(
    300, int(os.getenv("TG_SESSION_KEEPALIVE_SEC", "1800") or "1800")
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _restore_status(acc: dict[str, Any]) -> str:
    """Вернуть статус ready/warmup после успешного keepalive."""
    today = datetime.now().date().isoformat()
    wu = acc.get("warmup_until")
    if wu and str(wu) > today:
        return "warmup"
    return "ready"


async def probe_account(acc: dict[str, Any]) -> dict[str, Any]:
    """Проверить один аккаунт и обновить поля в БД."""
    account_id = int(acc["id"])
    now = _now()
    live = await check_telegram_session(str(acc.get("session_file") or ""))
    authorized = bool(live.get("ok") and live.get("authorized"))

    patch: dict[str, Any] = {
        "last_checked_at": now,
        "last_error": None if authorized else (live.get("error") or "unauthorized"),
    }
    if authorized:
        patch["last_ok_at"] = now
        if acc.get("status") in ("unavailable", None, ""):
            patch["status"] = _restore_status(acc)
        if live.get("label") and (
            not acc.get("label") or acc.get("label") == acc.get("phone")
        ):
            patch["label"] = live["label"]
    elif acc.get("status") != "blocked":
        patch["status"] = "unavailable"

    await update_send_account(account_id, **patch)
    return {
        "id": account_id,
        "ok": authorized,
        "error": live.get("error"),
        "username": live.get("username"),
        "label": live.get("label"),
    }


async def keepalive_all_telegram_sessions() -> dict[str, Any]:
    """Пройтись по всем tg_userbot и продлить/проверить сессии."""
    if not is_telethon_configured():
        return {"ok": False, "skipped": True, "reason": "telethon_not_configured", "items": []}

    rows = await list_send_accounts()
    tg_rows = [
        a
        for a in rows
        if a.get("kind") == "tg_userbot" and a.get("session_file")
    ]
    items: list[dict[str, Any]] = []
    for acc in tg_rows:
        try:
            items.append(await probe_account(acc))
        except Exception as exc:
            logger.exception("Keepalive failed for account %s", acc.get("id"))
            items.append(
                {
                    "id": acc.get("id"),
                    "ok": False,
                    "error": str(exc),
                }
            )
            try:
                await update_send_account(
                    int(acc["id"]),
                    last_checked_at=_now(),
                    last_error=str(exc),
                    status="unavailable",
                )
            except Exception:
                pass

    ok_n = sum(1 for i in items if i.get("ok"))
    bad_n = len(items) - ok_n
    if items:
        logger.info(
            "Telegram keepalive: %s ok, %s проблем из %s",
            ok_n,
            bad_n,
            len(items),
        )
    return {"ok": True, "checked": len(items), "ok_count": ok_n, "bad_count": bad_n, "items": items}


async def _keepalive_loop() -> None:
    # Небольшая пауза после старта, чтобы не конкурировать с ботом
    await asyncio.sleep(45)
    logger.info(
        "Telegram session keepalive запущен (каждые %s с)",
        KEEPALIVE_INTERVAL_SEC,
    )
    while True:
        try:
            await keepalive_all_telegram_sessions()
        except Exception:
            logger.exception("Ошибка в цикле keepalive Telegram-сессий")
        await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)


def start_telegram_session_keepalive() -> asyncio.Task:
    return asyncio.create_task(_keepalive_loop(), name="tg_session_keepalive")
