"""Создание заказа: Posiflora, Redis, уведомление флористу."""

from __future__ import annotations

import logging
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
    try:
        order_id = await create_posiflora_order(
            customer_name=data.get("name", ""),
            phone=data.get("phone", ""),
            recipient=data.get("recipient", ""),
            occasion=data.get("occasion", ""),
            relation=data.get("relation", ""),
            budget=data.get("budget", ""),
            delivery_date=data.get("date", ""),
            telegram_id=client_tg_id,
        )
        logger.info("✅ Заказ Posiflora: #%s", order_id)

        if redis:
            await save_order(
                redis,
                order_id,
                client_tg_id,
                status="new",
                details={
                    "name": data.get("name", ""),
                    "phone": data.get("phone", ""),
                    "date": data.get("date", ""),
                    "recipient": data.get("recipient", ""),
                    "occasion": data.get("occasion", ""),
                    "relation": data.get("relation", ""),
                    "budget": data.get("budget", ""),
                },
            )
    except Exception:
        logger.exception("❌ Ошибка Posiflora")
        posiflora_ok = False

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
