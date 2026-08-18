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
    PROVIDERS,
    AiComposeError,
    admin_assistant_reply,
    ai_settings_public,
    clear_ai_settings,
    detect_chat_intent,
    extract_customer_search_queries,
    generate_mailing_text,
    get_ai_folder_id,
    is_ai_configured,
    is_provider_configured,
    normalize_chat_messages,
    reset_ai_prompts,
    save_ai_prompts,
    save_provider_settings,
    suggest_chat_followups,
)
from config import ADMIN_PASSWORD, ADMIN_USERNAME, BOT_TOKEN
from mailing_db import (
    add_campaign_recipients,
    all_permissions_map,
    count_customers,
    create_admin_session,
    create_admin_user,
    create_campaign,
    create_personal_message,
    create_send_account,
    delete_admin_session,
    delete_admin_user,
    delete_send_account,
    generate_admin_password,
    get_admin_session,
    get_admin_user,
    get_admin_user_by_phone,
    get_campaign,
    get_customer,
    get_customer_by_phone,
    get_customer_by_tg_user_id,
    get_event,
    get_send_account,
    get_stats,
    list_admin_users,
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
    normalize_permissions,
    permissions_catalog,
    pick_ready_account,
    set_customer_tg_by_phone,
    set_customer_max_by_phone,
    set_event_auto_send,
    touch_admin_session,
    touch_admin_user_login,
    update_admin_user,
    update_campaign,
    update_send_account,
    upsert_customer,
    validate_admin_session,
    verify_admin_password,
    customers_by_ids,
    customers_for_segment,
    ADMIN_SESSION_HOURS,
    ADMIN_USER_ROLES,
    normalize_phone_db,

    get_fortune_play,
    delete_fortune_play,
    delete_fortune_play_by_id,
    clear_fortune_plays,
    claim_fortune_play_notified,
    list_fortune_plays,
    record_fortune_play,
    reveal_fortune_play,
    create_promotion,
    delete_promotion,
    get_active_discount_text,
    get_promotion,
    list_promotions,
    promotions_overview,
    update_promotion,
    append_customer_notes,
    get_fortune_plays_for_customer,
    get_customer_by_max_user_id,)
import runtime_settings
from fortune_wheel import (
    format_customer_prize_note,
    format_prize_congrats_message,
    get_config as get_wheel_config,
    is_promo_source,
    is_retry_prize,
    is_sealed_play,
    pick_winner as pick_wheel_winner,
    play_status,
    save_config as save_wheel_config,
)
from bot_metrics import get_bot_metrics, init_bot_metrics
from posiflora_sync import last_sync_info, sync_from_posiflora
from senders.matching import (
    build_recipients_for_customers,
    customer_can_receive,
    customer_messenger_status,
    normalize_channel,
    parse_channels,
    preview_mailing_match,
)
from senders.max_bot import get_max_bot_token, is_max_configured
from senders.max_userbot import (
    check_max_session,
    confirm_max_login,
    is_pymax_installed,
    remove_max_session_file,
    start_max_login,
)
from senders.telegram_userbot import (
    cancel_telegram_qr_login,
    check_telegram_session,
    confirm_telegram_login,
    confirm_telegram_qr_2fa,
    get_api_credentials,
    is_telethon_configured,
    poll_telegram_qr_login,
    recover_authorized_qr_sessions,
    refresh_telegram_qr_login,
    remove_session_file,
    resend_telegram_login_code,
    start_telegram_login,
    start_telegram_qr_login,
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
    """Каналы доставки: TG по id/телефону; MAX по id или max_profiles по телефону."""
    status = customer_messenger_status(c)
    parts = []
    if status["tg"]["reachable"]:
        parts.append("Telegram")
    if status["max"]["linked"]:
        parts.append("MAX")
    return ",".join(parts) or "—"


def _messengers_public(c: dict) -> dict[str, Any]:
    """Публичный статус привязки к Telegram / MAX для UI."""
    return customer_messenger_status(c)


def _parse_customer_ids(raw: Any) -> list[int]:
    """Из body/query: list, CSV или одно число → уникальные int id."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        parts = raw
    else:
        parts = str(raw).replace(";", ",").split(",")
    out: list[int] = []
    seen: set[int] = set()
    for p in parts:
        try:
            cid = int(str(p).strip())
        except (TypeError, ValueError):
            continue
        if cid <= 0 or cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _segment_label(seg: str) -> str:
    return {
        "all": "Все",
        "regular": "Постоянный",
        "new": "Новый",
        "inactive": "Давно не заказывал",
        "selected": "Выбранные клиенты",
        "channel_subscribers": "Подписчики канала",
        "channel_subscribers_new": "Новые подписчики канала",
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


_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_MONTHS_RU_SHORT = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)


def _when_label(days_until: int) -> tuple[str, str]:
    if days_until == 0:
        return "Сегодня", "today"
    if days_until == 1:
        return "Завтра", "soon"
    if days_until <= 7:
        return f"через {days_until} дн.", "soon"
    return f"через {days_until} дн.", "later"


def _kind_label(kind: str) -> str:
    return {
        "bday": "День рождения",
        "anniv": "Годовщина",
        "other": "Событие",
    }.get(kind or "other", "Событие")


def _format_day_month(iso: str | None) -> str:
    """15 марта — без года, удобно для ДР/годовщин."""
    if not iso:
        return "—"
    raw = str(iso)[:10]
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return raw
    return f"{dt.day} {_MONTHS_RU[dt.month - 1]}"


def _format_dt_short(iso: str | None) -> str:
    """4 авг · 14:30"""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return str(iso)[:16]
    return f"{dt.day} {_MONTHS_RU_SHORT[dt.month - 1]} · {dt.strftime('%H:%M')}"


def _next_occurrence(date_from: str | None) -> tuple[str | None, int | None]:
    """Ближайшая дата события в этом/следующем году и дней до неё."""
    if not date_from:
        return None, None
    raw = str(date_from)[:10]
    try:
        event_date = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None, None
    today = datetime.now().date()
    try:
        this_year = event_date.replace(year=today.year)
    except ValueError:
        this_year = event_date.replace(year=today.year, day=28)
    if this_year < today:
        try:
            this_year = event_date.replace(year=today.year + 1)
        except ValueError:
            this_year = event_date.replace(year=today.year + 1, day=28)
    return this_year.isoformat(), (this_year - today).days


def _primary_channel_for_event(e: dict) -> tuple[str, str]:
    """Канал доставки для события: TG по id/телефону; MAX по id или max_profiles."""
    from senders.matching import resolve_max_user_id_sync

    has_tg = bool(e.get("tg_user_id") or e.get("customer_phone") or e.get("phone"))
    has_max = (
        resolve_max_user_id_sync(
            max_user_id=e.get("max_user_id"),
            phone=e.get("customer_phone") or e.get("phone"),
        )
        is not None
    )
    if has_tg and has_max:
        return "TG · MAX", "tg"
    if has_tg:
        return "Telegram", "tg"
    if has_max:
        return "MAX", "max"
    return "нет канала", "none"


def _public_event(e: dict, *, days_until: int | None = None, next_date: str | None = None) -> dict:
    """Единый формат события для виджета и карточки клиента."""
    if days_until is None or next_date is None:
        computed_next, computed_days = _next_occurrence(e.get("date_from"))
        next_date = next_date or computed_next
        days_until = days_until if days_until is not None else computed_days
    days_until = int(days_until) if days_until is not None else 0
    when, when_class = _when_label(days_until)
    chan, chan_class = _primary_channel_for_event(e)
    last_auto = e.get("last_auto_sent_on")
    today_iso = datetime.now().date().isoformat()
    greeted_today = bool(last_auto and str(last_auto)[:10] == today_iso)
    return {
        "id": e["id"],
        "customer_id": e.get("cust_id") or e.get("customer_id"),
        "customer_name": e.get("customer_name"),
        "phone": e.get("customer_phone") or e.get("phone"),
        "phone_masked": _mask_phone(e.get("customer_phone") or e.get("phone") or ""),
        "title": e["title"],
        "kind": e["kind"],
        "kind_label": _kind_label(e.get("kind") or "other"),
        "date_from": e.get("date_from"),
        "date_label": _format_day_month(e.get("date_from")),
        "next_date": next_date,
        "next_date_label": _format_day_month(next_date),
        "days_until": days_until,
        "when_label": when,
        "when_class": when_class,
        "auto_send": bool(e.get("auto_send")),
        "last_auto_sent_on": last_auto,
        "greeted_today": greeted_today,
        "channel": chan,
        "channel_class": chan_class,
    }


# ── auth ───────────────────────────────────────────────────────────────────


def _role_label(role: str) -> str:
    return {"admin": "Администратор", "employee": "Сотрудник"}.get(role, role)


async def _current_admin(request: web.Request) -> dict[str, Any] | None:
    """Контекст текущего пользователя или None, если не авторизован."""
    token = _extract_token(request)
    if not token or not await validate_admin_session(token, touch=False):
        return None
    session = await get_admin_session(token)
    if not session:
        return None
    if session.get("user_id"):
        user = await get_admin_user(int(session["user_id"]))
        if not user or not user.get("is_active"):
            return None
        return {
            "source": "db",
            "user_id": user["id"],
            "role": user.get("role") or "employee",
            "permissions": user.get("permissions")
            or normalize_permissions(None, role=user.get("role")),
            "user": user,
        }
    return {
        "source": "env",
        "user_id": None,
        "role": "admin",
        "permissions": all_permissions_map(enabled=True),
        "user": None,
    }


def _has_perm(ctx: dict[str, Any] | None, perm: str) -> bool:
    if not ctx:
        return False
    perms = ctx.get("permissions") or {}
    return bool(perms.get(perm))


async def _require_perm(request: web.Request, perm: str) -> web.Response | None:
    err = await _require_admin(request)
    if err:
        return err
    ctx = await _current_admin(request)
    if not _has_perm(ctx, perm):
        return _json(
            {"error": "forbidden", "detail": "Недостаточно прав для этого действия"},
            status=403,
        )
    return None


async def handle_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    username = str(body.get("username") or body.get("login") or "").strip()
    password = str(body.get("password") or body.get("token") or "").strip()
    if not username or not password:
        return _json({"error": "invalid_credentials"}, status=401)

    # 1) Сотрудник из БД — логин = номер телефона
    db_user = await get_admin_user_by_phone(username)
    if db_user:
        if not db_user.get("is_active"):
            return _json({"error": "user_disabled", "detail": "Доступ отключён"}, status=403)
        if not verify_admin_password(password, db_user.get("password_hash") or ""):
            return _json({"error": "invalid_credentials"}, status=401)
        session = secrets.token_urlsafe(32)
        await create_admin_session(
            session,
            user_id=int(db_user["id"]),
            login=db_user.get("phone") or username,
        )
        await touch_admin_user_login(int(db_user["id"]))
        role = db_user.get("role") or "employee"
        perms = db_user.get("permissions") or normalize_permissions(
            None, role=role
        )
        return _json(
            {
                "token": session,
                "expires_hours": ADMIN_SESSION_HOURS,
                "username": db_user.get("phone") or username,
                "name": db_user.get("name") or "",
                "role": role,
                "permissions": perms,
                "user_id": int(db_user["id"]),
                "source": "db",
            }
        )

    # 2) Системный админ из .env
    if ADMIN_PASSWORD:
        user_ok = secrets.compare_digest(username.lower(), ADMIN_USERNAME.lower())
        pass_ok = secrets.compare_digest(password, ADMIN_PASSWORD)
        if user_ok and pass_ok:
            session = secrets.token_urlsafe(32)
            await create_admin_session(session, login=ADMIN_USERNAME)
            return _json(
                {
                    "token": session,
                    "expires_hours": ADMIN_SESSION_HOURS,
                    "username": ADMIN_USERNAME,
                    "name": "Администратор",
                    "role": "admin",
                    "permissions": all_permissions_map(enabled=True),
                    "user_id": None,
                    "source": "env",
                }
            )

    digits = re.sub(r"\D", "", username)
    looks_like_phone = len(digits) in (10, 11)
    if not ADMIN_PASSWORD and not looks_like_phone:
        return _json({"error": "admin_not_configured"}, status=503)
    return _json({"error": "invalid_credentials"}, status=401)


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
    ctx = await _current_admin(request)
    if ctx and ctx.get("source") == "db" and ctx.get("user"):
        user = ctx["user"]
        return _json(
            {
                "ok": True,
                "role": user.get("role") or "employee",
                "role_label": _role_label(user.get("role") or "employee"),
                "username": user.get("phone") or "",
                "name": user.get("name") or "",
                "user_id": user["id"],
                "phone": user.get("phone") or "",
                "permissions": ctx.get("permissions") or user.get("permissions"),
                "permission_catalog": permissions_catalog(),
                "source": "db",
                "session_hours": ADMIN_SESSION_HOURS,
                "expires_at": expires_at,
                "session_renewed": True,
            }
        )
    return _json(
        {
            "ok": True,
            "role": "admin",
            "role_label": "Администратор",
            "username": ADMIN_USERNAME,
            "name": "Администратор",
            "user_id": None,
            "phone": "",
            "permissions": all_permissions_map(enabled=True),
            "permission_catalog": permissions_catalog(),
            "source": "env",
            "session_hours": ADMIN_SESSION_HOURS,
            "expires_at": expires_at,
            "session_renewed": True,
        }
    )


# ── admin users (сотрудники) ───────────────────────────────────────────────


async def handle_users_list(request: web.Request) -> web.Response:
    err = await _require_perm(request, "access")
    if err:
        return err
    items = await list_admin_users(include_inactive=True)
    for u in items:
        u["role_label"] = _role_label(u.get("role") or "employee")
    env_admin = None
    if ADMIN_PASSWORD:
        env_admin = {
            "id": None,
            "phone": "",
            "name": "Системный администратор",
            "role": "admin",
            "role_label": "Администратор",
            "permissions": all_permissions_map(enabled=True),
            "is_active": True,
            "username": ADMIN_USERNAME,
            "source": "env",
            "created_at": None,
            "updated_at": None,
            "last_login_at": None,
        }
    return _json(
        {
            "items": items,
            "env_admin": env_admin,
            "roles": [{"id": r, "label": _role_label(r)} for r in ADMIN_USER_ROLES],
            "permission_catalog": permissions_catalog(),
        }
    )


async def handle_users_create(request: web.Request) -> web.Response:
    err = await _require_perm(request, "access")
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    phone = str(body.get("phone") or "").strip()
    name = str(body.get("name") or "").strip()
    role = str(body.get("role") or "employee").strip().lower()
    password = str(body.get("password") or "").strip()
    permissions = body.get("permissions")
    generated = False
    if not password:
        password = generate_admin_password()
        generated = True
    try:
        user = await create_admin_user(
            phone=phone,
            password=password,
            name=name,
            role=role,
            permissions=permissions,
        )
    except ValueError as exc:
        code = str(exc)
        status = 400
        detail = {
            "invalid_phone": "Укажите корректный номер телефона",
            "invalid_role": "Некорректная роль",
            "weak_password": "Пароль слишком короткий (минимум 6 символов)",
            "phone_taken": "Сотрудник с таким телефоном уже есть",
        }.get(code, code)
        if code == "phone_taken":
            status = 409
        return _json({"error": code, "detail": detail}, status=status)
    user["role_label"] = _role_label(user.get("role") or "employee")
    return _json(
        {
            "ok": True,
            "user": user,
            "password": password if generated or body.get("return_password") else None,
            "password_generated": generated,
        },
        status=201,
    )


async def handle_users_get(request: web.Request) -> web.Response:
    err = await _require_perm(request, "access")
    if err:
        return err
    try:
        user_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    user = await get_admin_user(user_id)
    if not user:
        return _json({"error": "not_found"}, status=404)
    user["role_label"] = _role_label(user.get("role") or "employee")
    return _json({"user": user, "permission_catalog": permissions_catalog()})


async def handle_users_patch(request: web.Request) -> web.Response:
    err = await _require_perm(request, "access")
    if err:
        return err
    try:
        user_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)

    kwargs: dict[str, Any] = {}
    if "name" in body:
        kwargs["name"] = str(body.get("name") or "")
    if "role" in body:
        kwargs["role"] = str(body.get("role") or "")
    if "is_active" in body:
        kwargs["is_active"] = bool(body.get("is_active"))
    if "password" in body and body.get("password"):
        kwargs["password"] = str(body.get("password"))
    if "permissions" in body:
        kwargs["permissions"] = body.get("permissions")

    try:
        user = await update_admin_user(user_id, **kwargs)
    except ValueError as exc:
        code = str(exc)
        detail = {
            "invalid_role": "Некорректная роль",
            "weak_password": "Пароль слишком короткий (минимум 6 символов)",
        }.get(code, code)
        return _json({"error": code, "detail": detail}, status=400)
    if not user:
        return _json({"error": "not_found"}, status=404)
    user["role_label"] = _role_label(user.get("role") or "employee")
    return _json({"ok": True, "user": user})


async def handle_users_reset_password(request: web.Request) -> web.Response:
    err = await _require_perm(request, "access")
    if err:
        return err
    try:
        user_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = str((body or {}).get("password") or "").strip()
    generated = False
    if not password:
        password = generate_admin_password()
        generated = True
    try:
        user = await update_admin_user(user_id, password=password)
    except ValueError as exc:
        return _json({"error": str(exc), "detail": "Слишком короткий пароль"}, status=400)
    if not user:
        return _json({"error": "not_found"}, status=404)
    user["role_label"] = _role_label(user.get("role") or "employee")
    return _json(
        {
            "ok": True,
            "user": user,
            "password": password,
            "password_generated": generated,
        }
    )


async def handle_users_delete(request: web.Request) -> web.Response:
    err = await _require_perm(request, "access")
    if err:
        return err
    try:
        user_id = int(request.match_info["id"])
    except (TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    # Нельзя удалить самого себя
    token = _extract_token(request)
    session = await get_admin_session(token) if token else None
    if session and session.get("user_id") is not None and int(session["user_id"]) == user_id:
        return _json(
            {"error": "cannot_delete_self", "detail": "Нельзя удалить свой аккаунт"},
            status=400,
        )
    ok = await delete_admin_user(user_id)
    if not ok:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True})


async def handle_generate_password(request: web.Request) -> web.Response:
    err = await _require_perm(request, "access")
    if err:
        return err
    length = 10
    try:
        body = await request.json()
        length = int(body.get("length") or 10)
    except Exception:
        pass
    return _json({"password": generate_admin_password(length)})


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
                "messengers": _messengers_public(c),
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
    fortune_plays = await get_fortune_plays_for_customer(customer_id)
    return _json(
        {
            "id": c["id"],
            "name": c["name"],
            "phone": c["phone"],
            "phone_masked": _mask_phone(c["phone"]),
            "segment": c["segment"],
            "segment_label": _segment_label(c["segment"]),
            "channels": _channel_for_customer(c),
            "messengers": _messengers_public(c),
            "tg_user_id": c.get("tg_user_id"),
            "max_user_id": c.get("max_user_id"),
            "notes": c.get("notes") or "",
            "fortune": [_serialize_fortune_play(p) for p in fortune_plays],
            "last_order_at": c.get("last_order_at"),
            "last_order_label": _format_relative(c.get("last_order_at")),
            "created_in_pf_at": c.get("created_in_pf_at"),
            "since_label": _format_relative(c.get("created_in_pf_at")),
            "events": [
                _public_event(
                    {
                        **e,
                        "customer_name": c["name"],
                        "customer_phone": c.get("phone"),
                        "tg_user_id": c.get("tg_user_id"),
                        "max_user_id": c.get("max_user_id"),
                        "cust_id": c["id"],
                    }
                )
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


# ── channel subscribers ─────────────────────────────────────────────────────


def _mask_phone_safe(phone: str | None) -> str:
    if not phone:
        return ""
    try:
        return _mask_phone(phone)
    except Exception:
        return ""


async def handle_channel_subscribers_list(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    from channel_subscriptions import (
        NEW_SUBSCRIBER_DAYS,
        get_channel_config,
        init_channel_subscriptions,
        list_subscribers,
    )

    await init_channel_subscriptions()
    list_filter = (request.query.get("filter") or "").strip().lower() or None
    status = (request.query.get("status") or "member").strip().lower()
    if status in ("all", "*"):
        status_filter = None
    elif status in ("member", "left"):
        status_filter = status
    else:
        status_filter = "member"
    search = request.query.get("search") or None
    only_new = (request.query.get("only_new") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    try:
        page = int(request.query.get("page", "1"))
    except ValueError:
        page = 1
    try:
        page_size = min(int(request.query.get("page_size", "100")), 500)
    except ValueError:
        page_size = 100

    items, total, stats = await list_subscribers(
        status=status_filter,
        search=search,
        only_new=only_new,
        list_filter=list_filter,
        page=page,
        page_size=page_size,
    )
    for it in items:
        phone = it.get("customer_phone")
        it["customer_phone_masked"] = _mask_phone_safe(phone) if phone else ""
        it["joined_label"] = _format_relative(it.get("joined_at")) if it.get("joined_at") else "—"
        it["left_label"] = _format_relative(it.get("left_at")) if it.get("left_at") else ""

    cfg = get_channel_config()
    return _json(
        {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "stats": stats,
            "channel": cfg,
            "new_days": NEW_SUBSCRIBER_DAYS,
            "filter": list_filter
            or ("new" if only_new else (status if status != "member" else "member")),
        }
    )


async def handle_channel_subscribers_settings(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    from channel_subscriptions import (
        get_channel_config,
        get_welcome_config,
        save_channel_config,
        save_welcome_config,
    )

    body: dict[str, Any] = {}
    if request.method != "GET":
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            body = {}

    if request.method == "GET":
        return _json({"channel": get_channel_config(), "welcome": get_welcome_config()})

    channel_id = body.get("channel_id")
    channel_username = body.get("channel_username")
    channel_title = body.get("channel_title")
    cid: int | None = None
    if channel_id is not None and str(channel_id).strip() != "":
        try:
            cid = int(str(channel_id).strip())
        except ValueError:
            return _json({"error": "bad_channel_id"}, status=400)
    elif "channel_id" in body:
        cid = 0

    uname: str | None = None
    if channel_username is not None:
        uname = str(channel_username).strip().lstrip("@")

    title: str | None = None
    if channel_title is not None:
        title = str(channel_title).strip()

    cfg = get_channel_config()
    if any(k in body for k in ("channel_id", "channel_username", "channel_title")):
        cfg = save_channel_config(
            channel_id=cid,
            channel_username=uname,
            channel_title=title,
        )

    welcome = get_welcome_config()
    welcome_body = body.get("welcome") if isinstance(body.get("welcome"), dict) else body
    welcome_keys = (
        "enabled",
        "text",
        "delay_minutes",
        "text_source",
        "promo_id",
        "welcome_enabled",
        "welcome_text",
        "welcome_delay_minutes",
        "welcome_text_source",
        "welcome_promo_id",
    )
    if any(k in welcome_body for k in welcome_keys) or "welcome" in body:
        enabled = welcome_body.get("enabled", welcome_body.get("welcome_enabled"))
        text = welcome_body.get("text", welcome_body.get("welcome_text"))
        delay = welcome_body.get("delay_minutes", welcome_body.get("welcome_delay_minutes"))
        text_source = welcome_body.get(
            "text_source", welcome_body.get("welcome_text_source")
        )
        promo_raw = welcome_body.get("promo_id", welcome_body.get("welcome_promo_id"))
        promo_id: int | None = None
        if promo_raw is not None:
            try:
                promo_id = max(0, int(promo_raw or 0))
            except (TypeError, ValueError):
                promo_id = 0
        welcome = save_welcome_config(
            enabled=None if enabled is None else bool(enabled),
            text=None if text is None else str(text),
            delay_minutes=None if delay is None else delay,
            text_source=None if text_source is None else str(text_source),
            promo_id=promo_id,
        )

    return _json({"ok": True, "channel": cfg, "welcome": welcome})


async def handle_auto_mail_settings(request: web.Request) -> web.Response:
    """GET/POST /api/admin/auto-mail/settings — автопоздравления по событиям."""
    err = await _require_admin(request)
    if err:
        return err
    from mailing_db import (
        get_auto_mail_settings,
        list_auto_mail_promo_options,
        save_auto_mail_settings,
    )

    if request.method == "GET":
        settings = get_auto_mail_settings()
        promos = await list_auto_mail_promo_options()
        return _json({"settings": settings, "promotions": promos})

    body: dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:
        body = {}
    payload = body.get("settings") if isinstance(body.get("settings"), dict) else body
    settings = save_auto_mail_settings(payload if isinstance(payload, dict) else {})
    promos = await list_auto_mail_promo_options()
    return _json({"ok": True, "settings": settings, "promotions": promos})


async def _pick_tg_userbot_account() -> dict[str, Any] | None:
    accounts = await list_send_accounts()
    tg_accounts = [
        a
        for a in accounts
        if a.get("kind") == "tg_userbot"
        and a.get("session_file")
        and (a.get("status") or "ready") in ("ready", "warmup")
    ]
    if not tg_accounts:
        tg_accounts = [
            a
            for a in accounts
            if a.get("kind") == "tg_userbot" and a.get("session_file")
        ]
    return tg_accounts[0] if tg_accounts else None


async def handle_channel_subscribers_sync(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    from channel_subscriptions import (
        get_channel_config,
        init_channel_subscriptions,
        sync_channel_subscribers_via_telethon,
    )

    await init_channel_subscriptions()
    cfg = get_channel_config()
    if not cfg.get("configured"):
        return _json(
            {
                "ok": False,
                "error": "channel_not_configured",
                "detail": "Сначала найдите канал кнопкой «Определить автоматически»",
            },
            status=400,
        )

    acc = await _pick_tg_userbot_account()
    if not acc:
        return _json(
            {
                "ok": False,
                "error": "no_telegram_account",
                "detail": "Подключите Telegram-аккаунт в Настройки → Telegram",
            },
            status=400,
        )

    result = await sync_channel_subscribers_via_telethon(
        session_file=str(acc["session_file"]),
        account_id=int(acc["id"]) if acc.get("id") is not None else None,
    )
    status = 200 if result.get("ok") else 502
    return _json(result, status=status)


async def handle_channel_subscribers_discover(request: web.Request) -> web.Response:
    """Найти каналы, где бот — админ, и при одном совпадении сохранить автоматически."""
    err = await _require_admin(request)
    if err:
        return err
    from channel_subscriptions import (
        discover_channels_where_bot_is_admin,
        init_channel_subscriptions,
        remember_bot_admin_channel,
    )

    await init_channel_subscriptions()
    body: dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:
        body = {}

    auto_save = body.get("auto_save", True)
    if isinstance(auto_save, str):
        auto_save = auto_save.strip().lower() in ("1", "true", "yes", "on")

    # Явный выбор канала из списка
    pick_id = body.get("channel_id")
    if pick_id is not None and str(pick_id).strip() != "":
        try:
            cid = int(str(pick_id).strip())
        except ValueError:
            return _json({"error": "bad_channel_id"}, status=400)
        uname = str(body.get("channel_username") or "").strip().lstrip("@")
        title = str(body.get("channel_title") or "").strip()
        cfg = remember_bot_admin_channel(
            channel_id=cid,
            channel_username=uname,
            channel_title=title,
            force=True,
        )
        return _json({"ok": True, "picked": True, "channel": cfg, "channels": []})

    acc = await _pick_tg_userbot_account()
    if not acc:
        return _json(
            {
                "ok": False,
                "error": "no_telegram_account",
                "detail": "Подключите Telegram-аккаунт в Настройки → Telegram — через него ищем каналы",
                "channels": [],
            },
            status=400,
        )

    result = await discover_channels_where_bot_is_admin(
        session_file=str(acc["session_file"]),
        account_id=int(acc["id"]) if acc.get("id") is not None else None,
        auto_save=bool(auto_save),
    )
    status = 200 if result.get("ok") else 502
    return _json(result, status=status)


async def handle_channel_subscribers_ensure(request: web.Request) -> web.Response:
    """Создать/найти CRM-карточки для подписчиков → письмо и рассылка."""
    err = await _require_admin(request)
    if err:
        return err
    from channel_subscriptions import (
        ensure_customer_for_subscriber,
        ensure_customers_for_subscribers,
        init_channel_subscriptions,
        list_member_tg_ids,
    )

    await init_channel_subscriptions()
    body: dict[str, Any] = {}
    try:
        raw = await request.json()
        if isinstance(raw, dict):
            body = raw
    except Exception:
        body = {}

    tg_ids_raw = body.get("tg_user_ids")
    if tg_ids_raw is None and body.get("tg_user_id") is not None:
        tg_ids_raw = [body.get("tg_user_id")]
    if body.get("all_members"):
        only_new = bool(body.get("only_new"))
        customers = await ensure_customers_for_subscribers(
            await list_member_tg_ids(only_new=only_new)
        )
    else:
        ids: list[int] = []
        for raw_id in tg_ids_raw or []:
            try:
                ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        if not ids:
            return _json({"error": "tg_user_ids_required"}, status=400)
        if len(ids) == 1:
            cust = await ensure_customer_for_subscriber(ids[0])
            customers = [cust] if cust else []
        else:
            customers = await ensure_customers_for_subscribers(ids)

    items = [
        {
            "id": c["id"],
            "name": c.get("name") or "",
            "phone": c.get("phone") or "",
            "phone_masked": _mask_phone_safe(c.get("phone")),
            "tg_user_id": c.get("tg_user_id"),
            "messengers": _messengers_public(c),
        }
        for c in customers
        if c
    ]
    return _json({"ok": True, "items": items, "total": len(items)})


# ── events ─────────────────────────────────────────────────────────────────


async def handle_events_upcoming(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        days = int(request.query.get("days", "14"))
    except ValueError:
        days = 14
    days = max(1, min(days, 366))
    events = await list_upcoming_events(days=days, limit=200)
    items = [
        _public_event(
            e,
            days_until=int(e.get("days_until", 0)),
            next_date=e.get("next_date"),
        )
        for e in events
    ]
    today_count = sum(1 for i in items if i["days_until"] == 0)
    auto_count = sum(1 for i in items if i["auto_send"])
    return _json(
        {
            "items": items,
            "days": days,
            "total": len(items),
            "today_count": today_count,
            "auto_count": auto_count,
        }
    )


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
    total = int(c.get("total_count") or 0)
    sent = int(c.get("sent_count") or 0)
    failed = int(c.get("failed_count") or 0)
    delivered = int(c.get("delivered_count") or 0)
    # Доставлено отдельно почти не пишется — успешные уходят в sent
    ok_count = max(sent, delivered)
    pending = max(0, total - ok_count - failed)
    when = "—"
    when_short = "—"
    if status == "sending":
        when = f"Идёт сейчас · {ok_count} из {total}"
        when_short = f"{ok_count}/{total}"
    elif status == "scheduled" and c.get("scheduled_at"):
        when = f"Запланирована на {_format_dt_short(c['scheduled_at'])}"
        when_short = _format_dt_short(c["scheduled_at"])
    elif status == "done":
        stamp = c.get("updated_at") or c.get("created_at")
        when = f"Отправлена {_format_dt_short(stamp)}"
        when_short = _format_dt_short(stamp)
        if failed:
            when += f" · {failed} с ошибкой"
    elif status == "draft":
        when = f"Черновик · {total} в очереди" if total else "Черновик · ещё не отправлена"
        when_short = "Черновик"
    elif status == "error":
        when = "Ошибка отправки"
        when_short = "Ошибка"
    channels = (c.get("channels") or "tg").replace("tg", "Telegram").replace("max", "MAX")
    media_path = c.get("media_path") or None
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
        "when_short": when_short,
        "scheduled_at": c.get("scheduled_at"),
        "total_count": total,
        "sent_count": sent,
        "delivered_count": delivered,
        "ok_count": ok_count,
        "failed_count": failed,
        "pending_count": pending,
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
        "media_path": media_path,
        "media_kind": c.get("media_kind"),
        "media_filename": c.get("media_filename"),
        "media_mime": c.get("media_mime"),
        "media_url": (
            f"/api/admin/campaigns/media/{media_path}" if media_path else None
        ),
        "has_media": bool(media_path),
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


async def handle_mailing_preview(request: web.Request) -> web.Response:
    """Превью сверки: сегмент/выбранные клиенты × каналы × готовые аккаунты."""
    err = await _require_admin(request)
    if err:
        return err
    segment = str(request.query.get("segment") or "all")
    seg_map = {
        "Постоянные": "regular",
        "Все клиенты": "all",
        "Новые": "new",
        "Давно не заказывали": "inactive",
        "Выбранные клиенты": "selected",
        "selected": "selected",
    }
    segment = seg_map.get(segment, segment)
    channels = str(request.query.get("channels") or "tg")
    customer_ids = _parse_customer_ids(request.query.get("customer_ids"))
    data = await preview_mailing_match(
        segment=segment,
        channels=channels,
        customer_ids=customer_ids or None,
    )
    return _json(data)


async def handle_campaign_media_upload(request: web.Request) -> web.Response:
    """Загрузка одного фото для рассылки (multipart field: file)."""
    err = await _require_admin(request)
    if err:
        return err
    if not (request.content_type or "").startswith("multipart/"):
        return _json({"error": "multipart_required"}, status=400)
    from campaign_media import CAMPAIGN_MEDIA_MAX_BYTES, save_campaign_photo

    reader = await request.multipart()
    raw = b""
    filename = ""
    mime = ""
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "file":
            filename = part.filename or "photo.jpg"
            mime = part.headers.get("Content-Type", "") or ""
            raw = await part.read(decode=False)
        else:
            await part.read(decode=False)
    if not raw:
        return _json({"error": "file_required"}, status=400)
    if len(raw) > CAMPAIGN_MEDIA_MAX_BYTES:
        return _json(
            {"error": "file_too_large", "max_mb": CAMPAIGN_MEDIA_MAX_BYTES // (1024 * 1024)},
            status=413,
        )
    try:
        meta = save_campaign_photo(raw, filename=filename, mime=mime)
    except ValueError as exc:
        code = str(exc)
        if code == "only_images":
            return _json(
                {"error": "only_images", "message": "Можно прикрепить только фото (JPG, PNG, WEBP, GIF)"},
                status=400,
            )
        if code == "file_too_large":
            return _json({"error": "file_too_large"}, status=413)
        return _json({"error": code or "bad_file"}, status=400)
    return _json(
        {
            "ok": True,
            **meta,
            "media_url": f"/api/admin/campaigns/media/{meta['media_path']}",
        }
    )


async def handle_campaign_media_get(request: web.Request) -> web.Response:
    """Отдача сохранённого фото рассылки (с авторизацией)."""
    err = await _require_admin(request)
    if err:
        return err
    from campaign_media import resolve_campaign_media

    name = str(request.match_info.get("name") or "")
    path = resolve_campaign_media(name)
    if not path:
        return _json({"error": "not_found"}, status=404)
    return web.FileResponse(path)


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
        "Выбранные клиенты": "selected",
        "selected": "selected",
    }
    segment = seg_map.get(segment, segment)
    customer_ids = _parse_customer_ids(body.get("customer_ids"))
    if customer_ids:
        segment = "selected"
    ch_list = parse_channels(body.get("channels") or "tg")
    channels = ",".join(ch_list)
    emoji = str(body.get("emoji") or "🌷")
    send_now = bool(body.get("send_now"))
    scheduled_at = body.get("scheduled_at")
    status = "draft"
    if send_now:
        status = "sending"
    elif scheduled_at:
        status = "scheduled"

    media_path = str(body.get("media_path") or "").strip() or None
    media_kind = None
    media_filename = None
    media_mime = None
    if media_path:
        from campaign_media import resolve_campaign_media

        resolved = resolve_campaign_media(media_path)
        if not resolved:
            return _json({"error": "media_not_found"}, status=400)
        media_kind = str(body.get("media_kind") or "photo")
        media_filename = str(body.get("media_filename") or resolved.name)[:180]
        media_mime = str(body.get("media_mime") or "image/jpeg")

    # Сверка клиентов с аккаунтами до постановки в очередь
    if customer_ids:
        customers = await customers_by_ids(customer_ids)
        if not customers:
            return _json(
                {
                    "error": "customers_not_found",
                    "message": "Выбранные клиенты не найдены в базе",
                },
                status=400,
            )
    else:
        customers = await customers_for_segment(segment)
    tg_ready = await pick_ready_account("tg_userbot") if "tg" in ch_list else None
    max_userbot = await pick_ready_account("max_userbot") if "max" in ch_list else None
    max_ok = (
        bool(max_userbot) or is_max_configured()
        if "max" in ch_list
        else False
    )
    match = build_recipients_for_customers(
        customers,
        ch_list,
        tg_accounts_ready=bool(tg_ready) if "tg" in ch_list else True,
        max_configured=max_ok if "max" in ch_list else True,
        max_allow_phone=bool(max_userbot) if "max" in ch_list else False,
    )
    recipients = match["recipients"]
    if not recipients:
        return _json(
            {
                "error": "no_reachable_recipients",
                "message": (
                    "Нет получателей для выбранных каналов. "
                    "Проверьте аккаунты Telegram/MAX и привязки клиентов."
                ),
                "match": {
                    "segment_total": match["segment_total"],
                    "reachable": match["reachable"],
                    "skipped": match["skipped"],
                    "skipped_samples": match["skipped_samples"],
                    "accounts": {
                        "tg_ready": bool(tg_ready),
                        "max_ready": max_ok,
                        "max_userbot": bool(max_userbot),
                        "max_bot": is_max_configured(),
                    },
                },
            },
            status=400,
        )

    cid = await create_campaign(
        title=title,
        message=message,
        segment=segment,
        channels=channels,
        emoji=emoji,
        status=status,
        scheduled_at=scheduled_at,
        media_path=media_path,
        media_kind=media_kind,
        media_filename=media_filename,
        media_mime=media_mime,
    )
    await add_campaign_recipients(cid, recipients)
    c = await get_campaign(cid)
    payload = _campaign_public(c)
    payload["match"] = {
        "segment_total": match["segment_total"],
        "reachable": match["reachable"],
        "skipped": match["skipped"],
        "will_send": match["reachable"]["total"],
    }
    return _json(payload, status=201)


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
    channel = normalize_channel(str(body.get("channel") or "tg")) or "tg"
    customer = await get_customer(customer_id)
    if not customer:
        return _json({"error": "not_found"}, status=404)
    ok, reason = customer_can_receive(customer, channel)
    if not ok:
        return _json(
            {
                "error": "unreachable",
                "message": reason or "Клиент недоступен в выбранном канале",
                "channel": channel,
            },
            status=400,
        )
    if channel == "tg":
        if not await pick_ready_account("tg_userbot"):
            return _json(
                {
                    "error": "no_tg_account",
                    "message": "Нет готовых Telegram-аккаунтов для отправки",
                },
                status=400,
            )
    elif channel == "max":
        max_userbot = await pick_ready_account("max_userbot")
        if not max_userbot and not is_max_configured():
            return _json(
                {
                    "error": "no_max_account",
                    "message": "Нет готового MAX-аккаунта и MAX-бот не подключён",
                },
                status=400,
            )
    msg_id = await create_personal_message(customer_id, message, channel=channel)
    return _json({"ok": True, "id": msg_id, "channel": channel})


# ── accounts ───────────────────────────────────────────────────────────────


async def handle_accounts_list(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    # Подхватить qr-сессии, где скан прошёл, а карточка в UI не создалась
    try:
        for recovered in await recover_authorized_qr_sessions():
            await _register_telegram_account(
                recovered, str(recovered.get("phone") or "")
            )
    except Exception:
        logger.exception("QR session recovery on accounts list failed")
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
    # Заглушка MAX-бота, если токена нет в списке как max_bot-строка
    has_max_bot_row = any(a["kind"] == "max_bot" for a in rows)
    has_max_userbot = any(
        a["kind"] == "max_userbot" and a.get("status") in ("ready", "warmup")
        for a in rows
    )
    max_ok = is_max_configured()
    if not has_max_bot_row:
        items.append(
            {
                "id": None,
                "kind": "max_bot",
                "label": "Veresk в MAX (бот)",
                "phone": "",
                "phone_masked": "MAX-бот",
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
            "max_userbot_ready": has_max_userbot,
            "pymax_installed": is_pymax_installed(),
            # Маркер деплоя: в UI/curl должно быть max-login-v31 (не старый образ)
            "server_build": "max-login-v31",
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


async def handle_telegram_connect_resend(request: web.Request) -> web.Response:
    """Повторно запросить код (часто App → SMS)."""
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
    result = await resend_telegram_login_code(phone)
    if not result.get("ok"):
        return _json(result, status=400)
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


async def handle_telegram_qr_start(request: web.Request) -> web.Response:
    """Старт входа по QR — без SMS/кода."""
    err = await _require_admin(request)
    if err:
        return err
    result = await start_telegram_qr_login()
    if not result.get("ok"):
        return _json(result, status=400)
    if result.get("already_authorized"):
        registered = await _register_telegram_account(
            result, str(result.get("phone") or "")
        )
        return _json(registered)
    return _json(result)


async def handle_telegram_qr_poll(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    login_id = str(body.get("login_id") or "").strip()
    if not login_id:
        return _json({"error": "login_id_required"}, status=400)
    result = await poll_telegram_qr_login(login_id)
    if result.get("pending"):
        return _json(result)
    if result.get("need_2fa"):
        return _json(result)
    if not result.get("ok"):
        return _json(result, status=400)
    registered = await _register_telegram_account(
        result, str(result.get("phone") or "")
    )
    return _json(registered)


async def handle_telegram_qr_refresh(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    login_id = str(body.get("login_id") or "").strip()
    if not login_id:
        return _json({"error": "login_id_required"}, status=400)
    result = await refresh_telegram_qr_login(login_id)
    if not result.get("ok"):
        return _json(result, status=400)
    return _json(result)


async def handle_telegram_qr_2fa(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    login_id = str(body.get("login_id") or "").strip()
    password = str(body.get("password") or "").strip()
    if not login_id:
        return _json({"error": "login_id_required"}, status=400)
    result = await confirm_telegram_qr_2fa(login_id, password)
    if not result.get("ok"):
        status = 200 if result.get("need_2fa") else 400
        return _json(result, status=status)
    registered = await _register_telegram_account(
        result, str(result.get("phone") or "")
    )
    return _json(registered)


async def handle_telegram_qr_cancel(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    login_id = ""
    try:
        body = await request.json()
        login_id = str(body.get("login_id") or "").strip()
    except Exception:
        pass
    await cancel_telegram_qr_login(login_id or None)
    return _json({"ok": True})


async def handle_telegram_account_check(request: web.Request) -> web.Response:
    """Проверить живой коннект Telegram / MAX userbot-аккаунта."""
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
    kind = acc.get("kind")
    if kind not in ("tg_userbot", "max_userbot"):
        return _json({"error": "unsupported_account"}, status=400)

    if kind == "max_userbot":
        live = await check_max_session(
            str(acc.get("session_file") or ""),
            phone=str(acc.get("phone") or "") or None,
        )
    else:
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
            "kind": kind,
            "error": live.get("error"),
            "tg_id": live.get("tg_id"),
            "max_user_id": live.get("max_user_id"),
            "username": live.get("username"),
            "label": live.get("label"),
            "phone": live.get("phone") or acc.get("phone"),
            "last_ok_at": now if authorized else acc.get("last_ok_at"),
        }
    )


async def handle_telegram_account_delete(request: web.Request) -> web.Response:
    """Отключить Telegram/MAX userbot-аккаунт: удалить запись и файл сессии."""
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
    kind = acc.get("kind")
    if kind not in ("tg_userbot", "max_userbot"):
        return _json({"error": "unsupported_account"}, status=400)

    deleted = await delete_send_account(account_id)
    if not deleted:
        return _json({"error": "not_found"}, status=404)
    session_file = str(deleted.get("session_file") or "")
    if kind == "tg_userbot":
        try:
            from senders.telegram_chat import release_session

            await release_session(session_file=session_file, account_id=account_id)
        except Exception:
            logger.exception("release chat session failed for account %s", account_id)
        remove_session_file(session_file)
    else:
        try:
            from senders.max_userbot_chat import release_session as release_max_chat

            await release_max_chat(session_file=session_file, account_id=account_id)
        except Exception:
            logger.exception("release MAX chat session failed for account %s", account_id)
        remove_max_session_file(session_file)
    return _json({"ok": True, "id": account_id, "kind": kind})


async def _register_max_userbot_account(result: dict[str, Any], phone: str) -> dict[str, Any]:
    warmup = (datetime.now() + timedelta(days=4)).date().isoformat()
    account_id = await create_send_account(
        kind="max_userbot",
        label=result.get("label") or phone,
        phone=result.get("phone") or phone,
        session_file=result.get("session_file") or "",
        daily_limit=150,
        status="warmup",
        warmup_until=warmup,
    )
    live = await check_max_session(
        str(result.get("session_file") or ""),
        phone=str(result.get("phone") or phone),
    )
    return {
        "ok": True,
        "account_id": account_id,
        "session_ok": bool(live.get("ok") and live.get("authorized")),
        "session_error": live.get("error"),
        "max_user_id": live.get("max_user_id") or result.get("max_user_id"),
        **result,
    }


async def handle_max_userbot_connect_start(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    if not is_pymax_installed():
        return _json(
            {
                "ok": False,
                "error": "pymax_missing",
                "detail": (
                    "Установите maxapi-python≥2.3.0 (Python ≥3.10) в образ bot "
                    "и перезапустите: docker compose build bot && docker compose up -d bot"
                ),
            },
            status=503,
        )
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "invalid_json"}, status=400)
    phone = str(body.get("phone") or "").strip()
    if not phone:
        return _json({"ok": False, "error": "phone_required", "detail": "Укажите телефон"}, status=400)
    # По умолчанию сбрасываем незавершённую сессию — иначе повтор на том же номере зависает
    reset_session = body.get("reset")
    if reset_session is None:
        reset_session = True
    try:
        result = await start_max_login(phone, reset_session=bool(reset_session))
    except Exception as exc:
        logger.exception("MAX userbot start failed")
        return _json(
            {
                "ok": False,
                "error": "max_login_failed",
                "detail": f"Не удалось начать вход в MAX: {exc}",
            },
            status=502,
        )
    if not result.get("ok"):
        return _json(
            {
                "ok": False,
                "error": result.get("error") or "max_login_failed",
                "detail": result.get("detail") or result.get("error") or "Не удалось отправить код",
                **{k: v for k, v in result.items() if k not in ("ok",)},
            },
            status=400,
        )
    if result.get("already_authorized"):
        try:
            registered = await _register_max_userbot_account(result, phone)
        except Exception as exc:
            logger.exception("MAX userbot register failed")
            return _json(
                {
                    "ok": False,
                    "error": "register_failed",
                    "detail": str(exc),
                },
                status=502,
            )
        return _json(registered)
    return _json(result)


async def handle_max_userbot_connect_confirm(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"ok": False, "error": "invalid_json"}, status=400)
    phone = str(body.get("phone") or "").strip()
    code = str(body.get("code") or "").strip()
    password = body.get("password")
    if not phone:
        return _json({"ok": False, "error": "phone_required", "detail": "Укажите телефон"}, status=400)
    if not code and not password:
        return _json(
            {"ok": False, "error": "phone_and_code_required", "detail": "Введите код из SMS / MAX"},
            status=400,
        )
    try:
        result = await confirm_max_login(
            phone, code, password=str(password) if password else None
        )
    except Exception as exc:
        logger.exception("MAX userbot confirm failed")
        return _json(
            {
                "ok": False,
                "error": "max_login_failed",
                "detail": f"Не удалось подтвердить вход: {exc}",
            },
            status=502,
        )
    if not result.get("ok"):
        status = 400
        if result.get("need_2fa"):
            status = 200
        return _json(
            {
                "ok": bool(result.get("ok")),
                "error": result.get("error") or "max_login_failed",
                "detail": result.get("detail") or result.get("error"),
                **{k: v for k, v in result.items() if k not in ("ok",)},
            },
            status=status,
        )

    try:
        registered = await _register_max_userbot_account(result, phone)
    except Exception as exc:
        logger.exception("MAX userbot register after confirm failed")
        return _json(
            {"ok": False, "error": "register_failed", "detail": str(exc)},
            status=502,
        )
    return _json(registered)


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
    bot_id = None
    if token:
        try:
            from max_bot.api import MaxBotAPI

            api = MaxBotAPI(token)
            try:
                me = await api.get_me()
                bot_name = me.get("name") or me.get("first_name")
                bot_username = me.get("username")
                bot_id = me.get("user_id")
            finally:
                await api.close()
        except Exception:
            logger.debug("Не удалось проверить MAX-токен при GET settings", exc_info=True)

    from max_bot.webhook_runtime import (
        florist_chat_id,
        webhook_enabled,
        webhook_secret,
        webhook_secret_source,
        webhook_url,
        webhook_url_source,
    )

    wh_url = webhook_url()
    wh_secret = webhook_secret()
    florist = florist_chat_id()
    florist_from_panel = runtime_settings.get("max_florist_chat_id") not in (None, "")

    return _json(
        {
            "configured": bool(token),
            "token_set": bool(token),
            "token_masked": _mask_token(token) if token else None,
            "from_env": from_env,
            "from_panel": from_panel,
            "bot_name": bot_name,
            "bot_username": bot_username,
            "bot_id": bot_id,
            "webhook_url": wh_url or None,
            "webhook_enabled": webhook_enabled(),
            "webhook_url_source": webhook_url_source(),
            "webhook_secret_set": bool(wh_secret),
            "webhook_secret_masked": _mask_token(wh_secret) if wh_secret else None,
            "webhook_secret_source": webhook_secret_source(),
            "florist_chat_id": florist or None,
            "florist_from_panel": florist_from_panel,
            "suggested_webhook_url": _suggested_max_webhook_url(request),
        }
    )


def _suggested_max_webhook_url(request: web.Request) -> str | None:
    """Подсказка URL webhook по Host запроса (если не localhost)."""
    from config import PUBLIC_BASE_URL

    host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(",")[0].strip()
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    if not host or host.startswith("127.") or "localhost" in host:
        return f"{PUBLIC_BASE_URL}/api/max/webhook"
    if ":" in host and not host.startswith("["):
        # убрать нестандартный порт из подсказки
        name, _, port = host.rpartition(":")
        if port.isdigit() and port not in ("443", "80"):
            host = name or host
    if proto != "https":
        proto = "https"
    return f"{proto}://{host}/api/max/webhook"


async def handle_max_settings_save(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)

    from max_bot.webhook_runtime import (
        register_webhook_subscription,
        reset_runtime_cache,
        unregister_webhook_subscription,
        validate_webhook_secret,
        validate_webhook_url,
        webhook_url as current_webhook_url,
    )

    # clear=true — убрать токен из панели (останется .env, если задан)
    if body.get("clear"):
        runtime_settings.delete_keys("max_bot_token")
        reset_runtime_cache()
        return _json(
            {
                "ok": True,
                "configured": is_max_configured(),
                "cleared": True,
            }
        )

    if body.get("clear_webhook"):
        old_url = current_webhook_url()
        if old_url and get_max_bot_token():
            await unregister_webhook_subscription(old_url)
        runtime_settings.delete_keys("max_webhook_url", "max_webhook_secret")
        return _json({"ok": True, "webhook_cleared": True, "webhook_enabled": False})

    updates: dict[str, Any] = {}
    token = str(body.get("token") or "").strip()
    me: dict[str, Any] | None = None

    # Пустой «Сохранить токен» без значения
    if "token" in body and not token and not any(
        k in body for k in ("webhook_url", "webhook_secret", "florist_chat_id", "register_webhook")
    ):
        return _json(
            {
                "ok": False,
                "error": "token_required",
                "detail": "Вставьте токен от @MasterBot в поле «Токен» и нажмите «Сохранить и проверить».",
            },
            status=400,
        )

    if token:
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
        except asyncio.TimeoutError:
            return _json(
                {
                    "ok": False,
                    "error": "max_unreachable",
                    "detail": "MAX API не ответил вовремя. Проверьте интернет и повторите.",
                },
                status=502,
            )
        except Exception as exc:
            # aiohttp.ClientError и сетевые сбои
            msg = str(exc)
            if "CERTIFICATE_VERIFY_FAILED" in msg or "SSLCertVerificationError" in msg:
                return _json(
                    {
                        "ok": False,
                        "error": "max_ssl_error",
                        "detail": (
                            "Ошибка SSL к MAX API (Russian Trusted CA). "
                            "Перезапустите админку после обновления — нужен файл "
                            "certs/russian_trusted_ca_bundle.pem."
                        ),
                    },
                    status=502,
                )
            if exc.__class__.__name__ in (
                "ClientError",
                "ClientConnectorError",
                "ClientConnectorCertificateError",
                "ClientConnectorSSLError",
                "ServerTimeoutError",
                "ClientOSError",
            ) or "aiohttp" in type(exc).__module__:
                return _json(
                    {
                        "ok": False,
                        "error": "max_unreachable",
                        "detail": f"Не удалось связаться с MAX API: {exc}",
                    },
                    status=502,
                )
            raise
        finally:
            await api.close()
        updates["max_bot_token"] = token

    if "webhook_url" in body:
        wh = str(body.get("webhook_url") or "").strip()
        if wh:
            url_err = validate_webhook_url(wh)
            if url_err:
                return _json({"ok": False, "error": "invalid_webhook_url", "detail": url_err}, status=400)
            updates["max_webhook_url"] = wh
        else:
            runtime_settings.delete_keys("max_webhook_url")

    if "webhook_secret" in body:
        sec = str(body.get("webhook_secret") or "").strip()
        if sec:
            sec_err = validate_webhook_secret(sec)
            if sec_err:
                return _json({"ok": False, "error": "invalid_webhook_secret", "detail": sec_err}, status=400)
            updates["max_webhook_secret"] = sec
        elif body.get("clear_webhook_secret"):
            runtime_settings.delete_keys("max_webhook_secret")

    if "florist_chat_id" in body:
        raw_florist = body.get("florist_chat_id")
        if raw_florist in (None, "", 0, "0"):
            runtime_settings.delete_keys("max_florist_chat_id")
        else:
            try:
                updates["max_florist_chat_id"] = str(int(raw_florist))
            except (TypeError, ValueError):
                return _json(
                    {"ok": False, "error": "invalid_florist_chat_id", "detail": "Нужен числовой chat_id"},
                    status=400,
                )

    if updates:
        runtime_settings.set_many(updates)
        reset_runtime_cache()

    want_register = bool(body.get("register_webhook") or updates.get("max_webhook_url"))
    subscribe_result = None
    if want_register:
        if not get_max_bot_token():
            return _json(
                {
                    "ok": False,
                    "error": "token_required",
                    "detail": "Сначала сохраните токен бота (шаг 1). Токен выдаёт @MasterBot в MAX.",
                    "webhook_url": current_webhook_url() or updates.get("max_webhook_url"),
                },
                status=400,
            )
        if current_webhook_url():
            subscribe_result = await register_webhook_subscription()

    from max_bot.webhook_runtime import (
        florist_chat_id,
        webhook_enabled,
        webhook_secret,
        webhook_secret_source,
        webhook_url,
        webhook_url_source,
    )

    wh_url = webhook_url()
    wh_secret = webhook_secret()
    return _json(
        {
            "ok": True,
            "configured": is_max_configured(),
            "bot_name": (me or {}).get("name") or (me or {}).get("first_name"),
            "bot_username": (me or {}).get("username"),
            "bot_id": (me or {}).get("user_id"),
            "webhook_url": wh_url or None,
            "webhook_enabled": webhook_enabled(),
            "webhook_url_source": webhook_url_source(),
            "webhook_secret_set": bool(wh_secret),
            "webhook_secret_masked": _mask_token(wh_secret) if wh_secret else None,
            "webhook_secret_source": webhook_secret_source(),
            "florist_chat_id": florist_chat_id() or None,
            "subscribe": subscribe_result,
        }
    )


async def handle_segment_counts(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    channel_n = 0
    channel_new_n = 0
    try:
        from channel_subscriptions import list_subscribers

        _, _, st = await list_subscribers(list_filter="member", page=1, page_size=1)
        channel_n = int(st.get("members") or 0)
        channel_new_n = int(st.get("new") or 0)
    except Exception:
        pass
    return _json(
        {
            "all": await count_customers(),
            "regular": await count_customers("regular"),
            "new": await count_customers("new"),
            "inactive": await count_customers("inactive"),
            "channel_subscribers": channel_n,
            "channel_subscribers_new": channel_new_n,
        }
    )


async def handle_ai_compose(request: web.Request) -> web.Response:
    """POST /api/admin/ai/compose — сгенерировать текст рассылки или личного сообщения."""
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
    client_name = str(body.get("client_name") or "").strip()
    occasion = str(body.get("occasion") or "").strip()
    if mode not in ("write", "improve"):
        mode = "write"

    try:
        text = await generate_mailing_text(
            prompt=prompt,
            current_text=current_text,
            segment=segment,
            mode=mode,
            client_name=client_name,
            occasion=occasion,
        )
    except AiComposeError as exc:
        status = 400 if exc.code in ("prompt_required",) else 502
        if exc.code == "ai_not_configured":
            status = 503
        return _json({"error": exc.code, "detail": exc.message}, status=status)

    return _json({"ok": True, "text": text})


async def _build_ai_chat_context(
    user_message: str,
    *,
    history_text: str = "",
    intent: str = "general",
    focus_customer_id: int | None = None,
) -> tuple[str, bool]:
    """Снимок CRM под запрос сотрудника. Возвращает (текст, нашлись ли клиенты)."""
    from ai_agent import build_customer_dossier, format_dossier_text

    lines: list[str] = []
    found_customers = False

    try:
        stats = await get_stats()
        lines.append(
            "Сводка: клиентов={customers}, отправлено за 30д={sent_month}, "
            "доставляемость={delivery_rate}%, TG-аккаунтов ready={accounts_ready}/{accounts_total}".format(
                customers=stats.get("customers", 0),
                sent_month=stats.get("sent_month", 0),
                delivery_rate=stats.get("delivery_rate")
                if stats.get("delivery_rate") is not None
                else "—",
                accounts_ready=stats.get("accounts_ready", 0),
                accounts_total=stats.get("accounts_total", 0),
            )
        )
    except Exception:
        logger.debug("AI chat: stats failed", exc_info=True)

    try:
        seg_all = await count_customers()
        seg_regular = await count_customers("regular")
        seg_new = await count_customers("new")
        seg_inactive = await count_customers("inactive")
        lines.append(
            f"Сегменты: all={seg_all}, regular={seg_regular}, "
            f"new={seg_new}, inactive={seg_inactive}"
        )
    except Exception:
        logger.debug("AI chat: segments failed", exc_info=True)

    event_days = 21
    event_limit = 12
    if intent == "events":
        event_days = 30
        event_limit = 20
    elif intent == "stats":
        event_days = 14
        event_limit = 8

    try:
        events = await list_upcoming_events(days=event_days, limit=event_limit)
        if events:
            today = [ev for ev in events if int(ev.get("days_until") or 0) == 0]
            week = [ev for ev in events if int(ev.get("days_until") or 0) <= 7]
            lines.append(
                f"Ближайшие события ({event_days} дн.): всего={len(events)}, "
                f"сегодня={len(today)}, в 7 дней={len(week)}"
            )
            for ev in events[:event_limit]:
                name = (ev.get("customer_name") or ev.get("name") or "—").strip()
                title = (ev.get("title") or ev.get("kind") or "событие").strip()
                when = (ev.get("next_date") or ev.get("event_date") or "").strip()
                days_until = ev.get("days_until")
                phone = (ev.get("customer_phone") or "").strip()
                cust_id = ev.get("cust_id") or ev.get("customer_id") or ""
                lines.append(
                    f"• через {days_until}д ({when}): {name} — {title} "
                    f"id={cust_id} тел={phone or '—'}"
                )
        else:
            lines.append(f"Ближайшие события ({event_days} дн.): нет")
    except Exception:
        logger.debug("AI chat: events failed", exc_info=True)

    try:
        campaigns = await list_campaigns(limit=8 if intent in ("copy", "stats") else 5)
        if campaigns:
            lines.append("Последние рассылки:")
            for c in campaigns[:8]:
                msg_preview = (c.get("message") or "")[:100]
                lines.append(
                    f"• #{c.get('id')} [{c.get('status')}] "
                    f"сегмент={c.get('segment')} каналы={c.get('channels') or '—'} "
                    f"«{msg_preview}»"
                )
    except Exception:
        logger.debug("AI chat: campaigns failed", exc_info=True)

    if intent == "inactive":
        try:
            rows, total = await list_customers(
                search="", segment="inactive", page=1, page_size=8
            )
            if rows:
                lines.append(f"Примеры inactive (всего {total}), до 8 шт.:")
                for c in rows[:8]:
                    lines.append(
                        f"• id={c.get('id')} {c.get('name') or '—'} "
                        f"тел={c.get('phone') or '—'} "
                        f"last_order={c.get('last_order_at') or '—'}"
                    )
        except Exception:
            logger.debug("AI chat: inactive sample failed", exc_info=True)

    seen_ids: set[int] = set()

    if focus_customer_id:
        try:
            dossier = await build_customer_dossier(int(focus_customer_id))
            if dossier:
                found_customers = True
                seen_ids.add(int(focus_customer_id))
                lines.append("=== Фокус: открытая карточка клиента ===")
                lines.append(format_dossier_text(dossier))
        except Exception:
            logger.debug("AI chat: focus dossier failed", exc_info=True)

    # Поиск клиента: текущее сообщение + недавняя история диалога
    search_blob = f"{user_message}\n{history_text}".strip()
    queries = extract_customer_search_queries(user_message)
    if not queries and history_text:
        queries = extract_customer_search_queries(history_text)

    for search_q in queries[:4]:
        try:
            rows, total = await list_customers(search=search_q, page=1, page_size=5)
            if not rows:
                continue
            found_customers = True
            lines.append(f"Поиск клиентов по «{search_q}» (найдено {total}):")
            for c in rows[:5]:
                cid = int(c.get("id") or 0)
                if not cid or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                try:
                    dossier = await build_customer_dossier(cid)
                    if dossier:
                        lines.append(format_dossier_text(dossier))
                        lines.append("---")
                    else:
                        lines.append(
                            f"• id={cid} {c.get('name') or '—'} "
                            f"тел={c.get('phone') or '—'}"
                        )
                except Exception:
                    lines.append(
                        f"• id={cid} {c.get('name') or '—'} "
                        f"тел={c.get('phone') or '—'}"
                    )
            if len(seen_ids) >= 6:
                break
        except Exception:
            logger.debug("AI chat: customer search failed", exc_info=True)

    if not found_customers and intent == "customer" and search_blob:
        lines.append(
            "По запросу клиент в CRM не найден. Попросите уточнить имя или телефон "
            "или вызовите lookup_customer."
        )

    lines.append(
        "Инструменты: lookup_customer, list_upcoming_events, list_segment_customers, "
        "get_shop_overview, list_recent_campaigns, list_fortune_plays, "
        "list_promotions, analyze_promotions. "
        "Панель: Клиенты, События, Главная, Акции, Чаты (TG/MAX), Колесо. "
        "Сайт: veresk.flowers."
    )
    return "\n".join(lines), found_customers


async def handle_ai_chat(request: web.Request) -> web.Response:
    """POST /api/admin/ai/chat — внутренний ИИ-помощник админки."""
    err = await _require_perm(request, "aichat")
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

    messages = normalize_chat_messages(body.get("messages"))
    if not messages or messages[-1]["role"] != "user":
        single = str(body.get("message") or body.get("prompt") or "").strip()
        if single:
            messages = [{"role": "user", "content": single}]
        else:
            return _json(
                {"error": "prompt_required", "detail": "Напишите сообщение"},
                status=400,
            )

    focus_customer_id: int | None = None
    raw_cid = body.get("customer_id")
    if raw_cid is not None and str(raw_cid).strip() != "":
        try:
            focus_customer_id = int(raw_cid)
        except (TypeError, ValueError):
            focus_customer_id = None

    last_user = messages[-1]["content"]
    intent = detect_chat_intent(last_user)
    if focus_customer_id and intent == "general":
        intent = "customer"
    history_text = "\n".join(
        m["content"] for m in messages[:-1] if m.get("role") == "user"
    )[-1500:]
    context, found_customers = await _build_ai_chat_context(
        last_user,
        history_text=history_text,
        intent=intent,
        focus_customer_id=focus_customer_id,
    )
    try:
        reply = await admin_assistant_reply(
            messages=messages,
            context=context,
            intent=intent,
            enable_tools=True,
        )
    except AiComposeError as exc:
        status = 400 if exc.code in ("prompt_required",) else 502
        if exc.code == "ai_not_configured":
            status = 503
        return _json({"error": exc.code, "detail": exc.message}, status=status)

    suggestions = suggest_chat_followups(intent, found_customers=found_customers)
    return _json(
        {
            "ok": True,
            "reply": reply,
            "message": reply,
            "intent": intent,
            "suggestions": suggestions,
            "customer_id": focus_customer_id,
            "tools_enabled": True,
        }
    )


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
        clear_ai_settings()
        return _json({"ok": True, "cleared": True, **ai_settings_public()})

    if body.get("reset_prompts"):
        reset_ai_prompts()
        return _json({"ok": True, "prompts_reset": True, **ai_settings_public()})

    # Только промпты — без смены оператора/ключа
    prompts_only = bool(body.get("prompts_only")) or (
        "prompts" in body
        and "provider" not in body
        and "api_key" not in body
        and "model" not in body
    )
    if "prompts" in body:
        prompt_errors = save_ai_prompts(body.get("prompts"))
        if prompt_errors:
            first = next(iter(prompt_errors.values()))
            return _json(
                {"error": "invalid_prompt", "detail": first, "fields": prompt_errors},
                status=400,
            )
        if prompts_only:
            return _json({"ok": True, "prompts_saved": True, **ai_settings_public()})

    provider = str(body.get("provider") or "").strip().lower()
    if not provider:
        provider = str(runtime_settings.get("ai_provider") or "openai")
    if provider not in PROVIDERS:
        return _json({"error": "invalid_provider", "detail": "Неизвестный оператор"}, status=400)

    api_key = str(body.get("api_key") or "").strip()
    # api_base принимаем только для custom — иначе скрытое поле формы
    # могло снова записать yandex endpoint в слот OpenRouter.
    if provider == "custom":
        api_base = str(body.get("api_base") or "").strip().rstrip("/")
    else:
        api_base = ""
    model = str(body.get("model") or "").strip()
    folder_id = str(body.get("folder_id") or "").strip()

    # Ключ обязателен только если у ЭТОГО оператора ещё нет сохранённого
    if not api_key and not is_provider_configured(provider):
        return _json(
            {
                "error": "api_key_required",
                "detail": f"Укажите API-ключ для {provider}",
            },
            status=400,
        )

    if provider == "yandexgpt":
        has_folder = bool(folder_id) or bool(get_ai_folder_id("yandexgpt"))
        if not has_folder:
            return _json(
                {
                    "error": "folder_id_required",
                    "detail": "Для YandexGPT укажите Folder ID каталога в Yandex Cloud",
                },
                status=400,
            )

    save_provider_settings(
        provider,
        api_key=api_key or None,
        api_base=api_base if "api_base" in body or api_base else None,
        model=model if ("model" in body or model) else None,
        folder_id=folder_id if ("folder_id" in body or folder_id) else None,
        activate=True,
    )

    return _json({"ok": True, "prompts_saved": "prompts" in body, **ai_settings_public()})


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

    # При фильтре по клиентам раньше брали limit*4 → Telethon scan до 400.
    # Достаточно чуть расширить выборку; CRM отфильтрует поверх.
    fetch_limit = min(limit + 40, 120) if clients_only else min(limit, 100)
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

        tg_ids, phones, _max_ids = await customer_contact_sets()

        def _is_known_client(row: dict[str, Any]) -> bool:
            peer_id = row.get("peer_id")
            try:
                if peer_id is not None and int(peer_id) in tg_ids:
                    return True
            except (TypeError, ValueError):
                pass
            # Также tg_user_id из сериализации диалога, если есть
            try:
                tid = row.get("tg_user_id")
                if tid is not None and int(tid) in tg_ids:
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
    enrich_peer = request.query.get("enrich_peer", "1") != "0"
    from senders.telegram_chat import get_dialog_messages

    try:
        data = await get_dialog_messages(
            str(acc["session_file"]),
            peer_id,
            account_id=int(acc["id"]),
            limit=limit,
            offset_id=offset_id,
            mark_read=mark_read,
            enrich_peer=enrich_peer,
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


# ── MAX chats inbox (личный номер + fallback бот) ─────────────────────────────


async def _ensure_max_chat_db() -> None:
    from max_bot.storage import init_max_db

    await init_max_db()


async def _list_max_userbot_accounts() -> list[dict[str, Any]]:
    rows = await list_send_accounts()
    items: list[dict[str, Any]] = []
    for a in rows:
        if a.get("kind") != "max_userbot" or not a.get("session_file"):
            continue
        items.append(
            {
                "id": a["id"],
                "label": a.get("label") or a.get("phone") or f"MAX {a['id']}",
                "phone": a.get("phone"),
                "status": a.get("status"),
                "phone_masked": _mask_phone(a["phone"]) if a.get("phone") else None,
                "session_file": a.get("session_file"),
            }
        )
    return items


async def _resolve_max_chat_account(
    request: web.Request,
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """account_id для личного MAX; если один аккаунт — берём его."""
    raw = request.query.get("account_id")
    if raw in (None, ""):
        try:
            body = getattr(request, "_chat_body", None)
            if isinstance(body, dict):
                raw = body.get("account_id")
        except Exception:
            raw = None

    rows = await _list_max_userbot_accounts()
    if not rows:
        return None, None  # нет userbot — можно fallback на бота

    if raw in (None, ""):
        if len(rows) == 1:
            return rows[0], None
        return None, _json(
            {
                "error": "account_id_required",
                "message": "Выберите MAX-аккаунт",
                "accounts": [
                    {
                        "id": a["id"],
                        "label": a.get("label"),
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
    if not acc or acc.get("kind") != "max_userbot" or not acc.get("session_file"):
        return None, _json({"error": "account_not_found"}, status=404)
    return {
        "id": acc["id"],
        "label": acc.get("label") or acc.get("phone"),
        "phone": acc.get("phone"),
        "status": acc.get("status"),
        "session_file": acc.get("session_file"),
        "phone_masked": _mask_phone(acc["phone"]) if acc.get("phone") else None,
    }, None


async def handle_max_chats_status(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()
    accounts = await _list_max_userbot_accounts()
    token = get_max_bot_token()
    bot_name = None
    bot_username = None
    bot_ok = False
    bot_error = None
    if token:
        try:
            from max_bot.api import MaxBotAPI

            api = MaxBotAPI(token)
            try:
                me = await api.get_me()
                bot_name = me.get("name") or me.get("first_name")
                bot_username = me.get("username")
                bot_ok = True
            finally:
                await api.close()
        except Exception as exc:
            bot_ok = False
            bot_error = str(exc)

    from max_bot.webhook_runtime import webhook_enabled, webhook_url

    mode = "userbot" if accounts else ("bot" if token else "none")
    label = None
    if mode == "userbot":
        label = accounts[0].get("label") if len(accounts) == 1 else f"{len(accounts)} номера MAX"
    elif mode == "bot":
        label = (
            f"@{bot_username}"
            if bot_username
            else bot_name or "MAX-бот"
        )

    return _json(
        {
            "configured": bool(accounts) or bool(token),
            "ok": bool(accounts) or bot_ok,
            "mode": mode,
            "pymax_installed": is_pymax_installed(),
            "accounts": [
                {
                    "id": a["id"],
                    "label": a.get("label"),
                    "phone": a.get("phone"),
                    "phone_masked": a.get("phone_masked"),
                    "status": a.get("status"),
                }
                for a in accounts
            ],
            "bot_configured": bool(token),
            "bot_ok": bot_ok,
            "bot_error": bot_error,
            "bot_name": bot_name,
            "bot_username": bot_username,
            "label": label,
            "webhook_enabled": webhook_enabled(),
            "webhook_url": webhook_url() or None,
        }
    )


async def handle_max_chats_dialogs(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()
    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err

    try:
        limit = int(request.query.get("limit") or 80)
    except (TypeError, ValueError):
        limit = 80
    query = str(request.query.get("q") or "").strip()
    clients_only = _truthy_query(request.query.get("clients_only"))
    only_users = clients_only or _truthy_query(request.query.get("only_users"))
    fetch_limit = min(limit * 4, 200) if clients_only else limit

    async def _filter_crm_clients(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not clients_only:
            return items[:limit]
        from mailing_db import customer_contact_sets

        _tg_ids, phones, max_ids = await customer_contact_sets()

        def _is_known(row: dict[str, Any]) -> bool:
            for key in ("max_user_id", "peer_id"):
                raw = row.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    # peer_id у бота может быть "user:123" / "chat:456"
                    if isinstance(raw, str) and ":" in raw:
                        kind, _, rest = raw.partition(":")
                        if kind == "user" and rest.isdigit() and int(rest) in max_ids:
                            return True
                    elif int(raw) in max_ids:
                        return True
                except (TypeError, ValueError):
                    pass
            phone = re.sub(r"\D", "", str(row.get("phone") or ""))
            if len(phone) >= 10 and phone[-10:] in phones:
                return True
            return False

        return [row for row in items if _is_known(row)][:limit]

    # Личный номер — как Telegram
    if acc is not None:
        from senders.max_userbot_chat import list_dialogs

        try:
            items = await list_dialogs(
                str(acc["session_file"]),
                phone=str(acc.get("phone") or ""),
                account_id=int(acc["id"]),
                limit=fetch_limit,
                query=query,
                only_users=only_users,
            )
        except Exception as exc:
            logger.exception("MAX userbot list dialogs failed")
            return _json(
                {
                    "error": str(exc),
                    "configured": True,
                    "mode": "userbot",
                    "items": [],
                },
                status=502,
            )
        items = await _filter_crm_clients(items)
        return _json(
            {
                "configured": True,
                "mode": "userbot",
                "account_id": int(acc["id"]),
                "account_label": acc.get("label") or acc.get("phone"),
                "only_users": only_users,
                "clients_only": clients_only,
                "items": items,
            }
        )

    # Fallback: бот-инбокс
    if not is_max_configured():
        return _json(
            {
                "configured": False,
                "mode": "none",
                "items": [],
                "error": "max_not_configured",
                "message": "Подключите личный номер MAX или токен бота в Настройках",
            }
        )
    from senders.max_chat import list_dialogs

    try:
        items = await list_dialogs(query=query, limit=fetch_limit)
    except Exception as exc:
        logger.exception("MAX list dialogs failed")
        return _json({"error": str(exc), "configured": True, "mode": "bot", "items": []}, status=502)
    items = await _filter_crm_clients(items)
    return _json(
        {
            "configured": True,
            "mode": "bot",
            "items": items,
            "account_label": "MAX-бот",
            "only_users": only_users,
            "clients_only": clients_only,
        }
    )


async def handle_max_chats_messages(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()
    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err

    peer = str(request.match_info.get("peer_id") or "").strip()
    if not peer:
        return _json({"error": "invalid_peer_id"}, status=400)
    try:
        limit = int(request.query.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    before_raw = request.query.get("before") or request.query.get("to")
    before_ts = None
    if before_raw not in (None, ""):
        try:
            before_ts = int(before_raw)
        except (TypeError, ValueError):
            before_ts = None

    if acc is not None:
        try:
            peer_id = int(peer)
        except (TypeError, ValueError):
            return _json({"error": "invalid_peer_id"}, status=400)
        from senders.max_userbot_chat import get_dialog_messages

        try:
            data = await get_dialog_messages(
                str(acc["session_file"]),
                peer_id,
                phone=str(acc.get("phone") or ""),
                account_id=int(acc["id"]),
                limit=limit,
                before_ts=before_ts,
                mark_read=request.query.get("mark_read", "1") != "0",
            )
        except Exception as exc:
            logger.exception("MAX userbot get messages failed")
            return _json({"error": str(exc)}, status=502)
        data["account_id"] = int(acc["id"])
        data["mode"] = "userbot"
        return _json(data)

    if not is_max_configured():
        return _json({"error": "max_not_configured"}, status=400)
    from senders.max_chat import get_dialog_messages

    try:
        data = await get_dialog_messages(peer, limit=limit, before_ts=before_ts)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("MAX get messages failed")
        return _json({"error": str(exc)}, status=502)
    data["mode"] = "bot"
    return _json(data)


async def handle_max_chats_send(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    request._chat_body = body  # type: ignore[attr-defined]

    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err

    peer = str(request.match_info.get("peer_id") or "").strip()
    if not peer:
        return _json({"error": "invalid_peer_id"}, status=400)
    text = str(body.get("text") or "").strip()

    if acc is not None:
        try:
            peer_id = int(peer)
        except (TypeError, ValueError):
            return _json({"error": "invalid_peer_id"}, status=400)
        from senders.max_userbot_chat import send_dialog_message

        try:
            message = await send_dialog_message(
                str(acc["session_file"]),
                peer_id,
                text,
                phone=str(acc.get("phone") or ""),
                account_id=int(acc["id"]),
            )
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("MAX userbot send failed")
            return _json({"error": str(exc)}, status=502)
        return _json(
            {
                "ok": True,
                "mode": "userbot",
                "message": message,
                "messages": [message],
                "peer_id": peer_id,
                "account_id": int(acc["id"]),
            }
        )

    if not is_max_configured():
        return _json({"error": "max_not_configured"}, status=400)
    from senders.max_chat import send_dialog_message

    try:
        message = await send_dialog_message(peer, text)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("MAX send failed")
        return _json({"error": str(exc)}, status=502)
    try:
        from max_bot.hub import publish_outbound_message

        await publish_outbound_message(
            peer_id=peer,
            message=message,
            dialog={
                "peer_id": peer,
                "last_message": message.get("text") or message.get("preview") or "",
                "last_out": True,
                "date": message.get("date"),
                "title": None,
            },
        )
    except Exception:
        logger.debug("MAX SSE publish after send failed", exc_info=True)
    return _json(
        {
            "ok": True,
            "mode": "bot",
            "message": message,
            "messages": [message],
            "peer_id": peer,
        }
    )


async def handle_max_chats_message_media(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()
    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err

    peer = str(request.match_info.get("peer_id") or "").strip()
    message_id = str(request.match_info.get("message_id") or "").strip()
    if not peer or not message_id:
        return _json({"error": "invalid_ids"}, status=400)

    try:
        if acc is not None:
            try:
                peer_id = int(peer)
            except (TypeError, ValueError):
                return _json({"error": "invalid_peer_id"}, status=400)
            from senders.max_userbot_chat import download_message_media

            data, mime, filename = await download_message_media(
                str(acc["session_file"]),
                peer_id,
                message_id,
                phone=str(acc.get("phone") or ""),
                account_id=int(acc["id"]),
            )
        else:
            if not is_max_configured():
                return _json({"error": "max_not_configured"}, status=400)
            from senders.max_chat import download_message_media

            data, mime, filename = await download_message_media(peer, message_id)
    except FileNotFoundError:
        return web.Response(status=404, headers=_cors())
    except ValueError as exc:
        return _json({"error": str(exc)}, status=413)
    except Exception as exc:
        logger.exception("MAX media download failed")
        return _json({"error": str(exc)}, status=502)
    return _media_response(data, mime, filename=filename)


async def handle_max_chats_send_media(request: web.Request) -> web.Response:
    """multipart/form-data: file|files, caption, account_id, as_document."""
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()

    peer = str(request.match_info.get("peer_id") or "").strip()
    if not peer:
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
            await part.read(decode=False)

    request._chat_body = {"account_id": account_id_raw}  # type: ignore[attr-defined]
    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err

    if not files:
        return _json({"error": "file_required"}, status=400)

    if acc is not None:
        from senders.max_userbot_chat import (
            MAX_UPLOAD_BYTES,
            MAX_UPLOAD_FILES,
            send_dialog_media,
        )

        if len(files) > MAX_UPLOAD_FILES:
            return _json({"error": f"max_{MAX_UPLOAD_FILES}_files"}, status=400)
        for f in files:
            if len(f["data"]) > MAX_UPLOAD_BYTES:
                return _json({"error": "file_too_large", "max_mb": 50}, status=413)
        try:
            peer_id = int(peer)
        except (TypeError, ValueError):
            return _json({"error": "invalid_peer_id"}, status=400)
        try:
            messages = await send_dialog_media(
                str(acc["session_file"]),
                peer_id,
                files,
                caption=caption,
                force_document=as_document,
                phone=str(acc.get("phone") or ""),
                account_id=int(acc["id"]),
            )
        except ValueError as exc:
            return _json({"error": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("MAX userbot send media failed")
            return _json({"error": str(exc)}, status=502)
        return _json(
            {
                "ok": True,
                "mode": "userbot",
                "messages": messages,
                "message": messages[-1] if messages else None,
                "peer_id": peer_id,
                "account_id": int(acc["id"]),
            }
        )

    if not is_max_configured():
        return _json({"error": "max_not_configured"}, status=400)
    from senders.max_chat import MAX_UPLOAD_BYTES, MAX_UPLOAD_FILES, send_dialog_media

    if len(files) > MAX_UPLOAD_FILES:
        return _json({"error": f"max_{MAX_UPLOAD_FILES}_files"}, status=400)
    for f in files:
        if len(f["data"]) > MAX_UPLOAD_BYTES:
            return _json({"error": "file_too_large", "max_mb": 50}, status=413)
    try:
        messages = await send_dialog_media(
            peer,
            files,
            caption=caption,
            force_document=as_document,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("MAX bot send media failed")
        return _json({"error": str(exc)}, status=502)

    message = messages[-1] if messages else None
    if message:
        try:
            from max_bot.hub import publish_outbound_message

            await publish_outbound_message(
                peer_id=peer,
                message=message,
                dialog={
                    "peer_id": peer,
                    "last_message": message.get("preview")
                    or message.get("text")
                    or "Медиа",
                    "last_out": True,
                    "date": message.get("date"),
                    "title": None,
                },
            )
        except Exception:
            logger.debug("publish outbound media failed", exc_info=True)

    return _json(
        {
            "ok": True,
            "mode": "bot",
            "messages": messages,
            "message": message,
            "peer_id": peer,
        }
    )


async def handle_max_chats_create(request: web.Request) -> web.Response:
    """Новый чат с личного MAX-номера по телефону."""
    err = await _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "invalid_json"}, status=400)
    request._chat_body = body  # type: ignore[attr-defined]
    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err
    if acc is None:
        return _json(
            {
                "error": "userbot_required",
                "message": "Новый чат доступен с личного номера MAX",
            },
            status=400,
        )

    phone = str(body.get("phone") or "").strip()
    name = str(body.get("name") or "").strip()
    first_message = str(body.get("message") or body.get("text") or "").strip()
    if not phone:
        return _json({"error": "phone_required"}, status=400)

    from senders.max_userbot_chat import create_or_open_dialog

    try:
        data = await create_or_open_dialog(
            str(acc["session_file"]),
            phone=phone,
            account_phone=str(acc.get("phone") or ""),
            account_id=int(acc["id"]),
            name=name,
            first_message=first_message,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("MAX create dialog failed")
        return _json({"error": str(exc)}, status=502)
    data["account_id"] = int(acc["id"])
    data["mode"] = "userbot"
    return _json(data)


async def handle_max_chats_client(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()
    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err

    peer = str(request.match_info.get("peer_id") or "").strip()
    if not peer:
        return _json({"error": "invalid_peer_id"}, status=400)

    if acc is not None:
        try:
            peer_id = int(peer)
        except (TypeError, ValueError):
            return _json({"error": "invalid_peer_id"}, status=400)
        from senders.max_userbot_chat import resolve_peer_profile

        try:
            peer_info = await resolve_peer_profile(
                str(acc["session_file"]),
                peer_id,
                phone=str(acc.get("phone") or ""),
                account_id=int(acc["id"]),
            )
        except Exception as exc:
            logger.exception("MAX userbot peer profile failed")
            return _json({"error": str(exc)}, status=502)

        # Совместимость с bot client_lookup: peer_key user:/chat:
        max_uid = peer_info.get("max_user_id")
        synthetic = f"user:{max_uid}" if max_uid is not None else f"chat:{peer_id}"
        from senders.max_chat import client_lookup_for_peer

        # Подставим телефон/имя из userbot-профиля в индекс, если есть
        try:
            from max_bot.storage import upsert_dialog

            await upsert_dialog(
                chat_id=peer_id,
                max_user_id=int(max_uid) if max_uid is not None else None,
                name=peer_info.get("title"),
                phone=peer_info.get("phone"),
            )
        except Exception:
            logger.debug("upsert dialog for client status failed", exc_info=True)

        try:
            data = await client_lookup_for_peer(synthetic)
        except Exception as exc:
            logger.exception("MAX client status failed")
            return _json({"error": str(exc)}, status=502)

        customer = data.get("customer")
        return _json(
            {
                "status": data.get("status"),
                "label": data.get("label"),
                "hint": data.get("hint"),
                "can_create": bool(data.get("can_create")),
                "need_phone": bool(data.get("need_phone")),
                "in_base": bool(data.get("in_base")),
                "peer": {**(data.get("peer") or {}), **peer_info, "peer_id": peer_id},
                "customer": _customer_public(customer) if customer else None,
                "configured": True,
                "mode": "userbot",
                "account_id": int(acc["id"]),
            }
        )

    from senders.max_chat import client_lookup_for_peer

    try:
        data = await client_lookup_for_peer(peer)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("MAX client status failed")
        return _json({"error": str(exc)}, status=502)

    customer = data.get("customer")
    return _json(
        {
            "status": data.get("status"),
            "label": data.get("label"),
            "hint": data.get("hint"),
            "can_create": bool(data.get("can_create")),
            "need_phone": bool(data.get("need_phone")),
            "in_base": bool(data.get("in_base")),
            "peer": data.get("peer"),
            "customer": _customer_public(customer) if customer else None,
            "configured": bool(data.get("configured")),
            "mode": "bot",
        }
    )


async def handle_max_chats_client_create(request: web.Request) -> web.Response:
    """Создать клиента из MAX-чата (Posiflora + локальная база + max_user_id)."""
    err = await _require_admin(request)
    if err:
        return err
    await _ensure_max_chat_db()
    peer = str(request.match_info.get("peer_id") or "").strip()
    if not peer:
        return _json({"error": "invalid_peer_id"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    request._chat_body = body  # type: ignore[attr-defined]

    acc, acc_err = await _resolve_max_chat_account(request)
    if acc_err:
        return acc_err

    # Для userbot сначала обогатим peer, потом создадим через общий хелпер
    if acc is not None:
        try:
            peer_id = int(peer)
        except (TypeError, ValueError):
            return _json({"error": "invalid_peer_id"}, status=400)
        from senders.max_userbot_chat import resolve_peer_profile

        try:
            peer_info = await resolve_peer_profile(
                str(acc["session_file"]),
                peer_id,
                phone=str(acc.get("phone") or ""),
                account_id=int(acc["id"]),
            )
        except Exception as exc:
            logger.exception("MAX userbot peer for create failed")
            return _json({"error": str(exc)}, status=502)
        max_uid = peer_info.get("max_user_id")
        if max_uid is None:
            return _json(
                {
                    "error": "not_a_user",
                    "message": "Клиента можно создать только из личного чата",
                },
                status=400,
            )
        try:
            from max_bot.storage import upsert_dialog

            await upsert_dialog(
                chat_id=peer_id,
                max_user_id=int(max_uid),
                name=peer_info.get("title"),
                phone=body.get("phone") or peer_info.get("phone"),
            )
        except Exception:
            pass
        peer = f"user:{int(max_uid)}"

    from senders.max_chat import create_client_from_peer

    try:
        data = await create_client_from_peer(
            peer,
            phone=str(body.get("phone") or "").strip() or None,
            name=str(body.get("name") or "").strip() or None,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("MAX create client failed")
        return _json({"error": str(exc)}, status=502)

    if data.get("error"):
        return _json(data, status=400)
    customer = data.get("customer")
    return _json(
        {
            **data,
            "customer": _customer_public(customer) if customer else None,
            "mode": "userbot" if acc is not None else "bot",
        }
    )


# ── MAX webhook + SSE ─────────────────────────────────────────────────────────


async def handle_max_webhook(request: web.Request) -> web.Response:
    """
    Публичный endpoint для Max (POST /subscriptions).
    Проверяет X-Max-Bot-Api-Secret, обновляет инбокс и крутит SurveyBot.
    """
    from max_bot.webhook_runtime import handle_incoming_update, webhook_secret

    expected = webhook_secret()
    if expected:
        got = request.headers.get("X-Max-Bot-Api-Secret", "").strip()
        if got != expected:
            return web.Response(status=403, text="forbidden")

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid_json")

    updates: list[dict[str, Any]] = []
    if isinstance(body, dict):
        if isinstance(body.get("updates"), list):
            updates = [u for u in body["updates"] if isinstance(u, dict)]
        elif body.get("update_type"):
            updates = [body]

    for update in updates:
        try:
            await handle_incoming_update(update)
        except Exception:
            logger.exception("MAX webhook update failed")

    return web.Response(status=200, text="ok")


async def handle_max_internal_event(request: web.Request) -> web.Response:
    """Уведомление из max_bot (long polling) → SSE админки без повторной анкеты."""
    from max_bot.hub import publish_update_event
    from max_bot.webhook_runtime import webhook_secret

    expected = webhook_secret() or get_max_bot_token()
    got = (
        request.headers.get("X-Max-Internal-Secret", "").strip()
        or _extract_token(request)
    )
    if expected and got != expected:
        return web.Response(status=403, text="forbidden")

    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    update = body.get("update") if isinstance(body, dict) else None
    if not isinstance(update, dict):
        update = body if isinstance(body, dict) else None
    if not isinstance(update, dict) or not update.get("update_type"):
        return _json({"error": "invalid_update"}, status=400)

    event = await publish_update_event(update)
    return _json({"ok": True, "event": event})


async def handle_max_chats_events(request: web.Request) -> web.Response:
    """SSE-поток realtime-событий MAX-чатов для админки."""
    err = await _require_admin(request)
    if err:
        return err

    from max_bot.hub import hub

    queue = await hub.subscribe()
    headers = {
        **_cors(),
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    resp = web.StreamResponse(status=200, headers=headers)
    await resp.prepare(request)
    try:
        await resp.write(b"event: ready\ndata: {\"ok\":true}\n\n")
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                continue
            await resp.write(f"data: {payload}\n\n".encode("utf-8"))
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        await hub.unsubscribe(queue)
    return resp


async def handle_max_webhook_status(request: web.Request) -> web.Response:
    err = await _require_admin(request)
    if err:
        return err
    from max_bot.hub import hub
    from max_bot.webhook_runtime import webhook_enabled, webhook_url

    return _json(
        {
            "webhook_enabled": webhook_enabled(),
            "webhook_url": webhook_url() or None,
            "sse_subscribers": hub.subscriber_count,
        }
    )



# ── promotions (акции) ──────────────────────────────────────────────────────


async def handle_promotions_list(request: web.Request) -> web.Response:
    err = await _require_perm(request, "promos")
    if err:
        return err
    status = (request.query.get("status") or "").strip() or None
    promo_type = (request.query.get("type") or "").strip() or None
    search = (request.query.get("q") or request.query.get("search") or "").strip()
    try:
        limit = int(request.query.get("limit") or 100)
    except ValueError:
        limit = 100
    try:
        offset = int(request.query.get("offset") or 0)
    except ValueError:
        offset = 0
    data = await list_promotions(
        status=status,
        promo_type=promo_type,
        search=search,
        limit=limit,
        offset=offset,
    )
    try:
        overview = await promotions_overview()
    except Exception:
        overview = {}
    try:
        discount = await get_active_discount_text()
    except Exception:
        discount = None
    return _json(
        {
            **data,
            "overview": overview,
            "active_discount": discount,
        }
    )


async def handle_promotions_overview(request: web.Request) -> web.Response:
    err = await _require_perm(request, "promos")
    if err:
        return err
    overview = await promotions_overview()
    try:
        discount = await get_active_discount_text()
    except Exception:
        discount = None
    return _json({**overview, "active_discount": discount})


async def handle_promotion_get(request: web.Request) -> web.Response:
    err = await _require_perm(request, "promos")
    if err:
        return err
    try:
        promo_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    row = await get_promotion(promo_id)
    if not row:
        return _json({"error": "not_found"}, status=404)
    return _json(row)


async def handle_promotion_create(request: web.Request) -> web.Response:
    err = await _require_perm(request, "promos")
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "invalid_json"}, status=400)
    try:
        row = await create_promotion(body)
    except ValueError as exc:
        return _json({"error": "validation_error", "detail": str(exc)}, status=400)
    return _json({"ok": True, "item": row}, status=201)


async def handle_promotion_patch(request: web.Request) -> web.Response:
    err = await _require_perm(request, "promos")
    if err:
        return err
    try:
        promo_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "invalid_json"}, status=400)
    try:
        row = await update_promotion(promo_id, body)
    except ValueError as exc:
        return _json({"error": "validation_error", "detail": str(exc)}, status=400)
    if not row:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True, "item": row})


async def handle_promotion_delete(request: web.Request) -> web.Response:
    err = await _require_perm(request, "promos")
    if err:
        return err
    try:
        promo_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    ok = await delete_promotion(promo_id)
    if not ok:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True})


async def handle_promotions_analyze(request: web.Request) -> web.Response:
    """POST /api/admin/promotions/analyze — ИИ-анализатор акций."""
    err = await _require_perm(request, "promos")
    if err:
        return err
    from ai_compose import AiComposeError, is_ai_configured
    from promo_ai import analyze_promotions

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
        body = {}
    if not isinstance(body, dict):
        body = {}
    focus = str(body.get("focus") or "").strip()
    try:
        horizon = int(body.get("horizon_days") or 14)
    except (TypeError, ValueError):
        horizon = 14
    try:
        result = await analyze_promotions(focus=focus, horizon_days=horizon)
    except AiComposeError as exc:
        status = 503 if getattr(exc, "code", "") == "ai_not_configured" else 502
        if getattr(exc, "code", "") in ("prompt_required", "ai_parse_error"):
            status = 400
        return _json(
            {
                "error": getattr(exc, "code", "ai_error"),
                "detail": getattr(exc, "message", str(exc)),
            },
            status=status,
        )
    except Exception:
        logger.exception("promotions analyze failed")
        return _json(
            {"error": "ai_error", "detail": "Не удалось выполнить анализ"},
            status=502,
        )
    return _json(result)


async def handle_promotions_analyze_apply(request: web.Request) -> web.Response:
    """POST /api/admin/promotions/analyze/apply — создать черновик из предложения ИИ."""
    err = await _require_perm(request, "promos")
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return _json({"error": "invalid_json"}, status=400)
    suggestion = (
        body.get("suggestion") if isinstance(body.get("suggestion"), dict) else body
    )
    if not isinstance(suggestion, dict):
        return _json({"error": "invalid_suggestion"}, status=400)

    def _default_message(s: dict[str, Any]) -> str:
        title = str(s.get("title") or "специальное предложение").strip()
        kind = str(s.get("promo_type") or "all").strip().lower()
        if kind == "birthday":
            return (
                "С днём рождения, {имя}! 🎂💐\n\n"
                "От всей души поздравляем и дарим скидку {скидка} на любой букет.\n\n"
                "Ваш Veresk 🌷"
            )
        if kind == "anniversary":
            return (
                "{имя}, поздравляем с годовщиной! 💍\n\n"
                "Отметьте этот день красивым букетом — дарим скидку {скидка}.\n\n"
                "Ваш Veresk 🌷"
            )
        if kind in ("inactive", "reactivation"):
            return (
                "Здравствуйте, {имя}!\n\n"
                "Давно не виделись — соскучились по вам. "
                "Специально для вас: {скидка} на букет.\n\n"
                "Ваш Veresk 🌷"
            )
        if kind in ("new", "welcome", "channel_subscribers_new"):
            return (
                "Здравствуйте, {имя}!\n\n"
                "Рады знакомству! В подарок — скидка {скидка} на первый букет.\n\n"
                "Ваш Veresk 🌷"
            )
        return (
            f"Здравствуйте, {{имя}}!\n\n"
            f"{title}: для вас скидка {{скидка}} на любой букет.\n\n"
            "Заказать: veresk.flowers\n\nВаш Veresk 🌷"
        )

    message_template = str(suggestion.get("message_template") or "").strip()
    if not message_template:
        message_template = _default_message(suggestion)

    discount_text = str(suggestion.get("discount_text") or "").strip()
    discount_pct = suggestion.get("discount_pct")
    if not discount_text and discount_pct is not None and discount_pct != "":
        try:
            pct = float(discount_pct)
            discount_text = (
                f"{int(pct)}%" if abs(pct - round(pct)) < 1e-9 else f"{pct:g}%"
            )
        except (TypeError, ValueError):
            discount_text = ""
    if not discount_text:
        discount_text = "15%"
        if discount_pct is None or discount_pct == "":
            discount_pct = 15

    description = (
        str(suggestion.get("description") or "").strip()
        or str(suggestion.get("rationale") or "").strip()
    )

    payload = {
        "title": suggestion.get("title"),
        "emoji": suggestion.get("emoji") or "🎁",
        "promo_type": suggestion.get("promo_type") or "all",
        "discount_pct": discount_pct,
        "discount_text": discount_text,
        "description": description,
        "message_template": message_template,
        "segment": suggestion.get("segment") or "all",
        "channels": suggestion.get("channels") or "tg,max",
        "status": body.get("status") or "draft",
        "starts_at": suggestion.get("starts_at"),
        "ends_at": suggestion.get("ends_at"),
        "use_in_auto_mail": suggestion.get("use_in_auto_mail", True),
        "use_in_mailing": suggestion.get("use_in_mailing", True),
        "priority": suggestion.get("priority") or 0,
        "tags": suggestion.get("tags") or [],
        "notes": (
            "Создано из ИИ-анализатора"
            + (
                f": {suggestion.get('rationale')}"
                if suggestion.get("rationale")
                else ""
            )
        )[:2000],
    }
    try:
        row = await create_promotion(payload)
    except ValueError as exc:
        return _json({"error": "validation_error", "detail": str(exc)}, status=400)
    return _json({"ok": True, "item": row}, status=201)


async def handle_wheel_get(request: web.Request) -> web.Response:
    err = await _require_perm(request, "wheel")
    if err:
        return err
    cfg = dict(get_wheel_config())
    tg_nick = None
    try:
        bot = request.app.get("bot")
        if bot is not None:
            me = await bot.get_me()
            tg_nick = str(getattr(me, "username", None) or "").strip() or None
    except Exception:
        logger.debug("wheel promo: bot.get_me failed", exc_info=True)
    try:
        from webapp_buttons import wheel_promo_share_links

        cfg["promo_links"] = wheel_promo_share_links(telegram_username=tg_nick)
    except Exception:
        logger.debug("wheel promo links failed", exc_info=True)
        cfg["promo_links"] = {
            "miniapp_url": "",
            "telegram_startapp": "",
            "telegram_bot": "",
            "max_startapp": "",
            "max_bot": "",
            "hint": "Не удалось собрать ссылки",
        }
    return _json(cfg)


async def handle_wheel_save(request: web.Request) -> web.Response:
    err = await _require_perm(request, "wheel")
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "invalid_json"}, status=400)
    try:
        cfg = save_wheel_config(body)
    except ValueError as exc:
        return _json({"error": "validation_error", "detail": str(exc)}, status=400)
    return _json({"ok": True, **cfg})


async def handle_wheel_public_get(request: web.Request) -> web.Response:
    """Публичный конфиг для Mini App — без авторизации."""
    cfg = dict(get_wheel_config())
    tg_nick = None
    try:
        bot = request.app.get("bot")
        if bot is not None:
            me = await bot.get_me()
            tg_nick = str(getattr(me, "username", None) or "").strip() or None
    except Exception:
        logger.debug("wheel public: bot.get_me failed", exc_info=True)
    try:
        from webapp_buttons import promo_bot_links, wheel_promo_share_links

        cfg["promo"] = promo_bot_links(telegram_username=tg_nick)
        cfg["promo_links"] = wheel_promo_share_links(telegram_username=tg_nick)
    except Exception:
        cfg["promo"] = {
            "telegram_url": "",
            "telegram_spin_url": "",
            "max_url": "",
            "max_spin_url": "",
        }
        cfg["promo_links"] = {}
    return _json(cfg)


def _serialize_fortune_play(
    row: dict[str, Any], *, hide_prize: bool = False
) -> dict[str, Any]:
    status = play_status(row)
    sealed = status == "sealed"
    hide = hide_prize or sealed
    return {
        "id": row.get("id"),
        "channel": row.get("channel"),
        "user_id": str(row.get("user_id") or ""),
        "first_name": row.get("first_name") or "",
        "last_name": row.get("last_name") or "",
        "username": row.get("username") or "",
        "full_name": row.get("full_name") or "",
        "prize_id": "" if hide else (row.get("prize_id") or ""),
        "prize_label": "" if hide else (row.get("prize_label") or ""),
        "discount_pct": None if hide else row.get("discount_pct"),
        "customer_id": row.get("customer_id"),
        "tg_user_id": row.get("tg_user_id"),
        "max_user_id": row.get("max_user_id"),
        "created_at": row.get("created_at"),
        "status": status,
        "source": row.get("source") or "",
        "revealed_at": row.get("revealed_at"),
        "ticket": sealed,
    }


async def handle_wheel_plays_list(request: web.Request) -> web.Response:
    err = await _require_perm(request, "wheel")
    if err:
        return err
    try:
        limit = int(request.query.get("limit") or 100)
    except ValueError:
        limit = 100
    try:
        offset = int(request.query.get("offset") or 0)
    except ValueError:
        offset = 0
    data = await list_fortune_plays(limit=limit, offset=offset)
    return _json(
        {
            "total": data["total"],
            "telegram": data["telegram"],
            "max": data["max"],
            "limit": data["limit"],
            "offset": data["offset"],
            "items": [_serialize_fortune_play(x) for x in data["items"]],
        }
    )


async def handle_wheel_play_delete(request: web.Request) -> web.Response:
    """DELETE /api/admin/wheel/plays/{id} — сбросить выигрыш одного участника."""
    err = await _require_perm(request, "wheel")
    if err:
        return err
    try:
        play_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return _json({"error": "invalid_id"}, status=400)
    ok = await delete_fortune_play_by_id(play_id)
    if not ok:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True, "deleted_id": play_id})


async def handle_wheel_plays_clear(request: web.Request) -> web.Response:
    """DELETE /api/admin/wheel/plays — сбросить всех участников (новый период)."""
    err = await _require_perm(request, "wheel")
    if err:
        return err
    confirm = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            confirm = str(body.get("confirm") or "").strip().lower()
    except Exception:
        confirm = str(request.query.get("confirm") or "").strip().lower()
    if confirm not in {"reset", "all", "yes", "1", "true"}:
        return _json(
            {
                "error": "confirm_required",
                "detail": "Передайте confirm=reset, чтобы сбросить всех участников",
            },
            status=400,
        )
    deleted = await clear_fortune_plays()
    return _json({"ok": True, "deleted": deleted})


async def _resolve_wheel_player(
    request: web.Request, *, body: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Определить игрока по Telegram/MAX initData. (player, error_response)."""
    from senders.max_bot import get_max_bot_token
    from telegram_auth import user_from_init_data

    tg_init = (
        request.headers.get("X-Telegram-Init-Data")
        or request.query.get("initData")
        or ""
    ).strip()
    max_init = (
        request.headers.get("X-Max-Init-Data")
        or request.headers.get("X-Max-WebApp-Init-Data")
        or request.query.get("maxInitData")
        or ""
    ).strip()

    if body is None:
        if request.method.upper() in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
            except Exception:
                body = {}
        else:
            body = {}
    if not isinstance(body, dict):
        body = {}

    channel_hint = str(
        body.get("channel") or request.query.get("channel") or ""
    ).strip().lower()

    # Prefer explicit channel if init data matches
    if channel_hint == "max" or (max_init and not tg_init):
        token = get_max_bot_token()
        user = user_from_init_data(max_init, token) if max_init and token else None
        if not user:
            return None, _json(
                {"error": "unauthorized", "detail": "Нужна авторизация MAX Mini App"},
                status=401,
            )
        return {
            "channel": "max",
            "user": user,
            "body": body,
        }, None

    token = BOT_TOKEN
    user = user_from_init_data(tg_init, token) if tg_init and token else None
    if not user:
        # fallback: try max if telegram failed
        token_m = get_max_bot_token()
        user_m = user_from_init_data(max_init, token_m) if max_init and token_m else None
        if user_m:
            return {"channel": "max", "user": user_m, "body": body}, None
        return None, _json(
            {"error": "unauthorized", "detail": "Нужна авторизация Telegram или MAX"},
            status=401,
        )
    return {"channel": "telegram", "user": user, "body": body}, None


async def _survey_profile_for_wheel(channel: str, uid: str) -> dict[str, Any] | None:
    """Анкета должна быть заполнена до розыгрыша."""
    try:
        if channel == "telegram":
            from client_db import get_client_profile

            return await get_client_profile(int(uid))
        from max_bot.storage import get_max_profile

        return await get_max_profile(int(uid))
    except Exception:
        logger.debug("wheel survey lookup failed", exc_info=True)
        return None


async def _resolve_customer_for_wheel(
    channel: str, uid: str, profile: dict[str, Any] | None
) -> dict[str, Any] | None:
    customer = None
    if channel == "telegram":
        try:
            customer = await get_customer_by_tg_user_id(int(uid))
        except Exception:
            customer = None
    else:
        try:
            customer = await get_customer_by_max_user_id(int(uid))
        except Exception:
            customer = None

    phone = str((profile or {}).get("phone") or "").strip()
    if not customer and phone:
        try:
            if channel == "telegram":
                await set_customer_tg_by_phone(phone, int(uid))
                customer = await get_customer_by_tg_user_id(int(uid))
            else:
                await set_customer_max_by_phone(phone, int(uid))
                customer = await get_customer_by_max_user_id(int(uid))
            if not customer:
                customer = await get_customer_by_phone(phone)
        except Exception:
            logger.debug("wheel customer link by phone failed", exc_info=True)
    return customer


async def _notify_wheel_prize(
    app: web.Application,
    *,
    channel: str,
    uid: str,
    prize_label: str | None = None,
    discount_pct: float | None = None,
) -> bool:
    """Отправить поздравление один раз (после анимации или fallback)."""
    play = await claim_fortune_play_notified(channel, uid)
    if not play:
        return False
    if is_sealed_play(play):
        return False
    label = str(prize_label or play.get("prize_label") or "")
    if is_retry_prize(label, play.get("prize_id")):
        return False
    pct = discount_pct if discount_pct is not None else play.get("discount_pct")
    text_md = format_prize_congrats_message(
        prize_label=label,
        discount_pct=pct,
        markdown=True,
    )
    try:
        if channel == "telegram":
            bot = app.get("bot")
            if bot is None:
                from aiogram import Bot
                from aiogram.client.session.aiohttp import AiohttpSession

                if not BOT_TOKEN:
                    return False
                bot = Bot(token=BOT_TOKEN, session=AiohttpSession(timeout=60))
                try:
                    await bot.send_message(int(uid), text_md, parse_mode="Markdown")
                finally:
                    await bot.session.close()
            else:
                await bot.send_message(int(uid), text_md, parse_mode="Markdown")
            return True

        token = get_max_bot_token()
        if not token:
            return False
        from max_bot.api import MaxBotAPI

        api = MaxBotAPI(token)
        try:
            await api.send_message(
                user_id=int(uid),
                text=format_prize_congrats_message(
                    prize_label=label,
                    discount_pct=pct,
                    markdown=False,
                ),
            )
        finally:
            await api.close()
        return True
    except Exception:
        logger.exception("Не удалось отправить поздравление с призом (%s/%s)", channel, uid)
        return False


async def _schedule_wheel_prize_fallback(
    app: web.Application, *, channel: str, uid: str, delay_sec: float = 14.0
) -> None:
    """Если Mini App не вызвал /notify после анимации — всё равно поздравить."""

    async def _run() -> None:
        try:
            await asyncio.sleep(delay_sec)
            await _notify_wheel_prize(app, channel=channel, uid=uid)
        except Exception:
            logger.debug("wheel prize fallback notify failed", exc_info=True)

    try:
        asyncio.create_task(_run())
    except Exception:
        logger.debug("wheel prize fallback schedule failed", exc_info=True)


async def handle_wheel_spin(request: web.Request) -> web.Response:
    """POST /api/wheel/spin — серверный розыгрыш + запись участника (1 раз).

    source=promo — каналный спин без анкеты: билет sealed, приз скрыт.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    player, err = await _resolve_wheel_player(request, body=body)
    if err:
        return err
    assert player is not None
    channel = player["channel"]
    user = player["user"]
    uid = str(user.get("id"))
    first = str(user.get("first_name") or "")
    last = str(user.get("last_name") or "")
    username = str(user.get("username") or "")
    promo = is_promo_source(body.get("source"))

    existing = await get_fortune_play(channel, uid)
    cfg = get_wheel_config()
    segments = cfg.get("segments") or []

    def _already_played_payload(play_row: dict[str, Any]) -> dict[str, Any]:
        sealed = is_sealed_play(play_row)
        winner_index = 0
        prize_id = str(play_row.get("prize_id") or "")
        for i, s in enumerate(segments):
            if str(s.get("id")) == prize_id or str(s.get("label")) == str(
                play_row.get("prize_label") or ""
            ):
                winner_index = i
                break
        segment: dict[str, Any]
        if sealed:
            segment = {"id": "", "label": ""}
        else:
            segment = {
                "id": play_row.get("prize_id") or "",
                "label": play_row.get("prize_label") or "",
            }
        return {
            "ok": True,
            "already_played": True,
            "sealed": sealed,
            "ticket": sealed,
            "error": "already_played",
            "detail": (
                "У вас уже есть запечатанный билет — откройте приз после анкеты в боте"
                if sealed
                else "Вы уже крутили колесо — приз закреплён"
            ),
            "winner_index": winner_index if not sealed else 0,
            "segment": segment,
            "discount_pct": None if sealed else play_row.get("discount_pct"),
            "play": _serialize_fortune_play(play_row, hide_prize=sealed),
            "config": cfg,
        }

    if existing and not is_retry_prize(
        existing.get("prize_label"), existing.get("prize_id")
    ):
        return _json(_already_played_payload(existing), status=409)
    if existing:
        try:
            await delete_fortune_play(channel, uid)
        except Exception:
            logger.debug("delete retry fortune_play failed", exc_info=True)

    profile = await _survey_profile_for_wheel(channel, uid)
    if not promo and not profile:
        return _json(
            {
                "error": "survey_required",
                "detail": "Сначала заполните анкету в боте — после неё откроется колесо фортуны",
            },
            status=403,
        )

    picked = pick_wheel_winner(segments)
    seg = picked["segment"]
    prize_label = str(seg.get("label") or "")
    prize_id = str(seg.get("id") or "")
    retry_prize = is_retry_prize(prize_label, prize_id)
    customer = (
        await _resolve_customer_for_wheel(channel, uid, profile or {})
        if profile
        else None
    )

    if profile and not (first or last):
        crm_name = ""
        if customer and customer.get("name"):
            crm_name = str(customer.get("name") or "").strip()
        if not crm_name:
            crm_name = str(profile.get("name") or "").strip()
        if crm_name:
            parts = crm_name.split(None, 1)
            first = parts[0] if parts else first
            last = parts[1] if len(parts) > 1 else last

    if retry_prize:
        return _json(
            {
                "ok": True,
                "already_played": False,
                "retry": True,
                "sealed": False,
                "ticket": False,
                "winner_index": picked["index"],
                "segment": seg,
                "discount_pct": picked.get("discount_pct"),
                "play": None,
                "config": cfg,
            }
        )

    status = "sealed" if promo else "revealed"
    source = "promo" if promo else "survey"
    play, created = await record_fortune_play(
        channel=channel,
        user_id=uid,
        first_name=first,
        last_name=last,
        username=username,
        prize_id=prize_id,
        prize_label=prize_label,
        discount_pct=picked.get("discount_pct"),
        customer_id=int(customer["id"]) if customer and customer.get("id") is not None else None,
        tg_user_id=int(uid) if channel == "telegram" else None,
        max_user_id=uid if channel == "max" else None,
        status=status,
        source=source,
    )
    if not created:
        return _json(_already_played_payload(play), status=409)

    # Промо: билет sealed — без CRM и без поздравления
    if promo:
        return _json(
            {
                "ok": True,
                "already_played": False,
                "retry": False,
                "sealed": True,
                "ticket": True,
                "winner_index": picked["index"],
                "segment": {"id": "", "label": ""},
                "discount_pct": None,
                "play": _serialize_fortune_play(play, hide_prize=True),
                "config": cfg,
            }
        )

    if customer and customer.get("id") is not None:
        try:
            note = format_customer_prize_note(
                channel=channel,
                prize_label=prize_label,
                discount_pct=picked.get("discount_pct"),
                created_at=str(play.get("created_at") or ""),
            )
            await append_customer_notes(int(customer["id"]), note)
            pf_id = str(customer.get("posiflora_id") or "").strip()
            if pf_id:
                try:
                    from posiflora import append_customer_prize_note_to_posiflora

                    await append_customer_prize_note_to_posiflora(pf_id, note)
                except Exception:
                    logger.exception(
                        "Не удалось записать приз в Posiflora #%s", pf_id
                    )
        except Exception:
            logger.exception("Не удалось записать приз в карточку клиента id=%s", customer.get("id"))

    await _schedule_wheel_prize_fallback(request.app, channel=channel, uid=uid)

    return _json(
        {
            "ok": True,
            "already_played": False,
            "retry": False,
            "sealed": False,
            "ticket": False,
            "winner_index": picked["index"],
            "segment": seg,
            "discount_pct": picked.get("discount_pct"),
            "play": _serialize_fortune_play(play),
            "config": cfg,
        }
    )


async def handle_wheel_notify(request: web.Request) -> web.Response:
    """POST /api/wheel/notify — поздравление после окончания анимации колеса."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    player, err = await _resolve_wheel_player(
        request, body=body if isinstance(body, dict) else {}
    )
    if err:
        return err
    assert player is not None
    channel = player["channel"]
    uid = str(player["user"].get("id"))
    play = await get_fortune_play(channel, uid)
    if not play:
        return _json({"ok": False, "sent": False, "error": "no_play"}, status=404)
    if is_retry_prize(play.get("prize_label"), play.get("prize_id")):
        return _json({"ok": True, "sent": False, "retry": True})
    if is_sealed_play(play):
        return _json({"ok": True, "sent": False, "sealed": True, "ticket": True})
    sent = await _notify_wheel_prize(request.app, channel=channel, uid=uid)
    return _json({"ok": True, "sent": sent})


async def handle_wheel_my_play(request: web.Request) -> web.Response:
    player, err = await _resolve_wheel_player(request, body={})
    if err:
        return err
    assert player is not None
    channel = player["channel"]
    uid = str(player["user"].get("id"))
    play = await get_fortune_play(channel, uid)
    if not play:
        return _json({"played": False, "status": None, "play": None, "ticket": False})
    if is_retry_prize(play.get("prize_label"), play.get("prize_id")):
        try:
            await delete_fortune_play(channel, uid)
        except Exception:
            logger.debug("cleanup retry fortune_play on /me failed", exc_info=True)
        return _json(
            {
                "played": False,
                "status": None,
                "play": None,
                "ticket": False,
                "retry_cleared": True,
            }
        )
    status = play_status(play)
    sealed = status == "sealed"
    return _json(
        {
            "played": True,
            "status": status,
            "ticket": sealed,
            "sealed": sealed,
            "play": _serialize_fortune_play(play, hide_prize=sealed),
        }
    )


async def _require_settings(request: web.Request) -> web.Response | None:
    return await _require_perm(request, "settings")


def _backup_fail(exc: Exception) -> web.Response:
    from backup import BackupError

    if isinstance(exc, BackupError):
        status = 404 if exc.code == "not_found" else 400
        if exc.code == "file_too_large":
            status = 413
        return _json({"error": exc.code, "detail": exc.message}, status=status)
    logger.exception("Ошибка резервной копии")
    return _json({"error": "backup_failed", "detail": "Не удалось выполнить операцию"}, status=500)


async def _save_backup_upload(request: web.Request):
    from pathlib import Path
    import tempfile

    from backup import MAX_UPLOAD_BYTES, BackupError, backups_dir, format_bytes

    if not (request.content_type or "").startswith("multipart/"):
        raise BackupError("multipart_required", "Нужен файл копии")
    reader = await request.multipart()
    dest: Path | None = None
    filename = "veresk-kopiya.zip"
    size = 0
    try:
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name != "file":
                await part.read(decode=False)
                continue
            filename = Path(part.filename or filename).name or filename
            fd, tmp_name = tempfile.mkstemp(
                prefix="veresk-up-", suffix=".zip", dir=str(backups_dir())
            )
            dest = Path(tmp_name)
            with open(fd, "wb") as out:
                while True:
                    chunk = await part.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise BackupError(
                            "file_too_large",
                            f"Файл больше {format_bytes(MAX_UPLOAD_BYTES)}",
                        )
                    out.write(chunk)
        if dest is None or size == 0:
            raise BackupError("file_required", "Выберите файл копии")
        return dest, filename
    except Exception:
        if dest is not None:
            dest.unlink(missing_ok=True)
        raise


async def _reinit_after_restore() -> None:
    from channel_subscriptions import init_channel_subscriptions
    from client_db import init_db
    from mailing_db import init_mailing_db
    from bot_metrics import init_bot_metrics

    await init_mailing_db()
    await init_bot_metrics()
    await init_channel_subscriptions()
    await init_db()
    try:
        from max_bot.storage import init_max_db

        await init_max_db()
    except Exception:
        logger.debug("init_max_db после restore пропущен", exc_info=True)


async def handle_backup_list(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import list_backups, live_info

    return _json({"live": live_info(), "items": list_backups()})


async def handle_backup_create(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import BackupError, create_backup

    try:
        item = await asyncio.to_thread(create_backup, kind="manual")
    except BackupError as exc:
        return _backup_fail(exc)
    except Exception as exc:
        return _backup_fail(exc)
    return _json({"ok": True, "backup": item})


async def handle_backup_get(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import BackupError, get_backup

    try:
        item = get_backup(str(request.match_info.get("id") or ""))
    except BackupError as exc:
        return _backup_fail(exc)
    if not item:
        return _json({"error": "not_found", "detail": "Такой копии нет"}, status=404)
    return _json({"backup": item})


async def handle_backup_file(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import BackupError, get_backup, resolve_backup_file
    from urllib.parse import quote

    ident = str(request.match_info.get("id") or "")
    try:
        path = resolve_backup_file(ident)
        item = get_backup(ident)
    except BackupError as exc:
        return _backup_fail(exc)
    if not path or not item:
        return _json({"error": "not_found", "detail": "Такой копии нет"}, status=404)
    filename = item.get("filename") or f"veresk-kopiya-{ident}.zip"
    resp = web.FileResponse(path)
    resp.content_type = "application/zip"
    quoted = quote(filename)
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quoted}"
    )
    for key, value in _cors().items():
        resp.headers[key] = value
    return resp


async def handle_backup_delete(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import BackupError, delete_backup

    try:
        delete_backup(str(request.match_info.get("id") or ""))
    except BackupError as exc:
        return _backup_fail(exc)
    return _json({"ok": True})


async def handle_backup_restore(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import BackupError, restore_backup

    ident = str(request.match_info.get("id") or "")
    try:
        result = await asyncio.to_thread(restore_backup, ident)
        await _reinit_after_restore()
    except BackupError as exc:
        return _backup_fail(exc)
    except Exception as exc:
        return _backup_fail(exc)
    return _json({"ok": True, **result})


async def handle_backup_upload(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import BackupError, import_uploaded_zip

    dest = None
    try:
        dest, filename = await _save_backup_upload(request)
        item = await asyncio.to_thread(import_uploaded_zip, dest, original_name=filename)
    except BackupError as exc:
        return _backup_fail(exc)
    except Exception as exc:
        return _backup_fail(exc)
    finally:
        if dest is not None:
            dest.unlink(missing_ok=True)
    return _json({"ok": True, "backup": item})


async def handle_backup_restore_upload(request: web.Request) -> web.Response:
    err = await _require_settings(request)
    if err:
        return err
    from backup import BackupError, create_backup, restore_from_zip

    dest = None
    try:
        dest, _filename = await _save_backup_upload(request)
        safety = await asyncio.to_thread(
            create_backup, kind="safety", note="Перед загрузкой копии"
        )
        result = await asyncio.to_thread(restore_from_zip, dest)
        await _reinit_after_restore()
    except BackupError as exc:
        return _backup_fail(exc)
    except Exception as exc:
        return _backup_fail(exc)
    finally:
        if dest is not None:
            dest.unlink(missing_ok=True)
    return _json({"ok": True, "safety": safety, **result})


def setup_admin_routes(app: web.Application) -> None:
    routes = [
        ("/api/admin/login", handle_login, "POST"),
        ("/api/admin/logout", handle_logout, "POST"),
        ("/api/admin/me", handle_me, "GET"),
        ("/api/admin/users", handle_users_list, "GET"),
        ("/api/admin/users", handle_users_create, "POST"),
        ("/api/admin/users/generate-password", handle_generate_password, "POST"),
        ("/api/admin/users/{id}", handle_users_get, "GET"),
        ("/api/admin/users/{id}", handle_users_patch, "PATCH"),
        ("/api/admin/users/{id}", handle_users_delete, "DELETE"),
        ("/api/admin/users/{id}/password", handle_users_reset_password, "POST"),
        ("/api/admin/stats", handle_stats, "GET"),
        ("/api/admin/bots/status", handle_bots_status, "GET"),
        ("/api/admin/sync", handle_sync, "POST"),
        ("/api/admin/clients", handle_clients_list, "GET"),
        ("/api/admin/clients/{id}", handle_client_detail, "GET"),
        ("/api/admin/channel-subscribers", handle_channel_subscribers_list, "GET"),
        ("/api/admin/channel-subscribers/settings", handle_channel_subscribers_settings, "GET"),
        ("/api/admin/channel-subscribers/settings", handle_channel_subscribers_settings, "POST"),
        ("/api/admin/auto-mail/settings", handle_auto_mail_settings, "GET"),
        ("/api/admin/auto-mail/settings", handle_auto_mail_settings, "POST"),
        ("/api/admin/backup", handle_backup_list, "GET"),
        ("/api/admin/backup", handle_backup_create, "POST"),
        ("/api/admin/backup/upload", handle_backup_upload, "POST"),
        ("/api/admin/backup/restore-file", handle_backup_restore_upload, "POST"),
        ("/api/admin/backup/{id}/file", handle_backup_file, "GET"),
        ("/api/admin/backup/{id}/restore", handle_backup_restore, "POST"),
        ("/api/admin/backup/{id}", handle_backup_get, "GET"),
        ("/api/admin/backup/{id}", handle_backup_delete, "DELETE"),
        ("/api/admin/channel-subscribers/sync", handle_channel_subscribers_sync, "POST"),
        ("/api/admin/channel-subscribers/discover", handle_channel_subscribers_discover, "POST"),
        ("/api/admin/channel-subscribers/ensure", handle_channel_subscribers_ensure, "POST"),
        ("/api/admin/events/upcoming", handle_events_upcoming, "GET"),
        ("/api/admin/events/{id}", handle_event_patch, "PATCH"),
        ("/api/admin/campaigns", handle_campaigns_list, "GET"),
        ("/api/admin/campaigns", handle_campaign_create, "POST"),
        ("/api/admin/campaigns/media", handle_campaign_media_upload, "POST"),
        ("/api/admin/campaigns/media/{name}", handle_campaign_media_get, "GET"),
        ("/api/admin/campaigns/{id}", handle_campaign_get, "GET"),
        ("/api/admin/campaigns/{id}", handle_campaign_patch, "PATCH"),
        ("/api/admin/campaigns/{id}/recipients", handle_campaign_recipients, "GET"),
        ("/api/admin/mailing/preview", handle_mailing_preview, "GET"),
        ("/api/admin/personal", handle_personal, "POST"),
        ("/api/admin/accounts", handle_accounts_list, "GET"),
        ("/api/admin/accounts/telegram/settings", handle_telegram_settings_get, "GET"),
        ("/api/admin/accounts/telegram/settings", handle_telegram_settings_save, "POST"),
        ("/api/admin/accounts/telegram/start", handle_telegram_connect_start, "POST"),
        ("/api/admin/accounts/telegram/resend", handle_telegram_connect_resend, "POST"),
        ("/api/admin/accounts/telegram/confirm", handle_telegram_connect_confirm, "POST"),
        ("/api/admin/accounts/telegram/qr/start", handle_telegram_qr_start, "POST"),
        ("/api/admin/accounts/telegram/qr/poll", handle_telegram_qr_poll, "POST"),
        ("/api/admin/accounts/telegram/qr/refresh", handle_telegram_qr_refresh, "POST"),
        ("/api/admin/accounts/telegram/qr/2fa", handle_telegram_qr_2fa, "POST"),
        ("/api/admin/accounts/telegram/qr/cancel", handle_telegram_qr_cancel, "POST"),
        ("/api/admin/accounts/telegram/keepalive", handle_telegram_keepalive, "POST"),
        ("/api/admin/accounts/max/userbot/start", handle_max_userbot_connect_start, "POST"),
        ("/api/admin/accounts/max/userbot/confirm", handle_max_userbot_connect_confirm, "POST"),
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
        ("/api/admin/max-chats/status", handle_max_chats_status, "GET"),
        ("/api/admin/max-chats/dialogs", handle_max_chats_dialogs, "GET"),
        ("/api/admin/max-chats/dialogs", handle_max_chats_create, "POST"),
        ("/api/admin/max-chats/dialogs/{peer_id}/messages", handle_max_chats_messages, "GET"),
        ("/api/admin/max-chats/dialogs/{peer_id}/messages/{message_id}/media", handle_max_chats_message_media, "GET"),
        ("/api/admin/max-chats/dialogs/{peer_id}/send", handle_max_chats_send, "POST"),
        ("/api/admin/max-chats/dialogs/{peer_id}/send-media", handle_max_chats_send_media, "POST"),
        ("/api/admin/max-chats/dialogs/{peer_id}/client", handle_max_chats_client, "GET"),
        ("/api/admin/max-chats/dialogs/{peer_id}/client", handle_max_chats_client_create, "POST"),
        ("/api/admin/max-chats/events", handle_max_chats_events, "GET"),
        ("/api/admin/max-chats/webhook", handle_max_webhook_status, "GET"),
        ("/api/max/webhook", handle_max_webhook, "POST"),
        ("/api/internal/max/event", handle_max_internal_event, "POST"),
        ("/api/admin/segments", handle_segment_counts, "GET"),
        ("/api/admin/ai/compose", handle_ai_compose, "POST"),
        ("/api/admin/ai/chat", handle_ai_chat, "POST"),
        ("/api/admin/ai/settings", handle_ai_settings_get, "GET"),
        ("/api/admin/ai/settings", handle_ai_settings_save, "POST"),
        ("/api/admin/promotions", handle_promotions_list, "GET"),
        ("/api/admin/promotions", handle_promotion_create, "POST"),
        ("/api/admin/promotions/overview", handle_promotions_overview, "GET"),
        ("/api/admin/promotions/analyze", handle_promotions_analyze, "POST"),
        ("/api/admin/promotions/analyze/apply", handle_promotions_analyze_apply, "POST"),
        ("/api/admin/promotions/{id}", handle_promotion_get, "GET"),
        ("/api/admin/promotions/{id}", handle_promotion_patch, "PATCH"),
        ("/api/admin/promotions/{id}", handle_promotion_delete, "DELETE"),
        ("/api/admin/wheel", handle_wheel_get, "GET"),
        ("/api/admin/wheel", handle_wheel_save, "POST"),
        ("/api/admin/wheel/plays", handle_wheel_plays_list, "GET"),
        ("/api/admin/wheel/plays", handle_wheel_plays_clear, "DELETE"),
        ("/api/admin/wheel/plays/{id}", handle_wheel_play_delete, "DELETE"),
        ("/api/wheel", handle_wheel_public_get, "GET"),
        ("/api/wheel/spin", handle_wheel_spin, "POST"),
        ("/api/wheel/notify", handle_wheel_notify, "POST"),
        ("/api/wheel/me", handle_wheel_my_play, "GET"),
    ]
    options_done: set[str] = set()
    for path, handler, method in routes:
        if path not in options_done:
            app.router.add_route("OPTIONS", path, handle_options)
            options_done.add(path)
        app.router.add_route(method, path, handler)


async def _admin_background_startup() -> None:
    """Posiflora + MAX webhook — после того, как HTTP уже слушает порт."""
    try:
        from max_bot.webhook_runtime import (
            ensure_runtime,
            register_webhook_subscription,
            webhook_enabled,
        )

        if not webhook_enabled():
            return
        try:
            from posiflora import start_token_refresher, warmup_token

            await warmup_token()
            start_token_refresher()
        except Exception:
            logger.exception(
                "Posiflora недоступна — анкеты MAX (webhook) только локально"
            )
        await ensure_runtime()
        await register_webhook_subscription()
    except Exception:
        logger.exception("MAX webhook startup failed")


async def on_admin_startup(_app: web.Application) -> None:
    """Не блокируем bind :3005 — иначе deploy/nginx ловят Connection refused / 502."""
    asyncio.create_task(_admin_background_startup(), name="admin-background-startup")
