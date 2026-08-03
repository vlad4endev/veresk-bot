"""
Метрики ботов Veresk: запуски (/start), анкеты, heartbeat статуса.

Хранится в том же SQLite (DATABASE_PATH), что и клиенты/MAX-анкеты.
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

PLATFORM_TELEGRAM = "telegram"
PLATFORM_MAX = "max"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_starts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    started_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bot_starts_platform_user
    ON bot_starts (platform, user_id);

CREATE INDEX IF NOT EXISTS idx_bot_starts_platform_time
    ON bot_starts (platform, started_at);

CREATE TABLE IF NOT EXISTS bot_heartbeats (
    platform TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


async def _run_db(fn: Callable[[], T]) -> T:
    return await asyncio.to_thread(fn)


async def init_bot_metrics() -> None:
    def _init() -> None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with _connect() as db:
            db.executescript(_SCHEMA)
            db.commit()
        _backfill_starts_if_empty()

    await _run_db(_init)
    logger.info("Метрики ботов готовы: %s", DATABASE_PATH)


def _backfill_starts_if_empty() -> None:
    """Если учёта запусков ещё не было — один старт на каждого известного пользователя."""
    with _connect() as db:
        count = db.execute("SELECT COUNT(*) FROM bot_starts").fetchone()[0]
        if count:
            return

        tg_rows = []
        try:
            tg_rows = db.execute(
                "SELECT tg_id, COALESCE(created_at, ?) FROM clients",
                (_now(),),
            ).fetchall()
        except sqlite3.Error:
            pass

        for tg_id, started_at in tg_rows:
            db.execute(
                "INSERT INTO bot_starts (platform, user_id, started_at) VALUES (?, ?, ?)",
                (PLATFORM_TELEGRAM, int(tg_id), started_at or _now()),
            )

        max_rows = []
        try:
            max_rows = db.execute(
                "SELECT max_user_id, COALESCE(created_at, ?) FROM max_profiles",
                (_now(),),
            ).fetchall()
        except sqlite3.Error:
            pass

        for max_user_id, started_at in max_rows:
            db.execute(
                "INSERT INTO bot_starts (platform, user_id, started_at) VALUES (?, ?, ?)",
                (PLATFORM_MAX, int(max_user_id), started_at or _now()),
            )

        if tg_rows or max_rows:
            db.commit()
            logger.info(
                "Бэкфилл запусков ботов: TG=%s, MAX=%s",
                len(tg_rows),
                len(max_rows),
            )


async def record_bot_start(platform: str, user_id: int) -> None:
    if not user_id:
        return
    now = _now()

    def _insert() -> None:
        with _connect() as db:
            db.execute(
                "INSERT INTO bot_starts (platform, user_id, started_at) VALUES (?, ?, ?)",
                (platform, int(user_id), now),
            )
            db.execute(
                """
                INSERT INTO bot_heartbeats (platform, last_seen) VALUES (?, ?)
                ON CONFLICT(platform) DO UPDATE SET last_seen = excluded.last_seen
                """,
                (platform, now),
            )
            db.commit()

    try:
        await _run_db(_insert)
    except Exception:
        logger.debug("Не удалось записать запуск бота %s/%s", platform, user_id, exc_info=True)


async def touch_bot_heartbeat(platform: str) -> None:
    now = _now()

    def _touch() -> None:
        with _connect() as db:
            db.execute(
                """
                INSERT INTO bot_heartbeats (platform, last_seen) VALUES (?, ?)
                ON CONFLICT(platform) DO UPDATE SET last_seen = excluded.last_seen
                """,
                (platform, now),
            )
            db.commit()

    try:
        await _run_db(_touch)
    except Exception:
        logger.debug("Не удалось обновить heartbeat %s", platform, exc_info=True)


def _count_surveys(db: sqlite3.Connection, platform: str) -> tuple[int, int]:
    """Возвращает (всего анкет, анкет за сегодня)."""
    today = _today_prefix()
    if platform == PLATFORM_TELEGRAM:
        try:
            total = db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            today_n = db.execute(
                "SELECT COUNT(*) FROM profiles WHERE created_at LIKE ?",
                (today + "%",),
            ).fetchone()[0]
            return int(total), int(today_n)
        except sqlite3.Error:
            return 0, 0
    if platform == PLATFORM_MAX:
        try:
            total = db.execute("SELECT COUNT(*) FROM max_profiles").fetchone()[0]
            today_n = db.execute(
                "SELECT COUNT(*) FROM max_profiles WHERE created_at LIKE ?",
                (today + "%",),
            ).fetchone()[0]
            return int(total), int(today_n)
        except sqlite3.Error:
            return 0, 0
    return 0, 0


def _platform_counts(db: sqlite3.Connection, platform: str) -> dict[str, Any]:
    today = _today_prefix()
    starts_total = db.execute(
        "SELECT COUNT(*) FROM bot_starts WHERE platform = ?",
        (platform,),
    ).fetchone()[0]
    starts_unique = db.execute(
        "SELECT COUNT(DISTINCT user_id) FROM bot_starts WHERE platform = ?",
        (platform,),
    ).fetchone()[0]
    starts_today = db.execute(
        "SELECT COUNT(*) FROM bot_starts WHERE platform = ? AND started_at LIKE ?",
        (platform, today + "%"),
    ).fetchone()[0]
    surveys_total, surveys_today = _count_surveys(db, platform)
    hb = db.execute(
        "SELECT last_seen FROM bot_heartbeats WHERE platform = ?",
        (platform,),
    ).fetchone()
    return {
        "starts": int(starts_unique),
        "starts_total": int(starts_total),
        "starts_today": int(starts_today),
        "surveys": int(surveys_total),
        "surveys_today": int(surveys_today),
        "last_seen": hb["last_seen"] if hb else None,
    }


async def get_bot_metrics() -> dict[str, Any]:
    def _get() -> dict[str, Any]:
        with _connect() as db:
            db.executescript(_SCHEMA)
            return {
                "telegram": _platform_counts(db, PLATFORM_TELEGRAM),
                "max": _platform_counts(db, PLATFORM_MAX),
            }

    return await _run_db(_get)
