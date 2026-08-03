"""
Адаптер MAX Bot API для раздела «Чаты» в админке.

Список диалогов — из локального индекса (max_dialogs ∪ max_profiles),
история — GET /messages, отправка — POST /messages.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from max_bot.api import MaxBotAPI
from max_bot.storage import (
    get_dialog,
    get_max_profile,
    list_dialogs_for_inbox,
    upsert_dialog,
)
from senders.max_bot import get_max_bot_token, is_max_configured

logger = logging.getLogger(__name__)


def parse_peer(peer: str | int) -> dict[str, int | None]:
    """
    peer: "chat:123" | "user:456" | "123" (как chat_id).
    Возвращает {chat_id, max_user_id}.
    """
    raw = str(peer or "").strip()
    if raw.startswith("user:"):
        try:
            return {"chat_id": None, "max_user_id": int(raw.split(":", 1)[1])}
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_peer") from exc
    if raw.startswith("chat:"):
        try:
            return {"chat_id": int(raw.split(":", 1)[1]), "max_user_id": None}
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_peer") from exc
    try:
        return {"chat_id": int(raw), "max_user_id": None}
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_peer") from exc


def peer_key(*, chat_id: int | None = None, max_user_id: int | None = None) -> str:
    if chat_id is not None:
        return f"chat:{int(chat_id)}"
    if max_user_id is not None:
        return f"user:{int(max_user_id)}"
    raise ValueError("peer_required")


def _ts_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    try:
        ts = int(value)
        # MAX timestamps обычно в миллисекундах
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value) if value else None


def _user_title(user: dict[str, Any] | None) -> str:
    if not user:
        return "Без имени"
    name = " ".join(
        filter(
            None,
            [
                user.get("first_name"),
                user.get("last_name"),
                user.get("name"),
            ],
        )
    ).strip()
    if name:
        return name
    if user.get("username"):
        return f"@{user['username']}"
    uid = user.get("user_id")
    return f"MAX {uid}" if uid is not None else "Без имени"


def _message_text(msg: dict[str, Any]) -> str:
    body = msg.get("body") or {}
    text = (body.get("text") or "").strip()
    if text:
        return text
    attachments = body.get("attachments") or []
    if attachments:
        kinds = [str(a.get("type") or "file") for a in attachments]
        return ", ".join(kinds)
    return ""


def _bot_user_id(me: dict[str, Any] | None) -> int | None:
    if not me:
        return None
    for key in ("user_id", "userId", "id"):
        raw = me.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    return None


def serialize_max_message(
    msg: dict[str, Any],
    *,
    bot_id: int | None = None,
) -> dict[str, Any]:
    sender = msg.get("sender") or {}
    sender_id = sender.get("user_id")
    try:
        sender_id_int = int(sender_id) if sender_id is not None else None
    except (TypeError, ValueError):
        sender_id_int = None

    out = False
    if bot_id is not None and sender_id_int is not None:
        out = sender_id_int == bot_id
    elif sender.get("is_bot"):
        out = True

    body = msg.get("body") or {}
    mid = body.get("mid") or msg.get("id") or msg.get("message_id")
    text = _message_text(msg)
    attachments = body.get("attachments") or []
    has_media = bool(attachments) and not text

    return {
        "id": str(mid) if mid is not None else str(msg.get("timestamp") or ""),
        "date": _ts_iso(msg.get("timestamp")),
        "out": out,
        "text": text,
        "preview": text[:120] if text else ("Медиа" if has_media else ""),
        "has_media": bool(attachments),
        "media_kind": (attachments[0].get("type") if attachments else None),
        "file_name": None,
        "from_id": sender_id_int,
        "from_name": "Вы" if out else _user_title(sender),
        "reply_to": None,
    }


async def _api() -> MaxBotAPI:
    token = get_max_bot_token()
    if not token:
        raise RuntimeError("MAX-бот не подключён")
    return MaxBotAPI(token)


async def list_dialogs(*, query: str = "", limit: int = 80) -> list[dict[str, Any]]:
    return await list_dialogs_for_inbox(query=query, limit=limit)


async def resolve_peer_info(peer: str) -> dict[str, Any]:
    parsed = parse_peer(peer)
    chat_id = parsed["chat_id"]
    max_user_id = parsed["max_user_id"]

    dialog = await get_dialog(chat_id=chat_id, max_user_id=max_user_id)
    if dialog:
        if chat_id is None and dialog.get("chat_id") is not None:
            chat_id = int(dialog["chat_id"])
        if max_user_id is None and dialog.get("max_user_id") is not None:
            max_user_id = int(dialog["max_user_id"])

    profile = None
    if max_user_id is not None:
        profile = await get_max_profile(int(max_user_id))

    title = (
        (dialog or {}).get("name")
        or (profile or {}).get("name")
        or (f"MAX {max_user_id}" if max_user_id is not None else f"Чат {chat_id}")
    )
    phone = (dialog or {}).get("phone") or (profile or {}).get("phone")

    return {
        "id": peer_key(chat_id=chat_id, max_user_id=max_user_id),
        "peer_id": peer_key(chat_id=chat_id, max_user_id=max_user_id),
        "chat_id": chat_id,
        "max_user_id": max_user_id,
        "title": title,
        "kind": "user",
        "username": None,
        "phone": phone,
        "tg_user_id": None,
    }


async def get_dialog_messages(
    peer: str,
    *,
    limit: int = 50,
    before_ts: int | None = None,
) -> dict[str, Any]:
    peer_info = await resolve_peer_info(peer)
    chat_id = peer_info.get("chat_id")
    limit = max(1, min(int(limit or 50), 100))

    if chat_id is None:
        return {
            "peer": peer_info,
            "messages": [],
            "has_more": False,
            "history_unavailable": True,
            "hint": "История появится после следующего сообщения клиента в MAX",
        }

    api = await _api()
    bot_id = None
    try:
        me = await api.get_me()
        bot_id = _bot_user_id(me)
        data = await api.get_messages(
            chat_id=int(chat_id),
            count=limit,
            to_ts=before_ts,
        )
    finally:
        await api.close()

    raw_msgs = data.get("messages") or []
    # API отдаёт от новых к старым — разворачиваем для UI (старые сверху)
    rows = [
        serialize_max_message(m, bot_id=bot_id)
        for m in reversed(list(raw_msgs))
    ]

    # Обогатим peer из dialog_with_user, если есть
    try:
        api2 = await _api()
        try:
            chat = await api2.get_chat(int(chat_id))
            dialog_user = chat.get("dialog_with_user") or {}
            if dialog_user:
                peer_info["title"] = _user_title(dialog_user) or peer_info["title"]
                if dialog_user.get("user_id") is not None:
                    peer_info["max_user_id"] = int(dialog_user["user_id"])
        except Exception:
            logger.debug("get_chat failed for %s", chat_id, exc_info=True)
        finally:
            await api2.close()
    except Exception:
        pass

    return {
        "peer": peer_info,
        "messages": rows,
        "has_more": len(raw_msgs) >= limit,
        "history_unavailable": False,
    }


async def send_dialog_message(peer: str, text: str) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise ValueError("Пустое сообщение")
    if len(body) > 4000:
        raise ValueError("Сообщение длиннее 4000 символов")

    peer_info = await resolve_peer_info(peer)
    chat_id = peer_info.get("chat_id")
    user_id = peer_info.get("max_user_id")
    if chat_id is None and user_id is None:
        raise ValueError("Неизвестный собеседник")

    api = await _api()
    bot_id = None
    try:
        me = await api.get_me()
        bot_id = _bot_user_id(me)
        # Предпочитаем user_id для личных диалогов (как в рассылках)
        if user_id is not None:
            result = await api.send_message(user_id=int(user_id), text=body, markdown=False)
        else:
            result = await api.send_message(chat_id=int(chat_id), text=body, markdown=False)
    finally:
        await api.close()

    # Ответ API может быть {message: {...}} или самим сообщением
    msg = result.get("message") if isinstance(result.get("message"), dict) else result
    if not isinstance(msg, dict):
        msg = {
            "timestamp": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            "body": {"text": body},
            "sender": {"user_id": bot_id, "is_bot": True},
        }

    # После отправки по user_id API часто возвращает chat_id получателя
    if chat_id is None:
        recipient = msg.get("recipient") or {}
        raw_chat = recipient.get("chat_id")
        if raw_chat is not None:
            try:
                chat_id = int(raw_chat)
                peer_info["chat_id"] = chat_id
                peer_info["id"] = peer_key(chat_id=chat_id, max_user_id=user_id)
                peer_info["peer_id"] = peer_info["id"]
            except (TypeError, ValueError):
                pass

    serialized = serialize_max_message(msg, bot_id=bot_id)
    if not serialized.get("text"):
        serialized["text"] = body
        serialized["preview"] = body[:120]
    serialized["out"] = True

    preview = body.replace("\n", " ")[:160]
    await upsert_dialog(
        chat_id=int(chat_id) if chat_id is not None else None,
        max_user_id=int(user_id) if user_id is not None else None,
        name=peer_info.get("title"),
        phone=peer_info.get("phone"),
        last_text=preview,
        last_at=_ts_iso(msg.get("timestamp")) or datetime.now().isoformat(timespec="seconds"),
        last_out=True,
    )
    serialized["peer_id"] = peer_info.get("peer_id")
    return serialized


async def client_lookup_for_peer(peer: str) -> dict[str, Any]:
    """Статус клиента CRM для MAX-диалога."""
    from mailing_db import get_customer_by_phone

    peer_info = await resolve_peer_info(peer)
    customer = None
    max_user_id = peer_info.get("max_user_id")
    phone = peer_info.get("phone") or ""

    if max_user_id is not None:
        try:
            from mailing_db import get_customer_by_max_user_id

            customer = await get_customer_by_max_user_id(int(max_user_id))
        except Exception:
            logger.debug("get_customer_by_max_user_id failed", exc_info=True)
            customer = None

    if not customer and phone:
        customer = await get_customer_by_phone(str(phone))

    phone_ok = bool(re.sub(r"\D", "", str(phone or "")))
    if customer:
        status = "in_base"
        label = "Клиент уже в базе"
        hint = f"{customer.get('name') or 'Клиент'} · {customer.get('phone') or 'без телефона'}"
        can_create = False
        need_phone = False
    else:
        status = "missing"
        label = "Клиента ещё нет в базе"
        if phone_ok:
            hint = "Есть телефон из анкеты MAX — можно найти в клиентах"
            can_create = False
            need_phone = False
        else:
            hint = "Клиент появится после заполнения анкеты в MAX"
            can_create = False
            need_phone = True

    return {
        "status": status,
        "label": label,
        "hint": hint,
        "can_create": can_create,
        "need_phone": need_phone,
        "in_base": bool(customer),
        "peer": peer_info,
        "customer": customer,
        "configured": is_max_configured(),
    }
