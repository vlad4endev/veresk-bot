"""
Живой пул PyMax-клиентов для раздела «Чаты → MAX» (личный номер).

Один Client на файл сессии — чтобы рассылки и чаты не открывали
второй коннект к одному SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, TypeVar

from senders.max_userbot import (
    _RejectPasswordProvider,
    _RejectSmsCodeProvider,
    _normalize_phone,
    _resolve_chat_id,
    _user_id,
    _user_label,
    is_pymax_installed,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _session_key(session_file: str) -> str:
    try:
        return str(Path(session_file or "").resolve())
    except OSError:
        return (session_file or "").strip()


def _phone_from_session(session_file: str, phone: str = "") -> str:
    if phone:
        return _normalize_phone(phone)
    path = Path(session_file or "")
    digits = re.sub(r"\D", "", path.stem.replace("max_acc_", ""))
    return f"+{digits}" if digits else ""


class _PoolEntry:
    __slots__ = (
        "client",
        "lock",
        "account_id",
        "session_file",
        "phone",
        "start_task",
        "ready",
        "failed",
        "error",
    )

    def __init__(
        self,
        client: Any,
        *,
        account_id: int | None,
        session_file: str,
        phone: str,
        start_task: asyncio.Task[Any],
        ready: asyncio.Event,
    ) -> None:
        self.client = client
        self.lock = asyncio.Lock()
        self.account_id = account_id
        self.session_file = session_file
        self.phone = phone
        self.start_task = start_task
        self.ready = ready
        self.failed = False
        self.error: str | None = None


_pool: dict[str, _PoolEntry] = {}
_pool_guard = asyncio.Lock()


async def _ensure_entry(
    session_file: str,
    *,
    phone: str = "",
    account_id: int | None = None,
    force_full_sync: bool = False,
) -> _PoolEntry:
    if not is_pymax_installed():
        raise RuntimeError("Библиотека maxapi-python (PyMax) не установлена")
    if not session_file or not Path(session_file).exists():
        raise RuntimeError("Файл сессии MAX не найден — переподключите номер")

    phone_norm = _phone_from_session(session_file, phone)
    if not phone_norm:
        raise RuntimeError("Неизвестный телефон сессии MAX")

    key = _session_key(session_file)
    async with _pool_guard:
        entry = _pool.get(key)
        if entry is not None:
            if account_id is not None:
                entry.account_id = account_id
            if entry.failed:
                _pool.pop(key, None)
            else:
                return entry

        from pymax import Client, ExtraConfig, SyncOverrides

        path = Path(session_file)
        # Полный sync чатов/контактов при первом открытии пула —
        # иначе client.chats может быть почти пустым после инкрементального login.
        extra_kwargs: dict[str, Any] = {
            "reconnect": True,
            "log_level": "WARNING",
            "sync": SyncOverrides(chats_sync=-1, contacts_sync=-1),
        }

        client = Client(
            phone=phone_norm,
            work_dir=str(path.parent),
            session_name=path.name,
            sms_code_provider=_RejectSmsCodeProvider(),
            password_provider=_RejectPasswordProvider(),
            extra_config=ExtraConfig(**extra_kwargs),
        )
        ready = asyncio.Event()

        @client.on_start()
        async def _on_start(_c: Any) -> None:
            ready.set()

        digits = re.sub(r"\D", "", phone_norm)
        start_task = asyncio.create_task(
            client.start(),
            name=f"max_chat_{digits}",
        )
        entry = _PoolEntry(
            client,
            account_id=account_id,
            session_file=session_file,
            phone=phone_norm,
            start_task=start_task,
            ready=ready,
        )
        _pool[key] = entry

    # Ждём login вне pool_guard
    try:
        deadline = asyncio.get_running_loop().time() + 35.0
        while not entry.ready.is_set():
            if entry.start_task.done():
                exc = None
                try:
                    exc = entry.start_task.exception()
                except asyncio.CancelledError:
                    exc = RuntimeError("connection_closed")
                err = str(exc) if exc else "Сессия MAX не авторизована"
                if "session_needs_reauth" in err or "session_needs_2fa" in err:
                    err = "Сессия не авторизована — переподключите номер в Настройках"
                entry.failed = True
                entry.error = err
                await release_session(session_file=session_file)
                raise RuntimeError(err)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                entry.failed = True
                entry.error = "Таймаут подключения к MAX"
                await release_session(session_file=session_file)
                raise RuntimeError(entry.error)
            try:
                await asyncio.wait_for(entry.ready.wait(), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue
    except Exception:
        raise

    return entry


@asynccontextmanager
async def max_session(
    session_file: str,
    *,
    phone: str = "",
    account_id: int | None = None,
    force_full_sync: bool = False,
) -> AsyncIterator[Any]:
    entry = await _ensure_entry(
        session_file,
        phone=phone,
        account_id=account_id,
        force_full_sync=force_full_sync,
    )
    async with entry.lock:
        if entry.failed or entry.start_task.done():
            await release_session(session_file=session_file)
            raise RuntimeError(entry.error or "Соединение с MAX закрыто — обновите чаты")
        yield entry.client


async def release_session(
    *,
    session_file: str | None = None,
    account_id: int | None = None,
) -> None:
    async with _pool_guard:
        keys: list[str] = []
        if session_file:
            keys.append(_session_key(session_file))
        if account_id is not None:
            for k, e in _pool.items():
                if e.account_id == account_id:
                    keys.append(k)
        entries = []
        for key in dict.fromkeys(keys):
            entry = _pool.pop(key, None)
            if entry:
                entries.append(entry)

    for entry in entries:
        try:
            await entry.client.stop()
        except Exception:
            logger.debug("MAX chat client stop failed", exc_info=True)
        if not entry.start_task.done():
            entry.start_task.cancel()
            try:
                await entry.start_task
            except (asyncio.CancelledError, Exception):
                pass


async def release_all_sessions() -> None:
    async with _pool_guard:
        entries = list(_pool.values())
        _pool.clear()
    for entry in entries:
        try:
            await entry.client.stop()
        except Exception:
            pass
        if not entry.start_task.done():
            entry.start_task.cancel()
            try:
                await entry.start_task
            except (asyncio.CancelledError, Exception):
                pass


async def run_with_client(
    session_file: str,
    fn: Callable[[Any], Any],
    *,
    phone: str = "",
    account_id: int | None = None,
) -> Any:
    async with max_session(session_file, phone=phone, account_id=account_id) as client:
        result = fn(client)
        if asyncio.iscoroutine(result):
            return await result
        return result


def _ts_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    try:
        ts = int(value)
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value) if value else None


def _chat_type_str(chat: Any) -> str:
    raw = getattr(chat, "type", None)
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw) or "").upper()


def _chat_kind(chat: Any) -> str:
    t = _chat_type_str(chat)
    if "DIALOG" in t:
        return "user"
    if "CHANNEL" in t:
        return "channel"
    if "CHAT" in t:
        return "group"
    return "unknown"


def _contact_name(user: Any) -> str | None:
    if user is None:
        return None
    names = getattr(user, "names", None) or []
    if names:
        first = names[0]
        name = getattr(first, "name", None) or getattr(first, "first_name", None)
        if name:
            return str(name).strip() or None
        first_n = getattr(first, "first_name", None) or ""
        last_n = getattr(first, "last_name", None) or ""
        joined = " ".join(x for x in [first_n, last_n] if x).strip()
        if joined:
            return joined
    for attr in ("name", "first_name"):
        val = getattr(user, attr, None)
        if val:
            return str(val).strip() or None
    return None


def _user_avatar_url(user: Any) -> str | None:
    if user is None:
        return None
    for attr in ("base_url", "base_raw_url"):
        val = getattr(user, attr, None)
        if val:
            return str(val).strip() or None
    return None


def _chat_avatar_url(chat: Any, contact: Any = None) -> str | None:
    url = _user_avatar_url(contact)
    if url:
        return url
    for attr in ("base_icon_url", "base_raw_icon_url"):
        val = getattr(chat, attr, None)
        if val:
            return str(val).strip() or None
    return None


def _contact_map(client: Any) -> dict[int, Any]:
    """Контакты + кэш users клиента (после sync / get_users)."""
    out: dict[int, Any] = {}
    cached = getattr(client, "users", None)
    if isinstance(cached, dict):
        for uid, item in cached.items():
            if item is None:
                continue
            try:
                out[int(uid)] = item
            except (TypeError, ValueError):
                pass
    for item in client.contacts or []:
        if item is None:
            continue
        uid = getattr(item, "id", None)
        if uid is not None:
            try:
                out[int(uid)] = item
            except (TypeError, ValueError):
                pass
    return out


async def _enrich_users(client: Any, user_ids: list[int], contacts: dict[int, Any]) -> None:
    """Подтянуть имена и аватарки собеседников (не только из телефонной книги)."""
    ids = sorted({int(uid) for uid in user_ids if uid is not None})
    if not ids:
        return
    missing = [
        uid
        for uid in ids
        if uid not in contacts
        or not _contact_name(contacts.get(uid))
        or not _user_avatar_url(contacts.get(uid))
    ]
    if not missing:
        return
    try:
        users = await client.get_users(missing)
    except Exception:
        logger.debug("get_users failed for %s ids", len(missing), exc_info=True)
        return
    for user in users or []:
        if user is None:
            continue
        uid = getattr(user, "id", None)
        if uid is None:
            continue
        try:
            contacts[int(uid)] = user
        except (TypeError, ValueError):
            continue


def _peer_user_id(chat: Any, me_id: int | None) -> int | None:
    participants = getattr(chat, "participants", None) or {}
    for raw in participants:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if me_id is not None and uid == int(me_id):
            continue
        return uid
    return None


def _message_text(msg: Any) -> str:
    text = (getattr(msg, "text", None) or "").strip()
    return text


_USERBOT_KIND_MAP = {
    "PHOTO": "photo",
    "VIDEO": "video",
    "FILE": "document",
    "AUDIO": "audio",
    "STICKER": "sticker",
    "SHARE": "webpage",
    "CONTACT": "contact",
    "CALL": None,
    "CONTROL": None,
    "INLINE_KEYBOARD": None,
    "UNKNOWN": "media",
}

_USERBOT_PREVIEW = {
    "photo": "🖼 Фото",
    "sticker": "🎟 Стикер",
    "video": "🎬 Видео",
    "audio": "🎵 Аудио",
    "voice": "🎤 Голосовое",
    "document": "📎 Файл",
    "webpage": "🔗 Ссылка",
    "contact": "👤 Контакт",
    "media": "Медиа",
}


def _attach_type_str(att: Any) -> str:
    raw = getattr(att, "type", None)
    return str(getattr(raw, "value", raw) or type(att).__name__).upper()


def _normalize_userbot_kind(att: Any) -> str | None:
    key = _attach_type_str(att)
    if key in _USERBOT_KIND_MAP:
        return _USERBOT_KIND_MAP[key]
    # class name fallback
    name = type(att).__name__.upper()
    for token, kind in (
        ("PHOTO", "photo"),
        ("VIDEO", "video"),
        ("FILE", "document"),
        ("AUDIO", "audio"),
        ("STICKER", "sticker"),
    ):
        if token in name or token in key:
            return kind
    return "media"


def _attach_media_url(att: Any) -> str | None:
    for attr in ("base_url", "url", "thumbnail", "lottie_url"):
        val = getattr(att, attr, None)
        if val:
            return str(val).strip() or None
    return None


def _attach_file_name(att: Any) -> str | None:
    for attr in ("name", "file_name", "filename"):
        val = getattr(att, attr, None)
        if val:
            return str(val).strip() or None
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


async def _http_get_bytes(url: str, *, limit: int = 20 * 1024 * 1024) -> tuple[bytes, str]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise FileNotFoundError(f"download_http_{resp.status}")
            data = await resp.content.read(limit + 1)
            if len(data) > limit:
                raise ValueError("media_too_large")
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            return data, ctype or "application/octet-stream"


def _serialize_message(
    msg: Any,
    *,
    me_id: int | None,
    contacts: dict[int, Any] | None = None,
) -> dict[str, Any]:
    sender = getattr(msg, "sender", None)
    try:
        sender_id = int(sender) if sender is not None else None
    except (TypeError, ValueError):
        sender_id = None
    out = bool(me_id is not None and sender_id is not None and sender_id == me_id)
    text = _message_text(msg)
    attaches = getattr(msg, "attaches", None) or []
    mid = getattr(msg, "id", None)
    from_name = "Вы" if out else None
    if not from_name and sender_id is not None and contacts:
        from_name = _contact_name(contacts.get(sender_id))
    if not from_name:
        from_name = f"MAX {sender_id}" if sender_id is not None else None

    first_kind = None
    media_url = None
    file_name = None
    file_id = None
    video_id = None
    for att in attaches:
        kind = _normalize_userbot_kind(att)
        if not kind:
            continue
        first_kind = kind
        media_url = _attach_media_url(att)
        file_name = _attach_file_name(att)
        raw_fid = getattr(att, "file_id", None)
        raw_vid = getattr(att, "video_id", None)
        try:
            file_id = int(raw_fid) if raw_fid is not None else None
        except (TypeError, ValueError):
            file_id = None
        try:
            video_id = int(raw_vid) if raw_vid is not None else None
        except (TypeError, ValueError):
            video_id = None
        break

    preview = text[:160] if text else (
        _USERBOT_PREVIEW.get(first_kind or "", "Медиа") if first_kind else ""
    )

    return {
        "id": str(mid) if mid is not None else str(getattr(msg, "time", "") or ""),
        "date": _ts_iso(getattr(msg, "time", None)),
        "out": out,
        "text": text,
        "preview": preview,
        "has_media": bool(first_kind),
        "media_kind": first_kind,
        "media_url": media_url,
        "file_name": file_name,
        "file_id": file_id,
        "video_id": video_id,
        "from_id": sender_id,
        "from_name": from_name,
        "reply_to": None,
    }


def _serialize_chat(
    chat: Any,
    *,
    me_id: int | None,
    contacts: dict[int, Any],
) -> dict[str, Any]:
    kind = _chat_kind(chat)
    peer_uid = _peer_user_id(chat, me_id) if kind == "user" else None
    contact = contacts.get(peer_uid) if peer_uid is not None else None
    contact_name = _contact_name(contact)
    chat_title = (getattr(chat, "title", None) or "").strip()
    # В личных диалогах имя из профиля MAX важнее пустого/служебного title
    if kind == "user":
        title = (
            contact_name
            or chat_title
            or (f"MAX {peer_uid}" if peer_uid is not None else None)
            or f"Чат {getattr(chat, 'id', '?')}"
        )
    else:
        title = (
            chat_title
            or contact_name
            or (f"MAX {peer_uid}" if peer_uid is not None else None)
            or f"Чат {getattr(chat, 'id', '?')}"
        )
    phone = None
    if contact is not None:
        raw_phone = getattr(contact, "phone", None)
        if raw_phone:
            phone = str(raw_phone)
            if phone.isdigit() and not phone.startswith("+"):
                phone = f"+{phone}"

    last = getattr(chat, "last_message", None)
    last_text = _message_text(last) if last else ""
    last_out = False
    if last is not None and me_id is not None:
        try:
            last_out = int(getattr(last, "sender", -1) or -1) == int(me_id)
        except (TypeError, ValueError):
            last_out = False

    chat_id = int(getattr(chat, "id"))
    avatar_url = _chat_avatar_url(chat, contact)
    description = None
    if contact is not None:
        raw_desc = getattr(contact, "description", None)
        if raw_desc:
            description = str(raw_desc).strip() or None
    if not description:
        raw_desc = getattr(chat, "description", None)
        if raw_desc:
            description = str(raw_desc).strip() or None

    return {
        "id": str(chat_id),
        "peer_id": chat_id,
        "title": title,
        "kind": kind,
        "username": None,
        "phone": phone,
        "avatar_url": avatar_url,
        "description": description,
        "tg_user_id": None,
        "max_user_id": peer_uid,
        "unread": int(getattr(chat, "new_messages", 0) or 0),
        "pinned": False,
        "is_user": kind == "user",
        "is_group": kind == "group",
        "is_channel": kind == "channel",
        "date": _ts_iso(
            getattr(chat, "last_event_time", None)
            or (getattr(last, "time", None) if last else None)
        ),
        "last_message": last_text[:160] if last_text else "",
        "last_out": last_out,
    }


async def _load_all_chats(client: Any) -> list[Any]:
    by_id: dict[int, Any] = {}
    for chat in client.chats or []:
        try:
            by_id[int(chat.id)] = chat
        except (TypeError, ValueError, AttributeError):
            continue

    marker = int(time.time() * 1000)
    for _ in range(25):
        try:
            batch = await client.fetch_chats(marker=marker)
        except Exception:
            logger.debug("fetch_chats failed marker=%s", marker, exc_info=True)
            break
        if not batch:
            break
        oldest = marker
        grew = False
        for chat in batch:
            try:
                cid = int(chat.id)
            except (TypeError, ValueError, AttributeError):
                continue
            if cid not in by_id:
                grew = True
            by_id[cid] = chat
            ev = int(getattr(chat, "last_event_time", 0) or 0)
            if ev and ev < oldest:
                oldest = ev
        if not grew and oldest >= marker:
            break
        if oldest >= marker:
            break
        marker = max(0, oldest - 1)
        if len(by_id) >= 600:
            break

    return list(by_id.values())


async def list_dialogs(
    session_file: str,
    *,
    phone: str = "",
    account_id: int | None = None,
    limit: int = 80,
    query: str = "",
    only_users: bool = False,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 80), 200))
    q = (query or "").strip().lower()

    async with max_session(
        session_file,
        phone=phone,
        account_id=account_id,
        force_full_sync=True,
    ) as client:
        me_id = _user_id(client.me)
        contacts = _contact_map(client)
        chats = await _load_all_chats(client)
        peer_uids: list[int] = []
        for chat in chats:
            if _chat_kind(chat) != "user":
                continue
            uid = _peer_user_id(chat, me_id)
            if uid is not None:
                peer_uids.append(uid)
        await _enrich_users(client, peer_uids, contacts)
        rows = [
            _serialize_chat(c, me_id=me_id, contacts=contacts)
            for c in chats
        ]
        rows.sort(key=lambda r: r.get("date") or "", reverse=True)

        items: list[dict[str, Any]] = []
        for row in rows:
            if only_users and row.get("kind") != "user":
                continue
            if q:
                hay = " ".join(
                    filter(
                        None,
                        [
                            row.get("title"),
                            row.get("phone"),
                            row.get("last_message"),
                            str(row.get("max_user_id") or ""),
                        ],
                    )
                ).lower()
                if q not in hay:
                    continue
            items.append(row)
            if len(items) >= limit:
                break
        return items


async def resolve_peer_profile(
    session_file: str,
    peer_id: int,
    *,
    phone: str = "",
    account_id: int | None = None,
) -> dict[str, Any]:
    async with max_session(session_file, phone=phone, account_id=account_id) as client:
        me_id = _user_id(client.me)
        contacts = _contact_map(client)
        try:
            chat = await client.get_chat(int(peer_id))
        except Exception:
            chat = None
            for c in client.chats or []:
                try:
                    if int(c.id) == int(peer_id):
                        chat = c
                        break
                except (TypeError, ValueError, AttributeError):
                    continue
        if chat is None:
            raise RuntimeError("Чат не найден")
        uid = _peer_user_id(chat, me_id) if _chat_kind(chat) == "user" else None
        if uid is not None:
            await _enrich_users(client, [uid], contacts)
        return _serialize_chat(chat, me_id=me_id, contacts=contacts)


async def get_dialog_messages(
    session_file: str,
    peer_id: int,
    *,
    phone: str = "",
    account_id: int | None = None,
    limit: int = 50,
    before_ts: int | None = None,
    mark_read: bool = True,
) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 100))
    async with max_session(session_file, phone=phone, account_id=account_id) as client:
        me_id = _user_id(client.me)
        contacts = _contact_map(client)
        chat = await client.get_chat(int(peer_id))
        uid = _peer_user_id(chat, me_id) if _chat_kind(chat) == "user" else None
        if uid is not None:
            await _enrich_users(client, [uid], contacts)
        peer = _serialize_chat(chat, me_id=me_id, contacts=contacts)

        from_time = before_ts
        if from_time is None:
            from_time = int(time.time() * 1000)

        history = await client.fetch_history(
            chat_id=int(peer_id),
            backward=limit,
            forward=0,
            from_time=int(from_time),
        )
        msg_list = list(history or [])
        # Обычно от новых к старым — для UI старые сверху
        msg_list.sort(key=lambda m: int(getattr(m, "time", 0) or 0))
        rows = [_serialize_message(m, me_id=me_id, contacts=contacts) for m in msg_list]

        if mark_read and msg_list and not before_ts:
            try:
                last = msg_list[-1]
                mid = getattr(last, "id", None)
                if mid is not None:
                    await client.read_message(message_id=mid, chat_id=int(peer_id))
            except Exception:
                logger.debug("MAX mark read failed for %s", peer_id, exc_info=True)

        return {
            "peer": {
                "id": str(peer_id),
                "peer_id": int(peer_id),
                "title": peer.get("title"),
                "kind": peer.get("kind"),
                "username": peer.get("username"),
                "phone": peer.get("phone"),
                "avatar_url": peer.get("avatar_url"),
                "description": peer.get("description"),
                "max_user_id": peer.get("max_user_id"),
                "tg_user_id": None,
            },
            "messages": rows,
            "has_more": len(msg_list) >= limit,
            "history_unavailable": False,
        }


async def send_dialog_message(
    session_file: str,
    peer_id: int,
    text: str,
    *,
    phone: str = "",
    account_id: int | None = None,
) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("Пустое сообщение")
    if len(body) > 4000:
        raise ValueError("Сообщение длиннее 4000 символов")

    async with max_session(session_file, phone=phone, account_id=account_id) as client:
        me_id = _user_id(client.me)
        contacts = _contact_map(client)
        sent = await client.send_message(chat_id=int(peer_id), text=body)
        if sent is None:
            return {
                "id": f"tmp:{int(time.time() * 1000)}",
                "date": datetime.now(tz=timezone.utc).isoformat(),
                "out": True,
                "text": body,
                "preview": body[:120],
                "has_media": False,
                "media_kind": None,
                "media_url": None,
                "file_name": None,
                "from_id": me_id,
                "from_name": "Вы",
                "reply_to": None,
            }
        return _serialize_message(sent, me_id=me_id, contacts=contacts)


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_FILES = 10
MAX_INLINE_MEDIA_BYTES = 20 * 1024 * 1024


def _pick_attach_builder(filename: str, mime: str, *, force_document: bool):
    from pymax import File, Photo, Video

    if force_document:
        return File
    name = (filename or "").lower()
    mime_l = (mime or "").lower()
    if mime_l.startswith("image/") or name.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
    ):
        return Photo
    if mime_l.startswith("video/") or name.endswith((".mp4", ".mov", ".webm", ".mkv")):
        return Video
    return File


async def download_message_media(
    session_file: str,
    peer_id: int,
    message_id: str | int,
    *,
    phone: str = "",
    account_id: int | None = None,
) -> tuple[bytes, str, str | None]:
    mid_raw = str(message_id).strip()
    try:
        mid_int = int(mid_raw)
    except (TypeError, ValueError):
        mid_int = None

    async with max_session(session_file, phone=phone, account_id=account_id) as client:
        msg = None
        if mid_int is not None and hasattr(client, "get_messages"):
            try:
                found = await client.get_messages(int(peer_id), [mid_int])
                if found:
                    msg = found[0]
            except Exception:
                logger.debug("get_messages by id failed", exc_info=True)
        if msg is None:
            # fallback: scan recent history
            history = await client.fetch_history(
                chat_id=int(peer_id),
                backward=80,
                forward=0,
                from_time=int(time.time() * 1000),
            )
            for candidate in history or []:
                if str(getattr(candidate, "id", "")) == mid_raw:
                    msg = candidate
                    break
        if msg is None:
            raise FileNotFoundError("message_not_found")

        attaches = getattr(msg, "attaches", None) or []
        if not attaches:
            raise FileNotFoundError("no_media")

        att = None
        kind = None
        for item in attaches:
            kind = _normalize_userbot_kind(item)
            if kind:
                att = item
                break
        if att is None:
            raise FileNotFoundError("no_media")

        filename = _attach_file_name(att)
        url = _attach_media_url(att)

        # Resolve temporary URLs for file/video
        if kind == "document" and hasattr(client, "get_file_by_id"):
            file_id = getattr(att, "file_id", None)
            if file_id is not None:
                try:
                    req = await client.get_file_by_id(
                        int(peer_id),
                        getattr(msg, "id", mid_raw),
                        int(file_id),
                    )
                    if req is not None and getattr(req, "url", None):
                        url = str(req.url)
                except Exception:
                    logger.debug("get_file_by_id failed", exc_info=True)
        if kind == "video" and hasattr(client, "get_video_by_id"):
            video_id = getattr(att, "video_id", None)
            if video_id is not None:
                try:
                    req = await client.get_video_by_id(
                        int(peer_id),
                        getattr(msg, "id", mid_raw),
                        int(video_id),
                    )
                    if req is not None and getattr(req, "url", None):
                        url = str(req.url)
                except Exception:
                    logger.debug("get_video_by_id failed", exc_info=True)

        if not url:
            raise FileNotFoundError("no_media_url")

        raw, ctype = await _http_get_bytes(url, limit=MAX_INLINE_MEDIA_BYTES)
        mime = _guess_mime(raw, ctype or "application/octet-stream")
        if kind in ("photo", "sticker") and mime.startswith("application/"):
            mime = "image/jpeg"
        if kind == "video" and mime.startswith("application/"):
            mime = "video/mp4"
        if kind in ("audio", "voice") and mime.startswith("application/"):
            mime = "audio/mpeg"
        return raw, mime, filename


async def send_dialog_media(
    session_file: str,
    peer_id: int,
    files: list[dict[str, Any]],
    *,
    caption: str = "",
    force_document: bool = False,
    phone: str = "",
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    if not files:
        raise ValueError("Нет файлов")
    if len(files) > MAX_UPLOAD_FILES:
        raise ValueError(f"Максимум {MAX_UPLOAD_FILES} файлов за раз")
    caption = (caption or "").strip()
    if len(caption) > 4000:
        raise ValueError("Подпись длиннее 4000 символов")

    import tempfile

    paths: list[str] = []
    builders: list[Any] = []
    try:
        for item in files:
            raw = item.get("data") or b""
            if not isinstance(raw, (bytes, bytearray)):
                raise ValueError("Некорректные данные файла")
            raw_b = bytes(raw)
            if not raw_b:
                raise ValueError("Пустой файл")
            if len(raw_b) > MAX_UPLOAD_BYTES:
                raise ValueError("Файл больше 50 МБ")
            filename = str(item.get("filename") or "file")
            mime = str(item.get("mime") or "application/octet-stream")
            suffix = Path(filename).suffix[:20] or ""
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            try:
                tmp.write(raw_b)
                tmp.flush()
            finally:
                tmp.close()
            paths.append(tmp.name)
            cls = _pick_attach_builder(filename, mime, force_document=force_document)
            builders.append(cls(path=tmp.name, name=filename))

        async with max_session(session_file, phone=phone, account_id=account_id) as client:
            me_id = _user_id(client.me)
            contacts = _contact_map(client)
            sent = await client.send_message(
                chat_id=int(peer_id),
                text=caption or "",
                attachments=builders,
            )
            if sent is None:
                return [
                    {
                        "id": f"tmp:{int(time.time() * 1000)}",
                        "date": datetime.now(tz=timezone.utc).isoformat(),
                        "out": True,
                        "text": caption,
                        "preview": caption[:120] if caption else "Медиа",
                        "has_media": True,
                        "media_kind": "media",
                        "media_url": None,
                        "file_name": files[0].get("filename") if files else None,
                        "from_id": me_id,
                        "from_name": "Вы",
                        "reply_to": None,
                    }
                ]
            if isinstance(sent, list):
                return [
                    _serialize_message(m, me_id=me_id, contacts=contacts)
                    for m in sent
                    if m is not None
                ]
            return [_serialize_message(sent, me_id=me_id, contacts=contacts)]
    finally:
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass


async def create_or_open_dialog(
    session_file: str,
    *,
    phone: str = "",
    account_phone: str = "",
    account_id: int | None = None,
    name: str = "",
    first_message: str = "",
) -> dict[str, Any]:
    phone_raw = (phone or "").strip()
    if not phone_raw:
        raise ValueError("Укажите телефон")

    async with max_session(
        session_file,
        phone=account_phone,
        account_id=account_id,
    ) as client:
        me_id = _user_id(client.me)
        chat_id, uid, err = await _resolve_chat_id(
            client,
            phone=phone_raw,
            name=name,
            max_user_id=None,
        )
        if err or chat_id is None:
            raise RuntimeError(err or "Не удалось открыть чат")

        contacts = _contact_map(client)
        try:
            chat = await client.get_chat(int(chat_id))
            peer = _serialize_chat(chat, me_id=me_id, contacts=contacts)
        except Exception:
            peer = {
                "id": str(chat_id),
                "peer_id": int(chat_id),
                "title": name or phone_raw,
                "kind": "user",
                "phone": _normalize_phone(phone_raw),
                "max_user_id": uid,
            }

        sent = None
        body = (first_message or "").strip()
        if body:
            msg = await client.send_message(chat_id=int(chat_id), text=body)
            sent = _serialize_message(msg, me_id=me_id) if msg else None

        if uid is not None:
            try:
                from mailing_db import set_customer_max_by_phone

                await set_customer_max_by_phone(_normalize_phone(phone_raw), int(uid))
            except Exception:
                logger.debug("auto-bind after create dialog failed", exc_info=True)

        return {
            "peer": peer,
            "message": sent,
            "max_user_id": uid,
            "label": _user_label(client.me),
        }
