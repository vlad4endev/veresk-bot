"""Фоновый диспетчер рассылок: очередь, расписание, авто-поздравления."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from config import MAILING_BATCH_SIZE, MAILING_SEND_INTERVAL
from mailing_db import (
    activate_due_campaigns,
    bump_account_sent,
    create_personal_message,
    fetch_pending_personal,
    fetch_pending_recipients,
    list_auto_events_for_today,
    mark_personal_status,
    mark_recipient_status,
    pick_ready_account,
)
from senders.max_bot import MaxBotSender
from senders.telegram_userbot import TelegramUserbotSender

logger = logging.getLogger(__name__)

_sender_cache: dict[int, TelegramUserbotSender] = {}
_auto_done_day: str | None = None


def _personalize(text: str, name: str) -> str:
    first = (name or "").split()[0] if name else ""
    return (
        text.replace("{имя}", first or "друг")
        .replace("{Имя}", first or "Друг")
        .replace("{name}", first or "друг")
    )


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


async def _send_via_channel(
    channel: str,
    *,
    phone: str,
    name: str,
    text: str,
) -> tuple[bool, str, str | None]:
    """Возвращает (ok, status, error)."""
    body = _personalize(text, name)
    if channel in ("tg", "telegram"):
        account = await pick_ready_account("tg_userbot")
        if not account:
            return False, "failed", "Нет готовых Telegram-аккаунтов"
        sender = await _get_tg_sender(account)
        if not sender:
            return False, "failed", "Нет session_file у аккаунта"
        result = await sender.send(phone=phone, name=name, text=body)
        if result.ok:
            await bump_account_sent(int(account["id"]))
        return result.ok, result.status, result.error

    if channel in ("max", "mx"):
        sender = MaxBotSender()
        result = await sender.send(phone=phone, name=name, text=body)
        return result.ok, result.status, result.error

    return False, "failed", f"Неизвестный канал: {channel}"


async def process_campaign_batch() -> int:
    await activate_due_campaigns()
    pending = await fetch_pending_recipients(limit=MAILING_BATCH_SIZE)
    processed = 0
    for row in pending:
        phone = row.get("customer_phone") or ""
        name = row.get("customer_name") or ""
        text = row.get("campaign_message") or ""
        channel = row.get("channel") or "tg"
        if not phone:
            await mark_recipient_status(
                int(row["id"]), "failed", error="Нет телефона"
            )
            processed += 1
            continue
        ok, status, error = await _send_via_channel(
            channel, phone=phone, name=name, text=text
        )
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
        if not phone:
            await mark_personal_status(int(row["id"]), "failed", error="Нет телефона")
            processed += 1
            continue
        ok, status, error = await _send_via_channel(
            channel, phone=phone, name=name, text=text
        )
        await mark_personal_status(
            int(row["id"]),
            "sent" if ok else "failed",
            error=None if ok else error,
        )
        processed += 1
        await asyncio.sleep(MAILING_SEND_INTERVAL)
    return processed


async def process_auto_greetings() -> int:
    """Раз в день создаёт personal_messages для событий с auto_send."""
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
        if ev.get("max_user_id") and not ev.get("tg_user_id"):
            channel = "max"
        await create_personal_message(int(ev["cust_id"]), text, channel=channel)
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
