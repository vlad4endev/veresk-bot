"""
Polling статусов заказов Posiflora.
Каждые POLL_INTERVAL секунд проверяет активные заказы.
При изменении статуса — уведомляет клиента в Telegram.
"""

import asyncio
import logging

from aiogram import Bot

from config import POLL_INTERVAL
from order_store import delete_order, get_all_orders, update_order_status
from posiflora import get_order_status

logger = logging.getLogger(__name__)

# ── Финальные статусы — после них заказ удаляется из Redis ───────────────────
FINAL_STATUSES = {"delivered", "cancelled", "returned"}

# ── Тексты уведомлений клиенту по статусу ────────────────────────────────────
# ВАЖНО: уточнить точные значения статусов у Posiflora (support@posiflora.com)
# Спросить: «Какие значения принимает поле status/deliveryStatus в заказах?»
STATUS_MESSAGES: dict[str, str] = {
    "confirmed": (
        "🌸 *Ваш заказ подтверждён!*\n\n"
        "Флорист приступает к работе.\n\n"
        "_Veresk · trail of happiness_"
    ),
    "in_progress": (
        "💐 *Ваш букет уже собирается!*\n\n"
        "Скоро будет готов.\n\n"
        "_Veresk · trail of happiness_"
    ),
    "delivering": (
        "🚗 *Букет передан курьеру!*\n\n"
        "Уже в пути к получателю.\n\n"
        "_Veresk · trail of happiness_"
    ),
    "delivered": (
        "🎉 *Букет доставлен!*\n\n"
        "Надеемся подарить незабываемые эмоции 💜\n\n"
        "_Спасибо, что выбираете Veresk · trail of happiness_"
    ),
    "cancelled": (
        "😔 *Заказ отменён*\n\n"
        "Если это ошибка — свяжитесь с нами:\n"
        "📞 *+7 994 004 99 83*\n"
        "💬 @verba\\_flowers\n\n"
        "_Veresk · trail of happiness_"
    ),
}


async def poll_once(bot: Bot, redis) -> None:
    """Один цикл проверки всех активных заказов."""
    orders = await get_all_orders(redis)
    if not orders:
        return

    logger.debug("🔄 Polling %s заказов...", len(orders))

    for order in orders:
        order_id = order["order_id"]
        tg_id = order["tg_id"]
        last_status = order["status"]

        try:
            current_status = await get_order_status(order_id)
        except Exception:
            logger.exception("❌ Не удалось получить статус заказа #%s", order_id)
            continue

        if current_status == last_status:
            continue

        logger.info("📊 Заказ #%s: %s → %s", order_id, last_status, current_status)

        msg = STATUS_MESSAGES.get(current_status)
        if msg:
            try:
                await bot.send_message(
                    chat_id=tg_id,
                    text=msg,
                    parse_mode="Markdown",
                )
                logger.info("✅ Клиент %s уведомлён: %s", tg_id, current_status)
            except Exception:
                logger.exception("❌ Не удалось уведомить клиента %s", tg_id)

        await update_order_status(redis, order_id, current_status)

        if current_status in FINAL_STATUSES:
            await delete_order(redis, order_id)


async def start_polling(bot: Bot, redis) -> None:
    """Запустить бесконечный цикл polling."""
    logger.info("🔄 Polling запущен (интервал: %s сек)", POLL_INTERVAL)
    while True:
        try:
            await poll_once(bot, redis)
        except Exception:
            logger.exception("❌ Ошибка в polling-цикле")
        await asyncio.sleep(POLL_INTERVAL)
