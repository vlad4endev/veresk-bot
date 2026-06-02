"""
Постоянное хранилище клиентов и истории заказов (SQLite).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    tg_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    posiflora_order_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    delivery_date TEXT NOT NULL,
    recipient TEXT NOT NULL,
    occasion TEXT NOT NULL,
    relation TEXT NOT NULL,
    budget TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tg_id) REFERENCES clients (tg_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_tg_created
    ON orders (tg_id, created_at DESC);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


async def _run_db(fn: Callable[[], T]) -> T:
    return await asyncio.to_thread(fn)


async def init_db() -> None:
    def _init() -> None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with _connect() as db:
            db.executescript(_SCHEMA)
            db.commit()

    await _run_db(_init)
    logger.info("База клиентов: %s", DATABASE_PATH)


async def get_client(tg_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            row = db.execute(
                "SELECT tg_id, name, phone, created_at, updated_at FROM clients WHERE tg_id = ?",
                (tg_id,),
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def upsert_client(tg_id: int, name: str, phone: str) -> None:
    now = _now()

    def _upsert() -> None:
        with _connect() as db:
            db.execute(
                """
                INSERT INTO clients (tg_id, name, phone, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET
                    name = excluded.name,
                    phone = excluded.phone,
                    updated_at = excluded.updated_at
                """,
                (tg_id, name.strip(), phone.strip(), now, now),
            )
            db.commit()

    await _run_db(_upsert)


async def insert_order(
    tg_id: int,
    posiflora_order_id: str,
    *,
    status: str = "new",
    name: str,
    phone: str,
    delivery_date: str,
    recipient: str,
    occasion: str,
    relation: str,
    budget: str,
) -> int:
    now = _now()

    def _insert() -> int:
        with _connect() as db:
            cur = db.execute(
                """
                INSERT INTO orders (
                    tg_id, posiflora_order_id, status,
                    name, phone, delivery_date, recipient, occasion, relation, budget,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tg_id,
                    posiflora_order_id,
                    status,
                    name,
                    phone,
                    delivery_date,
                    recipient,
                    occasion,
                    relation,
                    budget,
                    now,
                ),
            )
            db.commit()
            return int(cur.lastrowid)

    return await _run_db(_insert)


async def save_client_and_order(
    tg_id: int,
    data: dict[str, Any],
    posiflora_order_id: str,
    status: str = "new",
) -> None:
    name = str(data.get("name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    if not name or not phone:
        logger.warning("Пропуск сохранения клиента: нет имени или телефона (tg_id=%s)", tg_id)
        return

    await upsert_client(tg_id, name, phone)
    await insert_order(
        tg_id,
        posiflora_order_id,
        status=status,
        name=name,
        phone=phone,
        delivery_date=str(data.get("date", "")),
        recipient=str(data.get("recipient", "")),
        occasion=str(data.get("occasion", "")),
        relation=str(data.get("relation", "")),
        budget=str(data.get("budget", "")),
    )
    logger.info("Клиент %s и заказ #%s сохранены в БД", tg_id, posiflora_order_id)


async def get_orders_for_client(tg_id: int, limit: int = 15) -> list[dict[str, Any]]:
    def _get() -> list[dict[str, Any]]:
        with _connect() as db:
            rows = db.execute(
                """
                SELECT id, posiflora_order_id, status, delivery_date, recipient,
                       occasion, budget, created_at
                FROM orders
                WHERE tg_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (tg_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    return await _run_db(_get)


async def update_order_status_db(posiflora_order_id: str, status: str) -> None:
    def _update() -> None:
        with _connect() as db:
            db.execute(
                "UPDATE orders SET status = ? WHERE posiflora_order_id = ?",
                (status, posiflora_order_id),
            )
            db.commit()

    await _run_db(_update)
