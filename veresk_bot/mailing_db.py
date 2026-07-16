"""
SQLite-хранилище для админки рассылок: клиенты из Posiflora, события, кампании, аккаунты.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

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


async def init_mailing_db() -> None:
    def _init() -> None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with _connect() as db:
            db.executescript(_SCHEMA)
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
    segment: str = "all",
) -> int:
    now = _now()
    phone = normalize_phone_db(phone)

    def _upsert() -> int:
        with _connect() as db:
            row = db.execute(
                "SELECT id, tg_user_id FROM customers WHERE posiflora_id = ?",
                (posiflora_id,),
            ).fetchone()
            if row:
                new_tg = tg_user_id if tg_user_id is not None else row["tg_user_id"]
                db.execute(
                    """
                    UPDATE customers SET
                        name = ?, phone = ?, notes = ?,
                        last_order_at = COALESCE(?, last_order_at),
                        created_in_pf_at = COALESCE(?, created_in_pf_at),
                        tg_user_id = ?, segment = ?, synced_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        phone,
                        notes,
                        last_order_at,
                        created_in_pf_at,
                        new_tg,
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
                    posiflora_id, name, phone, tg_user_id, notes,
                    last_order_at, created_in_pf_at, segment, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    posiflora_id,
                    name,
                    phone,
                    tg_user_id,
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
    def _list() -> list[dict[str, Any]]:
        today = datetime.now().date()
        mmdd = today.strftime("%m-%d")
        with _connect() as db:
            rows = db.execute(
                """
                SELECT e.*, c.name AS customer_name, c.phone AS customer_phone,
                       c.tg_user_id, c.max_user_id, c.id AS cust_id
                FROM customer_events e
                JOIN customers c ON c.id = e.customer_id
                WHERE e.auto_send = 1
                  AND substr(e.date_from, 6, 5) = ?
                """,
                (mmdd,),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_list)


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
) -> int:
    now = _now()

    def _create() -> int:
        with _connect() as db:
            cur = db.execute(
                """
                INSERT INTO campaigns (
                    title, emoji, message, segment, channels, status,
                    scheduled_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    emoji,
                    message,
                    segment,
                    channels,
                    status,
                    scheduled_at,
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
    def _fetch() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT r.*, c.name AS customer_name, c.phone AS customer_phone,
                       c.tg_user_id, c.max_user_id,
                       camp.message AS campaign_message, camp.id AS camp_id
                FROM campaign_recipients r
                JOIN customers c ON c.id = r.customer_id
                JOIN campaigns camp ON camp.id = r.campaign_id
                WHERE r.status = 'pending'
                  AND camp.status IN ('sending', 'scheduled')
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


# ── admin sessions ─────────────────────────────────────────────────────────


async def create_admin_session(token: str, hours: int = 72) -> None:
    now = datetime.now()
    expires = (now + timedelta(hours=hours)).isoformat(timespec="seconds")

    def _create() -> None:
        with _connect() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO admin_sessions (token, created_at, expires_at)
                VALUES (?, ?, ?)
                """,
                (token, now.isoformat(timespec="seconds"), expires),
            )
            db.commit()

    await _run_db(_create)


async def validate_admin_session(token: str) -> bool:
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

    return await _run_db(_val)


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
