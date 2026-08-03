"""Фоновый диспетчер рассылок: очередь, расписание, авто-поздравления.

Сценарий отправки:
1. Берём pending-получателей только у кампаний со статусом sending
   (scheduled → sending делает activate_due_campaigns по scheduled_at).
2. Для Telegram — выбираем готовый userbot-аккаунт и шлём от его имени
   (по tg_user_id или телефону через ImportContacts).
3. Для MAX — сначала личный MAX-аккаунт (PyMax), иначе официальный MAX-бот.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from config import MAILING_BATCH_SIZE, MAILING_DISCOUNT_TEXT, MAILING_SEND_INTERVAL
from mailing_db import (
    activate_due_campaigns,
    bump_account_sent,
    create_personal_message,
    fetch_pending_personal,
    fetch_pending_recipients,
    list_auto_events_for_today,
    mark_event_auto_sent,
    mark_personal_status,
    mark_recipient_status,
    pick_ready_account,
)
from senders.matching import normalize_channel, resolve_max_user_id_sync
from senders.max_bot import MaxBotSender
from senders.max_userbot import MaxUserbotSender
from senders.telegram_userbot import TelegramUserbotSender

logger = logging.getLogger(__name__)

_sender_cache: dict[int, TelegramUserbotSender] = {}
_max_sender_cache: dict[int, MaxUserbotSender] = {}
_auto_done_day: str | None = None

# Ошибки «нет аккаунта / дневной лимит» — оставляем pending, не failed
_DEFER_ERRORS = (
    "Нет готовых Telegram-аккаунтов",
    "Нет готового MAX-аккаунта и MAX-бот не подключён",
    "Нет session_file у аккаунта",
)


def _personalize(text: str, name: str, *, discount: str | None = None) -> str:
    first = (name or "").split()[0] if name else ""
    disc = (discount if discount is not None else MAILING_DISCOUNT_TEXT) or "15%"
    return (
        text.replace("{имя}", first or "друг")
        .replace("{Имя}", first or "Друг")
        .replace("{name}", first or "друг")
        .replace("{скидка}", disc)
        .replace("{Скидка}", disc)
    )


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _get_tg_sender(account: dict) -> TelegramUserbotSender | None:
    aid = int(account["id"])
    if aid in _sender_cache:
        return _sender_cache[aid]
    session = account.get("session_file") or ""
    if not session:
        return None
    sender = TelegramUserbotSender(session, account_id=aid)
    _sender_cache[aid] = sender
    return sender


async def _get_max_userbot_sender(account: dict) -> MaxUserbotSender | None:
    aid = int(account["id"])
    if aid in _max_sender_cache:
        return _max_sender_cache[aid]
    session = account.get("session_file") or ""
    if not session:
        return None
    sender = MaxUserbotSender(
        session,
        account_id=aid,
        phone=str(account.get("phone") or ""),
    )
    _max_sender_cache[aid] = sender
    return sender


async def _send_via_channel(
    channel: str,
    *,
    phone: str,
    name: str,
    text: str,
    tg_user_id: int | None = None,
    max_user_id: int | None = None,
) -> tuple[bool, str, str | None]:
    """Возвращает (ok, status, error). status: sent | failed | deferred."""
    body = _personalize(text, name)
    ch = normalize_channel(channel) or channel

    if ch == "tg":
        account = await pick_ready_account("tg_userbot")
        if not account:
            return False, "deferred", "Нет готовых Telegram-аккаунтов"
        sender = await _get_tg_sender(account)
        if not sender:
            return False, "deferred", "Нет session_file у аккаунта"
        result = await sender.send(
            phone=phone,
            name=name,
            text=body,
            tg_user_id=tg_user_id,
        )
        if result.ok:
            await bump_account_sent(int(account["id"]))
            logger.info(
                "TG рассылка: %s → %s (аккаунт %s)",
                name or phone,
                "ok",
                account.get("label") or account["id"],
            )
        return result.ok, result.status, result.error

    if ch == "max":
        resolved = await asyncio.to_thread(
            resolve_max_user_id_sync,
            max_user_id=max_user_id,
            phone=phone,
        )
        # 1) Личный MAX-аккаунт (PyMax)
        account = await pick_ready_account("max_userbot")
        if account:
            sender = await _get_max_userbot_sender(account)
            if sender and sender.available:
                result = await sender.send(
                    phone=phone,
                    name=name,
                    text=body,
                    max_user_id=resolved,
                )
                if result.ok:
                    await bump_account_sent(int(account["id"]))
                    logger.info(
                        "MAX userbot рассылка: %s (аккаунт %s)",
                        name or phone,
                        account.get("label") or account["id"],
                    )
                return result.ok, result.status, result.error
        # 2) Fallback — официальный MAX-бот
        bot_sender = MaxBotSender()
        if not bot_sender.available:
            return (
                False,
                "deferred",
                "Нет готового MAX-аккаунта и MAX-бот не подключён",
            )
        result = await bot_sender.send(
            phone=phone,
            name=name,
            text=body,
            max_user_id=resolved,
        )
        return result.ok, result.status, result.error

    return False, "failed", f"Неизвестный канал: {channel}"


def _is_defer(status: str, error: str | None) -> bool:
    if status == "deferred":
        return True
    if error and (
        any(err in error for err in _DEFER_ERRORS) or error.startswith("FloodWait")
    ):
        return True
    return False


async def process_campaign_batch() -> int:
    await activate_due_campaigns()
    pending = await fetch_pending_recipients(limit=MAILING_BATCH_SIZE)
    processed = 0
    for row in pending:
        phone = row.get("customer_phone") or ""
        name = row.get("customer_name") or ""
        text = row.get("campaign_message") or ""
        channel = row.get("channel") or "tg"
        tg_uid = _parse_int(row.get("tg_user_id"))
        max_uid = _parse_int(row.get("max_user_id"))
        ch = normalize_channel(channel) or channel

        if ch == "tg" and not phone and tg_uid is None:
            await mark_recipient_status(
                int(row["id"]), "failed", error="Нет телефона и Telegram id"
            )
            processed += 1
            continue
        if ch == "max" and max_uid is None and not phone:
            await mark_recipient_status(
                int(row["id"]), "failed", error="Нет данных для MAX"
            )
            processed += 1
            continue

        ok, status, error = await _send_via_channel(
            channel,
            phone=phone,
            name=name,
            text=text,
            tg_user_id=tg_uid,
            max_user_id=max_uid,
        )
        if not ok and _is_defer(status, error):
            # Дневной лимит / нет аккаунта — не сжигаем очередь, подождём
            logger.info(
                "Рассылка отложена (получатель %s): %s",
                row.get("id"),
                error,
            )
            break
        await mark_recipient_status(
            int(row["id"]),
            "sent" if ok else "failed",
            error=None if ok else error,
        )
        processed += 1
        await asyncio.sleep(MAILING_SEND_INTERVAL)
    return processed


async def process_personal_batch() -> int:
    pending = await fetch_pending_personal(limit=MAILING_BATCH_SIZE)
    processed = 0
    for row in pending:
        phone = row.get("customer_phone") or ""
        name = row.get("customer_name") or ""
        text = row.get("message") or ""
        channel = row.get("channel") or "tg"
        tg_uid = _parse_int(row.get("tg_user_id"))
        max_uid = _parse_int(row.get("max_user_id"))
        ch = normalize_channel(channel) or channel

        if ch == "tg" and not phone and tg_uid is None:
            await mark_personal_status(
                int(row["id"]), "failed", error="Нет телефона и Telegram id"
            )
            processed += 1
            continue
        if ch == "max" and max_uid is None and not phone:
            await mark_personal_status(
                int(row["id"]), "failed", error="Нет данных для MAX"
            )
            processed += 1
            continue

        ok, status, error = await _send_via_channel(
            channel,
            phone=phone,
            name=name,
            text=text,
            tg_user_id=tg_uid,
            max_user_id=max_uid,
        )
        if not ok and _is_defer(status, error):
            logger.info(
                "Личное сообщение отложено (id=%s): %s",
                row.get("id"),
                error,
            )
            break
        await mark_personal_status(
            int(row["id"]),
            "sent" if ok else "failed",
            error=None if ok else error,
        )
        processed += 1
        await asyncio.sleep(MAILING_SEND_INTERVAL)
    return processed


async def process_auto_greetings() -> int:
    """Раз в день создаёт personal_messages для событий с auto_send.

    Дедуп: last_auto_sent_on в customer_events + in-memory _auto_done_day.
    """
    global _auto_done_day
    today = datetime.now().date().isoformat()
    if _auto_done_day == today:
        return 0
    events = await list_auto_events_for_today()
    created = 0
    for ev in events:
        kind = ev.get("kind") or "other"
        name = ev.get("customer_name") or ""
        first = name.split()[0] if name else "друг"
        if kind == "bday":
            text = (
                f"С днём рождения, {first}! 🎂💐\n\n"
                "Дарим вам скидку 15% на любой букет всю неделю. Ваш Veresk."
            )
        elif kind == "anniv":
            text = (
                f"{first}, поздравляем с годовщиной! 💍\n\n"
                "Отметьте этот день красивым букетом — дарим −15%. Ваш Veresk."
            )
        else:
            text = f"Здравствуйте, {first}! 🌷\n\nВаш Veresk напоминает о важной дате."
        channel = "tg"
        if ev.get("max_user_id") and not ev.get("tg_user_id") and not ev.get("customer_phone"):
            channel = "max"
        elif ev.get("max_user_id") and not ev.get("tg_user_id"):
            if not ev.get("customer_phone"):
                channel = "max"
        await create_personal_message(int(ev["cust_id"]), text, channel=channel)
        event_id = _parse_int(ev.get("id"))
        if event_id is not None:
            await mark_event_auto_sent(event_id, today)
        created += 1
    _auto_done_day = today
    if created:
        logger.info("Авто-поздравления: создано %s сообщений", created)
    return created


async def _dispatcher_loop() -> None:
    await asyncio.sleep(20)
    logger.info("📬 Диспетчер рассылок запущен")
    while True:
        try:
            await process_auto_greetings()
            n1 = await process_campaign_batch()
            n2 = await process_personal_batch()
            if n1 or n2:
                logger.info("Диспетчер: кампании=%s, личные=%s", n1, n2)
        except Exception:
            logger.exception("Ошибка в диспетчере рассылок")
        await asyncio.sleep(max(2.0, MAILING_SEND_INTERVAL))


def start_mailing_dispatcher() -> asyncio.Task:
    return asyncio.create_task(_dispatcher_loop())
