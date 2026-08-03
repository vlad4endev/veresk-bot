"""
Фоновое продление / проверка userbot-сессий (Telegram Telethon + MAX PyMax).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from mailing_db import list_send_accounts, update_send_account
from senders.max_userbot import check_max_session, is_pymax_installed
from senders.telegram_userbot import check_telegram_session, is_telethon_configured

logger = logging.getLogger(__name__)

KEEPALIVE_INTERVAL_SEC = max(
    300, int(os.getenv("TG_SESSION_KEEPALIVE_SEC", "1800") or "1800")
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _restore_status(acc: dict[str, Any]) -> str:
    today = datetime.now().date().isoformat()
    wu = acc.get("warmup_until")
    if wu and str(wu) > today:
        return "warmup"
    return "ready"


async def probe_account(acc: dict[str, Any]) -> dict[str, Any]:
    """Проверить один аккаунт и обновить поля в БД."""
    account_id = int(acc["id"])
    now = _now()
    kind = acc.get("kind")
    if kind == "max_userbot":
        live = await check_max_session(
            str(acc.get("session_file") or ""),
            phone=str(acc.get("phone") or "") or None,
        )
    else:
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
        "kind": kind,
        "ok": authorized,
        "error": live.get("error"),
        "username": live.get("username"),
        "label": live.get("label"),
    }


async def keepalive_all_telegram_sessions() -> dict[str, Any]:
    """Пройтись по tg_userbot и max_userbot, продлить/проверить сессии."""
    rows = await list_send_accounts()
    targets = [
        a
        for a in rows
        if a.get("session_file")
        and (
            (a.get("kind") == "tg_userbot" and is_telethon_configured())
            or (a.get("kind") == "max_userbot" and is_pymax_installed())
        )
    ]
    if not targets:
        reason = "no_accounts"
        if not is_telethon_configured() and not is_pymax_installed():
            reason = "not_configured"
        return {"ok": False, "skipped": True, "reason": reason, "items": []}

    items: list[dict[str, Any]] = []
    for acc in targets:
        try:
            items.append(await probe_account(acc))
        except Exception as exc:
            logger.exception("Keepalive failed for account %s", acc.get("id"))
            items.append(
                {
                    "id": acc.get("id"),
                    "kind": acc.get("kind"),
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
            "Session keepalive: %s ok, %s проблем из %s",
            ok_n,
            bad_n,
            len(items),
        )
    return {
        "ok": True,
        "checked": len(items),
        "ok_count": ok_n,
        "bad_count": bad_n,
        "items": items,
    }


async def _keepalive_loop() -> None:
    await asyncio.sleep(45)
    logger.info(
        "Userbot session keepalive запущен (каждые %s с)",
        KEEPALIVE_INTERVAL_SEC,
    )
    while True:
        try:
            await keepalive_all_telegram_sessions()
        except Exception:
            logger.exception("Ошибка в цикле keepalive сессий")
        await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)


def start_telegram_session_keepalive() -> asyncio.Task:
    return asyncio.create_task(_keepalive_loop(), name="tg_session_keepalive")
