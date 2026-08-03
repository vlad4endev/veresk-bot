"""
Живой пул Telethon-клиентов для раздела «Чаты» в админке.

Один клиент на файл сессии — чтобы рассылки, keepalive и чаты
не открывали второй SQLite-коннект к одному .session.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from senders.telegram_userbot import (
    _normalize_phone,
    get_api_credentials,
    is_telethon_configured,
)

logger = logging.getLogger(__name__)


def _session_key(session_file: str) -> str:
    base = (session_file or "").strip()
    if base.endswith(".session"):
        base = base[:-8]
    try:
        return str(Path(base).resolve())
    except OSError:
        return base


def _session_base(session_file: str) -> str:
    base = (session_file or "").strip()
    if base.endswith(".session"):
        base = base[:-8]
    return base


class _PoolEntry:
    __slots__ = ("client", "lock", "account_id", "session_file")

    def __init__(self, client: Any, account_id: int | None, session_file: str):
        self.client = client
        self.lock = asyncio.Lock()
        self.account_id = account_id
        self.session_file = session_file


_pool: dict[str, _PoolEntry] = {}
_pool_guard = asyncio.Lock()


async def _ensure_entry(session_file: str, account_id: int | None = None) -> _PoolEntry:
    if not is_telethon_configured():
        raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы")
    if not session_file:
        raise RuntimeError("session_file пустой")

    key = _session_key(session_file)
    async with _pool_guard:
        entry = _pool.get(key)
        if entry is not None:
            if account_id is not None:
                entry.account_id = account_id
            return entry

        from telethon import TelegramClient

        api_id, api_hash = get_api_credentials()
        client = TelegramClient(_session_base(session_file), api_id, api_hash)
        entry = _PoolEntry(client, account_id, session_file)
        _pool[key] = entry

    async with entry.lock:
        if not entry.client.is_connected():
            await asyncio.wait_for(entry.client.connect(), timeout=25)
        if not await entry.client.is_user_authorized():
            await release_session(session_file=session_file)
            raise RuntimeError("Сессия не авторизована — переподключите аккаунт")
    return entry


@asynccontextmanager
async def telegram_session(
    session_file: str,
    account_id: int | None = None,
) -> AsyncIterator[Any]:
    """Взять клиент под локом (для отправки / чтения / проверки)."""
    entry = await _ensure_entry(session_file, account_id)
    async with entry.lock:
        if not entry.client.is_connected():
            await asyncio.wait_for(entry.client.connect(), timeout=25)
        if not await entry.client.is_user_authorized():
            raise RuntimeError("Сессия не авторизована — переподключите аккаунт")
        yield entry.client


async def release_session(
    *,
    session_file: str | None = None,
    account_id: int | None = None,
) -> None:
    """Отключить и убрать клиент из пула (перед удалением аккаунта)."""
    async with _pool_guard:
        keys: list[str] = []
        if session_file:
            keys.append(_session_key(session_file))
        if account_id is not None:
            for k, e in _pool.items():
                if e.account_id == account_id:
                    keys.append(k)
        for key in dict.fromkeys(keys):
            entry = _pool.pop(key, None)
            if not entry:
                continue
            try:
                await entry.client.disconnect()
            except Exception:
                pass


async def release_all_sessions() -> None:
    async with _pool_guard:
        items = list(_pool.items())
        _pool.clear()
    for _, entry in items:
        try:
            await entry.client.disconnect()
        except Exception:
            pass


def _dt_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _entity_title(entity: Any) -> str:
    if entity is None:
        return "Без имени"
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    name = " ".join(x for x in [first, last] if x).strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    phone = getattr(entity, "phone", None)
    if phone:
        return f"+{phone}" if not str(phone).startswith("+") else str(phone)
    return f"id{getattr(entity, 'id', '?')}"


def _entity_kind(entity: Any) -> str:
    from telethon.tl import types

    if isinstance(entity, types.User):
        if getattr(entity, "bot", False):
            return "bot"
        return "user"
    if isinstance(entity, (types.Chat, types.ChatForbidden)):
        return "group"
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        if getattr(entity, "megagroup", False):
            return "group"
        if getattr(entity, "broadcast", False):
            return "channel"
        return "group"
    return "unknown"


def _message_preview(msg: Any) -> str:
    if msg is None:
        return ""
    text = (getattr(msg, "message", None) or "").strip()
    if text:
        return text[:160]
    if getattr(msg, "media", None) is None:
        return ""
    from telethon.tl import types

    media = msg.media
    if isinstance(media, types.MessageMediaPhoto):
        return "🖼 Фото"
    if isinstance(media, types.MessageMediaDocument):
        doc = getattr(media, "document", None)
        attrs = getattr(doc, "attributes", None) or []
        for a in attrs:
            if isinstance(a, types.DocumentAttributeSticker):
                return "🎟 Стикер"
            if isinstance(a, types.DocumentAttributeAnimated):
                return "🎞 GIF"
            if isinstance(a, types.DocumentAttributeVideo):
                if getattr(a, "round_message", False):
                    return "⏺ Видеосообщение"
                return "🎬 Видео"
            if isinstance(a, types.DocumentAttributeAudio):
                if getattr(a, "voice", False):
                    return "🎤 Голосовое"
                return "🎵 Аудио"
        return "📎 Файл"
    if isinstance(media, types.MessageMediaGeo):
        return "📍 Геолокация"
    if isinstance(media, types.MessageMediaContact):
        return "👤 Контакт"
    if isinstance(media, types.MessageMediaPoll):
        return "📊 Опрос"
    if isinstance(media, types.MessageMediaWebPage):
        return text or "🔗 Ссылка"
    return "Сообщение"


def _serialize_message(msg: Any, *, me_id: int | None) -> dict[str, Any]:
    sender = getattr(msg, "sender", None)
    out: dict[str, Any] = {
        "id": int(msg.id),
        "date": _dt_iso(getattr(msg, "date", None)),
        "out": bool(getattr(msg, "out", False)),
        "text": (getattr(msg, "message", None) or "").strip(),
        "preview": _message_preview(msg),
        "has_media": bool(getattr(msg, "media", None)),
        "from_id": None,
        "from_name": None,
        "reply_to": None,
    }
    if sender is not None:
        out["from_id"] = int(getattr(sender, "id", 0) or 0) or None
        out["from_name"] = _entity_title(sender)
    elif me_id and out["out"]:
        out["from_id"] = me_id
        out["from_name"] = "Вы"
    reply = getattr(msg, "reply_to", None)
    if reply is not None and getattr(reply, "reply_to_msg_id", None):
        out["reply_to"] = int(reply.reply_to_msg_id)
    return out


def _serialize_dialog(dialog: Any) -> dict[str, Any]:
    entity = dialog.entity
    last = dialog.message
    unread = int(getattr(dialog, "unread_count", 0) or 0)
    return {
        "id": str(dialog.id),
        "peer_id": int(dialog.id),
        "title": dialog.name or _entity_title(entity),
        "kind": _entity_kind(entity),
        "username": getattr(entity, "username", None),
        "phone": getattr(entity, "phone", None),
        "unread": unread,
        "pinned": bool(getattr(dialog, "pinned", False)),
        "is_user": bool(getattr(dialog, "is_user", False)),
        "is_group": bool(getattr(dialog, "is_group", False)),
        "is_channel": bool(getattr(dialog, "is_channel", False)),
        "date": _dt_iso(getattr(dialog, "date", None) or getattr(last, "date", None)),
        "last_message": _message_preview(last) if last else "",
        "last_out": bool(getattr(last, "out", False)) if last else False,
    }


async def list_dialogs(
    session_file: str,
    *,
    account_id: int | None = None,
    limit: int = 80,
    query: str = "",
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 80), 200))
    q = (query or "").strip().lower()
    async with telegram_session(session_file, account_id) as client:
        items: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=limit if not q else min(limit * 3, 300)):
            row = _serialize_dialog(dialog)
            if q:
                hay = " ".join(
                    filter(
                        None,
                        [
                            row.get("title"),
                            row.get("username"),
                            row.get("phone"),
                            row.get("last_message"),
                        ],
                    )
                ).lower()
                if q not in hay:
                    continue
            items.append(row)
            if len(items) >= limit:
                break
        return items


async def get_dialog_messages(
    session_file: str,
    peer_id: int,
    *,
    account_id: int | None = None,
    limit: int = 50,
    offset_id: int = 0,
    mark_read: bool = True,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 100))
    offset_id = max(0, int(offset_id or 0))
    async with telegram_session(session_file, account_id) as client:
        entity = await client.get_entity(peer_id)
        me = await client.get_me()
        me_id = int(me.id) if me else None
        kwargs: dict[str, Any] = {"limit": limit}
        if offset_id:
            kwargs["offset_id"] = offset_id
        raw = await client.get_messages(entity, **kwargs)
        msg_list = [m for m in list(raw) if m is not None]
        rows = [_serialize_message(m, me_id=me_id) for m in reversed(msg_list)]
        if mark_read and not offset_id:
            try:
                await client.send_read_acknowledge(entity)
            except Exception:
                logger.debug("mark read failed for %s", peer_id, exc_info=True)
        return {
            "peer": {
                "id": str(peer_id),
                "peer_id": int(peer_id),
                "title": _entity_title(entity),
                "kind": _entity_kind(entity),
                "username": getattr(entity, "username", None),
                "phone": getattr(entity, "phone", None),
            },
            "messages": rows,
            "has_more": len(msg_list) >= limit,
        }


async def send_dialog_message(
    session_file: str,
    peer_id: int,
    text: str,
    *,
    account_id: int | None = None,
) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("Пустое сообщение")
    if len(body) > 4096:
        raise ValueError("Сообщение длиннее 4096 символов")
    async with telegram_session(session_file, account_id) as client:
        entity = await client.get_entity(peer_id)
        me = await client.get_me()
        me_id = int(me.id) if me else None
        sent = await client.send_message(entity, body)
        return _serialize_message(sent, me_id=me_id)


async def create_or_open_dialog(
    session_file: str,
    *,
    account_id: int | None = None,
    phone: str = "",
    username: str = "",
    name: str = "",
    first_message: str = "",
) -> dict[str, Any]:
    """Найти/создать диалог по телефону или @username."""
    phone_raw = (phone or "").strip()
    user_raw = (username or "").strip().lstrip("@")
    if not phone_raw and not user_raw:
        raise ValueError("Укажите телефон или username")

    async with telegram_session(session_file, account_id) as client:
        entity = None
        if phone_raw:
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact

            phone_norm = _normalize_phone(phone_raw)
            display = (name or "").strip() or "Клиент"
            parts = display.split()
            contact = InputPhoneContact(
                client_id=0,
                phone=phone_norm,
                first_name=parts[0],
                last_name=" ".join(parts[1:]) if len(parts) > 1 else "",
            )
            result = await client(ImportContactsRequest([contact]))
            if result.users:
                entity = result.users[0]
            if entity is None:
                try:
                    entity = await client.get_entity(phone_norm)
                except Exception as exc:
                    raise RuntimeError(
                        "Не удалось найти пользователя по телефону — "
                        "возможно, он скрыл номер в Telegram"
                    ) from exc
        else:
            try:
                entity = await client.get_entity(user_raw)
            except Exception as exc:
                raise RuntimeError(f"Не найден @{user_raw}") from exc

        peer_id = int(getattr(entity, "id"))
        try:
            from telethon.utils import get_peer_id

            peer_id = int(get_peer_id(entity))
        except Exception:
            pass

        sent = None
        body = (first_message or "").strip()
        if body:
            me = await client.get_me()
            me_id = int(me.id) if me else None
            msg = await client.send_message(entity, body)
            sent = _serialize_message(msg, me_id=me_id)

        return {
            "peer": {
                "id": str(peer_id),
                "peer_id": peer_id,
                "title": _entity_title(entity),
                "kind": _entity_kind(entity),
                "username": getattr(entity, "username", None),
                "phone": getattr(entity, "phone", None)
                or (re.sub(r"\D", "", phone_raw) if phone_raw else None),
            },
            "message": sent,
        }
