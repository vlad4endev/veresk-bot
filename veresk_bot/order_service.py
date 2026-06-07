"""Создание заказа: Posiflora, Redis, уведомление флористу."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiogram import Bot

from client_db import save_client_and_order
from config import FLORIST_CHAT_ID
from notifications import notify_florist
from order_store import save_order
from posiflora import create_posiflora_order

logger = logging.getLogger(__name__)


async def submit_order(
    bot: Bot,
    data: dict[str, Any],
    client_tg_id: int,
    redis=None,
) -> tuple[str, bool]:
    """Создать заказ. Возвращает (order_id, posiflora_ok)."""
    order_id = "—"
    posiflora_ok = True
    details = {
        "name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "date": data.get("date", ""),
        "recipient": data.get("recipient", ""),
        "occasion": data.get("occasion", ""),
        "relation": data.get("relation", ""),
        "budget": data.get("budget", ""),
    }
    try:
        order_id = await create_posiflora_order(
            customer_name=details["name"],
            phone=details["phone"],
            recipient=details["recipient"],
            occasion=details["occasion"],
            relation=details["relation"],
            budget=details["budget"],
            delivery_date=details["date"],
            telegram_id=client_tg_id,
        )
        logger.info("✅ Заказ Posiflora: #%s", order_id)
    except Exception:
        logger.exception("❌ Ошибка Posiflora")
        posiflora_ok = False

    if order_id == "—":
        order_id = uuid.uuid4().hex[:10].upper()

    order_id = str(order_id)
    if redis:
        await save_order(
            redis,
            order_id,
            client_tg_id,
            status="new",
            details=details,
            in_posiflora=posiflora_ok,
        )

    await notify_florist(
        bot=bot,
        florist_chat_id=FLORIST_CHAT_ID,
        data=data,
        order_id=str(order_id),
        client_tg_id=client_tg_id,
        posiflora_ok=posiflora_ok,
    )

    try:
        await save_client_and_order(client_tg_id, data, str(order_id))
    except Exception:
        logger.exception("Не удалось сохранить заказ в БД клиентов")

    return str(order_id), posiflora_ok


async def finalize_miniapp_order(
    bot: Bot,
    data: dict[str, Any],
    client_tg_id: int,
    redis=None,
) -> tuple[str, bool]:
    """Создать заказ из Mini App и уведомить клиента в чате."""
    from webapp_buttons import tracking_keyboard

    await bot.send_message(
        client_tg_id,
        "✅ *Заявка принята!*\n\n"
        "Флорист свяжется с вами в течение *15 минут* 🌷\n\n"
        "_Спасибо, что выбираете Veresk_",
        parse_mode="Markdown",
    )

    order_id, posiflora_ok = await submit_order(bot, data, client_tg_id, redis=redis)

    if not posiflora_ok:
        await bot.send_message(
            client_tg_id,
            "⚠️ Заявка принята, но возникла задержка с CRM. "
            "Флорист свяжется с вами вручную.",
            parse_mode="Markdown",
        )

    track_kb = tracking_keyboard(order_id)
    if track_kb:
        await bot.send_message(
            client_tg_id,
            "Нажмите «Следить за заказом» — этапы и детали в приложении 💜",
            reply_markup=track_kb,
        )

    return order_id, posiflora_ok
