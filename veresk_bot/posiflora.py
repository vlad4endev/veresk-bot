import logging
from datetime import datetime, timedelta

import aiohttp

from config import (
    POSIFLORA_BASE_URL,
    POSIFLORA_PASSWORD,
    POSIFLORA_STORE_ID,
    POSIFLORA_USERNAME,
)

logger = logging.getLogger(__name__)

JSON_API_HEADERS = {
    "Content-Type": "application/vnd.api+json",
    "Accept": "application/vnd.api+json",
}

BUDGET_MAP = {
    "до 5 000 ₽": 4999,
    "до 10 000 ₽": 9999,
    "до 15 000 ₽": 14999,
    "от 15 000 ₽": 15000,
}

def _build_comment(
    customer_name: str,
    phone: str,
    recipient: str,
    occasion: str,
    relation: str,
    budget: str,
    telegram_id: int,
) -> str:
    return (
        "📱 Заказ через Telegram-бот\n"
        f"Клиент: {customer_name} (tg_id: {telegram_id})\n"
        f"Телефон: {phone}\n"
        f"Получатель: {recipient}\n"
        f"Кто: {relation}\n"
        f"Повод: {occasion}\n"
        f"Бюджет: {budget}"
    )


async def _get_access_token(session: aiohttp.ClientSession) -> str:
    async with session.post(
        f"{POSIFLORA_BASE_URL}/v1/sessions",
        headers=JSON_API_HEADERS,
        json={
            "data": {
                "type": "sessions",
                "attributes": {
                    "username": POSIFLORA_USERNAME,
                    "password": POSIFLORA_PASSWORD,
                },
            }
        },
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["data"]["attributes"]["accessToken"]


async def create_posiflora_order(
    customer_name: str,
    phone: str,
    recipient: str,
    occasion: str,
    relation: str,
    budget: str,
    delivery_date: str,
    telegram_id: int,
) -> str:
    today = datetime.now()
    date_map = {
        "Сегодня": today,
        "Завтра": today + timedelta(days=1),
        "Через 2–3 дня": today + timedelta(days=2),
        "Через неделю": today + timedelta(days=7),
        "Через 2 недели": today + timedelta(days=14),
    }
    delivery_dt = date_map.get(delivery_date, today + timedelta(days=1))
    delivery_at = delivery_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    amount = BUDGET_MAP.get(budget, 4999)
    comment = _build_comment(
        customer_name, phone, recipient, occasion, relation, budget, telegram_id
    )

    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)

        async with session.post(
            f"{POSIFLORA_BASE_URL}/v1/orders",
            headers={
                **JSON_API_HEADERS,
                "Authorization": f"Bearer {access_token}",
            },
            json={
                "data": {
                    "type": "orders",
                    "attributes": {
                        "amount": amount,
                        "comment": comment,
                        "deliveryAt": delivery_at,
                        "storeId": POSIFLORA_STORE_ID,
                        "source": "telegram",
                    },
                    "relationships": {
                        "client": {
                            "data": {
                                "type": "customers",
                                "attributes": {
                                    "name": customer_name,
                                    "phone": phone,
                                },
                            }
                        }
                    },
                }
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return str(data["data"]["id"])


async def get_order_status(order_id: str) -> str:
    """
    GET /v1/orders/{id}
    Возвращает текущий статус заказа.
    """
    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)
        async with session.get(
            f"{POSIFLORA_BASE_URL}/v1/orders/{order_id}",
            headers={
                **JSON_API_HEADERS,
                "Authorization": f"Bearer {access_token}",
            },
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            attrs = data["data"]["attributes"]
            return attrs.get("status") or attrs.get("deliveryStatus", "unknown")
