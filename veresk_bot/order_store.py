"""
Хранилище активных заказов в Redis.
Ключ: order:{order_id}
Значение: JSON { order_id, tg_id, status, created_at }
TTL: 7 дней
"""

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

ORDER_PREFIX = "order:"
ORDER_TTL = 60 * 60 * 24 * 7  # 7 дней в секундах


async def save_order(redis, order_id: str, tg_id: int, status: str = "new") -> None:
    """Сохранить новый заказ при создании."""
    key = f"{ORDER_PREFIX}{order_id}"
    data = {
        "order_id": order_id,
        "tg_id": tg_id,
        "status": status,
        "created_at": datetime.now().isoformat(),
    }
    await redis.set(key, json.dumps(data), ex=ORDER_TTL)
    logger.info("💾 Заказ #%s сохранён (tg_id=%s)", order_id, tg_id)


async def get_all_orders(redis) -> list[dict]:
    """Получить все активные заказы."""
    keys = await redis.keys(f"{ORDER_PREFIX}*")
    orders = []
    for key in keys:
        raw = await redis.get(key)
        if raw:
            try:
                orders.append(json.loads(raw))
            except Exception:
                logger.warning("Не удалось распарсить заказ %s", key)
    return orders


async def update_order_status(redis, order_id: str, new_status: str) -> None:
    """Обновить статус заказа."""
    key = f"{ORDER_PREFIX}{order_id}"
    raw = await redis.get(key)
    if not raw:
        return
    data = json.loads(raw)
    data["status"] = new_status
    await redis.set(key, json.dumps(data), ex=ORDER_TTL)


async def delete_order(redis, order_id: str) -> None:
    """Удалить завершённый заказ из хранилища."""
    await redis.delete(f"{ORDER_PREFIX}{order_id}")
    logger.info("🗑 Заказ #%s удалён из Redis", order_id)
