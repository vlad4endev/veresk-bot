"""Inline-кнопки открытия Telegram Mini App (трекер статуса)."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    WebAppInfo,
)

from config import MINIAPP_URL

logger = logging.getLogger(__name__)

TRACKER_OPEN_LABEL = "📋 Статус заказа"
TRACKER_FOLLOW_LABEL = "📋 Следить за заказом"


def miniapp_url(order_id: str | None = None) -> str | None:
    if not MINIAPP_URL:
        return None
    if order_id and order_id not in ("—", "", None):
        return f"{MINIAPP_URL}?order_id={order_id}"
    return MINIAPP_URL


def status_keyboard(order_id: str | None = None) -> InlineKeyboardMarkup | None:
    """Inline Web App: главная со статусом или экран конкретного заказа."""
    url = miniapp_url(order_id)
    if not url:
        return None
    label = TRACKER_FOLLOW_LABEL if order_id else TRACKER_OPEN_LABEL
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))]
        ]
    )


def tracking_keyboard(order_id: str) -> InlineKeyboardMarkup | None:
    if order_id in ("—", "", None):
        return None
    return status_keyboard(order_id)


def launch_keyboard(order_id: str | None = None) -> InlineKeyboardMarkup | None:
    return status_keyboard(order_id)


def orders_list_keyboard(orders: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    """Inline-кнопки трекера для каждого заказа в /orders."""
    rows: list[list[InlineKeyboardButton]] = []
    for o in orders[:8]:
        oid = str(o.get("posiflora_order_id", "")).strip()
        if not oid or oid == "—":
            continue
        url = miniapp_url(oid)
        if not url:
            break
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📋 №{oid}",
                    web_app=WebAppInfo(url=url),
                )
            ]
        )
    if not rows:
        return launch_keyboard()
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def reset_bot_menu_button(bot: Bot) -> None:
    """Убрать Web App из кнопки меню — открытие только через inline."""
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonDefault(),
            request_timeout=90,
        )
        logger.info("Кнопка меню сброшена (Mini App только inline)")
    except (TelegramAPIError, TelegramNetworkError) as exc:
        logger.warning("Не удалось сбросить кнопку меню: %s", exc)
