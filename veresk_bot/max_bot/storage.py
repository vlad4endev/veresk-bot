"""
Хранилище анкет и индекса диалогов MAX (SQLite, тот же файл veresk.db).

max_profiles — анкеты (user_id MAX ≠ tg_id).
max_dialogs  — индекс личных чатов для инбокса админки
               (GET /chats в MAX API больше недоступен).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS max_profiles (
    max_user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    budget TEXT NOT NULL,
    source TEXT NOT NULL,
    events_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS max_dialogs (
    chat_id INTEGER PRIMARY KEY,
    max_user_id INTEGER,
    name TEXT,
    phone TEXT,
    last_text TEXT,
    last_at TEXT,
    last_out INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_max_dialogs_user ON max_dialogs(max_user_id);
CREATE INDEX IF NOT EXISTS idx_max_dialogs_last ON max_dialogs(last_at);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


async def _run_db(fn: Callable[[], T]) -> T:
    return await asyncio.to_thread(fn)


async def init_max_db() -> None:
    def _init() -> None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with _connect() as db:
            db.executescript(_SCHEMA)
            db.commit()

    await _run_db(_init)
    logger.info("База анкет MAX: %s", DATABASE_PATH)


async def save_max_profile(max_user_id: int, profile: dict[str, Any]) -> None:
    name = str(profile.get("name", "")).strip()
    phone = str(profile.get("phone", "")).strip()
    budget = str(profile.get("budget", "")).strip()
    source = str(profile.get("source", "")).strip()
    events = profile.get("events") or []
    if not name or not phone:
        logger.warning(
            "Пропуск сохранения анкеты MAX: нет имени или телефона (user_id=%s)",
            max_user_id,
        )
        return

    now = _now()
    events_json = json.dumps(events, ensure_ascii=False)

    def _save() -> None:
        with _connect() as db:
            db.execute(
                """
                INSERT INTO max_profiles (
                    max_user_id, name, phone, budget, source, events_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(max_user_id) DO UPDATE SET
                    name = excluded.name,
                    phone = excluded.phone,
                    budget = excluded.budget,
                    source = excluded.source,
                    events_json = excluded.events_json,
                    updated_at = excluded.updated_at
                """,
                (max_user_id, name, phone, budget, source, events_json, now, now),
            )
            # Обновим имя/телефон в индексе диалогов, если уже есть
            db.execute(
                """
                UPDATE max_dialogs
                SET name = ?, phone = ?, updated_at = ?
                WHERE max_user_id = ?
                """,
                (name, phone, now, int(max_user_id)),
            )
            db.commit()

    await _run_db(_save)
    logger.info("Анкета MAX-клиента %s сохранена (%s событий)", max_user_id, len(events))


def _profile_lookup(db: sqlite3.Connection, max_user_id: int | None) -> dict[str, Any]:
    if max_user_id is None:
        return {}
    row = db.execute(
        "SELECT name, phone FROM max_profiles WHERE max_user_id = ?",
        (int(max_user_id),),
    ).fetchone()
    return dict(row) if row else {}


async def upsert_dialog(
    *,
    chat_id: int | None = None,
    max_user_id: int | None = None,
    name: str | None = None,
    phone: str | None = None,
    last_text: str | None = None,
    last_at: str | None = None,
    last_out: bool | None = None,
) -> None:
    """Создать/обновить диалог. Нужен chat_id или max_user_id."""
    if chat_id is None and max_user_id is None:
        return
    now = last_at or _now()

    def _upsert() -> None:
        with _connect() as db:
            existing: sqlite3.Row | None = None
            if chat_id is not None:
                existing = db.execute(
                    "SELECT * FROM max_dialogs WHERE chat_id = ?",
                    (int(chat_id),),
                ).fetchone()
            if existing is None and max_user_id is not None:
                existing = db.execute(
                    "SELECT * FROM max_dialogs WHERE max_user_id = ? ORDER BY updated_at DESC LIMIT 1",
                    (int(max_user_id),),
                ).fetchone()

            profile = _profile_lookup(
                db, max_user_id if max_user_id is not None else (existing["max_user_id"] if existing else None)
            )
            resolved_name = (name or "").strip() or (existing["name"] if existing else None) or profile.get("name") or ""
            resolved_phone = (phone or "").strip() or (existing["phone"] if existing else None) or profile.get("phone") or ""
            resolved_user = (
                int(max_user_id)
                if max_user_id is not None
                else (int(existing["max_user_id"]) if existing and existing["max_user_id"] is not None else None)
            )
            resolved_chat = (
                int(chat_id)
                if chat_id is not None
                else (int(existing["chat_id"]) if existing else None)
            )
            if resolved_chat is None:
                # Без chat_id хранить нельзя (PK). Ждём первого inbound с chat_id.
                return

            new_last_text = (
                last_text
                if last_text is not None
                else (existing["last_text"] if existing else None)
            )
            new_last_at = (
                last_at
                if last_at is not None
                else (existing["last_at"] if existing else None)
            ) or now
            new_last_out = (
                int(bool(last_out))
                if last_out is not None
                else (int(existing["last_out"]) if existing else 0)
            )

            db.execute(
                """
                INSERT INTO max_dialogs (
                    chat_id, max_user_id, name, phone,
                    last_text, last_at, last_out, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    max_user_id = COALESCE(excluded.max_user_id, max_dialogs.max_user_id),
                    name = CASE
                        WHEN excluded.name IS NOT NULL AND excluded.name != '' THEN excluded.name
                        ELSE max_dialogs.name
                    END,
                    phone = CASE
                        WHEN excluded.phone IS NOT NULL AND excluded.phone != '' THEN excluded.phone
                        ELSE max_dialogs.phone
                    END,
                    last_text = COALESCE(excluded.last_text, max_dialogs.last_text),
                    last_at = COALESCE(excluded.last_at, max_dialogs.last_at),
                    last_out = excluded.last_out,
                    updated_at = excluded.updated_at
                """,
                (
                    resolved_chat,
                    resolved_user,
                    resolved_name or None,
                    resolved_phone or None,
                    new_last_text,
                    new_last_at,
                    new_last_out,
                    now,
                ),
            )
            db.commit()

    await _run_db(_upsert)


async def get_dialog(
    *,
    chat_id: int | None = None,
    max_user_id: int | None = None,
) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = None
            if chat_id is not None:
                row = db.execute(
                    "SELECT * FROM max_dialogs WHERE chat_id = ?",
                    (int(chat_id),),
                ).fetchone()
            elif max_user_id is not None:
                row = db.execute(
                    """
                    SELECT * FROM max_dialogs
                    WHERE max_user_id = ?
                    ORDER BY COALESCE(last_at, updated_at) DESC
                    LIMIT 1
                    """,
                    (int(max_user_id),),
                ).fetchone()
            if not row:
                return None
            return dict(row)

    return await _run_db(_get)


async def get_max_profile(max_user_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM max_profiles WHERE max_user_id = ?",
                (int(max_user_id),),
            ).fetchone()
            return dict(row) if row else None

    return await _run_db(_get)


async def list_dialogs_for_inbox(
    *,
    query: str = "",
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Объединённый список: индекс диалогов ∪ анкеты без chat_id."""
    limit = max(1, min(int(limit or 80), 200))
    q = (query or "").strip().lower()
    q_digits = re.sub(r"\D", "", query or "")

    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            dialogs = [
                dict(r)
                for r in db.execute(
                    """
                    SELECT * FROM max_dialogs
                    ORDER BY COALESCE(last_at, updated_at) DESC
                    """
                ).fetchall()
            ]
            profiles = [
                dict(r)
                for r in db.execute(
                    """
                    SELECT max_user_id, name, phone, updated_at, created_at
                    FROM max_profiles
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            ]

        known_users = {
            int(d["max_user_id"])
            for d in dialogs
            if d.get("max_user_id") is not None
        }
        items: list[dict[str, Any]] = []

        for d in dialogs:
            uid = d.get("max_user_id")
            title = (d.get("name") or "").strip() or (
                f"MAX {uid}" if uid is not None else f"Чат {d['chat_id']}"
            )
            phone = (d.get("phone") or "").strip() or None
            row = {
                "id": str(d["chat_id"]),
                "peer_id": f"chat:{d['chat_id']}",
                "chat_id": int(d["chat_id"]),
                "max_user_id": int(uid) if uid is not None else None,
                "title": title,
                "kind": "user",
                "username": None,
                "phone": phone,
                "unread": 0,
                "pinned": False,
                "is_user": True,
                "is_group": False,
                "is_channel": False,
                "date": d.get("last_at") or d.get("updated_at"),
                "last_message": (d.get("last_text") or "").strip(),
                "last_out": bool(d.get("last_out")),
            }
            if q or q_digits:
                hay = " ".join(
                    filter(None, [title, phone, row["last_message"], str(uid or "")])
                ).lower()
                hay_digits = re.sub(r"\D", "", phone or "")
                if q and q not in hay and not (q_digits and q_digits in hay_digits):
                    continue
            items.append(row)

        for p in profiles:
            uid = int(p["max_user_id"])
            if uid in known_users:
                continue
            title = (p.get("name") or "").strip() or f"MAX {uid}"
            phone = (p.get("phone") or "").strip() or None
            row = {
                "id": f"user:{uid}",
                "peer_id": f"user:{uid}",
                "chat_id": None,
                "max_user_id": uid,
                "title": title,
                "kind": "user",
                "username": None,
                "phone": phone,
                "unread": 0,
                "pinned": False,
                "is_user": True,
                "is_group": False,
                "is_channel": False,
                "date": p.get("updated_at") or p.get("created_at"),
                "last_message": "Анкета заполнена",
                "last_out": False,
            }
            if q or q_digits:
                hay = " ".join(
                    filter(None, [title, phone, str(uid)])
                ).lower()
                hay_digits = re.sub(r"\D", "", phone or "")
                if q and q not in hay and not (q_digits and q_digits in hay_digits):
                    continue
            items.append(row)

        items.sort(key=lambda r: r.get("date") or "", reverse=True)
        return items[:limit]

    return await _run_db(_list)


def extract_chat_id_from_update(update: dict[str, Any]) -> int | None:
    """Достать chat_id из разных типов MAX Update."""
    for key in ("chat_id",):
        raw = update.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    for key in ("chat_id",):
        raw = recipient.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    callback = update.get("callback") or {}
    msg = callback.get("message") or {}
    recipient = msg.get("recipient") or {}
    raw = recipient.get("chat_id")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return None


def extract_user_from_update(update: dict[str, Any]) -> tuple[int | None, str | None]:
    """(user_id, display_name) из update."""
    update_type = update.get("update_type")
    if update_type == "bot_started":
        user = update.get("user") or {}
        uid = user.get("user_id")
        name = " ".join(
            filter(None, [user.get("first_name"), user.get("last_name"), user.get("name")])
        ).strip() or user.get("username")
        try:
            return (int(uid) if uid is not None else None, name or None)
        except (TypeError, ValueError):
            return None, name or None

    if update_type == "message_callback":
        user = (update.get("callback") or {}).get("user") or {}
        uid = user.get("user_id")
        name = " ".join(
            filter(None, [user.get("first_name"), user.get("last_name"), user.get("name")])
        ).strip() or user.get("username")
        try:
            return (int(uid) if uid is not None else None, name or None)
        except (TypeError, ValueError):
            return None, name or None

    message = update.get("message") or {}
    sender = message.get("sender") or {}
    if sender.get("is_bot"):
        return None, None
    uid = sender.get("user_id")
    name = " ".join(
        filter(None, [sender.get("first_name"), sender.get("last_name"), sender.get("name")])
    ).strip() or sender.get("username")
    try:
        return (int(uid) if uid is not None else None, name or None)
    except (TypeError, ValueError):
        return None, name or None
