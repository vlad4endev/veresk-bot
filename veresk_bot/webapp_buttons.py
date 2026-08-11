"""Inline-кнопки открытия Telegram Mini App (трекер статуса / колесо)."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

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
WHEEL_OPEN_LABEL = "🎡 Крутить колесо фортуны"


def miniapp_url(order_id: str | None = None) -> str | None:
    if not MINIAPP_URL:
        return None
    if order_id and order_id not in ("—", "", None):
        return f"{MINIAPP_URL}?order_id={order_id}"
    return MINIAPP_URL


def wheel_miniapp_url() -> str | None:
    """Deep link Mini App → экран колеса фортуны."""
    if not MINIAPP_URL:
        return None
    parts = urlsplit(MINIAPP_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["wheel"] = "1"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "wheel")
    )


_max_bot_username_cache: str | None = None


def _normalize_max_bot_username(value: str) -> str:
    """Убрать @ / и пробелы по краям."""
    return str(value or "").strip().lstrip("@/").strip()


def _is_valid_max_bot_username(value: str) -> bool:
    """Ник бота MAX: латиница/цифры/_, без пробелов и кириллицы (это title, не username)."""
    uname = _normalize_max_bot_username(value)
    if not uname or " " in uname or len(uname) > 64:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", uname))


def max_wheel_deeplink(username: str, *, start_param: str = "wheel") -> str:
    """Диплинк Mini App внутри MAX: https://max.ru/{bot}?startapp=wheel."""
    uname = _normalize_max_bot_username(username)
    param = str(start_param or "wheel").strip()
    if not _is_valid_max_bot_username(uname):
        return ""
    if param:
        return f"https://max.ru/{uname}?startapp={param}"
    return f"https://max.ru/{uname}?startapp"


async def resolve_max_bot_username() -> str | None:
    """Username MAX-бота для open_app / диплинка (кэш на процесс)."""
    global _max_bot_username_cache
    if _max_bot_username_cache:
        return _max_bot_username_cache

    def _accept(raw: str, *, source: str) -> str | None:
        uname = _normalize_max_bot_username(raw)
        if not uname:
            return None
        if not _is_valid_max_bot_username(uname):
            logger.warning(
                "MAX wheel: «%s» из %s — это не username (нужен ник латиницей, "
                "как в ссылке max.ru/Nick). Сейчас похоже на название бота.",
                uname[:80],
                source,
            )
            return None
        return uname

    # 1) .env
    try:
        from config import MAX_BOT_USERNAME

        accepted = _accept(MAX_BOT_USERNAME, source=".env MAX_BOT_USERNAME")
        if accepted:
            _max_bot_username_cache = accepted
            return accepted
    except Exception:
        pass

    # 2) runtime_settings (админка)
    try:
        import runtime_settings

        stored = str(runtime_settings.get("max_bot_username") or "").strip()
        accepted = _accept(stored, source="runtime_settings")
        if accepted:
            _max_bot_username_cache = accepted
            return accepted
    except Exception:
        pass

    # 3) GET /me
    try:
        from max_bot.api import MaxBotAPI
        from senders.max_bot import get_max_bot_token

        token = get_max_bot_token()
        if not token:
            logger.warning("MAX wheel: нет токена — не удалось узнать username бота")
            return None
        api = MaxBotAPI(token)
        try:
            me = await api.get_me()
        finally:
            await api.close()
        candidates = [me]
        if isinstance(me.get("user"), dict):
            candidates.append(me["user"])
        uname = ""
        for obj in candidates:
            for key in ("username", "user_name", "nick", "nickname"):
                accepted = _accept(
                    str((obj or {}).get(key) or ""),
                    source=f"get_me.{key}",
                )
                if accepted:
                    uname = accepted
                    break
            if uname:
                break
        if uname:
            _max_bot_username_cache = uname
            try:
                import runtime_settings

                runtime_settings.set_many({"max_bot_username": uname})
            except Exception:
                pass
            logger.info("MAX wheel: username бота = @%s", uname)
            return uname
        display = ""
        for obj in candidates:
            display = str((obj or {}).get("name") or (obj or {}).get("first_name") or "")
            if display:
                break
        logger.warning(
            "MAX wheel: get_me без валидного username (name=%r, keys=%s). "
            "Укажите MAX_BOT_USERNAME=ник из ссылки max.ru/Nick",
            display[:80],
            list((me or {}).keys()),
        )
    except Exception:
        logger.exception("MAX wheel: не удалось получить username бота")
    return None


async def max_wheel_keyboard() -> list[list[dict[str, Any]]] | None:
    """
    Кнопка колеса для MAX.

    Важно: НЕ давать прямую HTTPS-ссылку на miniapp и не использовать type=link —
    MAX откроет обычный браузер без initData (401 и баннер «откройте из MAX»).

    Правильно: type=open_app, web_app=<username бота>, payload=wheel
    (Mini App URL должен быть задан в кабинете партнёра MAX у этого бота).
    """
    from max_bot.api import btn_open_app

    username = await resolve_max_bot_username()
    if not username:
        return None

    logger.info("MAX wheel button: open_app web_app=%s payload=wheel", username)
    return [[btn_open_app(WHEEL_OPEN_LABEL, username, payload="wheel")]]


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


def wheel_keyboard() -> InlineKeyboardMarkup | None:
    """Inline Web App: открыть колесо фортуны после анкеты."""
    url = wheel_miniapp_url()
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=WHEEL_OPEN_LABEL, web_app=WebAppInfo(url=url))]
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
