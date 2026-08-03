"""
SQLite-хранилище для админки рассылок: клиенты из Posiflora, события, кампании, аккаунты.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    posiflora_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    tg_user_id INTEGER,
    max_user_id TEXT,
    segment TEXT NOT NULL DEFAULT 'all',
    notes TEXT NOT NULL DEFAULT '',
    last_order_at TEXT,
    created_in_pf_at TEXT,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers (phone);
CREATE INDEX IF NOT EXISTS idx_customers_segment ON customers (segment);
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers (name);
CREATE INDEX IF NOT EXISTS idx_customers_tg ON customers (tg_user_id);

CREATE TABLE IF NOT EXISTS customer_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    posiflora_event_id TEXT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    date_from TEXT NOT NULL,
    auto_send INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_customer ON customer_events (customer_id);
CREATE INDEX IF NOT EXISTS idx_events_date ON customer_events (date_from);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_pf
    ON customer_events (posiflora_event_id) WHERE posiflora_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS customer_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    posiflora_order_id TEXT NOT NULL UNIQUE,
    number TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    ordered_at TEXT,
    delivery_at TEXT,
    synced_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON customer_orders (customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_date ON customer_orders (ordered_at);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '🌷',
    message TEXT NOT NULL,
    segment TEXT NOT NULL DEFAULT 'all',
    channels TEXT NOT NULL DEFAULT 'tg',
    status TEXT NOT NULL DEFAULT 'draft',
    scheduled_at TEXT,
    media_path TEXT,
    media_kind TEXT,
    media_filename TEXT,
    media_mime TEXT,
    total_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    delivered_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns (status);

CREATE TABLE IF NOT EXISTS campaign_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT 'tg',
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,
    error TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns (id) ON DELETE CASCADE,
    FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_recipients_campaign
    ON campaign_recipients (campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_recipients_pending
    ON campaign_recipients (status) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS send_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    session_file TEXT NOT NULL DEFAULT '',
    daily_limit INTEGER NOT NULL DEFAULT 200,
    sent_today INTEGER NOT NULL DEFAULT 0,
    sent_day TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    warmup_until TEXT,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_ok_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_id INTEGER,
    login TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    phone_digits TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'employee',
    permissions TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_admin_users_digits ON admin_users (phone_digits);
CREATE INDEX IF NOT EXISTS idx_admin_users_active ON admin_users (is_active);

CREATE TABLE IF NOT EXISTS personal_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    channel TEXT NOT NULL DEFAULT 'tg',
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_phone_db(phone: str) -> str:
    """
    Единый формат хранения телефона в базе рассылок: +7(999)999-99-99.

    Принимает номер в любом виде («89991234567», «9991234567»,
    «+7 999 123-45-67»…). Если из номера не получается российский
    10-значный, возвращает исходную строку без изменений.
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]
    if len(digits) != 10:
        return str(phone or "").strip()
    return f"+7({digits[0:3]}){digits[3:6]}-{digits[6:8]}-{digits[8:10]}"


def _phone_digits(phone: str) -> str:
    """10 цифр национального номера для сравнения телефонов между собой."""
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]
    return digits


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


async def _run_db(fn: Callable[[], T]) -> T:
    return await asyncio.to_thread(fn)


def _ensure_column(db: sqlite3.Connection, table: str, column: str, typedef: str) -> None:
    cols = {str(r[1]) for r in db.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


async def init_mailing_db() -> None:
    def _init() -> None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with _connect() as db:
            db.executescript(_SCHEMA)
            for col, typedef in (
                ("last_checked_at", "TEXT"),
                ("last_ok_at", "TEXT"),
                ("last_error", "TEXT"),
            ):
                _ensure_column(db, "send_accounts", col, typedef)
            for col, typedef in (
                ("user_id", "INTEGER"),
                ("login", "TEXT DEFAULT ''"),
            ):
                _ensure_column(db, "admin_sessions", col, typedef)
            _ensure_column(db, "admin_users", "permissions", "TEXT DEFAULT ''")
            _ensure_column(db, "customer_events", "last_auto_sent_on", "TEXT")
            for col, typedef in (
                ("media_path", "TEXT"),
                ("media_kind", "TEXT"),
                ("media_filename", "TEXT"),
                ("media_mime", "TEXT"),
            ):
                _ensure_column(db, "campaigns", col, typedef)
            # Миграция: приводим ранее сохранённые телефоны к +7(999)999-99-99
            rows = db.execute("SELECT id, phone FROM customers").fetchall()
            for row in rows:
                formatted = normalize_phone_db(row["phone"])
                if formatted != row["phone"]:
                    db.execute(
                        "UPDATE customers SET phone = ? WHERE id = ?",
                        (formatted, row["id"]),
                    )
            db.commit()

    await _run_db(_init)
    logger.info("База рассылок готова: %s", DATABASE_PATH)


# ── customers ──────────────────────────────────────────────────────────────


async def upsert_customer(
    *,
    posiflora_id: str,
    name: str,
    phone: str,
    notes: str = "",
    last_order_at: str | None = None,
    created_in_pf_at: str | None = None,
    tg_user_id: int | None = None,
    max_user_id: int | None = None,
    segment: str = "all",
) -> int:
    now = _now()
    phone = normalize_phone_db(phone)
    max_id = str(int(max_user_id)) if max_user_id is not None else None

    def _upsert() -> int:
        with _connect() as db:
            row = db.execute(
                "SELECT id, tg_user_id, max_user_id FROM customers WHERE posiflora_id = ?",
                (posiflora_id,),
            ).fetchone()
            if row:
                new_tg = tg_user_id if tg_user_id is not None else row["tg_user_id"]
                new_max = max_id if max_id is not None else row["max_user_id"]
                db.execute(
                    """
                    UPDATE customers SET
                        name = ?, phone = ?, notes = ?,
                        last_order_at = COALESCE(?, last_order_at),
                        created_in_pf_at = COALESCE(?, created_in_pf_at),
                        tg_user_id = ?, max_user_id = ?, segment = ?, synced_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        phone,
                        notes,
                        last_order_at,
                        created_in_pf_at,
                        new_tg,
                        new_max,
                        segment,
                        now,
                        row["id"],
                    ),
                )
                db.commit()
                return int(row["id"])
            cur = db.execute(
                """
                INSERT INTO customers (
                    posiflora_id, name, phone, tg_user_id, max_user_id, notes,
                    last_order_at, created_in_pf_at, segment, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    posiflora_id,
                    name,
                    phone,
                    tg_user_id,
                    max_id,
                    notes,
                    last_order_at,
                    created_in_pf_at,
                    segment,
                    now,
                ),
            )
            db.commit()
            return int(cur.lastrowid)

    return await _run_db(_upsert)


async def get_customer(customer_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def get_customer_by_posiflora_id(posiflora_id: str) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM customers WHERE posiflora_id = ?", (posiflora_id,)
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def get_customer_by_tg_user_id(tg_user_id: int) -> dict[str, Any] | None:
    """Найти клиента CRM по Telegram user id."""
    try:
        tid = int(tg_user_id)
    except (TypeError, ValueError):
        return None

    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM customers WHERE tg_user_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def get_customer_by_max_user_id(max_user_id: int) -> dict[str, Any] | None:
    """Найти клиента CRM по MAX user id."""
    try:
        mid = int(max_user_id)
    except (TypeError, ValueError):
        return None

    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM customers WHERE max_user_id = ? LIMIT 1",
                (str(mid),),
            ).fetchone()
            if row:
                return dict(row)
            # На случай, если хранится как число без приведения к TEXT
            row = db.execute(
                "SELECT * FROM customers WHERE CAST(max_user_id AS TEXT) = ? LIMIT 1",
                (str(mid),),
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def customer_contact_sets() -> tuple[set[int], set[str]]:
    """Наборы tg_user_id и телефонов (последние 10 цифр) для быстрой фильтрации чатов."""

    def _sets() -> tuple[set[int], set[str]]:
        with _connect() as db:
            rows = db.execute(
                "SELECT tg_user_id, phone FROM customers"
            ).fetchall()
        tg_ids: set[int] = set()
        phones: set[str] = set()
        for row in rows:
            tid = row["tg_user_id"]
            if tid is not None:
                try:
                    tg_ids.add(int(tid))
                except (TypeError, ValueError):
                    pass
            digits = _phone_digits(row["phone"] or "")
            if digits:
                phones.add(digits)
        return tg_ids, phones

    return await _run_db(_sets)


async def get_customer_by_phone(phone: str) -> dict[str, Any] | None:
    """Найти клиента CRM по телефону (сравнение по 10 цифрам)."""
    target = _phone_digits(phone)
    if not target:
        return None

    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            rows = db.execute("SELECT * FROM customers").fetchall()
        for row in rows:
            if _phone_digits(row["phone"]) == target:
                return dict(row)
        return None

    return await _run_db(_get)


async def list_customers(
    *,
    segment: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    page = max(1, page)
    offset = (page - 1) * page_size

    def _list() -> tuple[list[dict[str, Any]], int]:
        with _connect() as db:
            where: list[str] = []
            params: list[Any] = []
            if segment and segment != "all":
                where.append("segment = ?")
                params.append(segment)
            if search:
                q = f"%{search.strip()}%"
                digits = re.sub(r"\D", "", search)
                if digits:
                    # Ищем и по «сырым» цифрам, игнорируя формат +7(999)999-99-99
                    stripped = (
                        "REPLACE(REPLACE(REPLACE(REPLACE(REPLACE("
                        "phone,'+',''),'(',''),')',''),'-',''),' ','')"
                    )
                    where.append(
                        f"(name LIKE ? OR phone LIKE ? OR {stripped} LIKE ?)"
                    )
                    params.extend([q, q, f"%{digits}%"])
                else:
                    where.append("(name LIKE ? OR phone LIKE ?)")
                    params.extend([q, q])
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = db.execute(
                f"SELECT COUNT(*) AS c FROM customers {clause}", params
            ).fetchone()["c"]
            rows = db.execute(
                f"""
                SELECT * FROM customers {clause}
                ORDER BY name COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    return await _run_db(_list)


async def count_customers(segment: str | None = None) -> int:
    def _count() -> int:
        with _connect() as db:
            if segment and segment != "all":
                row = db.execute(
                    "SELECT COUNT(*) AS c FROM customers WHERE segment = ?",
                    (segment,),
                ).fetchone()
            else:
                row = db.execute("SELECT COUNT(*) AS c FROM customers").fetchone()
        return int(row["c"])

    return await _run_db(_count)


async def set_customer_tg_by_phone(phone: str, tg_user_id: int) -> None:
    """Привязывает tg_id к клиенту; телефон сравнивается по цифрам,
    независимо от формата хранения/входа."""
    target = _phone_digits(phone)
    if not target:
        return

    def _set() -> None:
        with _connect() as db:
            rows = db.execute("SELECT id, phone FROM customers").fetchall()
            ids = [
                row["id"] for row in rows if _phone_digits(row["phone"]) == target
            ]
            for cid in ids:
                db.execute(
                    "UPDATE customers SET tg_user_id = ? WHERE id = ?",
                    (tg_user_id, cid),
                )
            db.commit()

    await _run_db(_set)


async def set_customer_max_by_phone(phone: str, max_user_id: int) -> None:
    """Привязывает MAX user id к клиенту по телефону."""
    target = _phone_digits(phone)
    if not target:
        return
    try:
        mid = str(int(max_user_id))
    except (TypeError, ValueError):
        return

    def _set() -> None:
        with _connect() as db:
            rows = db.execute("SELECT id, phone FROM customers").fetchall()
            ids = [
                row["id"] for row in rows if _phone_digits(row["phone"]) == target
            ]
            for cid in ids:
                db.execute(
                    "UPDATE customers SET max_user_id = ? WHERE id = ?",
                    (mid, cid),
                )
            db.commit()

    await _run_db(_set)


async def phone_to_tg_map() -> dict[str, int]:
    """Телефон → tg_id из ботовской таблицы clients (если есть)."""

    def _map() -> dict[str, int]:
        with _connect() as db:
            try:
                rows = db.execute("SELECT phone, tg_id FROM clients").fetchall()
            except sqlite3.OperationalError:
                return {}
        result: dict[str, int] = {}
        for row in rows:
            digits = "".join(c for c in str(row["phone"]) if c.isdigit())
            if len(digits) == 11 and digits[0] in ("7", "8"):
                digits = digits[1:]
            if digits:
                result[digits] = int(row["tg_id"])
        return result

    return await _run_db(_map)


# ── events ─────────────────────────────────────────────────────────────────


def _infer_kind(title: str) -> str:
    t = title.lower()
    if "день рождения" in t or "др" == t.strip() or "birthday" in t or "🎂" in title:
        return "bday"
    if "годовщин" in t or "свадьб" in t or "💍" in title or "anniv" in t:
        return "anniv"
    return "other"


async def upsert_customer_event(
    *,
    customer_id: int,
    posiflora_event_id: str | None,
    title: str,
    date_from: str,
    kind: str | None = None,
) -> int:
    kind = kind or _infer_kind(title)

    def _upsert() -> int:
        with _connect() as db:
            if posiflora_event_id:
                row = db.execute(
                    "SELECT id, auto_send FROM customer_events WHERE posiflora_event_id = ?",
                    (posiflora_event_id,),
                ).fetchone()
                if row:
                    db.execute(
                        """
                        UPDATE customer_events SET
                            customer_id = ?, title = ?, kind = ?, date_from = ?
                        WHERE id = ?
                        """,
                        (customer_id, title, kind, date_from, row["id"]),
                    )
                    db.commit()
                    return int(row["id"])
            cur = db.execute(
                """
                INSERT INTO customer_events (
                    customer_id, posiflora_event_id, title, kind, date_from, auto_send
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (customer_id, posiflora_event_id, title, kind, date_from),
            )
            db.commit()
            return int(cur.lastrowid)

    return await _run_db(_upsert)


async def list_events_for_customer(customer_id: int) -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT * FROM customer_events
                WHERE customer_id = ?
                ORDER BY date_from
                """,
                (customer_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_list)


async def next_events_for_customers(
    customer_ids: list[int],
) -> dict[int, dict[str, Any]]:
    """Ближайшее (по MM-DD, год игнорируем) событие для каждого клиента."""
    if not customer_ids:
        return {}

    def _list() -> dict[int, dict[str, Any]]:
        today = datetime.now().date()
        placeholders = ",".join("?" * len(customer_ids))
        with _connect() as db:
            rows = db.execute(
                f"""
                SELECT customer_id, title, kind, date_from
                FROM customer_events
                WHERE customer_id IN ({placeholders})
                """,
                customer_ids,
            ).fetchall()
        best: dict[int, dict[str, Any]] = {}
        for row in rows:
            raw = str(row["date_from"])[:10]
            try:
                event_date = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            try:
                this_year = event_date.replace(year=today.year)
            except ValueError:
                # 29 февраля
                this_year = event_date.replace(year=today.year, day=28)
            if this_year < today:
                try:
                    this_year = event_date.replace(year=today.year + 1)
                except ValueError:
                    this_year = event_date.replace(year=today.year + 1, day=28)
            delta = (this_year - today).days
            cid = int(row["customer_id"])
            cur = best.get(cid)
            if cur is None or delta < cur["days_until"]:
                best[cid] = {
                    "title": row["title"],
                    "kind": row["kind"],
                    "days_until": delta,
                    "next_date": this_year.isoformat(),
                }
        return best

    return await _run_db(_list)


async def list_upcoming_events(days: int = 14, limit: int = 50) -> list[dict[str, Any]]:
    """События в ближайшие `days` дней (по MM-DD, год игнорируем для ДР)."""

    def _list() -> list[dict[str, Any]]:
        today = datetime.now().date()
        with _connect() as db:
            rows = db.execute(
                """
                SELECT e.*, c.name AS customer_name, c.phone AS customer_phone,
                       c.tg_user_id, c.max_user_id, c.id AS cust_id
                FROM customer_events e
                JOIN customers c ON c.id = e.customer_id
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            raw = str(row["date_from"])[:10]
            try:
                event_date = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                continue
            # Годовщина/ДР — сравниваем по дню/месяцу в текущем году
            try:
                this_year = event_date.replace(year=today.year)
            except ValueError:
                # 29 февраля
                this_year = event_date.replace(year=today.year, day=28)
            if this_year < today:
                try:
                    this_year = event_date.replace(year=today.year + 1)
                except ValueError:
                    this_year = event_date.replace(year=today.year + 1, day=28)
            delta = (this_year - today).days
            if 0 <= delta <= days:
                item = dict(row)
                item["days_until"] = delta
                item["next_date"] = this_year.isoformat()
                result.append(item)
        result.sort(key=lambda x: x["days_until"])
        return result[:limit]

    return await _run_db(_list)


async def set_event_auto_send(event_id: int, auto_send: bool) -> bool:
    def _set() -> bool:
        with _connect() as db:
            cur = db.execute(
                "UPDATE customer_events SET auto_send = ? WHERE id = ?",
                (1 if auto_send else 0, event_id),
            )
            db.commit()
            return cur.rowcount > 0

    return await _run_db(_set)


async def get_event(event_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                """
                SELECT e.*, c.name AS customer_name, c.phone AS customer_phone,
                       c.tg_user_id, c.max_user_id
                FROM customer_events e
                JOIN customers c ON c.id = e.customer_id
                WHERE e.id = ?
                """,
                (event_id,),
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def list_auto_events_for_today() -> list[dict[str, Any]]:
    """События с auto_send на сегодня, ещё не поставленные в очередь сегодня."""

    def _list() -> list[dict[str, Any]]:
        today = datetime.now().date()
        mmdd = today.strftime("%m-%d")
        today_iso = today.isoformat()
        with _connect() as db:
            rows = db.execute(
                """
                SELECT e.*, c.name AS customer_name, c.phone AS customer_phone,
                       c.tg_user_id, c.max_user_id, c.id AS cust_id
                FROM customer_events e
                JOIN customers c ON c.id = e.customer_id
                WHERE e.auto_send = 1
                  AND substr(e.date_from, 6, 5) = ?
                  AND (e.last_auto_sent_on IS NULL OR e.last_auto_sent_on != ?)
                """,
                (mmdd, today_iso),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_list)


async def mark_event_auto_sent(event_id: int, day: str | None = None) -> None:
    """Помечает событие: авто-поздравление уже поставлено в очередь в этот день."""
    on = day or datetime.now().date().isoformat()

    def _mark() -> None:
        with _connect() as db:
            db.execute(
                "UPDATE customer_events SET last_auto_sent_on = ? WHERE id = ?",
                (on, event_id),
            )
            db.commit()

    await _run_db(_mark)


# ── orders (история покупок из Posiflora) ──────────────────────────────────


async def upsert_customer_order(
    *,
    customer_id: int,
    posiflora_order_id: str,
    number: str = "",
    amount: float = 0,
    status: str = "",
    comment: str = "",
    ordered_at: str | None = None,
    delivery_at: str | None = None,
) -> int:
    now = _now()

    def _upsert() -> int:
        with _connect() as db:
            row = db.execute(
                "SELECT id FROM customer_orders WHERE posiflora_order_id = ?",
                (posiflora_order_id,),
            ).fetchone()
            if row:
                db.execute(
                    """
                    UPDATE customer_orders SET
                        customer_id = ?, number = ?, amount = ?, status = ?,
                        comment = ?, ordered_at = ?, delivery_at = ?, synced_at = ?
                    WHERE id = ?
                    """,
                    (
                        customer_id,
                        number,
                        amount,
                        status,
                        comment,
                        ordered_at,
                        delivery_at,
                        now,
                        row["id"],
                    ),
                )
                db.commit()
                return int(row["id"])
            cur = db.execute(
                """
                INSERT INTO customer_orders (
                    customer_id, posiflora_order_id, number, amount, status,
                    comment, ordered_at, delivery_at, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    customer_id,
                    posiflora_order_id,
                    number,
                    amount,
                    status,
                    comment,
                    ordered_at,
                    delivery_at,
                    now,
                ),
            )
            db.commit()
            return int(cur.lastrowid)

    return await _run_db(_upsert)


async def list_orders_for_customer(
    customer_id: int, limit: int = 100
) -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT * FROM customer_orders
                WHERE customer_id = ?
                ORDER BY ordered_at DESC
                LIMIT ?
                """,
                (customer_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_list)


async def get_order_stats_for_customer(customer_id: int) -> dict[str, Any]:
    """Агрегаты по покупкам клиента: количество, сумма, средний чек, последняя."""

    def _stats() -> dict[str, Any]:
        with _connect() as db:
            row = db.execute(
                """
                SELECT COUNT(*) AS orders_count,
                       COALESCE(SUM(amount), 0) AS total_spent,
                       MAX(ordered_at) AS last_order_at
                FROM customer_orders
                WHERE customer_id = ?
                """,
                (customer_id,),
            ).fetchone()
        count = int(row["orders_count"] or 0)
        total = float(row["total_spent"] or 0)
        return {
            "orders_count": count,
            "total_spent": total,
            "avg_order": round(total / count) if count else 0,
            "last_order_at": row["last_order_at"],
        }

    return await _run_db(_stats)


async def update_customer_last_order(
    customer_id: int, last_order_at: str | None
) -> None:
    if not last_order_at:
        return

    def _set() -> None:
        with _connect() as db:
            db.execute(
                "UPDATE customers SET last_order_at = ? WHERE id = ?",
                (last_order_at, customer_id),
            )
            db.commit()

    await _run_db(_set)


# ── campaigns ──────────────────────────────────────────────────────────────


async def create_campaign(
    *,
    title: str,
    message: str,
    segment: str = "all",
    channels: str = "tg",
    emoji: str = "🌷",
    status: str = "draft",
    scheduled_at: str | None = None,
    media_path: str | None = None,
    media_kind: str | None = None,
    media_filename: str | None = None,
    media_mime: str | None = None,
) -> int:
    now = _now()

    def _create() -> int:
        with _connect() as db:
            cur = db.execute(
                """
                INSERT INTO campaigns (
                    title, emoji, message, segment, channels, status,
                    scheduled_at, media_path, media_kind, media_filename, media_mime,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    emoji,
                    message,
                    segment,
                    channels,
                    status,
                    scheduled_at,
                    media_path,
                    media_kind,
                    media_filename,
                    media_mime,
                    now,
                    now,
                ),
            )
            db.commit()
            return int(cur.lastrowid)

    return await _run_db(_create)


async def update_campaign(campaign_id: int, **fields: Any) -> bool:
    allowed = {
        "title",
        "emoji",
        "message",
        "segment",
        "channels",
        "status",
        "scheduled_at",
        "media_path",
        "media_kind",
        "media_filename",
        "media_mime",
        "total_count",
        "sent_count",
        "delivered_count",
        "failed_count",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = _now()

    def _update() -> bool:
        cols = ", ".join(f"{k} = ?" for k in updates)
        with _connect() as db:
            cur = db.execute(
                f"UPDATE campaigns SET {cols} WHERE id = ?",
                [*updates.values(), campaign_id],
            )
            db.commit()
            return cur.rowcount > 0

    return await _run_db(_update)


async def get_campaign(campaign_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def list_campaigns(limit: int = 50) -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT * FROM campaigns
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_list)


async def add_campaign_recipients(
    campaign_id: int,
    recipients: list[tuple[int, str]],
) -> int:
    """recipients: list of (customer_id, channel)."""

    def _add() -> int:
        with _connect() as db:
            db.executemany(
                """
                INSERT INTO campaign_recipients (campaign_id, customer_id, channel, status)
                VALUES (?, ?, ?, 'pending')
                """,
                [(campaign_id, cid, ch) for cid, ch in recipients],
            )
            count = len(recipients)
            db.execute(
                "UPDATE campaigns SET total_count = ?, updated_at = ? WHERE id = ?",
                (count, _now(), campaign_id),
            )
            db.commit()
            return count

    return await _run_db(_add)


async def list_campaign_recipients(
    campaign_id: int,
    *,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    page = max(1, page)
    offset = (page - 1) * page_size

    def _list() -> tuple[list[dict[str, Any]], int]:
        with _connect() as db:
            where = ["r.campaign_id = ?"]
            params: list[Any] = [campaign_id]
            if search:
                q = f"%{search.strip()}%"
                where.append("(c.name LIKE ? OR c.phone LIKE ?)")
                params.extend([q, q])
            clause = " AND ".join(where)
            total = db.execute(
                f"""
                SELECT COUNT(*) AS c
                FROM campaign_recipients r
                JOIN customers c ON c.id = r.customer_id
                WHERE {clause}
                """,
                params,
            ).fetchone()["c"]
            rows = db.execute(
                f"""
                SELECT r.*, c.name AS customer_name, c.phone AS customer_phone
                FROM campaign_recipients r
                JOIN customers c ON c.id = r.customer_id
                WHERE {clause}
                ORDER BY r.id
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    return await _run_db(_list)


async def fetch_pending_recipients(limit: int = 20) -> list[dict[str, Any]]:
    """Только кампании в статусе sending (scheduled активирует activate_due_campaigns)."""

    def _fetch() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT r.*, c.name AS customer_name, c.phone AS customer_phone,
                       c.tg_user_id, c.max_user_id,
                       camp.message AS campaign_message, camp.id AS camp_id,
                       camp.media_path AS campaign_media_path,
                       camp.media_kind AS campaign_media_kind,
                       camp.media_filename AS campaign_media_filename,
                       camp.media_mime AS campaign_media_mime
                FROM campaign_recipients r
                JOIN customers c ON c.id = r.customer_id
                JOIN campaigns camp ON camp.id = r.campaign_id
                WHERE r.status = 'pending'
                  AND camp.status = 'sending'
                ORDER BY r.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_fetch)


async def mark_recipient_status(
    recipient_id: int,
    status: str,
    *,
    error: str | None = None,
) -> None:
    now = _now() if status in ("sent", "delivered", "failed") else None

    def _mark() -> None:
        with _connect() as db:
            db.execute(
                """
                UPDATE campaign_recipients
                SET status = ?, sent_at = COALESCE(?, sent_at), error = ?
                WHERE id = ?
                """,
                (status, now, error, recipient_id),
            )
            row = db.execute(
                "SELECT campaign_id FROM campaign_recipients WHERE id = ?",
                (recipient_id,),
            ).fetchone()
            if row:
                cid = row["campaign_id"]
                stats = db.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status IN ('sent','delivered') THEN 1 ELSE 0 END) AS sent,
                        SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) AS deliv,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
                    FROM campaign_recipients WHERE campaign_id = ?
                    """,
                    (cid,),
                ).fetchone()
                db.execute(
                    """
                    UPDATE campaigns SET
                        sent_count = ?, delivered_count = ?, failed_count = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(stats["sent"] or 0),
                        int(stats["deliv"] or 0),
                        int(stats["failed"] or 0),
                        _now(),
                        cid,
                    ),
                )
                if int(stats["pending"] or 0) == 0:
                    db.execute(
                        "UPDATE campaigns SET status = 'done', updated_at = ? WHERE id = ? AND status = 'sending'",
                        (_now(), cid),
                    )
            db.commit()

    await _run_db(_mark)


async def activate_due_campaigns() -> list[int]:
    """Переводит scheduled-кампании с наступившим временем в sending."""
    now = _now()

    def _act() -> list[int]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT id FROM campaigns
                WHERE status = 'scheduled'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= ?
                """,
                (now,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                db.executemany(
                    "UPDATE campaigns SET status = 'sending', updated_at = ? WHERE id = ?",
                    [(now, i) for i in ids],
                )
                db.commit()
            return ids

    return await _run_db(_act)


# ── personal messages ──────────────────────────────────────────────────────


async def create_personal_message(
    customer_id: int,
    message: str,
    channel: str = "tg",
) -> int:
    now = _now()

    def _create() -> int:
        with _connect() as db:
            cur = db.execute(
                """
                INSERT INTO personal_messages (
                    customer_id, channel, message, status, created_at
                ) VALUES (?, ?, ?, 'pending', ?)
                """,
                (customer_id, channel, message, now),
            )
            db.commit()
            return int(cur.lastrowid)

    return await _run_db(_create)


async def fetch_pending_personal(limit: int = 10) -> list[dict[str, Any]]:
    def _fetch() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT p.*, c.name AS customer_name, c.phone AS customer_phone,
                       c.tg_user_id, c.max_user_id
                FROM personal_messages p
                JOIN customers c ON c.id = p.customer_id
                WHERE p.status = 'pending'
                ORDER BY p.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_fetch)


async def mark_personal_status(
    msg_id: int,
    status: str,
    *,
    error: str | None = None,
) -> None:
    def _mark() -> None:
        with _connect() as db:
            db.execute(
                """
                UPDATE personal_messages
                SET status = ?, sent_at = ?, error = ?
                WHERE id = ?
                """,
                (status, _now() if status != "pending" else None, error, msg_id),
            )
            db.commit()

    await _run_db(_mark)


async def list_messages_for_customer(
    customer_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            personal = db.execute(
                """
                SELECT id, message AS title, channel, status, sent_at AS date, 'personal' AS kind
                FROM personal_messages
                WHERE customer_id = ?
                """,
                (customer_id,),
            ).fetchall()
            campaign = db.execute(
                """
                SELECT r.id, camp.title, r.channel, r.status, r.sent_at AS date, 'campaign' AS kind
                FROM campaign_recipients r
                JOIN campaigns camp ON camp.id = r.campaign_id
                WHERE r.customer_id = ?
                """,
                (customer_id,),
            ).fetchall()
        items = [dict(r) for r in personal] + [dict(r) for r in campaign]
        items.sort(key=lambda x: x.get("date") or "", reverse=True)
        return items[:limit]

    return await _run_db(_list)


# ── send accounts ──────────────────────────────────────────────────────────


async def list_send_accounts() -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                "SELECT * FROM send_accounts ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_list)


async def create_send_account(
    *,
    kind: str,
    label: str,
    phone: str = "",
    session_file: str = "",
    daily_limit: int = 200,
    status: str = "ready",
    warmup_until: str | None = None,
) -> int:
    now = _now()

    def _create() -> int:
        with _connect() as db:
            cur = db.execute(
                """
                INSERT INTO send_accounts (
                    kind, label, phone, session_file, daily_limit,
                    sent_today, status, warmup_until, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    kind,
                    label,
                    phone,
                    session_file,
                    daily_limit,
                    status,
                    warmup_until,
                    now,
                ),
            )
            db.commit()
            return int(cur.lastrowid)

    return await _run_db(_create)


async def get_send_account(account_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM send_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def pick_ready_account(kind: str = "tg_userbot") -> dict[str, Any] | None:
    today = datetime.now().date().isoformat()

    def _pick() -> dict[str, Any] | None:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT * FROM send_accounts
                WHERE kind = ? AND status IN ('ready', 'warmup')
                ORDER BY sent_today ASC, id ASC
                """,
                (kind,),
            ).fetchall()
            for row in rows:
                acc = dict(row)
                if acc.get("sent_day") != today:
                    db.execute(
                        "UPDATE send_accounts SET sent_today = 0, sent_day = ? WHERE id = ?",
                        (today, acc["id"]),
                    )
                    acc["sent_today"] = 0
                    acc["sent_day"] = today
                if acc["status"] == "warmup" and acc.get("warmup_until"):
                    if acc["warmup_until"] > today:
                        # ещё греется — пониженный лимит
                        limit = min(acc["daily_limit"], 30)
                    else:
                        db.execute(
                            "UPDATE send_accounts SET status = 'ready' WHERE id = ?",
                            (acc["id"],),
                        )
                        acc["status"] = "ready"
                        limit = acc["daily_limit"]
                else:
                    limit = acc["daily_limit"]
                if acc["sent_today"] < limit:
                    db.commit()
                    return acc
            db.commit()
        return None

    return await _run_db(_pick)


async def bump_account_sent(account_id: int) -> None:
    today = datetime.now().date().isoformat()

    def _bump() -> None:
        with _connect() as db:
            row = db.execute(
                "SELECT sent_today, sent_day FROM send_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if not row:
                return
            sent = 0 if row["sent_day"] != today else int(row["sent_today"])
            db.execute(
                "UPDATE send_accounts SET sent_today = ?, sent_day = ? WHERE id = ?",
                (sent + 1, today, account_id),
            )
            db.commit()

    await _run_db(_bump)


async def update_send_account(account_id: int, **fields: Any) -> bool:
    allowed = {
        "label",
        "phone",
        "session_file",
        "daily_limit",
        "status",
        "warmup_until",
        "sent_today",
        "sent_day",
        "last_checked_at",
        "last_ok_at",
        "last_error",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False

    def _update() -> bool:
        cols = ", ".join(f"{k} = ?" for k in updates)
        with _connect() as db:
            cur = db.execute(
                f"UPDATE send_accounts SET {cols} WHERE id = ?",
                [*updates.values(), account_id],
            )
            db.commit()
            return cur.rowcount > 0

    return await _run_db(_update)


async def delete_send_account(account_id: int) -> dict[str, Any] | None:
    """Удалить аккаунт отправки. Возвращает удалённую строку или None."""

    def _delete() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM send_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            db.execute("DELETE FROM send_accounts WHERE id = ?", (account_id,))
            db.commit()
            return data

    return await _run_db(_delete)


# ── admin users & sessions ─────────────────────────────────────────────────


ADMIN_SESSION_HOURS = 72
# Продлеваем вход не чаще раза в час, чтобы не писать в БД на каждый запрос
ADMIN_SESSION_TOUCH_MIN_SECONDS = 3600

_ADMIN_PASSWORD_ITERS = 120_000
_ADMIN_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
ADMIN_USER_ROLES = ("admin", "employee")

# Разделы панели, которыми можно управлять для сотрудника
ADMIN_PERMISSION_DEFS: tuple[tuple[str, str], ...] = (
    ("home", "Рассылки"),
    ("clients", "Клиенты"),
    ("chats", "Чаты"),
    ("bots", "Боты"),
    ("settings", "Настройки"),
    ("aichat", "ИИ чат"),
    ("access", "Доступ (сотрудники)"),
)
ADMIN_PERMISSION_KEYS: tuple[str, ...] = tuple(k for k, _ in ADMIN_PERMISSION_DEFS)
# Базовый набор для нового сотрудника
ADMIN_PERMISSIONS_DEFAULT: tuple[str, ...] = ("home", "clients", "chats")


def permissions_catalog() -> list[dict[str, str]]:
    return [{"id": k, "label": label} for k, label in ADMIN_PERMISSION_DEFS]


def all_permissions_map(*, enabled: bool = True) -> dict[str, bool]:
    return {k: bool(enabled) for k in ADMIN_PERMISSION_KEYS}


def default_permissions_map() -> dict[str, bool]:
    allowed = set(ADMIN_PERMISSIONS_DEFAULT)
    return {k: k in allowed for k in ADMIN_PERMISSION_KEYS}


def normalize_permissions(
    raw: Any = None,
    *,
    role: str | None = None,
    full: bool = False,
) -> dict[str, bool]:
    """Приводит права к словарю {section: bool} по всем известным разделам."""
    if full or (role or "").strip().lower() == "admin":
        return all_permissions_map(enabled=True)

    enabled: set[str] = set()
    if raw is None or raw == "":
        enabled = set(ADMIN_PERMISSIONS_DEFAULT)
    elif isinstance(raw, dict):
        for key, val in raw.items():
            if str(key) in ADMIN_PERMISSION_KEYS and val:
                enabled.add(str(key))
    elif isinstance(raw, (list, tuple, set)):
        for item in raw:
            key = str(item).strip()
            if key in ADMIN_PERMISSION_KEYS:
                enabled.add(key)
    elif isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                parsed = json.loads(text)
                return normalize_permissions(parsed, role=role, full=full)
            except json.JSONDecodeError:
                for part in re.split(r"[,;\s]+", text):
                    key = part.strip()
                    if key in ADMIN_PERMISSION_KEYS:
                        enabled.add(key)
    return {k: k in enabled for k in ADMIN_PERMISSION_KEYS}


def permissions_to_storage(perms: dict[str, bool] | list[str] | None) -> str:
    normalized = normalize_permissions(perms)
    allowed = [k for k, on in normalized.items() if on]
    return json.dumps(allowed, ensure_ascii=False)


def generate_admin_password(length: int = 10) -> str:
    """Читаемый пароль без неоднозначных символов (O/0, I/l/1)."""
    n = max(8, min(int(length or 10), 32))
    return "".join(secrets.choice(_ADMIN_PASSWORD_ALPHABET) for _ in range(n))


def hash_admin_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _ADMIN_PASSWORD_ITERS,
    )
    return (
        f"pbkdf2_sha256${_ADMIN_PASSWORD_ITERS}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(dk).decode('ascii')}"
    )


def verify_admin_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = str(stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(hash_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return secrets.compare_digest(got, expected)


def _admin_user_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    role = d.get("role") or "employee"
    perms = normalize_permissions(d.get("permissions"), role=role)
    return {
        "id": int(d["id"]),
        "phone": d.get("phone") or "",
        "name": d.get("name") or "",
        "role": role,
        "permissions": perms,
        "is_active": bool(d.get("is_active")),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "last_login_at": d.get("last_login_at"),
    }


async def list_admin_users(*, include_inactive: bool = True) -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            if include_inactive:
                rows = db.execute(
                    "SELECT * FROM admin_users ORDER BY is_active DESC, name COLLATE NOCASE, id"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM admin_users WHERE is_active = 1 "
                    "ORDER BY name COLLATE NOCASE, id"
                ).fetchall()
            return [_admin_user_public(r) for r in rows]

    return await _run_db(_list)


async def get_admin_user(user_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM admin_users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
            return _admin_user_public(row) if row else None

    return await _run_db(_get)


async def get_admin_user_by_phone(phone: str) -> dict[str, Any] | None:
    digits = _phone_digits(phone)
    if len(digits) != 10:
        return None

    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM admin_users WHERE phone_digits = ?",
                (digits,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["password_hash"] = row["password_hash"]
            role = d.get("role") or "employee"
            d["permissions"] = normalize_permissions(d.get("permissions"), role=role)
            return d

    return await _run_db(_get)


async def create_admin_user(
    *,
    phone: str,
    password: str,
    name: str = "",
    role: str = "employee",
    permissions: Any = None,
) -> dict[str, Any]:
    phone_fmt = normalize_phone_db(phone)
    digits = _phone_digits(phone_fmt)
    if len(digits) != 10:
        raise ValueError("invalid_phone")
    role_n = (role or "employee").strip().lower()
    if role_n not in ADMIN_USER_ROLES:
        raise ValueError("invalid_role")
    if not password or len(password) < 6:
        raise ValueError("weak_password")
    now = _now()
    pw_hash = hash_admin_password(password)
    name_n = str(name or "").strip()
    perms_raw = (
        permissions_to_storage(all_permissions_map(enabled=True))
        if role_n == "admin"
        else permissions_to_storage(
            permissions if permissions is not None else default_permissions_map()
        )
    )

    def _create() -> dict[str, Any]:
        with _connect() as db:
            exists = db.execute(
                "SELECT id FROM admin_users WHERE phone_digits = ?",
                (digits,),
            ).fetchone()
            if exists:
                raise ValueError("phone_taken")
            cur = db.execute(
                """
                INSERT INTO admin_users
                    (phone, phone_digits, password_hash, name, role, permissions,
                     is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (phone_fmt, digits, pw_hash, name_n, role_n, perms_raw, now, now),
            )
            db.commit()
            row = db.execute(
                "SELECT * FROM admin_users WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            return _admin_user_public(row)

    return await _run_db(_create)


async def update_admin_user(
    user_id: int,
    *,
    name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    password: str | None = None,
    permissions: Any = None,
) -> dict[str, Any] | None:
    now = _now()

    def _upd() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT * FROM admin_users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
            if not row:
                return None
            fields: list[str] = []
            vals: list[Any] = []
            next_role = row["role"]
            if name is not None:
                fields.append("name = ?")
                vals.append(str(name).strip())
            if role is not None:
                role_n = str(role).strip().lower()
                if role_n not in ADMIN_USER_ROLES:
                    raise ValueError("invalid_role")
                fields.append("role = ?")
                vals.append(role_n)
                next_role = role_n
            if is_active is not None:
                fields.append("is_active = ?")
                vals.append(1 if is_active else 0)
            if password is not None:
                if len(password) < 6:
                    raise ValueError("weak_password")
                fields.append("password_hash = ?")
                vals.append(hash_admin_password(password))
            if permissions is not None or (
                role is not None and str(role).strip().lower() == "admin"
            ):
                if str(next_role).strip().lower() == "admin":
                    perms_raw = permissions_to_storage(all_permissions_map(enabled=True))
                else:
                    source = permissions if permissions is not None else row["permissions"]
                    perms_raw = permissions_to_storage(source)
                fields.append("permissions = ?")
                vals.append(perms_raw)
            if not fields:
                return _admin_user_public(row)
            fields.append("updated_at = ?")
            vals.append(now)
            vals.append(int(user_id))
            db.execute(
                f"UPDATE admin_users SET {', '.join(fields)} WHERE id = ?",
                vals,
            )
            db.commit()
            updated = db.execute(
                "SELECT * FROM admin_users WHERE id = ?",
                (int(user_id),),
            ).fetchone()
            return _admin_user_public(updated) if updated else None

    return await _run_db(_upd)


async def delete_admin_user(user_id: int) -> bool:
    def _del() -> bool:
        with _connect() as db:
            cur = db.execute(
                "DELETE FROM admin_users WHERE id = ?",
                (int(user_id),),
            )
            # Сбрасываем привязку сессий удалённого пользователя
            db.execute(
                "UPDATE admin_sessions SET user_id = NULL WHERE user_id = ?",
                (int(user_id),),
            )
            db.commit()
            return cur.rowcount > 0

    return await _run_db(_del)


async def touch_admin_user_login(user_id: int) -> None:
    now = _now()

    def _touch() -> None:
        with _connect() as db:
            db.execute(
                "UPDATE admin_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, int(user_id)),
            )
            db.commit()

    await _run_db(_touch)


async def create_admin_session(
    token: str,
    hours: int = ADMIN_SESSION_HOURS,
    *,
    user_id: int | None = None,
    login: str = "",
) -> None:
    now = datetime.now()
    expires = (now + timedelta(hours=hours)).isoformat(timespec="seconds")

    def _create() -> None:
        with _connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO admin_sessions (token, created_at, expires_at, user_id, login)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token,
                    now.isoformat(timespec="seconds"),
                    expires,
                    int(user_id) if user_id is not None else None,
                    str(login or ""),
                ),
            )
            db.commit()

    await _run_db(_create)


async def get_admin_session(token: str) -> dict[str, Any] | None:
    now = _now()

    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT token, created_at, expires_at, user_id, login FROM admin_sessions WHERE token = ?",
                (token,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
                db.commit()
                return None
            return {
                "token": row["token"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "user_id": row["user_id"],
                "login": row["login"] or "",
            }

    return await _run_db(_get)


async def touch_admin_session(
    token: str,
    *,
    hours: int = ADMIN_SESSION_HOURS,
    min_interval_sec: int = ADMIN_SESSION_TOUCH_MIN_SECONDS,
) -> str | None:
    """Скользящее продление сессии админки. Возвращает новый expires_at или None."""
    now = datetime.now()
    now_s = now.isoformat(timespec="seconds")
    new_expires = (now + timedelta(hours=hours)).isoformat(timespec="seconds")

    def _touch() -> str | None:
        with _connect() as db:
            row = db.execute(
                "SELECT expires_at FROM admin_sessions WHERE token = ?",
                (token,),
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] < now_s:
                db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
                db.commit()
                return None
            # Не трогаем, если до конца ещё далеко и недавно уже продлевали
            try:
                exp_dt = datetime.fromisoformat(str(row["expires_at"]))
                remaining = (exp_dt - now).total_seconds()
            except ValueError:
                remaining = 0
            # Продлеваем, если осталось меньше половины срока или пора обновить
            half = hours * 3600 / 2
            if remaining > half and remaining > min_interval_sec:
                return str(row["expires_at"])
            db.execute(
                "UPDATE admin_sessions SET expires_at = ? WHERE token = ?",
                (new_expires, token),
            )
            db.commit()
            return new_expires

    return await _run_db(_touch)


async def validate_admin_session(token: str, *, touch: bool = True) -> bool:
    now = _now()

    def _val() -> bool:
        with _connect() as db:
            row = db.execute(
                "SELECT expires_at FROM admin_sessions WHERE token = ?",
                (token,),
            ).fetchone()
            if not row:
                return False
            if row["expires_at"] < now:
                db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
                db.commit()
                return False
            return True

    ok = await _run_db(_val)
    if ok and touch:
        await touch_admin_session(token)
    return ok


async def delete_admin_session(token: str) -> None:
    def _del() -> None:
        with _connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
            db.commit()

    await _run_db(_del)


# ── stats ──────────────────────────────────────────────────────────────────


async def get_stats() -> dict[str, Any]:
    def _stats() -> dict[str, Any]:
        with _connect() as db:
            customers = db.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]
            accounts_ready = db.execute(
                "SELECT COUNT(*) AS c FROM send_accounts WHERE status = 'ready'"
            ).fetchone()["c"]
            accounts_total = db.execute(
                "SELECT COUNT(*) AS c FROM send_accounts"
            ).fetchone()["c"]
            month_ago = (datetime.now() - timedelta(days=30)).isoformat()
            sent = db.execute(
                """
                SELECT COUNT(*) AS c FROM campaign_recipients
                WHERE status IN ('sent', 'delivered') AND sent_at >= ?
                """,
                (month_ago,),
            ).fetchone()["c"]
            failed = db.execute(
                """
                SELECT COUNT(*) AS c FROM campaign_recipients
                WHERE status = 'failed' AND sent_at >= ?
                """,
                (month_ago,),
            ).fetchone()["c"]
            total_attempts = sent + failed
            delivery_rate = (
                round(100 * sent / total_attempts) if total_attempts else None
            )
        return {
            "customers": int(customers),
            "accounts_ready": int(accounts_ready),
            "accounts_total": int(accounts_total),
            "delivery_rate": delivery_rate,
            "sent_month": int(sent),
        }

    return await _run_db(_stats)


async def customers_for_segment(segment: str) -> list[dict[str, Any]]:
    def _list() -> list[dict[str, Any]]:
        with _connect() as db:
            if segment and segment != "all":
                rows = db.execute(
                    "SELECT * FROM customers WHERE segment = ? ORDER BY id",
                    (segment,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM customers ORDER BY id"
                ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_list)
