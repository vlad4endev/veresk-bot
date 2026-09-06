"""
Фоновое продление / проверка userbot-сессий (Telegram Telethon + MAX PyMax).

Сетевые сбои не считаются смертью сессии с первой попытки: сначала
reconnect пула, затем 2–3 подряд transient-ошибки. Реальная потеря
авторизации (файл, AuthKey, SMS/2FA) помечает аккаунт сразу.
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
    180, int(os.getenv("TG_SESSION_KEEPALIVE_SEC", "600") or "600")
)
UNHEALTHY_INTERVAL_SEC = max(
    90, int(os.getenv("TG_SESSION_KEEPALIVE_FAST_SEC", "180") or "180")
)
TRANSIENT_FAIL_LIMIT = max(
    2, int(os.getenv("TG_SESSION_TRANSIENT_FAILS", "3") or "3")
)

_FATAL_MARKERS = (
    "не авторизован",
    "unauthorized",
    "authkeyunregistered",
    "authkeyduplicated",
    "sessionrevoked",
    "sessionexpired",
    "userdeactivated",
    "userdeactivatedban",
    "session_needs_reauth",
    "session_needs_2fa",
    "файл сессии не найден",
    "переподключите",
    "нужен повторный вход",
    "telethon_missing",
    "pymax_missing",
    "api_id",
    "api_hash",
    "не заданы",
)

_TRANSIENT_MARKERS = (
    "timeout",
    "таймаут",
    "timed out",
    "connection",
    "соединен",
    "network",
    "temporarily",
    "server closed",
    "cancelled",
    "canceled",
    "closed",
    "reset by peer",
    "broken pipe",
    "try again",
    "temporary",
    "unavailable",
    "disconnect",
    "not connected",
    "websocket",
    "eof",
    "ssl",
)

_probe_locks: dict[int, asyncio.Lock] = {}
_probe_locks_guard = asyncio.Lock()
_last_summary: dict[str, Any] = {
    "ok_count": 0,
    "bad_count": 0,
    "checked": 0,
    "at": None,
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def classify_session_error(error: str | None) -> str:
    """fatal — нужна повторная авторизация; transient — сеть/коннект."""
    text = (error or "").strip().lower()
    if not text:
        return "fatal"
    if any(m in text for m in _FATAL_MARKERS):
        # «unavailable» в fatal markers нет, а в transient есть —
        # «не авторизована» уже поймана выше.
        return "fatal"
    if any(m in text for m in _TRANSIENT_MARKERS):
        return "transient"
    return "fatal"


def is_fatal_session_error(error: str | None) -> bool:
    return classify_session_error(error) == "fatal"


def _restore_status(acc: dict[str, Any]) -> str:
    today = datetime.now().date().isoformat()
    wu = acc.get("warmup_until")
    if wu and str(wu) > today:
        return "warmup"
    return "ready"


async def _lock_for(account_id: int) -> asyncio.Lock:
    async with _probe_locks_guard:
        lock = _probe_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            _probe_locks[account_id] = lock
        return lock


async def _release_pools(acc: dict[str, Any]) -> None:
    session_file = str(acc.get("session_file") or "")
    account_id = int(acc["id"]) if acc.get("id") is not None else None
    kind = acc.get("kind")
    try:
        if kind == "max_userbot":
            from senders.max_userbot_chat import release_session as release_max

            await release_max(session_file=session_file, account_id=account_id)
        else:
            from senders.telegram_chat import release_session as release_tg

            await release_tg(session_file=session_file, account_id=account_id)
    except Exception:
        logger.debug("release pool before retry failed", exc_info=True)


async def _live_check(acc: dict[str, Any]) -> dict[str, Any]:
    kind = acc.get("kind")
    if kind == "max_userbot":
        return await check_max_session(
            str(acc.get("session_file") or ""),
            phone=str(acc.get("phone") or "") or None,
        )
    return await check_telegram_session(str(acc.get("session_file") or ""))


async def probe_account(acc: dict[str, Any]) -> dict[str, Any]:
    """Проверить один аккаунт и обновить поля в БД."""
    account_id = int(acc["id"])
    lock = await _lock_for(account_id)
    async with lock:
        return await _probe_account_locked(acc)


async def _probe_account_locked(acc: dict[str, Any]) -> dict[str, Any]:
    account_id = int(acc["id"])
    now = _now()
    kind = acc.get("kind")
    live = await _live_check(acc)
    authorized = bool(live.get("ok") and live.get("authorized"))
    error = live.get("error")
    kind_class = classify_session_error(str(error or ""))

    if not authorized and kind_class == "transient":
        logger.warning(
            "Session probe transient for %s %s: %s — reconnect and retry",
            kind,
            acc.get("phone") or account_id,
            error,
        )
        await _release_pools(acc)
        await asyncio.sleep(1.2)
        live = await _live_check(acc)
        authorized = bool(live.get("ok") and live.get("authorized"))
        error = live.get("error")
        kind_class = classify_session_error(str(error or ""))

    prev_streak = int(acc.get("fail_streak") or 0)
    patch: dict[str, Any] = {"last_checked_at": now}

    if authorized:
        patch["last_ok_at"] = now
        patch["last_error"] = None
        patch["fail_streak"] = 0
        if acc.get("status") in ("unavailable", None, ""):
            patch["status"] = _restore_status(acc)
        if live.get("label") and (
            not acc.get("label") or acc.get("label") == acc.get("phone")
        ):
            patch["label"] = live["label"]
    elif acc.get("status") == "blocked":
        patch["last_error"] = error or "blocked"
        patch["fail_streak"] = prev_streak + 1
    else:
        streak = prev_streak + 1
        patch["fail_streak"] = streak
        patch["last_error"] = error or "unauthorized"
        fatal = kind_class == "fatal" or bool(live.get("fatal"))
        if fatal or streak >= TRANSIENT_FAIL_LIMIT:
            patch["status"] = "unavailable"
            if not fatal and streak >= TRANSIENT_FAIL_LIMIT:
                patch["last_error"] = (
                    f"{error or 'нет коннекта'} "
                    f"(после {streak} сбоев сети — переподключите, если не восстановится)"
                )
        else:
            logger.info(
                "Session %s %s transient fail %s/%s, статус не трогаем: %s",
                kind,
                acc.get("phone") or account_id,
                streak,
                TRANSIENT_FAIL_LIMIT,
                error,
            )

    await update_send_account(account_id, **patch)
    dead = (patch.get("status") == "unavailable") or (
        not authorized and acc.get("status") == "unavailable"
    )
    return {
        "id": account_id,
        "kind": kind,
        "ok": authorized,
        "error": live.get("error") if not authorized else None,
        "fatal": (not authorized) and kind_class == "fatal",
        "needs_reconnect": bool(dead and not authorized),
        "username": live.get("username"),
        "label": live.get("label"),
        "tg_id": live.get("tg_id"),
        "max_user_id": live.get("max_user_id"),
        "phone": live.get("phone"),
        "fail_streak": patch.get("fail_streak", 0),
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
        summary = {"ok": False, "skipped": True, "reason": reason, "items": []}
        _last_summary.update(
            {"ok_count": 0, "bad_count": 0, "checked": 0, "at": _now()}
        )
        return summary

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
                    "fatal": is_fatal_session_error(str(exc)),
                    "needs_reconnect": True,
                }
            )
            try:
                streak = int(acc.get("fail_streak") or 0) + 1
                patch: dict[str, Any] = {
                    "last_checked_at": _now(),
                    "last_error": str(exc),
                    "fail_streak": streak,
                }
                if is_fatal_session_error(str(exc)) or streak >= TRANSIENT_FAIL_LIMIT:
                    patch["status"] = "unavailable"
                await update_send_account(int(acc["id"]), **patch)
            except Exception:
                pass

    ok_n = sum(1 for i in items if i.get("ok"))
    bad_n = len(items) - ok_n
    reconnect_n = sum(1 for i in items if i.get("needs_reconnect"))
    if items:
        logger.info(
            "Session keepalive: %s ok, %s проблем (%s нужно переподключить) из %s",
            ok_n,
            bad_n,
            reconnect_n,
            len(items),
        )
    result = {
        "ok": True,
        "checked": len(items),
        "ok_count": ok_n,
        "bad_count": bad_n,
        "reconnect_count": reconnect_n,
        "items": items,
    }
    _last_summary.update(
        {
            "ok_count": ok_n,
            "bad_count": bad_n,
            "checked": len(items),
            "reconnect_count": reconnect_n,
            "at": _now(),
        }
    )
    return result


def last_keepalive_summary() -> dict[str, Any]:
    return dict(_last_summary)


def _next_interval(result: dict[str, Any]) -> int:
    if result.get("bad_count") or result.get("reconnect_count"):
        return UNHEALTHY_INTERVAL_SEC
    return KEEPALIVE_INTERVAL_SEC


async def _keepalive_loop() -> None:
    await asyncio.sleep(25)
    logger.info(
        "Userbot session keepalive запущен (обычно %s с, при сбое %s с)",
        KEEPALIVE_INTERVAL_SEC,
        UNHEALTHY_INTERVAL_SEC,
    )
    while True:
        result: dict[str, Any] = {}
        try:
            result = await keepalive_all_telegram_sessions()
        except Exception:
            logger.exception("Ошибка в цикле keepalive сессий")
            result = {"bad_count": 1}
        try:
            await _ping_live_pools()
        except Exception:
            logger.debug("pool ping after keepalive failed", exc_info=True)
        await asyncio.sleep(_next_interval(result))


async def _ping_live_pools() -> None:
    try:
        from senders.telegram_chat import ping_pooled_sessions

        await ping_pooled_sessions()
    except Exception:
        logger.debug("telegram pool ping failed", exc_info=True)
    try:
        from senders.max_userbot_chat import drop_dead_sessions

        await drop_dead_sessions()
    except Exception:
        logger.debug("max pool prune failed", exc_info=True)


async def _pool_ping_loop() -> None:
    await asyncio.sleep(70)
    while True:
        try:
            await _ping_live_pools()
        except Exception:
            logger.debug("pool ping loop failed", exc_info=True)
        await asyncio.sleep(120)


def start_telegram_session_keepalive() -> asyncio.Task:
    asyncio.create_task(_pool_ping_loop(), name="userbot_pool_ping")
    return asyncio.create_task(_keepalive_loop(), name="tg_session_keepalive")
