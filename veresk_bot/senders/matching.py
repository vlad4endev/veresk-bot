"""
Сверка клиентов из базы с каналами отправки (Telegram userbot / MAX).

Правила доставки:
- Telegram: отправка от имени подключённого userbot-аккаунта.
  Нужен телефон (ImportContacts) и/или tg_user_id; и хотя бы один ready tg_userbot.
- MAX: сначала личный MAX-аккаунт (PyMax userbot), иначе официальный MAX-бот.
  Userbot: достаточно телефона (search/import) или max_user_id.
  Бот: нужен max_user_id у клиента или в max_profiles по телефону.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Any

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

# Кэш phone_digits → max_user_id (полный скан max_profiles редко нужен)
_MAX_PHONE_CACHE: dict[str, int] = {}
_MAX_PHONE_CACHE_TS: float = 0.0
_MAX_PHONE_CACHE_TTL = 60.0


def normalize_channel(raw: str) -> str | None:
    ch = (raw or "").strip().lower()
    if ch in ("tg", "telegram"):
        return "tg"
    if ch in ("max", "mx"):
        return "max"
    return None


def parse_channels(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return ["tg"]
    if isinstance(raw, list):
        parts = raw
    else:
        text = (
            str(raw)
            .replace("Telegram", "tg")
            .replace("MAX", "max")
            .replace(";", ",")
        )
        parts = text.split(",")
    out: list[str] = []
    for p in parts:
        ch = normalize_channel(str(p))
        if ch and ch not in out:
            out.append(ch)
    return out or ["tg"]


def phone_digits(phone: str | None) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]
    return digits


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def invalidate_max_phone_cache() -> None:
    global _MAX_PHONE_CACHE, _MAX_PHONE_CACHE_TS
    _MAX_PHONE_CACHE = {}
    _MAX_PHONE_CACHE_TS = 0.0


def _load_max_phone_cache(force: bool = False) -> dict[str, int]:
    global _MAX_PHONE_CACHE, _MAX_PHONE_CACHE_TS
    now = time.monotonic()
    if (
        not force
        and _MAX_PHONE_CACHE_TS
        and (now - _MAX_PHONE_CACHE_TS) < _MAX_PHONE_CACHE_TTL
        and _MAX_PHONE_CACHE is not None
    ):
        return _MAX_PHONE_CACHE
    cache: dict[str, int] = {}
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT max_user_id, phone FROM max_profiles"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("max_profiles недоступна для matching", exc_info=True)
        _MAX_PHONE_CACHE = cache
        _MAX_PHONE_CACHE_TS = now
        return cache

    for user_id, stored_phone in rows:
        digits = phone_digits(stored_phone)
        uid = _parse_int(user_id)
        if digits and uid is not None and digits not in cache:
            cache[digits] = uid
    _MAX_PHONE_CACHE = cache
    _MAX_PHONE_CACHE_TS = now
    return cache


def resolve_max_user_id_sync(
    *,
    max_user_id: Any = None,
    phone: str | None = None,
) -> int | None:
    """max_user_id из карточки клиента, иначе поиск в max_profiles по телефону."""
    mid = _parse_int(max_user_id)
    if mid is not None:
        return mid

    target = phone_digits(phone)
    if not target:
        return None
    cache = _load_max_phone_cache()
    return cache.get(target)


def customer_can_receive_tg(customer: dict[str, Any]) -> tuple[bool, str | None]:
    if _parse_int(customer.get("tg_user_id")) is not None:
        return True, None
    if phone_digits(customer.get("phone")):
        return True, None
    return False, "Нет телефона и Telegram id"


def customer_can_receive_max(
    customer: dict[str, Any],
    *,
    allow_phone: bool = False,
) -> tuple[bool, str | None]:
    """
    allow_phone=True — можно слать через MAX userbot по телефону
    (search_by_phone / import_contacts), без заранее известного max_user_id.
    """
    mid = resolve_max_user_id_sync(
        max_user_id=customer.get("max_user_id"),
        phone=customer.get("phone"),
    )
    if mid is not None:
        return True, None
    if allow_phone and phone_digits(customer.get("phone")):
        return True, None
    return False, "Клиент не найден в MAX (нет max_user_id / анкеты)"


def customer_can_receive(
    customer: dict[str, Any],
    channel: str,
    *,
    max_allow_phone: bool = False,
) -> tuple[bool, str | None]:
    ch = normalize_channel(channel) or channel
    if ch == "tg":
        return customer_can_receive_tg(customer)
    if ch == "max":
        return customer_can_receive_max(customer, allow_phone=max_allow_phone)
    return False, f"Неизвестный канал: {channel}"


def build_recipients_for_customers(
    customers: list[dict[str, Any]],
    channels: list[str],
    *,
    tg_accounts_ready: bool = True,
    max_configured: bool = True,
    max_allow_phone: bool = False,
) -> dict[str, Any]:
    """
    Сверяет клиентов сегмента с каналами и доступностью аккаунтов.

    Возвращает:
      recipients: [(customer_id, channel), ...]
      reachable: {tg, max, total}
      skipped: {reason: count}
      skipped_samples: короткие примеры причин
    """
    ch_list = parse_channels(channels)
    recipients: list[tuple[int, str]] = []
    reachable = {"tg": 0, "max": 0, "total": 0}
    skipped: dict[str, int] = {}
    samples: list[str] = []

    def _skip(reason: str, name: str = "") -> None:
        skipped[reason] = skipped.get(reason, 0) + 1
        if len(samples) < 8:
            label = (name or "Клиент").strip() or "Клиент"
            samples.append(f"{label}: {reason}")

    for cust in customers:
        cid = int(cust["id"])
        name = str(cust.get("name") or "")
        for ch in ch_list:
            if ch == "tg" and not tg_accounts_ready:
                _skip("Нет готовых Telegram-аккаунтов", name)
                continue
            if ch == "max" and not max_configured:
                _skip("Нет готового MAX-аккаунта (личный или бот)", name)
                continue
            ok, err = customer_can_receive(
                cust, ch, max_allow_phone=max_allow_phone
            )
            if not ok:
                _skip(err or "Недоступен", name)
                continue
            recipients.append((cid, ch))
            reachable[ch] = reachable.get(ch, 0) + 1
            reachable["total"] += 1

    return {
        "recipients": recipients,
        "reachable": reachable,
        "skipped": skipped,
        "skipped_samples": samples,
        "channels": ch_list,
        "segment_total": len(customers),
    }


async def preview_mailing_match(
    *,
    segment: str,
    channels: str | list[str],
) -> dict[str, Any]:
    """Превью: сколько клиентов сегмента реально получат через выбранные каналы."""
    from mailing_db import customers_for_segment, list_send_accounts, pick_ready_account
    from senders.max_bot import is_max_configured

    customers = await customers_for_segment(segment or "all")
    accounts = await list_send_accounts()
    tg_ready = await pick_ready_account("tg_userbot")
    tg_count = sum(
        1
        for a in accounts
        if a.get("kind") == "tg_userbot" and a.get("status") in ("ready", "warmup")
    )
    max_userbot = await pick_ready_account("max_userbot")
    max_userbot_count = sum(
        1
        for a in accounts
        if a.get("kind") == "max_userbot" and a.get("status") in ("ready", "warmup")
    )
    max_bot_ok = is_max_configured()
    max_ready = bool(max_userbot) or max_bot_ok
    match = build_recipients_for_customers(
        customers,
        channels,
        tg_accounts_ready=bool(tg_ready),
        max_configured=max_ready,
        max_allow_phone=bool(max_userbot),
    )
    max_mode = "userbot" if max_userbot else ("bot" if max_bot_ok else "none")
    return {
        "segment": segment or "all",
        "segment_total": match["segment_total"],
        "channels": match["channels"],
        "accounts": {
            "tg": {
                "ready": bool(tg_ready),
                "count": tg_count,
                "label": (tg_ready or {}).get("label") if tg_ready else None,
            },
            "max": {
                "ready": max_ready,
                "configured": max_ready,
                "mode": max_mode,
                "userbot_ready": bool(max_userbot),
                "userbot_count": max_userbot_count,
                "bot_ready": max_bot_ok,
                "label": (max_userbot or {}).get("label") if max_userbot else None,
            },
        },
        "reachable": match["reachable"],
        "skipped": match["skipped"],
        "skipped_samples": match["skipped_samples"],
        "will_send": match["reachable"]["total"],
    }
