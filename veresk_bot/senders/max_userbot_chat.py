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
            return str(name)
        first_n = getattr(first, "first_name", None) or ""
        last_n = getattr(first, "last_name", None) or ""
        joined = " ".join(x for x in [first_n, last_n] if x).strip()
        if joined:
            return joined
    for attr in ("name", "first_name"):
        val = getattr(user, attr, None)
        if val:
            return str(val)
    return None


def _contact_map(client: Any) -> dict[int, Any]:
    out: dict[int, Any] = {}
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
    if text:
        return text
    attaches = getattr(msg, "attaches", None) or []
    if attaches:
        kinds = []
        for a in attaches:
            kind = getattr(a, "type", None) or type(a).__name__
            kinds.append(str(getattr(kind, "value", kind)))
        return ", ".join(kinds) if kinds else "Медиа"
    return ""


def _serialize_message(msg: Any, *, me_id: int | None) -> dict[str, Any]:
    sender = getattr(msg, "sender", None)
    try:
        sender_id = int(sender) if sender is not None else None
    except (TypeError, ValueError):
        sender_id = None
    out = bool(me_id is not None and sender_id is not None and sender_id == me_id)
    text = _message_text(msg)
    attaches = getattr(msg, "attaches", None) or []
    mid = getattr(msg, "id", None)
    return {
        "id": str(mid) if mid is not None else str(getattr(msg, "time", "") or ""),
        "date": _ts_iso(getattr(msg, "time", None)),
        "out": out,
        "text": text,
        "preview": text[:120] if text else ("Медиа" if attaches else ""),
        "has_media": bool(attaches),
        "media_kind": (
            str(getattr(getattr(attaches[0], "type", None), "value", getattr(attaches[0], "type", None)))
            if attaches
            else None
        ),
        "file_name": None,
        "from_id": sender_id,
        "from_name": "Вы" if out else (f"MAX {sender_id}" if sender_id else None),
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
    title = (
        (getattr(chat, "title", None) or "").strip()
        or _contact_name(contact)
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
    return {
        "id": str(chat_id),
        "peer_id": chat_id,
        "title": title,
        "kind": kind,
        "username": None,
        "phone": phone,
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
        rows = [_serialize_message(m, me_id=me_id) for m in msg_list]

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
                "username": None,
                "phone": peer.get("phone"),
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
                "file_name": None,
                "from_id": me_id,
                "from_name": "Вы",
                "reply_to": None,
            }
        return _serialize_message(sent, me_id=me_id)


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
