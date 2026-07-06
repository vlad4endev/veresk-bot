from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import aiohttp

from config import (
    POSIFLORA_BASE_URL,
    POSIFLORA_PASSWORD,
    POSIFLORA_STORE_ID,
    POSIFLORA_USERNAME,
)

logger = logging.getLogger(__name__)

JSON_API_HEADERS = {
    "Content-Type": "application/vnd.api+json",
    "Accept": "application/vnd.api+json",
}

BUDGET_MAP = {
    "до 5 000 ₽": 4999,
    "до 10 000 ₽": 9999,
    "до 15 000 ₽": 14999,
    "от 15 000 ₽": 15000,
}

# Кэш токена между запросами polling (секунды)
_TOKEN_CACHE: dict[str, float | str] = {"token": "", "expires_at": 0.0}
_TOKEN_TTL_SEC = 50 * 60


class PosifloraAuthError(Exception):
    """Неверный логин/пароль или URL API Posiflora."""


class PosifloraAPIError(Exception):
    """Ошибка запроса к Posiflora после авторизации."""


def _normalize_phone(phone: str) -> str:
    """Нормализует телефон для Posiflora (только цифры, код страны 7)."""
    digits = re.sub(r"\D", "", phone.strip())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits


def _survey_date_to_iso(date_str: str) -> str:
    """Дата из анкеты (ДД.ММ.ГГГГ) → YYYY-MM-DD для Posiflora."""
    raw = date_str.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.now().strftime("%Y-%m-%d")


def _api_v1_url(path: str) -> str:
    """Собирает URL v1 API (поддерживает BASE_URL с /v1 и без)."""
    base = POSIFLORA_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        root = base
    else:
        root = f"{base}/v1"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{root}{path}"


def _parse_delivery_date(delivery_date: str) -> datetime:
    """Преобразует дату из анкеты клиента в datetime."""
    today = datetime.now()
    presets = {
        "Сегодня": today,
        "Завтра": today + timedelta(days=1),
        "Через 2–3 дня": today + timedelta(days=2),
        "Через неделю": today + timedelta(days=7),
        "Через 2 недели": today + timedelta(days=14),
    }
    if delivery_date in presets:
        return presets[delivery_date]

    raw = delivery_date.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(
                hour=today.hour, minute=today.minute, second=today.second
            )
        except ValueError:
            continue

    return today + timedelta(days=1)


def _delivery_window(delivery_date: str) -> tuple[str, str, str]:
    """Возвращает (deliveryAt для заказа, startAt, endAt для события)."""
    delivery_dt = _parse_delivery_date(delivery_date)
    start = delivery_dt.replace(hour=10, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    iso = "%Y-%m-%dT%H:%M:%S+03:00"
    return (
        start.strftime(iso),
        start.strftime(iso),
        end.strftime(iso),
    )


def _build_comment(
    customer_name: str,
    phone: str,
    recipient: str,
    occasion: str,
    relation: str,
    budget: str,
    delivery_date: str,
    telegram_id: int,
) -> str:
    return (
        "📱 Заказ через Telegram-бот\n"
        f"Клиент: {customer_name} (tg_id: {telegram_id})\n"
        f"Телефон: {phone}\n"
        f"Дата доставки: {delivery_date}\n"
        f"Получатель: {recipient}\n"
        f"Кто: {relation}\n"
        f"Повод: {occasion}\n"
        f"Бюджет: {budget}"
    )


def _build_event_title(recipient: str, occasion: str) -> str:
    recipient = recipient.strip() or "получатель"
    occasion = occasion.strip() or "букет"
    return f"Букет для {recipient} · {occasion}"


def _build_survey_event_title(relation: str, occasion: str) -> str:
    relation = relation.strip() or "близкий"
    occasion = occasion.strip() or "повод"
    return f"{relation} · {occasion}"


def _build_profile_notes(
    profile: dict[str, Any],
    telegram_id: int,
) -> str:
    """Текст заметки в карточке клиента Posiflora (все поля анкеты)."""
    lines = [
        "📱 Анкета Veresk (Telegram-бот)",
        f"Telegram ID: {telegram_id}",
        f"Бюджет: {profile.get('budget', '—')}",
        f"Источник: {profile.get('source', '—')}",
        "",
        "Важные даты:",
    ]
    events = profile.get("events") or []
    if not events:
        lines.append("— нет дат")
    else:
        for index, event in enumerate(events, start=1):
            lines.append(
                f"{index}. {event.get('date', '—')} — "
                f"{event.get('relation', '—')} — {event.get('occasion', '—')}"
            )
    return "\n".join(lines)


async def _read_error_body(resp: aiohttp.ClientResponse) -> str:
    try:
        text = await resp.text()
        return text[:2000] if text else ""
    except Exception:
        return ""


def _format_json_api_errors(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body

    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return json.dumps(errors, ensure_ascii=False, indent=2)
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _api_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    access_token: str | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = dict(JSON_API_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    url = _api_v1_url(path)

    try:
        async with session.request(method, url, headers=headers, json=json_body) as resp:
            body = await _read_error_body(resp)
            if resp.status >= 400:
                formatted = _format_json_api_errors(body)
                logger.error(
                    "Posiflora %s %s → HTTP %s\n%s",
                    method,
                    path,
                    resp.status,
                    formatted,
                )
                raise PosifloraAPIError(f"HTTP {resp.status}: {formatted}")
            if not body:
                return {}
            return json.loads(body)
    except aiohttp.ClientError as exc:
        logger.exception("Сетевая ошибка Posiflora %s %s", method, path)
        raise PosifloraAPIError(f"Сетевая ошибка: {exc}") from exc


async def _get_access_token(session: aiohttp.ClientSession) -> str:
    now = time.time()
    cached = _TOKEN_CACHE.get("token")
    if cached and now < float(_TOKEN_CACHE.get("expires_at", 0)):
        return str(cached)

    try:
        data = await _api_request(
            session,
            "POST",
            "/sessions",
            json_body={
                "data": {
                    "type": "sessions",
                    "attributes": {
                        "username": POSIFLORA_USERNAME,
                        "password": POSIFLORA_PASSWORD,
                    },
                }
            },
        )
    except PosifloraAPIError as exc:
        if "401" in str(exc):
            raise PosifloraAuthError(
                "Авторизация Posiflora отклонена (401). "
                "Укажите рабочий логин/пароль и URL вашего салона."
            ) from exc
        raise

    token = data["data"]["attributes"]["accessToken"]
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + _TOKEN_TTL_SEC
    return token


async def find_customer_by_phone(
    session: aiohttp.ClientSession,
    access_token: str,
    phone: str,
) -> str | None:
    normalized = _normalize_phone(phone)
    data = await _api_request(
        session,
        "GET",
        f"/customers?search={quote(normalized, safe='')}",
        access_token=access_token,
    )
    customers = data.get("data") or []
    if not customers:
        return None
    return str(customers[0]["id"])


async def create_customer(
    session: aiohttp.ClientSession,
    access_token: str,
    phone: str,
    title: str,
    *,
    notes: str = "",
) -> str:
    data = await _api_request(
        session,
        "POST",
        "/customers",
        access_token=access_token,
        json_body={
            "data": {
                "type": "customers",
                "attributes": {
                    "title": title.strip(),
                    "phone": _normalize_phone(phone),
                    "countryCode": 7,
                    "status": "on",
                    "notes": notes,
                },
            }
        },
    )
    return str(data["data"]["id"])


async def update_customer(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    *,
    title: str | None = None,
    notes: str | None = None,
) -> None:
    attributes: dict[str, str] = {}
    if title is not None:
        attributes["title"] = title.strip()
    if notes is not None:
        attributes["notes"] = notes

    await _api_request(
        session,
        "PATCH",
        f"/customers/{customer_id}",
        access_token=access_token,
        json_body={
            "data": {
                "type": "customers",
                "id": customer_id,
                "attributes": attributes,
            }
        },
    )


async def find_or_create_customer(
    session: aiohttp.ClientSession,
    access_token: str,
    phone: str,
    title: str,
    *,
    notes: str = "",
) -> tuple[str, bool]:
    """Find or Create клиента. Возвращает (customer_id, created)."""
    customer_id = await find_customer_by_phone(session, access_token, phone)
    if customer_id:
        logger.info("Posiflora: клиент найден по телефону, id=%s", customer_id)
        await update_customer(
            session, access_token, customer_id, title=title, notes=notes
        )
        return customer_id, False

    customer_id = await create_customer(
        session, access_token, phone, title, notes=notes
    )
    logger.info("Posiflora: клиент создан, id=%s", customer_id)
    return customer_id, True


async def create_customer_event(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    *,
    title: str,
    date_from: str,
    date_until: str,
) -> str | None:
    """
    POST /customer-events — событие в календаре клиента.
    Поля по документации Posiflora: title, dateFrom, dateUntil.
    """
    try:
        data = await _api_request(
            session,
            "POST",
            "/customer-events",
            access_token=access_token,
            json_body={
                "data": {
                    "type": "customer-events",
                    "attributes": {
                        "title": title,
                        "dateFrom": date_from,
                        "dateUntil": date_until,
                    },
                    "relationships": {
                        "customer": {
                            "data": {"type": "customers", "id": customer_id},
                        }
                    },
                }
            },
        )
        return str(data["data"]["id"])
    except PosifloraAPIError as exc:
        if "500" in str(exc):
            logger.warning(
                "Posiflora: POST /customer-events недоступен (500), "
                "событие «%s» сохранено только в notes клиента",
                title,
            )
            return None
        raise


async def sync_survey_profile_to_posiflora(
    profile: dict[str, Any],
    telegram_id: int,
) -> dict[str, Any]:
    """
    Синхронизация анкеты (7 вопросов) с Posiflora:
    1. Find or Create клиента по телефону
    2. Обновление notes (имя, бюджет, источник, все даты)
    3. Создание customer-events для каждой важной даты из анкеты
    """
    name = str(profile.get("name", "")).strip()
    phone = str(profile.get("phone", "")).strip()
    events = list(profile.get("events") or [])

    if not name or not phone:
        raise PosifloraAPIError("Для синхронизации нужны имя и телефон клиента")

    notes = _build_profile_notes(profile, telegram_id)
    event_ids: list[str] = []
    events_failed = 0

    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)
        customer_id, created = await find_or_create_customer(
            session,
            access_token,
            phone,
            name,
            notes=notes,
        )

        for event in events:
            title = _build_survey_event_title(
                str(event.get("relation", "")),
                str(event.get("occasion", "")),
            )
            date_iso = _survey_date_to_iso(str(event.get("date", "")))
            event_id = await create_customer_event(
                session,
                access_token,
                customer_id,
                title=title,
                date_from=date_iso,
                date_until=date_iso,
            )
            if event_id:
                event_ids.append(event_id)
                logger.info(
                    "Posiflora: событие #%s «%s» (%s) для клиента #%s",
                    event_id,
                    title,
                    date_iso,
                    customer_id,
                )
            else:
                events_failed += 1

    # Клиент синхронизирован; события могут быть только в notes, если API events падает
    posiflora_ok = bool(customer_id)
    return {
        "customer_id": customer_id,
        "customer_created": created,
        "event_ids": event_ids,
        "events_total": len(events),
        "events_synced": len(event_ids),
        "events_failed": events_failed,
        "posiflora_ok": posiflora_ok,
    }


async def _create_order(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    *,
    customer_name: str,
    phone: str,
    recipient: str,
    occasion: str,
    relation: str,
    budget: str,
    delivery_date: str,
    telegram_id: int,
) -> str:
    delivery_at, _, _ = _delivery_window(delivery_date)
    amount = BUDGET_MAP.get(budget, 4999)
    comment = _build_comment(
        customer_name,
        phone,
        recipient,
        occasion,
        relation,
        budget,
        delivery_date,
        telegram_id,
    )

    data = await _api_request(
        session,
        "POST",
        "/orders",
        access_token=access_token,
        json_body={
            "data": {
                "type": "orders",
                "attributes": {
                    "amount": amount,
                    "comment": comment,
                    "deliveryAt": delivery_at,
                    "storeId": POSIFLORA_STORE_ID,
                    "source": "telegram",
                },
                "relationships": {
                    "client": {
                        "data": {"type": "customers", "id": customer_id},
                    }
                },
            }
        },
    )
    return str(data["data"]["id"])


async def create_posiflora_order(
    customer_name: str,
    phone: str,
    recipient: str,
    occasion: str,
    relation: str,
    budget: str,
    delivery_date: str,
    telegram_id: int,
) -> str:
    """
    Find or Create клиента, событие в календаре и заказ в Posiflora.
    Все поля из Mini App передаются в CRM.
    """
    delivery_at, _, _ = _delivery_window(delivery_date)
    event_title = _build_event_title(recipient, occasion)
    date_iso = delivery_at[:10]

    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)

        customer_id, _ = await find_or_create_customer(
            session,
            access_token,
            phone,
            customer_name,
            notes=_build_comment(
                customer_name,
                phone,
                recipient,
                occasion,
                relation,
                budget,
                delivery_date,
                telegram_id,
            ),
        )

        event_id = await create_customer_event(
            session,
            access_token,
            customer_id,
            title=event_title,
            date_from=date_iso,
            date_until=date_iso,
        )
        if event_id:
            logger.info(
                "Posiflora: событие #%s создано для клиента #%s", event_id, customer_id
            )

        order_id = await _create_order(
            session,
            access_token,
            customer_id,
            customer_name=customer_name,
            phone=phone,
            recipient=recipient,
            occasion=occasion,
            relation=relation,
            budget=budget,
            delivery_date=delivery_date,
            telegram_id=telegram_id,
        )
        return order_id


async def get_order_status(order_id: str) -> str:
    """
    GET /v1/orders/{id}
    Возвращает текущий статус заказа.
    """
    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)
        data = await _api_request(
            session,
            "GET",
            f"/orders/{order_id}",
            access_token=access_token,
        )
        if not data.get("data"):
            raise PosifloraAPIError(f"Заказ #{order_id} не найден в Posiflora")
        attrs = data["data"]["attributes"]
        return attrs.get("status") or attrs.get("deliveryStatus", "unknown")
