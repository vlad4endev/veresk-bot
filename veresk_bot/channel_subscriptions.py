"""
Подписчики Telegram-канала: хранение, live-учёт и выгрузка через Telethon.

Bot API не отдаёт полный список участников — только chat_member-события.
Полный снимок берём через userbot (аккаунт должен быть админом канала).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

import runtime_settings
from config import DATABASE_PATH, TELEGRAM_CHANNEL_ID, TELEGRAM_CHANNEL_USERNAME

logger = logging.getLogger(__name__)

T = TypeVar("T")

NEW_SUBSCRIBER_DAYS = 3
STATUS_MEMBER = "member"
STATUS_LEFT = "left"
_ACTIVE = frozenset({"member", "administrator", "creator", "restricted"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_subscriptions (
    tg_user_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    joined_at TEXT,
    left_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'event'
);

CREATE INDEX IF NOT EXISTS idx_channel_subs_status
    ON channel_subscriptions (status, joined_at DESC);

CREATE INDEX IF NOT EXISTS idx_channel_subs_joined
    ON channel_subscriptions (joined_at DESC);

CREATE INDEX IF NOT EXISTS idx_channel_subs_name
    ON channel_subscriptions (first_name, last_name, username);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            return dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def _dt_to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def is_new_subscriber(joined_at: str | None, *, days: int = NEW_SUBSCRIBER_DAYS) -> bool:
    dt = _parse_dt(joined_at)
    if not dt:
        return False
    return dt >= datetime.now() - timedelta(days=days)


def get_channel_config() -> dict[str, Any]:
    """ID/username канала: runtime_settings → .env."""
    raw_id = runtime_settings.get("telegram_channel_id")
    channel_id = 0
    if raw_id not in (None, ""):
        try:
            channel_id = int(raw_id)
        except (TypeError, ValueError):
            channel_id = 0
    if not channel_id:
        channel_id = int(TELEGRAM_CHANNEL_ID or 0)

    username = str(runtime_settings.get("telegram_channel_username") or "").strip()
    if not username:
        username = (TELEGRAM_CHANNEL_USERNAME or "").strip()
    username = username.lstrip("@").strip()

    title = str(runtime_settings.get("telegram_channel_title") or "").strip()

    return {
        "channel_id": channel_id,
        "channel_username": username,
        "channel_title": title,
        "configured": bool(channel_id or username),
        "new_days": NEW_SUBSCRIBER_DAYS,
    }


def save_channel_config(
    *,
    channel_id: int | None = None,
    channel_username: str | None = None,
    channel_title: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if channel_id is not None:
        payload["telegram_channel_id"] = int(channel_id) if channel_id else 0
    if channel_username is not None:
        payload["telegram_channel_username"] = str(channel_username).strip().lstrip("@")
    if channel_title is not None:
        payload["telegram_channel_title"] = str(channel_title).strip()
    if payload:
        runtime_settings.set_many(payload)
    return get_channel_config()


def bot_user_id_from_token(token: str | None = None) -> int:
    """Числовой id бота — первая часть BOT_TOKEN до ':'."""
    from config import BOT_TOKEN as _tok

    raw = (token or _tok or "").strip()
    try:
        return int(raw.split(":", 1)[0])
    except (TypeError, ValueError, IndexError):
        return 0


def remember_bot_admin_channel(
    *,
    channel_id: int,
    channel_username: str | None = None,
    channel_title: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """
    Сохранить канал, где бот стал админом.
    Если канал уже настроен на другой id — не перезаписываем (кроме force=True).
    """
    cfg = get_channel_config()
    cid = int(channel_id)
    uname = (channel_username or "").strip().lstrip("@")
    title = (channel_title or "").strip()

    if cfg["configured"] and not force:
        if cfg["channel_id"] and cfg["channel_id"] != cid:
            return {**cfg, "skipped": True, "reason": "other_channel_configured"}
        # Тот же канал / только username — допишем недостающие поля
        save_kwargs: dict[str, Any] = {}
        if not cfg["channel_id"]:
            save_kwargs["channel_id"] = cid
        elif cfg["channel_id"] == cid:
            save_kwargs["channel_id"] = cid
        if uname and not cfg["channel_username"]:
            save_kwargs["channel_username"] = uname
        elif uname and cfg["channel_id"] == cid:
            save_kwargs["channel_username"] = uname
        if title and (not cfg.get("channel_title") or cfg["channel_id"] == cid):
            save_kwargs["channel_title"] = title
        if save_kwargs:
            return {**save_channel_config(**save_kwargs), "skipped": False, "updated": True}
        return {**cfg, "skipped": False, "updated": False}

    return {
        **save_channel_config(
            channel_id=cid,
            channel_username=uname or None,
            channel_title=title or None,
        ),
        "skipped": False,
        "updated": True,
    }


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


async def _run_db(fn: Callable[[], T]) -> T:
    return await asyncio.to_thread(fn)


async def init_channel_subscriptions() -> None:
    def _init() -> None:
        Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
        with _connect() as db:
            db.executescript(_SCHEMA)
            db.commit()

    await _run_db(_init)
    logger.info("Подписчики канала: таблица готова (%s)", DATABASE_PATH)


def _full_name(first_name: str, last_name: str, username: str) -> str:
    name = " ".join(x for x in (first_name.strip(), last_name.strip()) if x).strip()
    if name:
        return name
    if username:
        return f"@{username.lstrip('@')}"
    return "Без имени"


def _subscriber_tags(
    *,
    is_new: bool,
    has_survey: bool,
    is_linked: bool,
    status: str,
) -> list[dict[str, str]]:
    tags: list[dict[str, str]] = []
    if status == STATUS_MEMBER:
        tags.append({"id": "member", "label": "Подписан", "tone": "regular"})
    else:
        tags.append({"id": "left", "label": "Отписался", "tone": "inactive"})
    if is_new:
        tags.append({"id": "new", "label": "Новый", "tone": "new"})
    if has_survey:
        tags.append({"id": "survey", "label": "Анкета", "tone": "ok"})
    else:
        tags.append({"id": "no_survey", "label": "Без анкеты", "tone": "muted"})
    if is_linked:
        tags.append({"id": "linked", "label": "В базе", "tone": "soft"})
    return tags


def _row_public(
    row: sqlite3.Row | dict[str, Any],
    *,
    customer: dict[str, Any] | None = None,
    has_survey: bool = False,
) -> dict[str, Any]:
    d = dict(row)
    joined_at = d.get("joined_at")
    is_new = d.get("status") == STATUS_MEMBER and is_new_subscriber(joined_at)
    first_name = d.get("first_name") or ""
    last_name = d.get("last_name") or ""
    username = d.get("username") or ""
    cust_name = (customer or {}).get("name") or None
    return {
        "tg_user_id": int(d["tg_user_id"]),
        "channel_id": int(d.get("channel_id") or 0),
        "status": d.get("status") or STATUS_LEFT,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": cust_name or _full_name(first_name, last_name, username),
        "joined_at": joined_at,
        "left_at": d.get("left_at"),
        "first_seen_at": d.get("first_seen_at"),
        "updated_at": d.get("updated_at"),
        "source": d.get("source") or "event",
        "is_new": is_new,
        "has_survey": bool(has_survey),
        "is_linked": bool(customer),
        "customer_id": int(customer["id"]) if customer else None,
        "customer_name": cust_name,
        "customer_phone": (customer or {}).get("phone") or None,
        "tags": _subscriber_tags(
            is_new=is_new,
            has_survey=bool(has_survey),
            is_linked=bool(customer),
            status=d.get("status") or STATUS_LEFT,
        ),
    }


async def upsert_subscription(
    *,
    tg_user_id: int,
    channel_id: int,
    status: str,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
    joined_at: str | None = None,
    source: str = "event",
) -> None:
    if not tg_user_id:
        return
    now = _now()
    status_norm = (status or "").strip().lower() or STATUS_LEFT
    is_active = status_norm in _ACTIVE
    stored_status = STATUS_MEMBER if is_active else STATUS_LEFT

    def _upsert() -> None:
        with _connect() as db:
            db.executescript(_SCHEMA)
            row = db.execute(
                "SELECT * FROM channel_subscriptions WHERE tg_user_id = ?",
                (int(tg_user_id),),
            ).fetchone()
            if row:
                prev = dict(row)
                new_joined = prev.get("joined_at")
                new_left = prev.get("left_at")
                if is_active:
                    if prev.get("status") != STATUS_MEMBER:
                        new_joined = joined_at or now
                    elif joined_at and not new_joined:
                        new_joined = joined_at
                    new_left = None
                else:
                    if prev.get("status") == STATUS_MEMBER:
                        new_left = now
                db.execute(
                    """
                    UPDATE channel_subscriptions SET
                        channel_id = ?,
                        status = ?,
                        username = COALESCE(NULLIF(?, ''), username),
                        first_name = COALESCE(NULLIF(?, ''), first_name),
                        last_name = COALESCE(NULLIF(?, ''), last_name),
                        joined_at = ?,
                        left_at = ?,
                        updated_at = ?,
                        source = ?
                    WHERE tg_user_id = ?
                    """,
                    (
                        int(channel_id),
                        stored_status,
                        username or "",
                        first_name or "",
                        last_name or "",
                        new_joined,
                        new_left,
                        now,
                        source or prev.get("source") or "event",
                        int(tg_user_id),
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT INTO channel_subscriptions (
                        tg_user_id, channel_id, status, username, first_name, last_name,
                        joined_at, left_at, first_seen_at, updated_at, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(tg_user_id),
                        int(channel_id),
                        stored_status,
                        username or "",
                        first_name or "",
                        last_name or "",
                        joined_at or (now if is_active else None),
                        None if is_active else now,
                        now,
                        now,
                        source or "event",
                    ),
                )
            db.commit()

    try:
        await _run_db(_upsert)
    except Exception:
        logger.debug(
            "Не удалось сохранить подписку channel=%s user=%s",
            channel_id,
            tg_user_id,
            exc_info=True,
        )


async def mark_missing_as_left(channel_id: int, present_ids: set[int]) -> int:
    """После полной выгрузки: кто не в списке — отмечен как отписавшийся."""
    now = _now()

    def _mark() -> int:
        with _connect() as db:
            db.executescript(_SCHEMA)
            rows = db.execute(
                """
                SELECT tg_user_id FROM channel_subscriptions
                WHERE channel_id = ? AND status = ?
                """,
                (int(channel_id), STATUS_MEMBER),
            ).fetchall()
            left_n = 0
            for row in rows:
                uid = int(row["tg_user_id"])
                if uid in present_ids:
                    continue
                db.execute(
                    """
                    UPDATE channel_subscriptions
                    SET status = ?, left_at = ?, updated_at = ?, source = 'sync'
                    WHERE tg_user_id = ?
                    """,
                    (STATUS_LEFT, now, now, uid),
                )
                left_n += 1
            db.commit()
            return left_n

    return await _run_db(_mark)


def _normalize_list_filter(
    *,
    status: str | None,
    only_new: bool,
    list_filter: str | None,
) -> str:
    """Единый ключ фильтра: member|new|left|survey|no_survey|linked|unlinked|all."""
    raw = (list_filter or "").strip().lower()
    if raw in {
        "member",
        "new",
        "left",
        "survey",
        "no_survey",
        "linked",
        "unlinked",
        "all",
    }:
        return raw
    if only_new:
        return "new"
    if status in (None, "", "all", "*"):
        return "all"
    if status == STATUS_LEFT:
        return "left"
    return "member"


async def list_subscribers(
    *,
    status: str | None = STATUS_MEMBER,
    search: str | None = None,
    only_new: bool = False,
    list_filter: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    page = max(1, page)
    page_size = max(1, min(page_size, 500))
    offset = (page - 1) * page_size
    q = (search or "").strip()
    new_since = (datetime.now() - timedelta(days=NEW_SUBSCRIBER_DAYS)).isoformat(
        timespec="seconds"
    )
    filt = _normalize_list_filter(status=status, only_new=only_new, list_filter=list_filter)

    def _list() -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        with _connect() as db:
            db.executescript(_SCHEMA)
            # profiles может ещё не быть, если бот не инициализировал client_db
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    tg_id INTEGER PRIMARY KEY,
                    name TEXT,
                    phone TEXT,
                    budget TEXT,
                    source TEXT,
                    events_json TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            base_from = """
                FROM channel_subscriptions s
                LEFT JOIN customers c ON c.tg_user_id = s.tg_user_id
                LEFT JOIN profiles p ON p.tg_id = s.tg_user_id
            """
            where = ["1=1"]
            params: list[Any] = []

            if filt == "member":
                where.append("s.status = ?")
                params.append(STATUS_MEMBER)
            elif filt == "new":
                where.append("s.status = ?")
                params.append(STATUS_MEMBER)
                where.append("s.joined_at IS NOT NULL AND s.joined_at >= ?")
                params.append(new_since)
            elif filt == "left":
                where.append("s.status = ?")
                params.append(STATUS_LEFT)
            elif filt == "survey":
                where.append("s.status = ?")
                params.append(STATUS_MEMBER)
                where.append("p.tg_id IS NOT NULL")
            elif filt == "no_survey":
                where.append("s.status = ?")
                params.append(STATUS_MEMBER)
                where.append("p.tg_id IS NULL")
            elif filt == "linked":
                where.append("s.status = ?")
                params.append(STATUS_MEMBER)
                where.append("c.id IS NOT NULL")
            elif filt == "unlinked":
                where.append("s.status = ?")
                params.append(STATUS_MEMBER)
                where.append("c.id IS NULL")

            if q:
                like = f"%{q}%"
                where.append(
                    """(
                        s.first_name LIKE ? OR s.last_name LIKE ?
                        OR s.username LIKE ? OR CAST(s.tg_user_id AS TEXT) LIKE ?
                        OR (s.first_name || ' ' || s.last_name) LIKE ?
                        OR IFNULL(c.name, '') LIKE ?
                        OR IFNULL(c.phone, '') LIKE ?
                    )"""
                )
                params.extend([like, like, like, like, like, like, like])

            where_sql = " AND ".join(where)
            total = db.execute(
                f"SELECT COUNT(*) {base_from} WHERE {where_sql}",
                params,
            ).fetchone()[0]

            rows = db.execute(
                f"""
                SELECT s.*,
                       c.id AS _cust_id, c.name AS _cust_name, c.phone AS _cust_phone,
                       CASE WHEN p.tg_id IS NULL THEN 0 ELSE 1 END AS _has_survey
                {base_from}
                WHERE {where_sql}
                ORDER BY
                    CASE WHEN s.status = 'member'
                         AND s.joined_at IS NOT NULL
                         AND s.joined_at >= ? THEN 0 ELSE 1 END,
                    CASE WHEN p.tg_id IS NOT NULL THEN 0 ELSE 1 END,
                    COALESCE(s.joined_at, s.first_seen_at) DESC,
                    s.tg_user_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, new_since, page_size, offset],
            ).fetchall()

            def _count(extra_sql: str, extra_params: list[Any] | tuple[Any, ...] = ()) -> int:
                try:
                    return int(
                        db.execute(
                            f"SELECT COUNT(*) {base_from} WHERE {extra_sql}",
                            list(extra_params),
                        ).fetchone()[0]
                    )
                except sqlite3.Error:
                    return 0

            members = _count("s.status = ?", (STATUS_MEMBER,))
            new_n = _count(
                "s.status = ? AND s.joined_at IS NOT NULL AND s.joined_at >= ?",
                (STATUS_MEMBER, new_since),
            )
            left_n = _count("s.status = ?", (STATUS_LEFT,))
            survey_n = _count("s.status = ? AND p.tg_id IS NOT NULL", (STATUS_MEMBER,))
            no_survey_n = _count("s.status = ? AND p.tg_id IS NULL", (STATUS_MEMBER,))
            linked_n = _count("s.status = ? AND c.id IS NOT NULL", (STATUS_MEMBER,))
            unlinked_n = _count("s.status = ? AND c.id IS NULL", (STATUS_MEMBER,))

            items: list[dict[str, Any]] = []
            for r in rows:
                cust = None
                if r["_cust_id"] is not None:
                    cust = {
                        "id": int(r["_cust_id"]),
                        "name": r["_cust_name"] or "",
                        "phone": r["_cust_phone"] or "",
                    }
                items.append(
                    _row_public(
                        r,
                        customer=cust,
                        has_survey=bool(r["_has_survey"]),
                    )
                )

            stats = {
                "members": members,
                "new": new_n,
                "left": left_n,
                "survey": survey_n,
                "no_survey": no_survey_n,
                "linked": linked_n,
                "unlinked": unlinked_n,
                "total_tracked": members + left_n,
            }
            return items, int(total), stats

    return await _run_db(_list)


async def list_member_tg_ids(*, only_new: bool = False) -> list[int]:
    """tg_user_id активных подписчиков (опционально только новые)."""
    new_since = (datetime.now() - timedelta(days=NEW_SUBSCRIBER_DAYS)).isoformat(
        timespec="seconds"
    )

    def _ids() -> list[int]:
        with _connect() as db:
            db.executescript(_SCHEMA)
            if only_new:
                rows = db.execute(
                    """
                    SELECT tg_user_id FROM channel_subscriptions
                    WHERE status = ? AND joined_at IS NOT NULL AND joined_at >= ?
                    ORDER BY joined_at DESC
                    """,
                    (STATUS_MEMBER, new_since),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT tg_user_id FROM channel_subscriptions
                    WHERE status = ?
                    ORDER BY COALESCE(joined_at, first_seen_at) DESC
                    """,
                    (STATUS_MEMBER,),
                ).fetchall()
        return [int(r["tg_user_id"]) for r in rows]

    return await _run_db(_ids)


async def get_subscription(tg_user_id: int) -> dict[str, Any] | None:
    def _get() -> dict[str, Any] | None:
        with _connect() as db:
            db.executescript(_SCHEMA)
            row = db.execute(
                "SELECT * FROM channel_subscriptions WHERE tg_user_id = ?",
                (int(tg_user_id),),
            ).fetchone()
        return dict(row) if row else None

    return await _run_db(_get)


async def ensure_customer_for_subscriber(tg_user_id: int) -> dict[str, Any] | None:
    """Найти или создать CRM-карточку для подписчика (чтобы писать / слать рассылки)."""
    from mailing_db import get_customer, get_customer_by_tg_user_id, upsert_customer

    tid = int(tg_user_id)
    if not tid:
        return None
    existing = await get_customer_by_tg_user_id(tid)
    if existing:
        return existing

    sub = await get_subscription(tid)
    profile: dict[str, Any] | None = None
    try:
        from client_db import get_client_profile

        profile = await get_client_profile(tid)
    except Exception:
        profile = None

    name = ""
    phone = ""
    if profile:
        name = str(profile.get("name") or "").strip()
        phone = str(profile.get("phone") or "").strip()
    if not name and sub:
        name = _full_name(
            str(sub.get("first_name") or ""),
            str(sub.get("last_name") or ""),
            str(sub.get("username") or ""),
        )
    if not name:
        name = f"TG {tid}"

    cid = await upsert_customer(
        posiflora_id=f"tg:{tid}",
        name=name,
        phone=phone,
        tg_user_id=tid,
        segment="new",
        notes="Создан из подписчиков канала",
    )
    return await get_customer(cid)


async def ensure_customers_for_subscribers(tg_user_ids: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in tg_user_ids:
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if not tid or tid in seen:
            continue
        seen.add(tid)
        cust = await ensure_customer_for_subscriber(tid)
        if cust:
            out.append(cust)
    return out


async def customers_for_channel_subscribers(*, only_new: bool = False) -> list[dict[str, Any]]:
    """CRM-клиенты для сегмента рассылки «Подписчики канала»."""
    ids = await list_member_tg_ids(only_new=only_new)
    return await ensure_customers_for_subscribers(ids)


async def sync_channel_subscribers_via_telethon(
    *,
    session_file: str,
    account_id: int | None = None,
    channel_ref: str | int | None = None,
) -> dict[str, Any]:
    """Полная выгрузка участников канала через Telethon-сессию."""
    cfg = get_channel_config()
    ref: str | int | None = channel_ref
    if ref is None:
        if cfg["channel_username"]:
            ref = cfg["channel_username"]
        elif cfg["channel_id"]:
            ref = int(cfg["channel_id"])
    if not ref:
        return {
            "ok": False,
            "error": "channel_not_configured",
            "detail": "Укажите ID или @username канала в настройках подписчиков",
        }
    if not session_file:
        return {
            "ok": False,
            "error": "no_session",
            "detail": "Нет подключённого Telegram-аккаунта для выгрузки",
        }

    try:
        from senders.telegram_chat import telegram_session
        from telethon.tl.types import (
            ChannelParticipant,
            ChannelParticipantAdmin,
            ChannelParticipantBanned,
            ChannelParticipantCreator,
            ChannelParticipantSelf,
            User,
        )
    except ImportError as exc:
        return {
            "ok": False,
            "error": "telethon_missing",
            "detail": str(exc),
        }

    present: set[int] = set()
    upserts = 0
    channel_id = int(cfg["channel_id"] or 0)
    is_broadcast = False
    participants_count: int | None = None

    try:
        async with telegram_session(session_file, account_id) as client:
            entity = await client.get_entity(ref)
            channel_id = int(getattr(entity, "id", 0) or 0)
            is_broadcast = bool(getattr(entity, "broadcast", False))
            is_channel_like = bool(
                is_broadcast or getattr(entity, "megagroup", False)
            )
            # Для каналов/супергрупп Telethon отдаёт «короткий» id;
            # bot API и chat_member используют -100XXXXXXXXXX.
            if channel_id > 0 and is_channel_like:
                full_id = int(f"-100{channel_id}")
            elif channel_id < 0:
                full_id = channel_id
            else:
                full_id = channel_id

            # Сохраняем полный id канала, если ещё не был задан
            if full_id and full_id != cfg["channel_id"]:
                save_channel_config(channel_id=full_id)
                channel_id = full_id
            else:
                channel_id = full_id or channel_id

            try:
                full = await client.get_entity(entity)
                participants_count = getattr(full, "participants_count", None)
                if participants_count is None:
                    from telethon.tl.functions.channels import GetFullChannelRequest

                    full_ch = await client(GetFullChannelRequest(entity))
                    participants_count = getattr(
                        getattr(full_ch, "full_chat", None),
                        "participants_count",
                        None,
                    )
            except Exception:
                logger.debug("Не удалось получить participants_count", exc_info=True)

            async for user in client.iter_participants(entity, aggressive=True):
                if not isinstance(user, User) or getattr(user, "bot", False):
                    continue
                uid = int(user.id)
                present.add(uid)
                participant = getattr(user, "participant", None)
                joined_dt: datetime | None = None
                status = STATUS_MEMBER
                if isinstance(
                    participant,
                    (
                        ChannelParticipant,
                        ChannelParticipantSelf,
                        ChannelParticipantAdmin,
                        ChannelParticipantCreator,
                    ),
                ):
                    raw_date = getattr(participant, "date", None)
                    if isinstance(raw_date, datetime):
                        joined_dt = raw_date
                elif isinstance(participant, ChannelParticipantBanned):
                    status = STATUS_LEFT
                    raw_date = getattr(participant, "date", None)
                    if isinstance(raw_date, datetime):
                        joined_dt = raw_date

                await upsert_subscription(
                    tg_user_id=uid,
                    channel_id=channel_id,
                    status=status,
                    username=str(getattr(user, "username", None) or ""),
                    first_name=str(getattr(user, "first_name", None) or ""),
                    last_name=str(getattr(user, "last_name", None) or ""),
                    joined_at=_dt_to_iso(joined_dt),
                    source="sync",
                )
                upserts += 1
    except Exception as exc:
        logger.exception("Ошибка выгрузки подписчиков канала")
        return {
            "ok": False,
            "error": "sync_failed",
            "detail": str(exc)[:400],
        }

    # У broadcast-каналов API обычно отдаёт только админов — не помечаем остальных left.
    left_n = 0
    can_mark_left = bool(channel_id) and present and not is_broadcast
    if can_mark_left and participants_count and len(present) < max(3, int(participants_count) * 0.5):
        can_mark_left = False
    if can_mark_left:
        left_n = await mark_missing_as_left(channel_id, present)

    _, _, stats = await list_subscribers(status=STATUS_MEMBER, page=1, page_size=1)
    note = None
    if is_broadcast:
        note = (
            "У публичного канала Telegram не отдаёт полный список подписчиков — "
            "только админов. Новые подписки и отписки учитываются автоматически, "
            "если бот — админ канала."
        )
    return {
        "ok": True,
        "channel_id": channel_id,
        "synced": upserts,
        "marked_left": left_n,
        "stats": stats,
        "broadcast": is_broadcast,
        "participants_count": participants_count,
        "note": note,
    }


def matches_configured_channel(chat_id: int, chat_username: str | None = None) -> bool:
    """True, если событие относится к настроенному каналу."""
    cfg = get_channel_config()
    configured = int(cfg["channel_id"] or 0)
    uname = (cfg["channel_username"] or "").lower()
    if configured:
        return int(chat_id) == configured
    if uname:
        return (chat_username or "").lower().lstrip("@") == uname
    # Пока канал не задан — не пишем чужие chat_member-события
    return False


def _channel_full_id(entity: Any) -> int:
    channel_id = int(getattr(entity, "id", 0) or 0)
    is_channel_like = bool(
        getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False)
    )
    if channel_id > 0 and is_channel_like:
        return int(f"-100{channel_id}")
    return channel_id


async def discover_channels_where_bot_is_admin(
    *,
    session_file: str,
    account_id: int | None = None,
    bot_id: int | None = None,
    auto_save: bool = True,
) -> dict[str, Any]:
    """
    Найти каналы, где бот — админ (через Telethon-сессию userbot).

    Userbot должен состоять в этих каналах (видеть их в диалогах).
    Если найден ровно один канал и конфиг пуст — сохраняем автоматически.
    """
    if not session_file:
        return {
            "ok": False,
            "error": "no_session",
            "detail": "Нет подключённого Telegram-аккаунта",
            "channels": [],
        }

    bid = int(bot_id or bot_user_id_from_token() or 0)
    if not bid:
        return {
            "ok": False,
            "error": "no_bot_id",
            "detail": "Не удалось определить id бота из BOT_TOKEN",
            "channels": [],
        }

    try:
        from senders.telegram_chat import telegram_session
        from telethon.tl.types import ChannelParticipantsAdmins
    except ImportError as exc:
        return {
            "ok": False,
            "error": "telethon_missing",
            "detail": str(exc),
            "channels": [],
        }

    found: list[dict[str, Any]] = []
    try:
        async with telegram_session(session_file, account_id) as client:
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                # Только каналы (broadcast), не супергруппы/чаты
                if not bool(getattr(entity, "broadcast", False)):
                    continue
                try:
                    bot_is_admin = False
                    async for admin in client.iter_participants(
                        entity, filter=ChannelParticipantsAdmins()
                    ):
                        if int(getattr(admin, "id", 0) or 0) == bid:
                            bot_is_admin = True
                            break
                    if not bot_is_admin:
                        continue
                except Exception:
                    logger.debug(
                        "Не удалось проверить админов канала %s",
                        getattr(entity, "id", "?"),
                        exc_info=True,
                    )
                    continue

                full_id = _channel_full_id(entity)
                title = str(getattr(entity, "title", None) or dialog.name or "").strip()
                username = str(getattr(entity, "username", None) or "").strip()
                participants_count = getattr(entity, "participants_count", None)
                try:
                    participants_count = (
                        int(participants_count) if participants_count is not None else None
                    )
                except (TypeError, ValueError):
                    participants_count = None

                found.append(
                    {
                        "channel_id": full_id,
                        "channel_username": username,
                        "channel_title": title,
                        "participants_count": participants_count,
                    }
                )
    except Exception as exc:
        logger.exception("Ошибка поиска каналов, где бот админ")
        return {
            "ok": False,
            "error": "discover_failed",
            "detail": str(exc)[:400],
            "channels": [],
        }

    found.sort(
        key=lambda c: (
            -(c.get("participants_count") or 0),
            (c.get("channel_title") or "").lower(),
        )
    )

    cfg = get_channel_config()
    auto_saved = False
    if auto_save and len(found) == 1 and not cfg.get("configured"):
        ch = found[0]
        remember_bot_admin_channel(
            channel_id=int(ch["channel_id"]),
            channel_username=ch.get("channel_username") or "",
            channel_title=ch.get("channel_title") or "",
            force=True,
        )
        auto_saved = True
    elif auto_save and len(found) == 1 and cfg.get("configured"):
        ch = found[0]
        if int(cfg.get("channel_id") or 0) in (0, int(ch["channel_id"])) or (
            cfg.get("channel_username") or ""
        ).lower() == (ch.get("channel_username") or "").lower():
            remember_bot_admin_channel(
                channel_id=int(ch["channel_id"]),
                channel_username=ch.get("channel_username") or "",
                channel_title=ch.get("channel_title") or "",
                force=False,
            )

    # Кэш списка для UI
    try:
        runtime_settings.set_many(
            {
                "telegram_bot_admin_channels": found,
                "telegram_bot_admin_channels_at": _now(),
            }
        )
    except Exception:
        logger.debug("Не удалось сохранить кэш найденных каналов", exc_info=True)

    return {
        "ok": True,
        "channels": found,
        "bot_id": bid,
        "auto_saved": auto_saved,
        "channel": get_channel_config(),
        "need_pick": len(found) > 1 and not get_channel_config().get("configured"),
    }
