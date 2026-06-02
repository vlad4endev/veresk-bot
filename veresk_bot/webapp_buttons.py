"""Кнопки открытия Mini App (трекер статуса заказа)."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from config import MINIAPP_URL

logger = logging.getLogger(__name__)

TRACKER_OPEN_LABEL = "📋 Статус заказа"
TRACKER_FOLLOW_LABEL = "📋 Следить за заказом"
MENU_BUTTON_TEXT = "📋 Статус заказа"


def miniapp_url(order_id: str | None = None) -> str | None:
    if not MINIAPP_URL:
        return None
    if order_id and order_id not in ("—", "", None):
        return f"{MINIAPP_URL}?order_id={order_id}"
    return MINIAPP_URL


def status_keyboard(order_id: str | None = None) -> InlineKeyboardMarkup | None:
    """Кнопка Web App: главная со статусом или экран конкретного заказа."""
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


def tracker_reply_keyboard(order_id: str | None = None) -> ReplyKeyboardMarkup | None:
    """
    Кнопка внизу чата (Web App) — работает у любого клиента,
    не зависит от FLORIST_CHAT_ID.
    """
    url = miniapp_url(order_id)
    if not url:
        return None
    label = TRACKER_FOLLOW_LABEL if order_id else TRACKER_OPEN_LABEL
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=label, web_app=WebAppInfo(url=url))],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def orders_list_keyboard(orders: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    """Кнопки трекера для каждого заказа в /orders."""
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


async def setup_bot_menu_button(bot: Bot) -> None:
    """Кнопка меню слева внизу — трекер для всех пользователей."""
    url = miniapp_url()
    if not url:
        logger.warning("MINIAPP_URL не задан — кнопка меню Mini App не установлена")
        return
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text=MENU_BUTTON_TEXT,
                web_app=WebAppInfo(url=url),
            ),
            request_timeout=90,
        )
        logger.info("Кнопка меню трекера заказа установлена")
    except TelegramAPIError as exc:
        logger.error(
            "Кнопка меню Mini App не установлена: %s. "
            "Проверьте MINIAPP_URL (HTTPS), домен в @BotFather → Domain, "
            "и что URL совпадает с публичным адресом miniapp.",
            exc,
        )
    except TelegramNetworkError as exc:
        logger.warning("Не удалось установить кнопку меню Mini App: %s", exc)
