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


def wheel_promo_miniapp_url() -> str | None:
    """Ссылка для канала: колесо в режиме запечатанного билета."""
    if not MINIAPP_URL:
        return None
    parts = urlsplit(MINIAPP_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["wheel"] = "1"
    query["promo"] = "1"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "wheel")
    )


def telegram_bot_start_url(
    payload: str = "open_ticket", *, telegram_username: str | None = None
) -> str:
    """https://t.me/<bot>?start=<payload> — диплинк в чат с ботом."""
    nick = str(telegram_username or "").strip().lstrip("@") or _telegram_bot_username()
    if not nick:
        return ""
    arg = str(payload or "open_ticket").strip() or "open_ticket"
    return f"https://t.me/{nick}?start={arg}"


def promo_bot_links(*, telegram_username: str | None = None) -> dict[str, str]:
    """Ссылки на ботов: вход в промо-колесо и раскрытие билета после анкеты."""
    tg_ticket = telegram_bot_start_url(
        "open_ticket", telegram_username=telegram_username
    )
    tg_spin = telegram_bot_start_url(
        "wheel_promo", telegram_username=telegram_username
    )
    max_url = ""
    max_spin = ""
    try:
        from config import MAX_BOT_USERNAME

        nick = _normalize_max_bot_username(MAX_BOT_USERNAME)
        if nick:
            max_url = f"https://max.ru/{nick}"
            max_spin = max_wheel_deeplink(nick, start_param="wheel_promo") or max_url
    except Exception:
        max_url = ""
        max_spin = ""
    return {
        "telegram_url": tg_ticket,
        "telegram_spin_url": tg_spin,
        "max_url": max_url,
        "max_spin_url": max_spin,
    }


def _telegram_bot_username() -> str:
    try:
        from config import TELEGRAM_BOT_USERNAME

        return str(TELEGRAM_BOT_USERNAME or "").strip().lstrip("@")
    except Exception:
        return ""


def _max_bot_username_for_promo() -> str:
    try:
        from config import MAX_BOT_USERNAME

        return _normalize_max_bot_username(MAX_BOT_USERNAME)
    except Exception:
        return ""


def wheel_promo_share_links(
    *, telegram_username: str | None = None, max_username: str | None = None
) -> dict[str, str]:
    """Ссылки для публикации промо-колеса (админка «Фортуна»)."""
    tg_nick = str(telegram_username or "").strip().lstrip("@") or _telegram_bot_username()
    max_nick = _normalize_max_bot_username(
        max_username or _max_bot_username_for_promo()
    )
    miniapp = wheel_promo_miniapp_url() or ""
    # Надёжный вход для поста в канал: бот → кнопка Mini App (с initData).
    tg_channel = (
        f"https://t.me/{tg_nick}?start=wheel_promo" if tg_nick else ""
    )
    tg_startapp = (
        f"https://t.me/{tg_nick}?startapp=wheel_promo" if tg_nick else ""
    )
    tg_bot = (
        f"https://t.me/{tg_nick}?start=open_ticket" if tg_nick else ""
    )
    max_startapp = (
        max_wheel_deeplink(max_nick, start_param="wheel_promo") if max_nick else ""
    )
    max_bot = f"https://max.ru/{max_nick}" if max_nick else ""
    hint_parts: list[str] = [
        "В пост канала копируйте Telegram/MAX — не HTTPS Mini App "
        "(в Safari аккаунт не виден)."
    ]
    if not tg_nick:
        hint_parts.append(
            "Задайте TELEGRAM_BOT_USERNAME в .env — появятся ссылки t.me"
        )
    if not max_nick:
        hint_parts.append("MAX_BOT_USERNAME пуст — ссылка MAX недоступна")
    return {
        "miniapp_url": miniapp,
        "telegram_channel": tg_channel,
        "telegram_startapp": tg_startapp,
        "telegram_bot": tg_bot,
        "max_startapp": max_startapp,
        "max_bot": max_bot,
        "telegram_bot_username": tg_nick,
        "max_bot_username": max_nick,
        "hint": " ".join(hint_parts),
    }


_max_bot_username_cache: str | None = None
_max_bot_user_id_cache: int | None = None


def _normalize_max_bot_username(value: str) -> str:
    """Убрать @ / и пробелы по краям."""
    return str(value or "").strip().lstrip("@/").strip()


def _is_valid_max_bot_username(value: str) -> bool:
    """Ник бота MAX: латиница/цифры/_, без пробелов и кириллицы (это title, не username)."""
    uname = _normalize_max_bot_username(value)
    if not uname or " " in uname or len(uname) > 64:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", uname))


def _parse_max_bot_user_id(*candidates: Any) -> int | None:
    """user_id бота из get_me / idNNNN_…_bot."""
    for raw in candidates:
        if raw is None or raw is False:
            continue
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and raw > 0:
            return raw
        text = str(raw).strip()
        if text.isdigit():
            return int(text)
        m = re.fullmatch(r"id(\d+)(?:_\d+)?_bot", text, flags=re.I)
        if m:
            return int(m.group(1))
    return None


def max_wheel_deeplink(username: str, *, start_param: str = "wheel") -> str:
    """Диплинк Mini App внутри MAX: https://max.ru/{bot}?startapp=wheel."""
    uname = _normalize_max_bot_username(username)
    param = str(start_param or "wheel").strip()
    if not _is_valid_max_bot_username(uname):
        return ""
    if param:
        return f"https://max.ru/{uname}?startapp={param}"
    return f"https://max.ru/{uname}?startapp"


async def resolve_max_bot_identity() -> tuple[str | None, int | None]:
    """(username, user_id) MAX-бота для open_app / диплинка."""
    global _max_bot_username_cache, _max_bot_user_id_cache

    def _accept_username(raw: str, *, source: str) -> str | None:
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

    username = _max_bot_username_cache
    user_id = _max_bot_user_id_cache

    if not username:
        try:
            from config import MAX_BOT_USERNAME

            username = _accept_username(
                MAX_BOT_USERNAME, source=".env MAX_BOT_USERNAME"
            )
        except Exception:
            username = None

    if not username:
        try:
            import runtime_settings

            username = _accept_username(
                str(runtime_settings.get("max_bot_username") or ""),
                source="runtime_settings",
            )
        except Exception:
            pass

    # Всегда тянем /me: нужен contact_id; username подтянем, если .env пустой
    try:
        from max_bot.api import MaxBotAPI
        from senders.max_bot import get_max_bot_token

        token = get_max_bot_token()
        if token:
            api = MaxBotAPI(token)
            try:
                me = await api.get_me()
            finally:
                await api.close()
            candidates = [me]
            if isinstance(me.get("user"), dict):
                candidates.append(me["user"])

            if not username:
                for obj in candidates:
                    for key in ("username", "user_name", "nick", "nickname"):
                        accepted = _accept_username(
                            str((obj or {}).get(key) or ""),
                            source=f"get_me.{key}",
                        )
                        if accepted:
                            username = accepted
                            break
                    if username:
                        break

            if user_id is None:
                for obj in candidates:
                    user_id = _parse_max_bot_user_id(
                        (obj or {}).get("user_id"),
                        (obj or {}).get("id"),
                        (obj or {}).get("bot_id"),
                        username,
                    )
                    if user_id is not None:
                        break

            if username and not _max_bot_username_cache:
                try:
                    import runtime_settings

                    runtime_settings.set_many({"max_bot_username": username})
                except Exception:
                    pass
                logger.info(
                    "MAX wheel: bot identity username=%s user_id=%s",
                    username,
                    user_id,
                )
        elif not username:
            logger.warning("MAX wheel: нет токена — не удалось узнать username бота")
    except Exception:
        logger.exception("MAX wheel: не удалось получить identity бота")

    if user_id is None and username:
        user_id = _parse_max_bot_user_id(username)

    if username:
        _max_bot_username_cache = username
    if user_id is not None:
        _max_bot_user_id_cache = user_id

    if not username:
        logger.warning(
            "MAX wheel: нет валидного username. "
            "Укажите MAX_BOT_USERNAME=ник из ссылки max.ru/Nick"
        )
    return username, user_id


async def resolve_max_bot_username() -> str | None:
    """Username MAX-бота для open_app / диплинка (кэш на процесс)."""
    username, _user_id = await resolve_max_bot_identity()
    return username


async def max_wheel_keyboard() -> list[list[dict[str, Any]]] | None:
    """
    Кнопка колеса для MAX.

    Нужно: Mini App URL в кабинете партнёра MAX у этого бота.
    Кнопка: open_app (username + contact_id) + запасной диплинк max.ru/?startapp.
    """
    from max_bot.api import btn_link, btn_open_app

    username, bot_user_id = await resolve_max_bot_identity()
    if not username and bot_user_id is None:
        return None

    rows: list[list[dict[str, Any]]] = []
    open_web_app = username or ""
    if open_web_app or bot_user_id is not None:
        rows.append(
            [
                btn_open_app(
                    WHEEL_OPEN_LABEL,
                    open_web_app,
                    payload="wheel",
                    contact_id=bot_user_id,
                )
            ]
        )
        logger.info(
            "MAX wheel button: open_app web_app=%s contact_id=%s payload=wheel",
            open_web_app or None,
            bot_user_id,
        )

    # Диплинк max.ru открывает Mini App внутри клиента (не admin.* в браузере)
    if username:
        deep = max_wheel_deeplink(username, start_param="wheel")
        if deep:
            rows.append([btn_link("🎡 Открыть через MAX", deep)])

    return rows or None


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


def wheel_promo_keyboard() -> InlineKeyboardMarkup | None:
    """Inline Web App: промо-колесо с запечатанным билетом (вход из канала)."""
    url = wheel_promo_miniapp_url()
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=WHEEL_OPEN_LABEL, web_app=WebAppInfo(url=url)
                )
            ]
        ]
    )


async def max_wheel_promo_keyboard() -> list[list[dict[str, Any]]] | None:
    """Кнопка промо-колеса для MAX (payload=wheel_promo)."""
    from max_bot.api import btn_link, btn_open_app

    username, bot_user_id = await resolve_max_bot_identity()
    if not username and bot_user_id is None:
        return None

    rows: list[list[dict[str, Any]]] = []
    open_web_app = username or ""
    if open_web_app or bot_user_id is not None:
        rows.append(
            [
                btn_open_app(
                    WHEEL_OPEN_LABEL,
                    open_web_app,
                    payload="wheel_promo",
                    contact_id=bot_user_id,
                )
            ]
        )
    if username:
        deep = max_wheel_deeplink(username, start_param="wheel_promo")
        if deep:
            rows.append([btn_link("🎡 Открыть через MAX", deep)])
    return rows or None


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
