"""
HTTP API админ-панели рассылок: /api/admin/*
Авторизация: Bearer-токен после POST /api/admin/login (логин + пароль).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web

from ai_compose import (
    PROVIDER_PRESETS,
    PROVIDERS,
    AiComposeError,
    ai_settings_public,
    generate_mailing_text,
    is_ai_configured,
)
from config import ADMIN_PASSWORD, ADMIN_USERNAME, BOT_TOKEN
from mailing_db import (
    add_campaign_recipients,
    count_customers,
    create_admin_session,
    create_campaign,
    create_personal_message,
    create_send_account,
    delete_admin_session,
    delete_send_account,
    get_campaign,
    get_customer,
    get_customer_by_phone,
    get_customer_by_tg_user_id,
    get_event,
    get_send_account,
    get_stats,
    list_campaign_recipients,
    list_campaigns,
    list_customers,
    get_order_stats_for_customer,
    list_events_for_customer,
    list_messages_for_customer,
    list_orders_for_customer,
    list_send_accounts,
    list_upcoming_events,
    next_events_for_customers,
    set_customer_tg_by_phone,
    set_event_auto_send,
    touch_admin_session,
    update_campaign,
    update_send_account,
    upsert_customer,
    validate_admin_session,
    customers_for_segment,
    ADMIN_SESSION_HOURS,
    normalize_phone_db,
)
import runtime_settings
from bot_metrics import get_bot_metrics, init_bot_metrics
from posiflora_sync import last_sync_info, sync_from_posiflora
from senders.max_bot import get_max_bot_token, is_max_configured
from senders.telegram_userbot import (
    check_telegram_session,
    confirm_telegram_login,
    get_api_credentials,
    is_telethon_configured,
    remove_session_file,
    start_telegram_login,
)

logger = logging.getLogger(__name__)

AUTH_HEADER = "Authorization"


def _cors() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    }


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=_cors())


async def handle_options(_request: web.Request) -> web.Response:
    return web.Response(status=204, headers=_cors())


def _extract_token(request: web.Request) -> str:
    auth = request.headers.get(AUTH_HEADER, "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query.get("token", "").strip()


async def _require_admin(request: web.Request) -> web.Response | None:
    """Возвращает Response с ошибкой или None, если OK."""
    if not ADMIN_PASSWORD:
        return _json({"error": "admin_not_configured"}, status=503)
    token = _extract_token(request)
    if not token or not await validate_admin_session(token):
        return _json({"error": "unauthorized"}, status=401)
    return None


def _mask_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 10:
        return f"+7 {digits[-10:-7]} ··· {digits[-2:]}"
    return phone or "—"


def _channel_for_customer(c: dict) -> str:
    parts = []
    if c.get("tg_user_id") or c.get("phone"):
        parts.append("Telegram")
    if c.get("max_user_id"):
        parts.append("MAX")
    return ",".join(parts) or "Telegram"


def _segment_label(seg: str) -> str:
    return {
        "all": "Все",
        "regular": "Постоянный",
        "new": "Новый",
        "inactive": "Давно не заказывал",
    }.get(seg, seg)


def _format_relative(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return iso
    delta = datetime.now() - dt
    days = delta.days
    if days < 0:
        return iso[:10]
    if days == 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 30:
        return f"{days} дн. назад"
    if days < 365:
        return f"{days // 30} мес. назад"
    return f"{days // 365} г. назад"


def _when_label(days_until: int) -> tuple[str, str]:
    if days_until == 0:
        return "Сегодня", "today"
    if days_until == 1:
        return "Завтра", "soon"
    return f"через {days_until} дн.", "later"


# ── auth ───────────────────────────────────────────────────────────────────


async def handle_login(request: web.Request) -> web.Response:
    if not ADMIN_PASSWORD:
        return _json({"error": "admin_not_configured"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    username = str(body.get("username") or body.get("login") or "").strip()
    password = str(body.get("password") or body.get("token") or "").strip()
    # Логин сравниваем без учёта регистра (удобно с телефона), пароль — строго
    user_ok = secrets.compare_digest(username.lower(), ADMIN_USERNAME.lower())
    pass_ok = secrets.compare_digest(password, ADMIN_PASSWORD)
    if not username or not password or not (user_ok and pass_ok):
        return _json({"error": "invalid_credentials"}, status=401)
    session = secrets.token_urlsafe(32)
    await create_admin_session(session)
    return _json({"token": session, "expires_hours": 72, "username": ADMIN_USERNAME})


async def handle_logout(request: web.Request) -> web.Response:
    token = _extract_token(request)
    if token:
        await delete_admin_session(token)
    return _json({"ok": True})


async def handle_me(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    token = _extract_token(request)
    expires_at = await touch_admin_session(token) if token else None
    return _json(
        {
            "ok": True,
            "role": "admin",
            "username": ADMIN_USERNAME,
            "source": "env",
            "session_hours": ADMIN_SESSION_HOURS,
            "expires_at": expires_at,
            "session_renewed": True,
        }
    )


# ── stats / sync ───────────────────────────────────────────────────────────


async def handle_stats(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    stats = await get_stats()
    sync = last_sync_info()
    return _json({**stats, "sync": sync})


async def handle_sync(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    result = await sync_from_posiflora()
    status = 200 if result.get("ok") else 502
    return _json(result, status=status)


# ── clients ────────────────────────────────────────────────────────────────


async def handle_clients_list(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    segment = request.query.get("segment") or None
    if segment == "all":
        segment = None
    # UI labels → internal
    seg_map = {
        "Постоянные": "regular",
        "Новые": "new",
        "Давно не заказывали": "inactive",
        "regular": "regular",
        "new": "new",
        "inactive": "inactive",
    }
    if segment:
        segment = seg_map.get(segment, segment)
    search = request.query.get("search") or None
    page = int(request.query.get("page", "1"))
    page_size = min(int(request.query.get("page_size", "50")), 200)
    rows, total = await list_customers(
        segment=segment, search=search, page=page, page_size=page_size
    )
    next_events = await next_events_for_customers([c["id"] for c in rows])
    items = []
    for c in rows:
        ev = next_events.get(c["id"])
        next_event = None
        if ev:
            when, when_cls = _when_label(ev["days_until"])
            next_event = {
                "title": ev["title"],
                "kind": ev["kind"],
                "days_until": ev["days_until"],
                "next_date": ev["next_date"],
                "when_label": when,
                "when_cls": when_cls,
            }
        items.append(
            {
                "id": c["id"],
                "name": c["name"],
                "phone": c["phone"],
                "phone_masked": _mask_phone(c["phone"]),
                "segment": c["segment"],
                "segment_label": _segment_label(c["segment"]),
                "channels": _channel_for_customer(c),
                "tg_user_id": c.get("tg_user_id"),
                "max_user_id": c.get("max_user_id"),
                "last_order_at": c.get("last_order_at"),
                "last_order_label": _format_relative(c.get("last_order_at")),
                "created_in_pf_at": c.get("created_in_pf_at"),
                "next_event": next_event,
            }
        )
    return _json({"items": items, "total": total, "page": page, "page_size": page_size})


async def handle_client_detail(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        customer_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json({"error": "bad_id"}, status=400)
    c = await get_customer(customer_id)
    if not c:
        return _json({"error": "not_found"}, status=404)
    events = await list_events_for_customer(customer_id)
    messages = await list_messages_for_customer(customer_id)
    orders = await list_orders_for_customer(customer_id)
    order_stats = await get_order_stats_for_customer(customer_id)
    return _json(
        {
            "id": c["id"],
            "name": c["name"],
            "phone": c["phone"],
            "phone_masked": _mask_phone(c["phone"]),
            "segment": c["segment"],
            "segment_label": _segment_label(c["segment"]),
            "channels": _channel_for_customer(c),
            "tg_user_id": c.get("tg_user_id"),
            "max_user_id": c.get("max_user_id"),
            "notes": c.get("notes") or "",
            "last_order_at": c.get("last_order_at"),
            "last_order_label": _format_relative(c.get("last_order_at")),
            "created_in_pf_at": c.get("created_in_pf_at"),
            "since_label": _format_relative(c.get("created_in_pf_at")),
            "events": [
                {
                    "id": e["id"],
                    "title": e["title"],
                    "kind": e["kind"],
                    "date_from": e["date_from"],
                    "auto_send": bool(e["auto_send"]),
                }
                for e in events
            ],
            "messages": messages,
            "orders": [
                {
                    "id": o["id"],
                    "number": o.get("number") or "",
                    "amount": o.get("amount") or 0,
                    "status": o.get("status") or "",
                    "comment": o.get("comment") or "",
                    "ordered_at": o.get("ordered_at"),
                    "ordered_label": _format_relative(o.get("ordered_at")),
                    "delivery_at": o.get("delivery_at"),
                }
                for o in orders
            ],
            "order_stats": order_stats,
        }
    )


# ── events ─────────────────────────────────────────────────────────────────


async def handle_events_upcoming(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    days = int(request.query.get("days", "14"))
    events = await list_upcoming_events(days=days)
    items = []
    for e in events:
        when, when_class = _when_label(int(e.get("days_until", 0)))
        chan = "Telegram" if e.get("tg_user_id") or e.get("customer_phone") else "MAX"
        chan_class = "tg" if chan == "Telegram" else "max"
        items.append(
            {
                "id": e["id"],
                "customer_id": e.get("cust_id") or e.get("customer_id"),
                "customer_name": e.get("customer_name"),
                "phone": e.get("customer_phone"),
                "phone_masked": _mask_phone(e.get("customer_phone") or ""),
                "title": e["title"],
                "kind": e["kind"],
                "date_from": e["date_from"],
                "next_date": e.get("next_date"),
                "days_until": e.get("days_until"),
                "when_label": when,
                "when_class": when_class,
                "auto_send": bool(e["auto_send"]),
                "channel": chan,
                "channel_class": chan_class,
            }
        )
    return _json({"items": items})


async def handle_event_patch(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        event_id = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json({"error": "bad_id"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    if "auto_send" in body:
        ok = await set_event_auto_send(event_id, bool(body["auto_send"]))
        if not ok:
            return _json({"error": "not_found"}, status=404)
    ev = await get_event(event_id)
    return _json({"ok": True, "event": ev})


# ── campaigns ──────────────────────────────────────────────────────────────


def _campaign_public(c: dict) -> dict:
    status = c["status"]
    status_map = {
        "draft": ("Черновик", "draft"),
        "scheduled": ("Запланирована", "plan"),
        "sending": ("Отправляется", "sending"),
        "done": ("Отправлено", "done"),
        "error": ("Ошибка", "err"),
    }
    label, sclass = status_map.get(status, (status, "neutral"))
    when = "—"
    if status == "sending":
        when = f"Идёт сейчас · {c.get('sent_count', 0)} из {c.get('total_count', 0)}"
    elif status == "scheduled" and c.get("scheduled_at"):
        when = f"Запланирована на {c['scheduled_at']}"
    elif status == "done":
        when = f"Отправлена {c.get('updated_at') or c.get('created_at') or ''}"
    elif status == "draft":
        when = "Ещё не отправлена"
    channels = (c.get("channels") or "tg").replace("tg", "Telegram").replace("max", "MAX")
    return {
        "id": c["id"],
        "title": c["title"],
        "emoji": c.get("emoji") or "🌷",
        "message": c["message"],
        "segment": c["segment"],
        "segment_label": _segment_label(c["segment"]),
        "channels": channels,
        "status": status,
        "status_label": label,
        "status_class": sclass,
        "when": when,
        "scheduled_at": c.get("scheduled_at"),
        "total_count": c.get("total_count", 0),
        "sent_count": c.get("sent_count", 0),
        "delivered_count": c.get("delivered_count", 0),
        "failed_count": c.get("failed_count", 0),
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
    }


async def handle_campaigns_list(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    rows = await list_campaigns()
    return _json({"items": [_campaign_public(c) for c in rows]})


async def handle_campaign_get(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        cid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json({"error": "bad_id"}, status=400)
    c = await get_campaign(cid)
    if not c:
        return _json({"error": "not_found"}, status=404)
    return _json(_campaign_public(c))


async def handle_campaign_create(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    title = str(body.get("title") or "Рассылка").strip()
    message = str(body.get("message") or "").strip()
    if not message:
        return _json({"error": "message_required"}, status=400)
    segment = str(body.get("segment") or "all")
    seg_map = {
        "Постоянные": "regular",
        "Все клиенты": "all",
        "Новые": "new",
        "Давно не заказывали": "inactive",
    }
    segment = seg_map.get(segment, segment)
    channels = str(body.get("channels") or "tg")
    emoji = str(body.get("emoji") or "🌷")
    send_now = bool(body.get("send_now"))
    scheduled_at = body.get("scheduled_at")
    status = "draft"
    if send_now:
        status = "sending"
    elif scheduled_at:
        status = "scheduled"

    cid = await create_campaign(
        title=title,
        message=message,
        segment=segment,
        channels=channels,
        emoji=emoji,
        status=status,
        scheduled_at=scheduled_at,
    )
    customers = await customers_for_segment(segment)
    recipients: list[tuple[int, str]] = []
    ch_list = [x.strip() for x in channels.replace("Telegram", "tg").replace("MAX", "max").split(",") if x.strip()]
    if not ch_list:
        ch_list = ["tg"]
    for cust in customers:
        for ch in ch_list:
            recipients.append((int(cust["id"]), ch if ch in ("tg", "max") else "tg"))
    if recipients:
        await add_campaign_recipients(cid, recipients)
    c = await get_campaign(cid)
    return _json(_campaign_public(c), status=201)


async def handle_campaign_patch(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        cid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json({"error": "bad_id"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    fields: dict[str, Any] = {}
    for key in ("title", "message", "emoji", "segment", "channels", "status", "scheduled_at"):
        if key in body:
            fields[key] = body[key]
    if body.get("send_now"):
        fields["status"] = "sending"
        fields["scheduled_at"] = None
    ok = await update_campaign(cid, **fields)
    if not ok:
        return _json({"error": "not_found"}, status=404)
    c = await get_campaign(cid)
    return _json(_campaign_public(c))


async def handle_campaign_recipients(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        cid = int(request.match_info["id"])
    except (KeyError, ValueError):
        return _json({"error": "bad_id"}, status=400)
    search = request.query.get("search")
    page = int(request.query.get("page", "1"))
    rows, total = await list_campaign_recipients(
        cid, search=search, page=page, page_size=50
    )
    items = [
        {
            "id": r["id"],
            "customer_id": r["customer_id"],
            "name": r.get("customer_name"),
            "phone": r.get("customer_phone"),
            "phone_masked": _mask_phone(r.get("customer_phone") or ""),
            "channel": r["channel"],
            "status": r["status"],
            "sent_at": r.get("sent_at"),
            "error": r.get("error"),
        }
        for r in rows
    ]
    return _json({"items": items, "total": total, "page": page})


# ── personal ───────────────────────────────────────────────────────────────


async def handle_personal(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    try:
        customer_id = int(body["customer_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "customer_id_required"}, status=400)
    message = str(body.get("message") or "").strip()
    if not message:
        return _json({"error": "message_required"}, status=400)
    channel = str(body.get("channel") or "tg")
    if channel in ("Telegram", "telegram"):
        channel = "tg"
    if channel in ("MAX", "max"):
        channel = "max"
    customer = await get_customer(customer_id)
    if not customer:
        return _json({"error": "not_found"}, status=404)
    msg_id = await create_personal_message(customer_id, message, channel=channel)
    return _json({"ok": True, "id": msg_id})


# ── accounts ───────────────────────────────────────────────────────────────


async def handle_accounts_list(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    rows = await list_send_accounts()
    check_live = request.query.get("check") == "1"

    # Параллельно проверяем живые Telethon-сессии при ?check=1
    session_checks: dict[int, dict[str, Any]] = {}
    if check_live:
        tg_rows = [
            a
            for a in rows
            if a.get("kind") == "tg_userbot" and a.get("session_file")
        ]

        async def _check_one(acc: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            result = await check_telegram_session(str(acc.get("session_file") or ""))
            return int(acc["id"]), result

        if tg_rows:
            results = await asyncio.gather(
                *[_check_one(a) for a in tg_rows], return_exceptions=True
            )
            for item in results:
                if isinstance(item, Exception):
                    logger.exception("account session check failed: %s", item)
                    continue
                acc_id, result = item
                session_checks[acc_id] = result

    items = []
    for a in rows:
        entry: dict[str, Any] = {
            "id": a["id"],
            "kind": a["kind"],
            "label": a["label"],
            "phone": a["phone"],
            "phone_masked": _mask_phone(a["phone"]) if a["phone"] else a["label"],
            "daily_limit": a["daily_limit"],
            "sent_today": a["sent_today"],
            "status": a["status"],
            "warmup_until": a.get("warmup_until"),
            "has_session": bool(a.get("session_file")),
            "last_checked_at": a.get("last_checked_at"),
            "last_ok_at": a.get("last_ok_at"),
            "last_error": a.get("last_error"),
        }
        live = session_checks.get(int(a["id"]))
        if live is not None:
            entry["session_ok"] = bool(live.get("ok") and live.get("authorized"))
            entry["session_error"] = live.get("error")
            if live.get("username"):
                entry["tg_username"] = live["username"]
            if live.get("label"):
                entry["tg_name"] = live["label"]
            if live.get("ok") and a.get("kind") == "tg_userbot":
                # Подтянуть имя из живой сессии, если в БД только телефон
                if live.get("label") and (
                    not a.get("label") or a.get("label") == a.get("phone")
                ):
                    await update_send_account(int(a["id"]), label=live["label"])
                    entry["label"] = live["label"]
        items.append(entry)
    # Заглушка MAX, если токена нет и аккаунта нет
    has_max = any(a["kind"] == "max_bot" for a in rows)
    max_ok = is_max_configured()
    if not has_max:
        items.append(
            {
                "id": None,
                "kind": "max_bot",
                "label": "Veresk в MAX",
                "phone": "",
                "phone_masked": "MAX",
                "daily_limit": 150,
                "sent_today": 0,
                "status": "ready" if max_ok else "unavailable",
                "warmup_until": None,
                "placeholder": True,
            }
        )
    return _json(
        {
            "items": items,
            "telethon_configured": is_telethon_configured(),
            "max_configured": max_ok,
            "checked": check_live,
        }
    )


async def _register_telegram_account(result: dict[str, Any], phone: str) -> dict[str, Any]:
    """Сохранить Telethon-сессию как send_account и проверить живой коннект."""
    warmup = (datetime.now() + timedelta(days=4)).date().isoformat()
    account_id = await create_send_account(
        kind="tg_userbot",
        label=result.get("label") or phone,
        phone=result.get("phone") or phone,
        session_file=result.get("session_file") or "",
        daily_limit=200,
        status="warmup",
        warmup_until=warmup,
    )
    live = await check_telegram_session(str(result.get("session_file") or ""))
    return {
        "ok": True,
        "account_id": account_id,
        "session_ok": bool(live.get("ok") and live.get("authorized")),
        "session_error": live.get("error"),
        "tg_username": live.get("username"),
        **result,
    }


async def handle_telegram_connect_start(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    phone = str(body.get("phone") or "").strip()
    if not phone:
        return _json({"error": "phone_required"}, status=400)
    result = await start_telegram_login(phone)
    if not result.get("ok"):
        return _json(result, status=400)
    if result.get("already_authorized"):
        registered = await _register_telegram_account(result, phone)
        return _json(registered)
    return _json(result)


async def handle_telegram_connect_confirm(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    phone = str(body.get("phone") or "").strip()
    code = str(body.get("code") or "").strip()
    password = body.get("password")
    if not phone:
        return _json({"error": "phone_required"}, status=400)
    if not code and not password:
        return _json({"error": "phone_and_code_required"}, status=400)
    result = await confirm_telegram_login(
        phone, code, password=str(password) if password else None
    )
    if not result.get("ok"):
        status = 400
        if result.get("need_2fa"):
            status = 200
        return _json(result, status=status)

    registered = await _register_telegram_account(result, phone)
    return _json(registered)


async def handle_telegram_account_check(request: web.Request) -> web.Response:
    """Проверить живой коннект Telethon-аккаунта."""
    err = await _require_admin(request)
    if err:
        return err
    try:
        account_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    acc = await get_send_account(account_id)
    if not acc:
        return _json({"error": "not_found"}, status=404)
    if acc.get("kind") != "tg_userbot":
        return _json({"error": "not_telegram_account"}, status=400)

    live = await check_telegram_session(str(acc.get("session_file") or ""))
    authorized = bool(live.get("ok") and live.get("authorized"))
    now = datetime.now().isoformat(timespec="seconds")
    patch: dict[str, Any] = {
        "last_checked_at": now,
        "last_error": None if authorized else (live.get("error") or "unauthorized"),
    }
    if not authorized and acc.get("status") not in ("unavailable", "blocked"):
        patch["status"] = "unavailable"
    elif authorized:
        patch["last_ok_at"] = now
        today = datetime.now().date().isoformat()
        wu = acc.get("warmup_until")
        if acc.get("status") == "unavailable":
            if wu and str(wu) > today:
                patch["status"] = "warmup"
            else:
                patch["status"] = "ready"
        if live.get("label"):
            patch["label"] = live["label"]
    await update_send_account(account_id, **patch)

    return _json(
        {
            "ok": authorized,
            "authorized": authorized,
            "account_id": account_id,
            "error": live.get("error"),
            "tg_id": live.get("tg_id"),
            "username": live.get("username"),
            "label": live.get("label"),
            "phone": live.get("phone") or acc.get("phone"),
            "last_ok_at": now if authorized else acc.get("last_ok_at"),
        }
    )


async def handle_telegram_account_delete(request: web.Request) -> web.Response:
    """Отключить Telegram-аккаунт: удалить запись и файл сессии."""
    err = await _require_admin(request)
    if err:
        return err
    try:
        account_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    acc = await get_send_account(account_id)
    if not acc:
        return _json({"error": "not_found"}, status=404)
    if acc.get("kind") != "tg_userbot":
        return _json({"error": "not_telegram_account"}, status=400)

    deleted = await delete_send_account(account_id)
    if not deleted:
        return _json({"error": "not_found"}, status=404)
    try:
        from senders.telegram_chat import release_session

        await release_session(
            session_file=str(deleted.get("session_file") or ""),
            account_id=account_id,
        )
    except Exception:
        logger.exception("release chat session failed for account %s", account_id)
    remove_session_file(str(deleted.get("session_file") or ""))
    return _json({"ok": True, "id": account_id})


async def handle_telegram_keepalive(request: web.Request) -> web.Response:
    """Принудительно продлить/проверить все Telegram-сессии."""
    err = await _require_admin(request)
    if err:
        return err
    from senders.session_keepalive import keepalive_all_telegram_sessions

    result = await keepalive_all_telegram_sessions()
    return _json(result)


async def handle_telegram_settings_get(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    api_id, api_hash = get_api_credentials()
    from_env = bool(runtime_settings.get("telegram_api_id")) is False and bool(api_id)
    return _json(
        {
            "configured": bool(api_id and api_hash),
            "api_id": api_id or None,
            "api_hash_set": bool(api_hash),
            "from_env": from_env,
        }
    )


async def handle_telegram_settings_save(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)

    raw_id = str(body.get("api_id") or "").strip()
    raw_hash = str(body.get("api_hash") or "").strip()
    if not raw_id or not raw_hash:
        return _json({"error": "api_id_and_hash_required"}, status=400)
    try:
        api_id = int(raw_id)
    except ValueError:
        return _json({"error": "api_id_must_be_number"}, status=400)
    if api_id <= 0:
        return _json({"error": "api_id_must_be_positive"}, status=400)

    runtime_settings.set_many(
        {"telegram_api_id": api_id, "telegram_api_hash": raw_hash}
    )
    return _json({"ok": True, "configured": is_telethon_configured()})


def _mask_token(token: str) -> str:
    if len(token) <= 10:
        return "••••••••"
    return token[:4] + "…" + token[-4:]


async def handle_max_settings_get(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    token = get_max_bot_token()
    from_panel = bool(runtime_settings.get("max_bot_token"))
    from_env = bool(token) and not from_panel
    bot_name = None
    bot_username = None
    if token:
        try:
            from max_bot.api import MaxBotAPI

            api = MaxBotAPI(token)
            try:
                me = await api.get_me()
                bot_name = me.get("name") or me.get("first_name")
                bot_username = me.get("username")
            finally:
                await api.close()
        except Exception:
            logger.debug("Не удалось проверить MAX-токен при GET settings", exc_info=True)
    return _json(
        {
            "configured": bool(token),
            "token_set": bool(token),
            "token_masked": _mask_token(token) if token else None,
            "from_env": from_env,
            "from_panel": from_panel,
            "bot_name": bot_name,
            "bot_username": bot_username,
        }
    )


async def handle_max_settings_save(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)

    # clear=true — убрать токен из панели (останется .env, если задан)
    if body.get("clear"):
        runtime_settings.delete_keys("max_bot_token")
        return _json(
            {
                "ok": True,
                "configured": is_max_configured(),
                "cleared": True,
            }
        )

    token = str(body.get("token") or "").strip()
    if not token:
        return _json({"error": "token_required"}, status=400)

    # Проверяем токен через GET /me перед сохранением
    from max_bot.api import MaxAPIError, MaxBotAPI

    api = MaxBotAPI(token)
    try:
        me = await api.get_me()
    except MaxAPIError as exc:
        return _json(
            {
                "ok": False,
                "error": "invalid_token",
                "detail": str(exc),
            },
            status=400,
        )
    finally:
        await api.close()

    runtime_settings.set_many({"max_bot_token": token})
    return _json(
        {
            "ok": True,
            "configured": True,
            "bot_name": me.get("name") or me.get("first_name"),
            "bot_username": me.get("username"),
            "bot_id": me.get("user_id"),
        }
    )


async def handle_segment_counts(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    return _json(
        {
            "all": await count_customers(),
            "regular": await count_customers("regular"),
            "new": await count_customers("new"),
            "inactive": await count_customers("inactive"),
        }
    )


async def handle_ai_compose(request: web.Request) -> web.Response:
    """POST /api/admin/ai/compose — сгенерировать текст рассылки."""
    err = await _require_admin(request)
    if err:
        return err
    if not is_ai_configured():
        return _json(
            {
                "error": "ai_not_configured",
                "detail": "Подключите ИИ в Настройках → Сервисы",
            },
            status=503,
        )
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)

    prompt = str(body.get("prompt") or "").strip()
    current_text = str(body.get("current_text") or "")
    segment = str(body.get("segment") or "all").strip() or "all"
    mode = str(body.get("mode") or "write").strip() or "write"
    if mode not in ("write", "improve"):
        mode = "write"

    try:
        text = await generate_mailing_text(
            prompt=prompt,
            current_text=current_text,
            segment=segment,
            mode=mode,
        )
    except AiComposeError as exc:
        status = 400 if exc.code in ("prompt_required",) else 502
        if exc.code == "ai_not_configured":
            status = 503
        return _json({"error": exc.code, "detail": exc.message}, status=status)

    return _json({"ok": True, "text": text})


async def handle_ai_settings_get(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    return _json(ai_settings_public())


async def handle_ai_settings_save(request: web.Request) -> web.Response:
    """POST /api/admin/ai/settings — сохранить или сбросить настройки ИИ."""
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)

    if body.get("clear"):
        runtime_settings.delete_keys(
            "ai_provider",
            "ai_api_key",
            "ai_api_base",
            "ai_model",
            "ai_folder_id",
        )
        return _json({"ok": True, "cleared": True, **ai_settings_public()})

    provider = str(body.get("provider") or "").strip().lower()
    if provider and provider not in PROVIDERS:
        return _json({"error": "invalid_provider", "detail": "Неизвестный оператор"}, status=400)

    api_key = str(body.get("api_key") or "").strip()
    api_base = str(body.get("api_base") or "").strip().rstrip("/")
    model = str(body.get("model") or "").strip()
    folder_id = str(body.get("folder_id") or "").strip()

    if not api_key and not is_ai_configured():
        return _json({"error": "api_key_required", "detail": "Укажите API-ключ"}, status=400)

    values: dict = {}
    if provider:
        values["ai_provider"] = provider
    else:
        provider = str(runtime_settings.get("ai_provider") or "openai")

    if api_key:
        values["ai_api_key"] = api_key

    preset = PROVIDER_PRESETS.get(provider) or PROVIDER_PRESETS["openai"]
    if provider == "custom":
        if api_base:
            values["ai_api_base"] = api_base
        elif "api_base" in body:
            values["ai_api_base"] = preset["api_base"]
    else:
        # Фиксированный endpoint оператора (можно переопределить явно)
        values["ai_api_base"] = api_base or preset["api_base"]

    if model:
        values["ai_model"] = model
    elif "model" in body or provider:
        values["ai_model"] = model or preset["model"]

    if provider == "yandexgpt":
        if folder_id:
            values["ai_folder_id"] = folder_id
        elif not runtime_settings.get("ai_folder_id"):
            return _json(
                {
                    "error": "folder_id_required",
                    "detail": "Для YandexGPT укажите Folder ID каталога в Yandex Cloud",
                },
                status=400,
            )
    elif "folder_id" in body:
        values["ai_folder_id"] = folder_id

    if values:
        runtime_settings.set_many(values)

    return _json({"ok": True, **ai_settings_public()})


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _heartbeat_age_seconds(last_seen: str | None) -> float | None:
    dt = _parse_iso(last_seen)
    if not dt:
        return None
    return max(0.0, (datetime.now() - dt).total_seconds())


async def _probe_telegram_bot() -> dict[str, Any]:
    """Проверка Telegram Bot API (getMe)."""
    token = (BOT_TOKEN or "").strip()
    if not token:
        return {
            "configured": False,
            "api_ok": False,
            "username": None,
            "name": None,
            "bot_id": None,
            "error": "not_configured",
        }
    try:
        import aiohttp

        url = f"https://api.telegram.org/bot{token}/getMe"
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json(content_type=None)
        if not data.get("ok"):
            return {
                "configured": True,
                "api_ok": False,
                "username": None,
                "name": None,
                "bot_id": None,
                "error": str(data.get("description") or "getMe_failed"),
            }
        result = data.get("result") or {}
        return {
            "configured": True,
            "api_ok": True,
            "username": result.get("username"),
            "name": result.get("first_name") or result.get("username"),
            "bot_id": result.get("id"),
            "error": None,
        }
    except Exception as exc:
        logger.debug("Telegram getMe failed", exc_info=True)
        return {
            "configured": True,
            "api_ok": False,
            "username": None,
            "name": None,
            "bot_id": None,
            "error": str(exc)[:200],
        }


async def _probe_max_bot() -> dict[str, Any]:
    token = get_max_bot_token()
    if not token:
        return {
            "configured": False,
            "api_ok": False,
            "username": None,
            "name": None,
            "bot_id": None,
            "error": "not_configured",
        }
    try:
        from max_bot.api import MaxBotAPI

        api = MaxBotAPI(token)
        try:
            me = await api.get_me()
        finally:
            await api.close()
        return {
            "configured": True,
            "api_ok": True,
            "username": me.get("username"),
            "name": me.get("name") or me.get("first_name") or me.get("username"),
            "bot_id": me.get("user_id"),
            "error": None,
        }
    except Exception as exc:
        logger.debug("MAX getMe failed", exc_info=True)
        return {
            "configured": True,
            "api_ok": False,
            "username": None,
            "name": None,
            "bot_id": None,
            "error": str(exc)[:200],
        }


def _resolve_bot_status(
    *,
    configured: bool,
    api_ok: bool,
    last_seen: str | None,
    stale_after: float = 90.0,
) -> str:
    """online | idle | offline | not_configured"""
    if not configured:
        return "not_configured"
    if not api_ok:
        return "offline"
    age = _heartbeat_age_seconds(last_seen)
    if age is None:
        return "idle"
    if age <= stale_after:
        return "online"
    return "idle"


async def handle_bots_status(request: web.Request) -> web.Response:
    """GET /api/admin/bots/status — статус TG/MAX + запуски и анкеты."""
    err = await _require_admin(request)
    if err:
        return err

    await init_bot_metrics()
    metrics = await get_bot_metrics()
    tg_probe, max_probe = await asyncio.gather(
        _probe_telegram_bot(),
        _probe_max_bot(),
    )

    tg_m = metrics.get("telegram") or {}
    max_m = metrics.get("max") or {}

    telegram = {
        **tg_probe,
        "status": _resolve_bot_status(
            configured=bool(tg_probe.get("configured")),
            api_ok=bool(tg_probe.get("api_ok")),
            last_seen=tg_m.get("last_seen"),
        ),
        "starts": tg_m.get("starts", 0),
        "starts_total": tg_m.get("starts_total", 0),
        "starts_today": tg_m.get("starts_today", 0),
        "surveys": tg_m.get("surveys", 0),
        "surveys_today": tg_m.get("surveys_today", 0),
        "last_seen": tg_m.get("last_seen"),
    }
    max_bot = {
        **max_probe,
        "status": _resolve_bot_status(
            configured=bool(max_probe.get("configured")),
            api_ok=bool(max_probe.get("api_ok")),
            last_seen=max_m.get("last_seen"),
        ),
        "starts": max_m.get("starts", 0),
        "starts_total": max_m.get("starts_total", 0),
        "starts_today": max_m.get("starts_today", 0),
        "surveys": max_m.get("surveys", 0),
        "surveys_today": max_m.get("surveys_today", 0),
        "last_seen": max_m.get("last_seen"),
    }

    return _json(
        {
            "telegram": telegram,
            "max": max_bot,
            "totals": {
                "starts": int(telegram["starts"]) + int(max_bot["starts"]),
                "surveys": int(telegram["surveys"]) + int(max_bot["surveys"]),
                "starts_today": int(telegram["starts_today"])
                + int(max_bot["starts_today"]),
                "surveys_today": int(telegram["surveys_today"])
                + int(max_bot["surveys_today"]),
            },
        }
    )


# ── Telegram chats (live userbot inbox) ─────────────────────────────────────


async def _resolve_chat_account(request: web.Request) -> tuple[dict[str, Any] | None, web.Response | None]:
    """account_id из query/body; если один tg-аккаунт — берём его."""
    raw = request.query.get("account_id")
    if raw in (None, ""):
        try:
            body = getattr(request, "_chat_body", None)
            if isinstance(body, dict):
                raw = body.get("account_id")
        except Exception:
            raw = None

    rows = [
        a
        for a in await list_send_accounts()
        if a.get("kind") == "tg_userbot" and a.get("session_file")
    ]
    if not rows:
        return None, _json(
            {
                "error": "no_telegram_accounts",
                "message": "Подключите Telegram-аккаунт в Настройках",
            },
            status=400,
        )

    if raw in (None, ""):
        if len(rows) == 1:
            return rows[0], None
        return None, _json(
            {
                "error": "account_id_required",
                "message": "Выберите Telegram-аккаунт",
                "accounts": [
                    {
                        "id": a["id"],
                        "label": a.get("label") or a.get("phone"),
                        "phone": a.get("phone"),
                        "status": a.get("status"),
                    }
                    for a in rows
                ],
            },
            status=400,
        )

    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        return None, _json({"error": "invalid_account_id"}, status=400)

    acc = await get_send_account(account_id)
    if not acc or acc.get("kind") != "tg_userbot" or not acc.get("session_file"):
        return None, _json({"error": "account_not_found"}, status=404)
    return acc, None


async def handle_chats_accounts(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    rows = await list_send_accounts()
    items = []
    for a in rows:
        if a.get("kind") != "tg_userbot" or not a.get("session_file"):
            continue
        items.append(
            {
                "id": a["id"],
                "label": a.get("label") or a.get("phone") or f"Аккаунт {a['id']}",
                "phone": a.get("phone"),
                "status": a.get("status"),
                "phone_masked": _mask_phone(a["phone"]) if a.get("phone") else None,
            }
        )
    return _json(
        {
            "items": items,
            "telethon_configured": is_telethon_configured(),
        }
    )


def _truthy_query(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


async def handle_chats_dialogs(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    try:
        limit = int(request.query.get("limit") or 80)
    except (TypeError, ValueError):
        limit = 80
    query = str(request.query.get("q") or "").strip()
    clients_only = _truthy_query(request.query.get("clients_only"))
    only_users = clients_only or _truthy_query(request.query.get("only_users"))
    from senders.telegram_chat import list_dialogs

    # При фильтре по клиентам берём больше диалогов, потом отсекаем
    fetch_limit = min(limit * 4, 200) if clients_only else limit
    try:
        items = await list_dialogs(
            str(acc["session_file"]),
            account_id=int(acc["id"]),
            limit=fetch_limit,
            query=query,
            only_users=only_users,
        )
    except Exception as exc:
        logger.exception("list dialogs failed")
        return _json({"error": str(exc)}, status=502)

    if clients_only:
        from mailing_db import customer_contact_sets

        tg_ids, phones = await customer_contact_sets()

        def _is_known_client(row: dict[str, Any]) -> bool:
            peer_id = row.get("peer_id")
            try:
                if peer_id is not None and int(peer_id) in tg_ids:
                    return True
            except (TypeError, ValueError):
                pass
            phone = re.sub(r"\D", "", str(row.get("phone") or ""))
            if len(phone) >= 10 and phone[-10:] in phones:
                return True
            return False

        items = [row for row in items if _is_known_client(row)][:limit]
    elif only_users:
        items = items[:limit]

    return _json(
        {
            "account_id": int(acc["id"]),
            "account_label": acc.get("label") or acc.get("phone"),
            "only_users": only_users,
            "clients_only": clients_only,
            "items": items,
        }
    )


async def handle_chats_messages(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    try:
        peer_id = int(request.match_info["peer_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_peer_id"}, status=400)
    try:
        limit = int(request.query.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    try:
        offset_id = int(request.query.get("offset_id") or 0)
    except (TypeError, ValueError):
        offset_id = 0
    mark_read = request.query.get("mark_read", "1") != "0"
    from senders.telegram_chat import get_dialog_messages

    try:
        data = await get_dialog_messages(
            str(acc["session_file"]),
            peer_id,
            account_id=int(acc["id"]),
            limit=limit,
            offset_id=offset_id,
            mark_read=mark_read,
        )
    except Exception as exc:
        logger.exception("get messages failed")
        return _json({"error": str(exc)}, status=502)
    data["account_id"] = int(acc["id"])
    return _json(data)


async def handle_chats_send(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    request._chat_body = body  # type: ignore[attr-defined]
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    try:
        peer_id = int(request.match_info["peer_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_peer_id"}, status=400)
    text = str(body.get("text") or body.get("message") or "").strip()
    if not text:
        return _json({"error": "message_required"}, status=400)
    from senders.telegram_chat import send_dialog_message
    from telethon.errors import FloodWaitError

    try:
        msg = await send_dialog_message(
            str(acc["session_file"]),
            peer_id,
            text,
            account_id=int(acc["id"]),
        )
    except FloodWaitError as exc:
        return _json(
            {"error": f"FloodWait {int(getattr(exc, 'seconds', 60))}s"},
            status=429,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("send chat message failed")
        return _json({"error": str(exc)}, status=502)
    return _json({"ok": True, "message": msg, "account_id": int(acc["id"])})


async def handle_chats_send_media(request: web.Request) -> web.Response:
    """multipart/form-data: file|files, caption, account_id, as_document."""
    err = await _require_admin(request)
    if err:
        return err
    try:
        peer_id = int(request.match_info["peer_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_peer_id"}, status=400)

    if not request.content_type.startswith("multipart/"):
        return _json({"error": "multipart_required"}, status=400)

    reader = await request.multipart()
    account_id_raw: str | None = None
    caption = ""
    as_document = False
    files: list[dict[str, Any]] = []

    while True:
        part = await reader.next()
        if part is None:
            break
        name = part.name or ""
        if name == "account_id":
            account_id_raw = (await part.text()).strip()
        elif name in ("caption", "text", "message"):
            caption = (await part.text()).strip()
        elif name in ("as_document", "force_document"):
            as_document = (await part.text()).strip().lower() in ("1", "true", "yes")
        elif name in ("file", "files", "media"):
            filename = part.filename or "file"
            data = await part.read(decode=False)
            if data:
                files.append(
                    {
                        "filename": filename,
                        "data": data,
                        "mime": part.headers.get("Content-Type", ""),
                    }
                )
        else:
            # skip unknown fields
            await part.read(decode=False)

    request._chat_body = {"account_id": account_id_raw}  # type: ignore[attr-defined]
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None

    if not files:
        return _json({"error": "file_required"}, status=400)

    from senders.telegram_chat import MAX_UPLOAD_BYTES, MAX_UPLOAD_FILES, send_dialog_media
    from telethon.errors import FloodWaitError

    if len(files) > MAX_UPLOAD_FILES:
        return _json({"error": f"max_{MAX_UPLOAD_FILES}_files"}, status=400)
    for f in files:
        if len(f["data"]) > MAX_UPLOAD_BYTES:
            return _json({"error": "file_too_large", "max_mb": 50}, status=413)

    try:
        messages = await send_dialog_media(
            str(acc["session_file"]),
            peer_id,
            files,
            caption=caption,
            force_document=as_document,
            account_id=int(acc["id"]),
        )
    except FloodWaitError as exc:
        return _json(
            {"error": f"FloodWait {int(getattr(exc, 'seconds', 60))}s"},
            status=429,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("send media failed")
        return _json({"error": str(exc)}, status=502)

    return _json(
        {
            "ok": True,
            "messages": messages,
            "message": messages[-1] if messages else None,
            "account_id": int(acc["id"]),
        }
    )


def _customer_public(c: dict[str, Any] | None) -> dict[str, Any] | None:
    if not c:
        return None
    return {
        "id": c["id"],
        "name": c.get("name") or "",
        "phone": c.get("phone") or "",
        "phone_masked": _mask_phone(c["phone"]) if c.get("phone") else None,
        "tg_user_id": c.get("tg_user_id"),
        "posiflora_id": c.get("posiflora_id"),
        "segment": c.get("segment") or "all",
    }


async def _find_local_customer_for_peer(
    peer: dict[str, Any],
) -> dict[str, Any] | None:
    tg_id = peer.get("tg_user_id") or (
        peer.get("peer_id") if peer.get("kind") in ("user", "bot") else None
    )
    if tg_id is not None:
        found = await get_customer_by_tg_user_id(int(tg_id))
        if found:
            return found
    phone = peer.get("phone") or ""
    if phone:
        found = await get_customer_by_phone(str(phone))
        if found:
            return found
    # Бот мог уже сохранить телефон по tg_id
    try:
        from client_db import get_client

        if tg_id is not None:
            bot_client = await get_client(int(tg_id))
            if bot_client and bot_client.get("phone"):
                found = await get_customer_by_phone(str(bot_client["phone"]))
                if found:
                    return found
                # обогатим peer телефоном для UI
                peer["phone"] = bot_client["phone"]
                if not peer.get("title") and bot_client.get("name"):
                    peer["title"] = bot_client["name"]
    except Exception:
        pass
    return None


async def handle_chats_client_status(request: web.Request) -> web.Response:
    """Статус: есть ли собеседник в базе клиентов."""
    err = await _require_admin(request)
    if err:
        return err
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    try:
        peer_id = int(request.match_info["peer_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_peer_id"}, status=400)

    from senders.telegram_chat import resolve_peer_profile

    try:
        peer = await resolve_peer_profile(
            str(acc["session_file"]),
            peer_id,
            account_id=int(acc["id"]),
        )
    except Exception as exc:
        logger.exception("resolve peer for client status failed")
        return _json({"error": str(exc)}, status=502)

    customer = await _find_local_customer_for_peer(peer)
    kind = peer.get("kind") or "unknown"
    is_user = kind == "user"

    if customer:
        status = "in_base"
        label = "Клиент уже в базе"
        hint = f"{customer.get('name') or 'Клиент'} · {customer.get('phone') or 'без телефона'}"
        can_create = False
        need_phone = False
    elif not is_user:
        status = "not_user"
        label = "Это не личный чат"
        hint = "Клиента можно создать только из переписки с человеком"
        can_create = False
        need_phone = False
    else:
        phone_ok = bool(re.sub(r"\D", "", str(peer.get("phone") or "")))
        status = "missing"
        label = "Клиента ещё нет в базе"
        if phone_ok:
            hint = "Можно добавить в базу и Posiflora"
            can_create = True
            need_phone = False
        else:
            hint = "В Telegram нет номера — укажите телефон при создании"
            can_create = True
            need_phone = True

    return _json(
        {
            "status": status,
            "label": label,
            "hint": hint,
            "can_create": can_create,
            "need_phone": need_phone,
            "in_base": bool(customer),
            "peer": peer,
            "customer": _customer_public(customer),
            "account_id": int(acc["id"]),
        }
    )


async def handle_chats_client_create(request: web.Request) -> web.Response:
    """Создать клиента из чата: Posiflora + локальная база + привязка TG."""
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    request._chat_body = body if isinstance(body, dict) else {}  # type: ignore[attr-defined]
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    try:
        peer_id = int(request.match_info["peer_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_peer_id"}, status=400)

    from senders.telegram_chat import resolve_peer_profile

    try:
        peer = await resolve_peer_profile(
            str(acc["session_file"]),
            peer_id,
            account_id=int(acc["id"]),
        )
    except Exception as exc:
        logger.exception("resolve peer for create failed")
        return _json({"error": str(exc)}, status=502)

    if peer.get("kind") != "user":
        return _json(
            {
                "error": "not_a_user",
                "message": "Клиента можно создать только из личного чата",
            },
            status=400,
        )

    existing = await _find_local_customer_for_peer(peer)
    if existing:
        # Допривяжем tg_user_id, если ещё не было
        tg_uid = peer.get("tg_user_id") or peer_id
        if not existing.get("tg_user_id") and tg_uid:
            await set_customer_tg_by_phone(existing.get("phone") or "", int(tg_uid))
            existing = await get_customer(int(existing["id"])) or existing
        return _json(
            {
                "ok": True,
                "created": False,
                "already_exists": True,
                "label": "Клиент уже в базе",
                "customer": _customer_public(existing),
                "peer": peer,
            }
        )

    phone_raw = str(body.get("phone") or peer.get("phone") or "").strip()
    phone_fmt = normalize_phone_db(phone_raw)
    digits = re.sub(r"\D", "", phone_fmt)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]
    if len(digits) != 10:
        return _json(
            {
                "error": "phone_required",
                "message": "Нужен номер телефона клиента (+7…)",
                "need_phone": True,
                "peer": peer,
            },
            status=400,
        )

    name = (
        str(body.get("name") or "").strip()
        or peer.get("title")
        or " ".join(
            filter(
                None,
                [peer.get("first_name"), peer.get("last_name")],
            )
        )
        or (f"@{peer['username']}" if peer.get("username") else "")
        or "Клиент"
    )
    tg_uid = int(peer.get("tg_user_id") or peer_id)
    notes_parts = [f"Telegram ID: {tg_uid}"]
    if peer.get("username"):
        notes_parts.append(f"@{peer['username']}")
    notes = " · ".join(notes_parts)

    import aiohttp
    from posiflora import (
        _get_access_token,
        get_or_create_customer_id_by_phone,
        get_or_create_customer_source,
    )

    try:
        async with aiohttp.ClientSession() as session:
            token = await _get_access_token(session)
            source_id = None
            try:
                source_id = await get_or_create_customer_source(
                    session, token, "Telegram"
                )
            except Exception:
                logger.debug("customer source Telegram unavailable", exc_info=True)
            pf_id, pf_created = await get_or_create_customer_id_by_phone(
                session,
                token,
                phone_fmt,
                name,
                notes=notes,
                source_id=source_id,
            )
    except Exception as exc:
        logger.exception("Posiflora create from chat failed")
        return _json(
            {
                "error": "posiflora_failed",
                "message": f"Не удалось создать в Posiflora: {exc}",
            },
            status=502,
        )

    customer_id = await upsert_customer(
        posiflora_id=str(pf_id),
        name=name,
        phone=phone_fmt,
        notes=notes,
        tg_user_id=tg_uid,
        segment="new",
    )
    await set_customer_tg_by_phone(phone_fmt, tg_uid)

    try:
        from client_db import upsert_client

        await upsert_client(tg_uid, name, phone_fmt)
    except Exception:
        logger.debug("bot clients upsert skipped", exc_info=True)

    customer = await get_customer(int(customer_id))
    return _json(
        {
            "ok": True,
            "created": True,
            "posiflora_created": bool(pf_created),
            "label": "Клиент добавлен",
            "hint": "Сохранён в базе и в Posiflora",
            "customer": _customer_public(customer),
            "peer": peer,
            "account_id": int(acc["id"]),
        }
    )


async def handle_chats_create(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    request._chat_body = body  # type: ignore[attr-defined]
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    phone = str(body.get("phone") or "").strip()
    username = str(body.get("username") or "").strip()
    name = str(body.get("name") or "").strip()
    first_message = str(body.get("message") or body.get("text") or "").strip()
    if not phone and not username:
        return _json({"error": "phone_or_username_required"}, status=400)
    from senders.telegram_chat import create_or_open_dialog
    from telethon.errors import FloodWaitError

    try:
        data = await create_or_open_dialog(
            str(acc["session_file"]),
            account_id=int(acc["id"]),
            phone=phone,
            username=username,
            name=name,
            first_message=first_message,
        )
    except FloodWaitError as exc:
        return _json(
            {"error": f"FloodWait {int(getattr(exc, 'seconds', 60))}s"},
            status=429,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("create dialog failed")
        return _json({"error": str(exc)}, status=502)
    data["ok"] = True
    data["account_id"] = int(acc["id"])
    return _json(data)


def _media_response(
    data: bytes,
    content_type: str,
    *,
    filename: str | None = None,
    cache: str = "private, max-age=3600",
) -> web.Response:
    headers = {
        **_cors(),
        "Cache-Control": cache,
        "Content-Type": content_type,
    }
    if filename:
        # ASCII-safe Content-Disposition
        safe = re.sub(r"[^\w.\-]+", "_", filename)[:120] or "file"
        headers["Content-Disposition"] = f'inline; filename="{safe}"'
    return web.Response(body=data, headers=headers)


async def handle_chats_avatar(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    try:
        peer_id = int(request.match_info["peer_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_peer_id"}, status=400)
    from senders.telegram_chat import download_peer_avatar

    try:
        data, mime = await download_peer_avatar(
            str(acc["session_file"]),
            peer_id,
            account_id=int(acc["id"]),
        )
    except FileNotFoundError:
        return web.Response(status=404, headers=_cors())
    except Exception as exc:
        logger.exception("avatar download failed")
        return _json({"error": str(exc)}, status=502)
    return _media_response(data, mime, cache="private, max-age=86400")


async def handle_chats_message_media(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    acc, acc_err = await _resolve_chat_account(request)
    if acc_err:
        return acc_err
    assert acc is not None
    try:
        peer_id = int(request.match_info["peer_id"])
        message_id = int(request.match_info["message_id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_ids"}, status=400)
    thumb = request.query.get("thumb", "0") in ("1", "true", "yes")
    from senders.telegram_chat import download_message_media

    try:
        data, mime, filename = await download_message_media(
            str(acc["session_file"]),
            peer_id,
            message_id,
            account_id=int(acc["id"]),
            thumb=thumb,
        )
    except FileNotFoundError:
        return web.Response(status=404, headers=_cors())
    except ValueError as exc:
        return _json({"error": str(exc)}, status=413)
    except Exception as exc:
        logger.exception("media download failed")
        return _json({"error": str(exc)}, status=502)
    return _media_response(data, mime, filename=filename)


def setup_admin_routes(app: web.Application) -> None:
    routes = [
        ("/api/admin/login", handle_login, "POST"),
        ("/api/admin/logout", handle_logout, "POST"),
        ("/api/admin/me", handle_me, "GET"),
        ("/api/admin/stats", handle_stats, "GET"),
        ("/api/admin/bots/status", handle_bots_status, "GET"),
        ("/api/admin/sync", handle_sync, "POST"),
        ("/api/admin/clients", handle_clients_list, "GET"),
        ("/api/admin/clients/{id}", handle_client_detail, "GET"),
        ("/api/admin/events/upcoming", handle_events_upcoming, "GET"),
        ("/api/admin/events/{id}", handle_event_patch, "PATCH"),
        ("/api/admin/campaigns", handle_campaigns_list, "GET"),
        ("/api/admin/campaigns", handle_campaign_create, "POST"),
        ("/api/admin/campaigns/{id}", handle_campaign_get, "GET"),
        ("/api/admin/campaigns/{id}", handle_campaign_patch, "PATCH"),
        ("/api/admin/campaigns/{id}/recipients", handle_campaign_recipients, "GET"),
        ("/api/admin/personal", handle_personal, "POST"),
        ("/api/admin/accounts", handle_accounts_list, "GET"),
        ("/api/admin/accounts/telegram/settings", handle_telegram_settings_get, "GET"),
        ("/api/admin/accounts/telegram/settings", handle_telegram_settings_save, "POST"),
        ("/api/admin/accounts/telegram/start", handle_telegram_connect_start, "POST"),
        ("/api/admin/accounts/telegram/confirm", handle_telegram_connect_confirm, "POST"),
        ("/api/admin/accounts/telegram/keepalive", handle_telegram_keepalive, "POST"),
        ("/api/admin/accounts/{id}/check", handle_telegram_account_check, "POST"),
        ("/api/admin/accounts/{id}", handle_telegram_account_delete, "DELETE"),
        ("/api/admin/accounts/max/settings", handle_max_settings_get, "GET"),
        ("/api/admin/accounts/max/settings", handle_max_settings_save, "POST"),
        ("/api/admin/chats/accounts", handle_chats_accounts, "GET"),
        ("/api/admin/chats/dialogs", handle_chats_dialogs, "GET"),
        ("/api/admin/chats/dialogs", handle_chats_create, "POST"),
        ("/api/admin/chats/dialogs/{peer_id}/messages", handle_chats_messages, "GET"),
        ("/api/admin/chats/dialogs/{peer_id}/messages/{message_id}/media", handle_chats_message_media, "GET"),
        ("/api/admin/chats/dialogs/{peer_id}/avatar", handle_chats_avatar, "GET"),
        ("/api/admin/chats/dialogs/{peer_id}/send", handle_chats_send, "POST"),
        ("/api/admin/chats/dialogs/{peer_id}/send-media", handle_chats_send_media, "POST"),
        ("/api/admin/chats/dialogs/{peer_id}/client", handle_chats_client_status, "GET"),
        ("/api/admin/chats/dialogs/{peer_id}/client", handle_chats_client_create, "POST"),
        ("/api/admin/segments", handle_segment_counts, "GET"),
        ("/api/admin/ai/compose", handle_ai_compose, "POST"),
        ("/api/admin/ai/settings", handle_ai_settings_get, "GET"),
        ("/api/admin/ai/settings", handle_ai_settings_save, "POST"),
    ]
    options_done: set[str] = set()
    for path, handler, method in routes:
        if path not in options_done:
            app.router.add_route("OPTIONS", path, handle_options)
            options_done.add(path)
        app.router.add_route(method, path, handler)
