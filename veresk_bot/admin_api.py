"""
HTTP API админ-панели рассылок: /api/admin/*
Авторизация: Bearer-токен после POST /api/admin/login (логин + пароль).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web

from config import ADMIN_PASSWORD, ADMIN_USERNAME
from mailing_db import (
    add_campaign_recipients,
    count_customers,
    create_admin_session,
    create_campaign,
    create_personal_message,
    create_send_account,
    delete_admin_session,
    get_campaign,
    get_customer,
    get_event,
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
    set_event_auto_send,
    update_campaign,
    validate_admin_session,
    customers_for_segment,
)
import runtime_settings
from posiflora_sync import last_sync_info, sync_from_posiflora
from senders.max_bot import get_max_bot_token, is_max_configured
from senders.telegram_userbot import (
    confirm_telegram_login,
    get_api_credentials,
    is_telethon_configured,
    start_telegram_login,
)

logger = logging.getLogger(__name__)

AUTH_HEADER = "Authorization"


def _cors() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "GET, POST, PATCH, OPTIONS",
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
    return _json({"ok": True, "role": "admin"})


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
    items = []
    for c in rows:
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
    items = []
    for a in rows:
        items.append(
            {
                "id": a["id"],
                "kind": a["kind"],
                "label": a["label"],
                "phone": a["phone"],
                "phone_masked": _mask_phone(a["phone"]) if a["phone"] else a["label"],
                "daily_limit": a["daily_limit"],
                "sent_today": a["sent_today"],
                "status": a["status"],
                "warmup_until": a.get("warmup_until"),
            }
        )
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
        }
    )


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
    status = 200 if result.get("ok") else 400
    return _json(result, status=status)


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
    if not phone or not code:
        return _json({"error": "phone_and_code_required"}, status=400)
    result = await confirm_telegram_login(
        phone, code, password=str(password) if password else None
    )
    if not result.get("ok"):
        status = 400
        if result.get("need_2fa"):
            status = 200
        return _json(result, status=status)

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
    return _json({"ok": True, "account_id": account_id, **result})


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


def setup_admin_routes(app: web.Application) -> None:
    routes = [
        ("/api/admin/login", handle_login, "POST"),
        ("/api/admin/logout", handle_logout, "POST"),
        ("/api/admin/me", handle_me, "GET"),
        ("/api/admin/stats", handle_stats, "GET"),
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
        ("/api/admin/accounts/max/settings", handle_max_settings_get, "GET"),
        ("/api/admin/accounts/max/settings", handle_max_settings_save, "POST"),
        ("/api/admin/segments", handle_segment_counts, "GET"),
    ]
    options_done: set[str] = set()
    for path, handler, method in routes:
        if path not in options_done:
            app.router.add_route("OPTIONS", path, handle_options)
            options_done.add(path)
        app.router.add_route(method, path, handler)
