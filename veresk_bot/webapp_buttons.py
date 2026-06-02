"""Кнопка открытия Mini App для отслеживания заказа."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import MINIAPP_URL


def tracking_keyboard(order_id: str) -> InlineKeyboardMarkup | None:
    if not MINIAPP_URL or order_id in ("—", "", None):
        return None
    url = f"{MINIAPP_URL}?order_id={order_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💜 Следить за заказом",
                    web_app=WebAppInfo(url=url),
                )
            ]
        ]
    )
