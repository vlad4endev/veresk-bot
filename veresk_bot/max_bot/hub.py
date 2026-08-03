"""
Realtime-хаб MAX-чатов для админки (SSE).

Живёт в процессе bot/webapp: webhook Max и internal notify из max_bot
публикуют события; открытые вкладки админки получают их через EventSource.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from max_bot.storage import (
    extract_chat_id_from_update,
    extract_user_from_update,
    upsert_dialog,
)
from senders.max_chat import peer_key, serialize_max_message

logger = logging.getLogger(__name__)

# Типы, которые нужны для инбокса + анкеты
WEBHOOK_UPDATE_TYPES = [
    "message_created",
    "message_callback",
    "bot_started",
    "bot_added",
    "bot_removed",
    "bot_stopped",
    "dialog_removed",
    "chat_title_changed",
]


class MaxChatHub:
    """In-memory pub/sub для Server-Sent Events."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        async with self._lock:
            dead: list[asyncio.Queue[str]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Медленный клиент — выкидываем, переподключится
                    dead.append(q)
            for q in dead:
                self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


hub = MaxChatHub()


def _message_preview_from_update(update: dict[str, Any]) -> str | None:
    update_type = update.get("update_type")
    if update_type == "bot_started":
        return "Начал диалог с ботом"
    if update_type == "message_callback":
        return "Нажал кнопку"
    if update_type != "message_created":
        return None
    message = update.get("message") or {}
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    if text:
        return text.replace("\n", " ")[:160]
    if body.get("attachments"):
        return "Медиа"
    return "Сообщение"


def _serialize_inbound_message(update: dict[str, Any]) -> dict[str, Any] | None:
    if update.get("update_type") != "message_created":
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("sender") or {}
    if sender.get("is_bot"):
        # Исходящие бота тоже могут прийти — пометим out
        return serialize_max_message(message, bot_id=sender.get("user_id"))
    return serialize_max_message(message, bot_id=None)


async def index_update_for_inbox(update: dict[str, Any]) -> dict[str, Any] | None:
    """Обновить max_dialogs и вернуть краткий dialog-snapshot для UI."""
    chat_id = extract_chat_id_from_update(update)
    user_id, name = extract_user_from_update(update)
    preview = _message_preview_from_update(update)
    last_out = False
    if update.get("update_type") == "message_created":
        sender = ((update.get("message") or {}).get("sender") or {})
        last_out = bool(sender.get("is_bot"))

    if chat_id is None and user_id is None:
        return None

    try:
        await upsert_dialog(
            chat_id=chat_id,
            max_user_id=user_id,
            name=name,
            last_text=preview,
            last_out=last_out if preview is not None else None,
            last_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception:
        logger.exception("index_update_for_inbox failed")
        return None

    try:
        peer = peer_key(chat_id=chat_id, max_user_id=user_id)
    except ValueError:
        return None

    return {
        "peer_id": peer,
        "chat_id": chat_id,
        "max_user_id": user_id,
        "title": name or (f"MAX {user_id}" if user_id is not None else f"Чат {chat_id}"),
        "last_message": preview or "",
        "last_out": last_out,
        "date": datetime.now().isoformat(timespec="seconds"),
    }


async def publish_update_event(update: dict[str, Any]) -> dict[str, Any]:
    """Индексация + SSE-событие. Не трогает SurveyBot."""
    dialog = await index_update_for_inbox(update)
    message = _serialize_inbound_message(update)
    event = {
        "type": "message" if message else "dialog_upsert",
        "update_type": update.get("update_type"),
        "peer_id": (dialog or {}).get("peer_id"),
        "chat_id": (dialog or {}).get("chat_id"),
        "dialog": dialog,
        "message": message,
    }
    await hub.publish(event)
    return event


async def publish_outbound_message(
    *,
    peer_id: str,
    message: dict[str, Any],
    dialog: dict[str, Any] | None = None,
) -> None:
    await hub.publish(
        {
            "type": "message",
            "update_type": "message_created",
            "peer_id": peer_id,
            "chat_id": (dialog or {}).get("chat_id"),
            "dialog": dialog,
            "message": message,
        }
    )
