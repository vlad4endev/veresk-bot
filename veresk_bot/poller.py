"""
Polling статусов заказов Posiflora.
Каждые POLL_INTERVAL секунд проверяет активные заказы.
При изменении статуса — уведомляет клиента в Telegram.
"""

import asyncio
import logging

from aiogram import Bot

from client_db import update_order_status_db
from config import POLL_INTERVAL
from order_status import normalize_status
from order_store import get_all_orders, is_local_order_id, update_order_status
from posiflora import PosifloraAuthError, PosifloraAPIError, get_order_status
from webapp_buttons import tracking_keyboard

logger = logging.getLogger(__name__)

# Финальные статусы — заказ остаётся в Redis до TTL (Mini App)
FINAL_STATUSES = {"delivered", "cancelled", "returned"}

_auth_error_logged = False

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

    global _auth_error_logged

    for order in orders:
        order_id = order["order_id"]
        tg_id = order["tg_id"]
        last_status = normalize_status(order["status"])

        if order.get("in_posiflora") is False or is_local_order_id(order_id):
            logger.debug(
                "Пропуск polling #%s — заказ не в Posiflora (локальный ID)",
                order_id,
            )
            continue

        try:
            current_status = normalize_status(await get_order_status(order_id))
        except PosifloraAuthError:
            if not _auth_error_logged:
                logger.error(
                    "❌ Posiflora: неверная авторизация — проверьте .env "
                    "(POSIFLORA_BASE_URL, POSIFLORA_USERNAME, POSIFLORA_PASSWORD). "
                    "Polling статусов приостановлен до исправления."
                )
                _auth_error_logged = True
            return
        except PosifloraAPIError as exc:
            logger.warning("Posiflora #%s: %s", order_id, exc)
            continue
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
                    reply_markup=tracking_keyboard(order_id),
                )
                logger.info("✅ Клиент %s уведомлён: %s", tg_id, current_status)
            except Exception:
                logger.exception("❌ Не удалось уведомить клиента %s", tg_id)

        await update_order_status(redis, order_id, current_status)
        try:
            await update_order_status_db(order_id, current_status)
        except Exception:
            logger.debug("Не обновлён статус в SQLite для #%s", order_id)


async def start_polling(bot: Bot, redis) -> None:
    """Запустить бесконечный цикл polling."""
    logger.info("🔄 Polling запущен (интервал: %s сек)", POLL_INTERVAL)
    while True:
        try:
            await poll_once(bot, redis)
        except Exception:
            logger.exception("❌ Ошибка в polling-цикле")
        await asyncio.sleep(POLL_INTERVAL)
