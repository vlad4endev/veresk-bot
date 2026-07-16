"""
Синхронизация клиентов и событий из Posiflora → локальная SQLite (mailing_db).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from config import POSIFLORA_SYNC_INTERVAL
from mailing_db import (
    phone_to_tg_map,
    upsert_customer,
    upsert_customer_event,
    get_customer_by_posiflora_id,
)
from posiflora import PosifloraAPIError, fetch_customers_and_events

logger = logging.getLogger(__name__)

_sync_lock: asyncio.Lock | None = None
_last_sync: dict[str, Any] = {
    "at": None,
    "customers": 0,
    "events": 0,
    "error": None,
}


def _get_sync_lock() -> asyncio.Lock:
    global _sync_lock
    if _sync_lock is None:
        _sync_lock = asyncio.Lock()
    return _sync_lock


def _normalize_phone(phone: str, country_code: Any = None) -> str:
    digits = re.sub(r"\D", "", str(phone or "").strip())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]
    if len(digits) == 10:
        return digits
    if country_code and len(digits) < 10:
        return digits
    return digits


def _compute_segment(
    *,
    created_at: str | None,
    last_order_at: str | None,
    notes: str,
) -> str:
    """
    Сегменты для UI:
    - new — клиент создан < 30 дней назад
    - inactive — давно не заказывал (> 60 дней)
    - regular — постоянный
    - all — запасной
    """
    now = datetime.now()
    created = None
    if created_at:
        try:
            created = datetime.fromisoformat(
                str(created_at).replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except ValueError:
            created = None

    last = None
    if last_order_at:
        try:
            last = datetime.fromisoformat(
                str(last_order_at).replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except ValueError:
            last = None

    if created and (now - created) < timedelta(days=30):
        return "new"
    if last:
        if (now - last) > timedelta(days=60):
            return "inactive"
        return "regular"
    if created and (now - created) > timedelta(days=30):
        return "inactive"
    if notes and ("заказ" in notes.lower() or "анкета" in notes.lower()):
        return "regular"
    return "all"


def last_sync_info() -> dict[str, Any]:
    return dict(_last_sync)


async def sync_from_posiflora() -> dict[str, Any]:
    """Полная синхронизация. Безопасно вызывать параллельно — есть lock."""
    lock = _get_sync_lock()
    if lock.locked():
        return {"ok": False, "error": "sync_in_progress", **last_sync_info()}

    async with lock:
        try:
            payload = await fetch_customers_and_events()
        except PosifloraAPIError as exc:
            _last_sync["error"] = str(exc)
            logger.exception("Синхронизация Posiflora не удалась")
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            _last_sync["error"] = str(exc)
            logger.exception("Синхронизация Posiflora: неожиданная ошибка")
            return {"ok": False, "error": str(exc)}

        tg_map = await phone_to_tg_map()
        customers = payload.get("customers") or []
        events = payload.get("events") or []
        pf_to_local: dict[str, int] = {}

        for c in customers:
            phone = _normalize_phone(c.get("phone", ""), c.get("country_code"))
            name = (c.get("title") or "").strip() or "Без имени"
            notes = c.get("notes") or ""
            created = c.get("created_at")
            if isinstance(created, str):
                created = created[:19]
            segment = _compute_segment(
                created_at=created,
                last_order_at=None,
                notes=notes,
            )
            tg_id = tg_map.get(phone)
            local_id = await upsert_customer(
                posiflora_id=c["id"],
                name=name,
                phone=phone,
                notes=notes,
                created_in_pf_at=created,
                tg_user_id=tg_id,
                segment=segment,
            )
            pf_to_local[c["id"]] = local_id

        events_synced = 0
        for ev in events:
            pf_customer = ev.get("customer_id")
            if not pf_customer:
                continue
            local_id = pf_to_local.get(pf_customer)
            if not local_id:
                existing = await get_customer_by_posiflora_id(pf_customer)
                if existing:
                    local_id = int(existing["id"])
                else:
                    continue
            date_from = (ev.get("date_from") or "")[:10]
            if not date_from:
                continue
            await upsert_customer_event(
                customer_id=local_id,
                posiflora_event_id=ev.get("id"),
                title=ev.get("title") or "Событие",
                date_from=date_from,
            )
            events_synced += 1

        _last_sync.update(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "customers": len(customers),
                "events": events_synced,
                "error": None,
            }
        )
        logger.info(
            "Posiflora sync: %s клиентов, %s событий",
            len(customers),
            events_synced,
        )
        return {"ok": True, **last_sync_info()}


async def _sync_loop(interval: int) -> None:
    # Первая синхронизация через 15 сек после старта (дать токену прогреться)
    await asyncio.sleep(15)
    while True:
        try:
            await sync_from_posiflora()
        except Exception:
            logger.exception("Фоновый sync Posiflora упал")
        await asyncio.sleep(max(60, interval))


def start_posiflora_sync(interval: int | None = None) -> asyncio.Task:
    sec = interval if interval is not None else POSIFLORA_SYNC_INTERVAL
    logger.info("🔄 Posiflora sync: каждые %s сек", sec)
    return asyncio.create_task(_sync_loop(sec))
