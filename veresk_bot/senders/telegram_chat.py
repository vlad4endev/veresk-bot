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
    __slots__ = ("client", "lock", "account_id", "session_file", "me_id")

    def __init__(self, client: Any, account_id: int | None, session_file: str):
        self.client = client
        self.lock = asyncio.Lock()
        self.account_id = account_id
        self.session_file = session_file
        self.me_id: int | None = None


_pool: dict[str, _PoolEntry] = {}
_pool_guard = asyncio.Lock()
_avatar_mem: dict[str, tuple[float, bytes, str]] = {}
_AVATAR_MEM_TTL = 6 * 3600
_AVATAR_MEM_MAX = 256
_dialogs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_DIALOGS_CACHE_TTL = 4.0


def _dialogs_cache_key(
    session_file: str,
    account_id: int | None,
    limit: int,
    query: str,
    only_users: bool,
) -> str:
    return f"{_session_key(session_file)}|{account_id or 0}|{limit}|{query}|{int(only_users)}"


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
        if entry.me_id is None:
            try:
                me = await entry.client.get_me()
                entry.me_id = int(me.id) if me else None
            except Exception:
                logger.debug("warmup get_me failed", exc_info=True)
    return entry


def _invalidate_dialogs_cache(session_file: str) -> None:
    prefix = _session_key(session_file) + "|"
    dead = [k for k in _dialogs_cache if k.startswith(prefix)]
    for k in dead:
        _dialogs_cache.pop(k, None)


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


async def _cached_me_id(session_file: str, account_id: int | None, client: Any) -> int | None:
    key = _session_key(session_file)
    entry = _pool.get(key)
    if entry is not None and entry.me_id is not None:
        return entry.me_id
    me = await client.get_me()
    me_id = int(me.id) if me else None
    if entry is not None:
        entry.me_id = me_id
    return me_id


def _avatar_cache_dir() -> Path:
    from senders.telegram_userbot import sessions_path

    path = sessions_path() / "avatar_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _avatar_cache_key(account_id: int | None, peer_id: int) -> str:
    return f"{account_id or 0}_{int(peer_id)}"


def _avatar_from_mem(key: str) -> tuple[bytes, str] | None:
    import time

    hit = _avatar_mem.get(key)
    if not hit:
        return None
    ts, data, mime = hit
    if time.time() - ts > _AVATAR_MEM_TTL:
        _avatar_mem.pop(key, None)
        return None
    return data, mime


def _avatar_to_mem(key: str, data: bytes, mime: str) -> None:
    import time

    if len(_avatar_mem) >= _AVATAR_MEM_MAX:
        # выкинем самый старый
        oldest = min(_avatar_mem.items(), key=lambda kv: kv[1][0])[0]
        _avatar_mem.pop(oldest, None)
    _avatar_mem[key] = (time.time(), data, mime)


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


def _media_kind(msg: Any) -> str | None:
    """Тип вложения: photo / sticker / animation / video / video_note / voice / audio / document / …"""
    if msg is None or getattr(msg, "media", None) is None:
        return None
    from telethon.tl import types

    media = msg.media
    if isinstance(media, types.MessageMediaPhoto):
        return "photo"
    if isinstance(media, types.MessageMediaWebPage):
        return "webpage"
    if isinstance(media, types.MessageMediaGeo):
        return "geo"
    if isinstance(media, types.MessageMediaContact):
        return "contact"
    if isinstance(media, types.MessageMediaPoll):
        return "poll"
    if isinstance(media, types.MessageMediaDocument):
        doc = getattr(media, "document", None)
        attrs = getattr(doc, "attributes", None) or []
        for a in attrs:
            if isinstance(a, types.DocumentAttributeSticker):
                return "sticker"
            if isinstance(a, types.DocumentAttributeAnimated):
                return "animation"
            if isinstance(a, types.DocumentAttributeVideo):
                if getattr(a, "round_message", False):
                    return "video_note"
                return "video"
            if isinstance(a, types.DocumentAttributeAudio):
                if getattr(a, "voice", False):
                    return "voice"
                return "audio"
        mime = (getattr(doc, "mime_type", None) or "").lower()
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return "document"
    return "media"


def _message_preview(msg: Any) -> str:
    if msg is None:
        return ""
    text = (getattr(msg, "message", None) or "").strip()
    if text:
        return text[:160]
    kind = _media_kind(msg)
    labels = {
        "photo": "🖼 Фото",
        "sticker": "🎟 Стикер",
        "animation": "🎞 GIF",
        "video": "🎬 Видео",
        "video_note": "⏺ Видеосообщение",
        "voice": "🎤 Голосовое",
        "audio": "🎵 Аудио",
        "document": "📎 Файл",
        "geo": "📍 Геолокация",
        "contact": "👤 Контакт",
        "poll": "📊 Опрос",
        "webpage": "🔗 Ссылка",
        "media": "Медиа",
    }
    if kind:
        return labels.get(kind, "Медиа")
    return ""


def _doc_filename(msg: Any) -> str | None:
    from telethon.tl import types

    media = getattr(msg, "media", None)
    if not isinstance(media, types.MessageMediaDocument):
        return None
    doc = getattr(media, "document", None)
    for a in getattr(doc, "attributes", None) or []:
        if isinstance(a, types.DocumentAttributeFilename):
            name = getattr(a, "file_name", None)
            if name:
                return str(name)
    return None


def _guess_mime(data: bytes, fallback: str = "application/octet-stream") -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    if data.startswith(b"ID3") or data[:2] == b"\xff\xfb":
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    return fallback


def _serialize_message(msg: Any, *, me_id: int | None) -> dict[str, Any]:
    sender = getattr(msg, "sender", None)
    kind = _media_kind(msg)
    out: dict[str, Any] = {
        "id": int(msg.id),
        "date": _dt_iso(getattr(msg, "date", None)),
        "out": bool(getattr(msg, "out", False)),
        "text": (getattr(msg, "message", None) or "").strip(),
        "preview": _message_preview(msg),
        "has_media": bool(kind),
        "media_kind": kind,
        "file_name": _doc_filename(msg),
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
    only_users: bool = False,
) -> list[dict[str, Any]]:
    import time

    limit = max(1, min(int(limit or 80), 120))
    q = (query or "").strip().lower()
    cache_key = _dialogs_cache_key(session_file, account_id, limit, q, only_users)
    hit = _dialogs_cache.get(cache_key)
    if hit and time.monotonic() - hit[0] < _DIALOGS_CACHE_TTL:
        return [dict(row) for row in hit[1]]

    # Раньше scan доходил до 400 и блокировал чаты надолго.
    # Для UI достаточно недавних диалогов; CRM-фильтр добирает сверху.
    if q:
        scan = min(max(limit * 2, limit), 160)
    elif only_users:
        scan = min(max(limit + 40, limit), 140)
    else:
        scan = limit
    async with telegram_session(session_file, account_id) as client:
        items: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=scan):
            row = _serialize_dialog(dialog)
            if only_users and row.get("kind") != "user":
                continue
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
        _dialogs_cache[cache_key] = (time.monotonic(), items)
        if len(_dialogs_cache) > 64:
            oldest = min(_dialogs_cache.items(), key=lambda kv: kv[1][0])[0]
            _dialogs_cache.pop(oldest, None)
        return items


async def get_dialog_messages(
    session_file: str,
    peer_id: int,
    *,
    account_id: int | None = None,
    limit: int = 50,
    offset_id: int = 0,
    mark_read: bool = True,
    enrich_peer: bool = True,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 100))
    offset_id = max(0, int(offset_id or 0))
    async with telegram_session(session_file, account_id) as client:
        try:
            entity = await client.get_input_entity(peer_id)
        except Exception:
            entity = await client.get_entity(peer_id)
        me_id = await _cached_me_id(session_file, account_id, client)
        kwargs: dict[str, Any] = {"limit": limit}
        if offset_id:
            kwargs["offset_id"] = offset_id
        raw = await client.get_messages(entity, **kwargs)
        msg_list = [m for m in list(raw) if m is not None]
        rows = [
            _serialize_message(m, me_id=me_id)
            for m in reversed(msg_list)
        ]
        if mark_read and not offset_id:
            try:
                await client.send_read_acknowledge(entity)
            except Exception:
                logger.debug("mark read failed for %s", peer_id, exc_info=True)
        peer: dict[str, Any] = {
            "id": str(peer_id),
            "peer_id": int(peer_id),
            "title": "",
            "kind": "user",
            "username": None,
            "phone": None,
        }
        if enrich_peer:
            # Для заголовка нужен полный entity (username/phone/title)
            try:
                full = await client.get_entity(peer_id)
            except Exception:
                full = entity
            peer = {
                "id": str(peer_id),
                "peer_id": int(peer_id),
                "title": _entity_title(full),
                "kind": _entity_kind(full),
                "username": getattr(full, "username", None),
                "phone": getattr(full, "phone", None),
            }
        return {
            "peer": peer,
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
        try:
            entity = await client.get_input_entity(peer_id)
        except Exception:
            entity = await client.get_entity(peer_id)
        me_id = await _cached_me_id(session_file, account_id, client)
        sent = await client.send_message(entity, body)
        _invalidate_dialogs_cache(session_file)
        return _serialize_message(sent, me_id=me_id)


MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB на файл
MAX_UPLOAD_FILES = 10


async def send_dialog_media(
    session_file: str,
    peer_id: int,
    files: list[dict[str, Any]],
    *,
    caption: str = "",
    force_document: bool = False,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Отправить фото / видео / файлы.
    files: [{filename, data: bytes, mime}]
    Несколько фото/видео → альбом (caption на первом).
    """
    if not files:
        raise ValueError("Нет файлов")
    if len(files) > MAX_UPLOAD_FILES:
        raise ValueError(f"Максимум {MAX_UPLOAD_FILES} файлов за раз")
    caption = (caption or "").strip()
    if len(caption) > 1024:
        raise ValueError("Подпись длиннее 1024 символов")

    import tempfile

    paths: list[str] = []
    try:
        for item in files:
            raw = item.get("data") or b""
            if not isinstance(raw, (bytes, bytearray)):
                raise ValueError("Некорректные данные файла")
            raw = bytes(raw)
            if not raw:
                raise ValueError("Пустой файл")
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError("Файл больше 50 МБ")
            name = str(item.get("filename") or "file")
            suffix = Path(name).suffix[:20] or ""
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            try:
                tmp.write(raw)
                tmp.flush()
            finally:
                tmp.close()
            paths.append(tmp.name)

        async with telegram_session(session_file, account_id) as client:
            try:
                entity = await client.get_input_entity(peer_id)
            except Exception:
                entity = await client.get_entity(peer_id)
            me_id = await _cached_me_id(session_file, account_id, client)

            # Один файл или альбом
            send_path: str | list[str] = paths[0] if len(paths) == 1 else paths
            sent = await client.send_file(
                entity,
                send_path,
                caption=caption or None,
                force_document=force_document,
                supports_streaming=True,
            )
            if not isinstance(sent, list):
                sent = [sent]
            _invalidate_dialogs_cache(session_file)
            return [_serialize_message(m, me_id=me_id) for m in sent if m is not None]
    finally:
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


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
        # Для каналов/супергрупп Telethon dialog.id может отличаться —
        # берём через get_peer_id.
        try:
            from telethon.utils import get_peer_id

            peer_id = int(get_peer_id(entity))
        except Exception:
            pass

        sent = None
        body = (first_message or "").strip()
        if body:
            me_id = await _cached_me_id(session_file, account_id, client)
            msg = await client.send_message(entity, body)
            sent = _serialize_message(msg, me_id=me_id)
            _invalidate_dialogs_cache(session_file)

        return {
            "peer": {
                "id": str(peer_id),
                "peer_id": peer_id,
                "title": _entity_title(entity),
                "kind": _entity_kind(entity),
                "username": getattr(entity, "username", None),
                "phone": getattr(entity, "phone", None) or (
                    re.sub(r"\D", "", phone_raw) if phone_raw else None
                ),
            },
            "message": sent,
        }


async def resolve_account_session(account: dict[str, Any]) -> tuple[str, int]:
    account_id = int(account["id"])
    session_file = str(account.get("session_file") or "")
    if not session_file:
        raise RuntimeError("У аккаунта нет session_file")
    return session_file, account_id


async def resolve_peer_profile(
    session_file: str,
    peer_id: int,
    *,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Актуальные данные собеседника из Telegram (для привязки к CRM)."""
    async with telegram_session(session_file, account_id) as client:
        entity = await client.get_entity(peer_id)
        kind = _entity_kind(entity)
        phone = getattr(entity, "phone", None)
        if phone is not None:
            phone = str(phone).lstrip("+")
        # Для пользователя peer_id в диалогах = user id
        tg_user_id = None
        if kind in ("user", "bot"):
            tg_user_id = int(getattr(entity, "id", peer_id) or peer_id)
        return {
            "id": str(peer_id),
            "peer_id": int(peer_id),
            "tg_user_id": tg_user_id,
            "title": _entity_title(entity),
            "kind": kind,
            "username": getattr(entity, "username", None),
            "phone": phone,
            "first_name": getattr(entity, "first_name", None),
            "last_name": getattr(entity, "last_name", None),
        }


MAX_INLINE_MEDIA_BYTES = 20 * 1024 * 1024  # 20 MB


async def download_peer_avatar(
    session_file: str,
    peer_id: int,
    *,
    account_id: int | None = None,
) -> tuple[bytes, str]:
    """Скачать аватар диалога. FileNotFoundError если фото нет."""
    cache_key = _avatar_cache_key(account_id, peer_id)
    cached = _avatar_from_mem(cache_key)
    if cached:
        return cached

    disk = _avatar_cache_dir() / f"{cache_key}.bin"
    meta = _avatar_cache_dir() / f"{cache_key}.mime"
    if disk.is_file():
        try:
            data = disk.read_bytes()
            mime = meta.read_text().strip() if meta.is_file() else "image/jpeg"
            if data:
                _avatar_to_mem(cache_key, data, mime)
                return data, mime
        except OSError:
            pass

    async with telegram_session(session_file, account_id) as client:
        try:
            entity = await client.get_input_entity(peer_id)
        except Exception:
            entity = await client.get_entity(peer_id)
        data = await client.download_profile_photo(entity, file=bytes)
        if not data:
            raise FileNotFoundError("no_avatar")
        raw = bytes(data)
        mime = _guess_mime(raw, "image/jpeg")
        _avatar_to_mem(cache_key, raw, mime)
        try:
            disk.write_bytes(raw)
            meta.write_text(mime)
        except OSError:
            logger.debug("avatar disk cache write failed", exc_info=True)
        return raw, mime


async def download_message_media(
    session_file: str,
    peer_id: int,
    message_id: int,
    *,
    account_id: int | None = None,
    thumb: bool = False,
) -> tuple[bytes, str, str | None]:
    """
    Скачать медиа сообщения.
    thumb=True — превью для видео (если есть).
    """
    async with telegram_session(session_file, account_id) as client:
        entity = await client.get_entity(peer_id)
        msg = await client.get_messages(entity, ids=int(message_id))
        if msg is None:
            raise FileNotFoundError("message_not_found")
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        if msg is None or not getattr(msg, "media", None):
            raise FileNotFoundError("no_media")

        kind = _media_kind(msg)
        filename = _doc_filename(msg)
        mime_hint = "application/octet-stream"
        doc = getattr(getattr(msg, "media", None), "document", None)
        if doc is not None and getattr(doc, "mime_type", None):
            mime_hint = str(doc.mime_type)

        data = None
        if thumb and kind in ("video", "video_note", "animation", "document"):
            try:
                data = await client.download_media(msg, thumb=-1, file=bytes)
            except Exception:
                data = None
        if not data:
            # Проверка размера документа до скачивания
            if doc is not None:
                size = int(getattr(doc, "size", 0) or 0)
                if size > MAX_INLINE_MEDIA_BYTES:
                    raise ValueError("media_too_large")
            data = await client.download_media(msg, file=bytes)

        if not data:
            raise FileNotFoundError("download_failed")

        raw = bytes(data)
        if len(raw) > MAX_INLINE_MEDIA_BYTES:
            raise ValueError("media_too_large")

        mime = _guess_mime(raw, mime_hint)
        if kind in ("photo", "sticker", "animation", "webpage") and mime.startswith(
            "application/"
        ):
            mime = "image/jpeg"
        if kind in ("voice", "audio") and mime.startswith("application/"):
            mime = mime_hint if mime_hint.startswith("audio/") else "audio/ogg"
        if kind in ("video", "video_note") and mime.startswith("application/"):
            mime = mime_hint if mime_hint.startswith("video/") else "video/mp4"
        return raw, mime, filename
