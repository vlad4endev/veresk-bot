"""
Хранилище активных заказов в Redis.
Ключ: order:{order_id}
Индекс: tg_order:{tg_id} → order_id
TTL: 7 дней
"""

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

ORDER_PREFIX = "order:"
TG_ORDER_PREFIX = "tg_order:"
ORDER_TTL = 60 * 60 * 24 * 7  # 7 дней в секундах


def _order_key(order_id: str) -> str:
    return f"{ORDER_PREFIX}{order_id}"


def _tg_key(tg_id: int) -> str:
    return f"{TG_ORDER_PREFIX}{tg_id}"


async def save_order(
    redis,
    order_id: str,
    tg_id: int,
    status: str = "new",
    details: dict[str, Any] | None = None,
) -> None:
    """Сохранить новый заказ при создании."""
    key = _order_key(order_id)
    data = {
        "order_id": order_id,
        "tg_id": tg_id,
        "status": status,
        "created_at": datetime.now().isoformat(),
        "details": details or {},
    }
    await redis.set(key, json.dumps(data, ensure_ascii=False), ex=ORDER_TTL)
    await redis.set(_tg_key(tg_id), order_id, ex=ORDER_TTL)
    logger.info("💾 Заказ #%s сохранён (tg_id=%s)", order_id, tg_id)


async def get_order(redis, order_id: str) -> dict | None:
    raw = await redis.get(_order_key(order_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Не удалось распарсить заказ #%s", order_id)
        return None


async def get_order_for_user(redis, order_id: str, tg_id: int) -> dict | None:
    order = await get_order(redis, order_id)
    if not order or order.get("tg_id") != tg_id:
        return None
    return order


async def get_active_order_by_tg(redis, tg_id: int) -> dict | None:
    """Последний активный заказ пользователя."""
    order_id = await redis.get(_tg_key(tg_id))
    if not order_id:
        return None
    if isinstance(order_id, bytes):
        order_id = order_id.decode()
    return await get_order(redis, order_id)


async def get_all_orders(redis) -> list[dict]:
    """Получить все активные заказы."""
    keys = await redis.keys(f"{ORDER_PREFIX}*")
    orders = []
    for key in keys:
        raw = await redis.get(key)
        if raw:
            try:
                orders.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning("Не удалось распарсить заказ %s", key)
    return orders


async def update_order_status(redis, order_id: str, new_status: str) -> None:
    """Обновить статус заказа."""
    key = _order_key(order_id)
    raw = await redis.get(key)
    if not raw:
        return
    data = json.loads(raw)
    data["status"] = new_status
    data["updated_at"] = datetime.now().isoformat()
    await redis.set(key, json.dumps(data, ensure_ascii=False), ex=ORDER_TTL)


async def delete_order(redis, order_id: str) -> None:
    """Удалить заказ из хранилища."""
    order = await get_order(redis, order_id)
    if order:
        await redis.delete(_tg_key(order["tg_id"]))
    await redis.delete(_order_key(order_id))
    logger.info("🗑 Заказ #%s удалён из Redis", order_id)
