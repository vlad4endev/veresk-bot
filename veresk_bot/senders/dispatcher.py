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
    auto_mail_send_time_reached,
    bump_account_sent,
    create_personal_message,
    fetch_pending_personal,
    fetch_pending_recipients,
    get_active_discount_text,
    get_auto_mail_settings,
    get_promotion,
    list_auto_events_for_today,
    mark_event_auto_sent,
    mark_personal_status,
    mark_recipient_status,
    pick_auto_mail_promo,
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
    media_path: str | None = None,
    media_filename: str | None = None,
    media_mime: str | None = None,
    segment: str | None = None,
) -> tuple[bool, str, str | None]:
    """Возвращает (ok, status, error). status: sent | failed | deferred."""
    try:
        discount = await get_active_discount_text(segment=segment)
    except Exception:
        discount = MAILING_DISCOUNT_TEXT or "15%"
    body = _personalize(text, name, discount=discount)
    ch = normalize_channel(channel) or channel
    media = (media_path or "").strip() or None

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
            media_path=media,
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
                    media_path=media,
                    media_filename=media_filename,
                    media_mime=media_mime,
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
            media_path=media,
            media_filename=media_filename,
            media_mime=media_mime,
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

        media_stored = row.get("campaign_media_path")
        media_abs = None
        if media_stored:
            from campaign_media import resolve_campaign_media

            resolved = resolve_campaign_media(media_stored)
            media_abs = str(resolved) if resolved else None

        ok, status, error = await _send_via_channel(
            channel,
            phone=phone,
            name=name,
            text=text,
            tg_user_id=tg_uid,
            max_user_id=max_uid,
            media_path=media_abs,
            media_filename=row.get("campaign_media_filename"),
            media_mime=row.get("campaign_media_mime"),
            segment=row.get("campaign_segment"),
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
            segment=row.get("customer_segment"),
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


def prefer_auto_channel_when_both() -> str:
    """Канал автопоздравления, когда у клиента доступны и Telegram, и MAX."""
    try:
        prefer = str(get_auto_mail_settings().get("prefer_channel") or "tg").lower()
    except Exception:
        prefer = "tg"
    return "max" if prefer == "max" else "tg"


def _normalize_event_kind(kind: Any) -> str:
    raw = str(kind or "other").strip().lower()
    if raw in ("bday", "birthday"):
        return "bday"
    if raw in ("anniv", "anniversary"):
        return "anniv"
    return "other"


async def _resolve_auto_greeting_text(
    *,
    kind: str,
    name: str,
    kind_cfg: dict[str, Any],
) -> str:
    """Собрать текст автопоздравления по настройкам kind (promo / custom)."""
    try:
        discount = await get_active_discount_text()
    except Exception:
        discount = MAILING_DISCOUNT_TEXT or "15%"

    fallback = (
        str(kind_cfg.get("text") or "").strip()
        or str(kind_cfg.get("default_text") or "").strip()
    )
    source = str(kind_cfg.get("text_source") or "custom").strip().lower()
    promo = None

    if source == "promo":
        promo_id = kind_cfg.get("promo_id")
        if promo_id:
            try:
                promo = await get_promotion(int(promo_id))
            except Exception:
                promo = None
                logger.debug(
                    "Авто-поздравление: не удалось загрузить акцию id=%s",
                    promo_id,
                    exc_info=True,
                )
            if promo and not promo.get("is_live"):
                promo = None
        if not promo:
            try:
                promo = await pick_auto_mail_promo(kind)
            except Exception:
                logger.debug(
                    "Авто-поздравление: не удалось взять акцию для kind=%s",
                    kind,
                    exc_info=True,
                )

    if promo and (promo.get("discount_display") or promo.get("discount_text")):
        discount = (
            promo.get("discount_display")
            or promo.get("discount_text")
            or discount
        )

    template = ""
    if source == "promo" and promo:
        template = str(promo.get("message_template") or "").strip()
    if not template:
        template = fallback
    if not template:
        # Последний страховочный дефолт (если настройки пустые)
        first = name.split()[0] if name else "друг"
        if kind == "bday":
            template = (
                f"С днём рождения, {first}! 🎂💐\n\n"
                f"Дарим вам скидку {discount} на любой букет всю неделю. Ваш Veresk."
            )
            return template
        if kind == "anniv":
            return (
                f"{first}, поздравляем с годовщиной! 💍\n\n"
                f"Отметьте этот день красивым букетом — дарим −{str(discount).lstrip('−-')}. "
                "Ваш Veresk."
            )
        return f"Здравствуйте, {first}! 🌷\n\nВаш Veresk напоминает о важной дате."

    return _personalize(template, name, discount=discount)


async def _auto_greeting_channel(ev: dict[str, Any]) -> str | None:
    """Выбрать канал для автопоздравления по реальным идентификаторам клиента."""
    has_tg = bool(ev.get("tg_user_id") or ev.get("customer_phone"))
    resolved_max = await asyncio.to_thread(
        resolve_max_user_id_sync,
        max_user_id=ev.get("max_user_id"),
        phone=ev.get("customer_phone"),
    )
    has_max = resolved_max is not None
    if not has_max and ev.get("customer_phone"):
        # MAX userbot может найти по телефону без заранее известного max_user_id
        if await pick_ready_account("max_userbot"):
            has_max = True
    if has_tg and has_max:
        ch = prefer_auto_channel_when_both()
        return "max" if ch == "max" else "tg"
    if has_tg:
        return "tg"
    if has_max:
        return "max"
    return None


async def process_auto_greetings() -> int:
    """Раз в день создаёт personal_messages для событий с auto_send.

    Учитывает Настройки → Автопоздравления: enabled, send_time, kinds.
    Дедуп: last_auto_sent_on в customer_events + in-memory _auto_done_day.
    """
    global _auto_done_day
    now = datetime.now()
    today = now.date().isoformat()
    if _auto_done_day == today:
        return 0

    try:
        settings = get_auto_mail_settings()
    except Exception:
        logger.debug("Авто-поздравления: не удалось прочитать настройки", exc_info=True)
        settings = {
            "enabled": True,
            "send_time": "10:00",
            "prefer_channel": "tg",
            "kinds": {},
        }

    if not settings.get("enabled", True):
        return 0
    if not auto_mail_send_time_reached(settings, now=now):
        return 0

    events = await list_auto_events_for_today()
    kinds_cfg = settings.get("kinds") if isinstance(settings.get("kinds"), dict) else {}
    created = 0
    for ev in events:
        kind = _normalize_event_kind(ev.get("kind"))
        kind_cfg = kinds_cfg.get(kind) if isinstance(kinds_cfg.get(kind), dict) else {}
        if kind_cfg and kind_cfg.get("enabled") is False:
            continue
        if not kind_cfg:
            kind_cfg = {
                "enabled": True,
                "text_source": "promo" if kind in ("bday", "anniv") else "custom",
                "promo_id": None,
                "text": "",
            }

        name = ev.get("customer_name") or ""
        text = await _resolve_auto_greeting_text(
            kind=kind, name=name, kind_cfg=kind_cfg
        )
        channel = await _auto_greeting_channel(ev)
        if not channel:
            logger.info(
                "Авто-поздравление пропущено (нет канала): cust=%s event=%s",
                ev.get("cust_id"),
                ev.get("id"),
            )
            continue
        await create_personal_message(int(ev["cust_id"]), text, channel=channel)
        event_id = _parse_int(ev.get("id"))
        if event_id is not None:
            await mark_event_auto_sent(event_id, today)
        created += 1

    # Помечаем день только после наступления send_time и прохода очереди
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
            try:
                from channel_subscriptions import process_due_channel_welcomes

                n_welcome = await process_due_channel_welcomes()
            except Exception:
                n_welcome = 0
                logger.debug("Welcome-диспетчер: ошибка", exc_info=True)
            n1 = await process_campaign_batch()
            n2 = await process_personal_batch()
            if n1 or n2 or n_welcome:
                logger.info(
                    "Диспетчер: кампании=%s, личные=%s, welcome=%s",
                    n1,
                    n2,
                    n_welcome,
                )
        except Exception:
            logger.exception("Ошибка в диспетчере рассылок")
        await asyncio.sleep(max(2.0, MAILING_SEND_INTERVAL))


def start_mailing_dispatcher() -> asyncio.Task:
    return asyncio.create_task(_dispatcher_loop())
