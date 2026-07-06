from __future__ import annotations

import json
import logging
import re
import time
import uuid
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
    "более 15 000 ₽": 15000,
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


def _budget_to_amount(budget: str) -> int:
    """Сумма бюджета из анкеты для отображения в карточке клиента."""
    clean = budget.strip()
    if clean in BUDGET_MAP:
        return BUDGET_MAP[clean]
    digits = re.sub(r"\D", "", clean)
    if digits:
        return int(digits)
    return 4999


def _build_profile_notes(
    profile: dict[str, Any],
    telegram_id: int,
) -> str:
    """Полная карточка ответов анкеты → customers.attributes.notes."""
    name = str(profile.get("name", "")).strip() or "—"
    phone = str(profile.get("phone", "")).strip() or "—"
    budget = str(profile.get("budget", "")).strip() or "—"
    source = str(profile.get("source", "")).strip() or "—"
    amount = _budget_to_amount(budget) if budget != "—" else None

    lines = [
        "📱 Анкета Veresk (Telegram-бот)",
        "─────────────────────────",
        f"👤 Имя: {name}",
        f"📞 Телефон: {phone}",
        f"💰 Бюджет: {budget}",
    ]
    if amount is not None:
        lines.append(f"💵 Сумма: {amount:,} ₽".replace(",", " "))
    lines.extend(
        [
            f"📣 Откуда узнали: {source}",
            f"🆔 Telegram ID: {telegram_id}",
            "",
            "📅 Важные даты:",
        ]
    )
    events = profile.get("events") or []
    if not events:
        lines.append("— нет дат")
    else:
        for index, event in enumerate(events, start=1):
            lines.append(
                f"{index}. {event.get('date', '—')} · "
                f"{event.get('relation', '—')} · {event.get('occasion', '—')}"
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


async def _list_customer_sources(
    session: aiohttp.ClientSession,
    access_token: str,
) -> list[dict[str, Any]]:
    data = await _api_request(
        session,
        "GET",
        "/customer-sources",
        access_token=access_token,
    )
    return list(data.get("data") or [])


async def get_or_create_customer_source(
    session: aiohttp.ClientSession,
    access_token: str,
    title: str,
) -> str | None:
    """
    Источник клиента — отдельный справочник Posiflora (customer-sources).
    GET /customer-sources → POST /customer-sources при отсутствии.
    """
    clean = title.strip()
    if not clean:
        return None

    for item in await _list_customer_sources(session, access_token):
        attrs = item.get("attributes") or {}
        if attrs.get("title", "").strip().lower() == clean.lower():
            return str(item["id"])

    data = await _api_request(
        session,
        "POST",
        "/customer-sources",
        access_token=access_token,
        json_body={
            "data": {
                "type": "customer-sources",
                "attributes": {"title": clean},
            }
        },
    )
    return str(data["data"]["id"])


def _customer_relationships(source_id: str | None) -> dict[str, Any] | None:
    if not source_id:
        return None
    return {
        "customerSources": {
            "data": [{"type": "customer-sources", "id": source_id}],
        }
    }


async def create_customer(
    session: aiohttp.ClientSession,
    access_token: str,
    phone: str,
    title: str,
    *,
    notes: str = "",
    source_id: str | None = None,
) -> str:
    payload: dict[str, Any] = {
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
    }
    relationships = _customer_relationships(source_id)
    if relationships:
        payload["data"]["relationships"] = relationships

    data = await _api_request(
        session,
        "POST",
        "/customers",
        access_token=access_token,
        json_body=payload,
    )
    return str(data["data"]["id"])


async def update_customer(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    *,
    title: str | None = None,
    notes: str | None = None,
    source_id: str | None = None,
) -> None:
    attributes: dict[str, str] = {}
    if title is not None:
        attributes["title"] = title.strip()
    if notes is not None:
        attributes["notes"] = notes

    payload: dict[str, Any] = {
        "data": {
            "type": "customers",
            "id": customer_id,
            "attributes": attributes,
        }
    }
    relationships = _customer_relationships(source_id)
    if relationships:
        payload["data"]["relationships"] = relationships

    await _api_request(
        session,
        "PATCH",
        f"/customers/{customer_id}",
        access_token=access_token,
        json_body=payload,
    )


async def sync_customer_card_from_survey(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    profile: dict[str, Any],
    telegram_id: int,
    *,
    source_id: str | None,
) -> None:
    """
    Заполняет карточку клиента Posiflora по ответам анкеты бота.

    | Поле анкеты | Posiflora API                  |
    |-------------|--------------------------------|
    | name        | attributes.title               |
    | phone       | attributes.phone, countryCode  |
    | source      | relationships.customerSources  |
    | все ответы  | attributes.notes             |
    """
    name = str(profile.get("name", "")).strip()
    notes = _build_profile_notes(profile, telegram_id)
    await update_customer(
        session,
        access_token,
        customer_id,
        title=name,
        notes=notes,
        source_id=source_id,
    )
    logger.info(
        "Posiflora: карточка клиента #%s — имя, источник, notes",
        customer_id,
    )


async def get_or_create_customer_id_by_phone(
    session: aiohttp.ClientSession,
    access_token: str,
    phone: str,
    title: str,
    *,
    notes: str = "",
    source_id: str | None = None,
) -> tuple[str, bool]:
    """
    1. GET /customers?search={phone} — если найден, вернуть id.
    2. Иначе POST /customers — создать и вернуть id.
    """
    customer_id = await find_customer_by_phone(session, access_token, phone)
    if customer_id:
        logger.info("Posiflora: клиент найден по телефону, id=%s", customer_id)
        return customer_id, False

    customer_id = await create_customer(
        session,
        access_token,
        phone,
        title,
        notes=notes,
        source_id=source_id,
    )
    logger.info("Posiflora: клиент создан, id=%s", customer_id)
    return customer_id, True


async def find_or_create_customer(
    session: aiohttp.ClientSession,
    access_token: str,
    phone: str,
    title: str,
    *,
    notes: str = "",
    source_id: str | None = None,
) -> tuple[str, bool]:
    """Find or Create клиента. Возвращает (customer_id, created)."""
    customer_id, created = await get_or_create_customer_id_by_phone(
        session,
        access_token,
        phone,
        title,
        notes=notes,
        source_id=source_id,
    )
    if not created:
        await update_customer(
            session,
            access_token,
            customer_id,
            title=title,
            notes=notes,
            source_id=source_id,
        )
    return customer_id, created


def _build_survey_celebration_title(
    relation: str,
    occasion: str,
    date_iso: str,
) -> str:
    """Уникальный заголовок праздника: дата в title, т.к. Posiflora не допускает дубликаты."""
    base = _build_survey_event_title(relation, occasion)
    try:
        dt = datetime.strptime(date_iso, "%Y-%m-%d")
        date_label = dt.strftime("%d.%m.%Y")
    except ValueError:
        date_label = date_iso
    return f"{base} · {date_label}"


async def list_customer_celebrations(
    session: aiohttp.ClientSession,
    access_token: str,
    *,
    include_dates: bool = True,
) -> dict[str, Any]:
    """GET /customer-celebrations — список праздников салона."""
    path = "/customer-celebrations"
    if include_dates:
        path += "?include=dates"
    return await _api_request(session, "GET", path, access_token=access_token)


async def get_customer_celebration(
    session: aiohttp.ClientSession,
    access_token: str,
    celebration_id: str,
) -> dict[str, Any]:
    """GET /customer-celebration/{id} — карточка праздника."""
    return await _api_request(
        session,
        "GET",
        f"/customer-celebration/{celebration_id}",
        access_token=access_token,
    )


async def list_customer_celebration_dates(
    session: aiohttp.ClientSession,
    access_token: str,
    *,
    customer_id: str | None = None,
) -> dict[str, Any]:
    """GET /customer-celebration-dates — связи клиент ↔ праздник ↔ дата."""
    path = "/customer-celebration-dates"
    if customer_id:
        path += f"?filter[customer]={quote(customer_id, safe='')}"
    return await _api_request(session, "GET", path, access_token=access_token)


async def find_customer_celebration_by_title(
    session: aiohttp.ClientSession,
    access_token: str,
    title: str,
) -> str | None:
    """Ищет праздник в справочнике по точному совпадению title."""
    clean = title.strip()
    if not clean:
        return None

    data = await list_customer_celebrations(session, access_token)
    for item in data.get("data") or []:
        attrs = item.get("attributes") or {}
        if attrs.get("title", "").strip() == clean:
            return str(item["id"])
    return None


async def create_customer_celebration(
    session: aiohttp.ClientSession,
    access_token: str,
    *,
    title: str,
    date_from: str,
    date_until: str | None = None,
    customer_id: str | None = None,
) -> str:
    """
    POST /customer-celebrations — создаёт праздник в справочнике Posiflora.

    Документация: https://posiflora.com/api/#tag/Customer-Celebrations-API

    Тело запроса: title + вложенные celebration-dates (dateFrom, dateUntil).
    Опционально relationships.customer — привязка к клиенту (best-effort).
    """
    until = date_until or date_from
    relationships: dict[str, Any] = {
        "dates": {
            "data": [
                {
                    "type": "celebration-dates",
                    "attributes": {
                        "dateFrom": date_from,
                        "dateUntil": until,
                    },
                }
            ],
        }
    }
    if customer_id:
        relationships["customer"] = {
            "data": {"type": "customers", "id": customer_id},
        }

    payload = {
        "data": {
            "type": "customer-celebrations",
            "attributes": {"title": title.strip()},
            "relationships": relationships,
        }
    }

    try:
        data = await _api_request(
            session,
            "POST",
            "/customer-celebrations",
            access_token=access_token,
            json_body=payload,
        )
        return str(data["data"]["id"])
    except PosifloraAPIError as exc:
        if "already registered" not in str(exc):
            raise
        existing_id = await find_customer_celebration_by_title(
            session, access_token, title
        )
        if existing_id:
            logger.info(
                "Posiflora: праздник «%s» уже есть в справочнике, id=%s",
                title,
                existing_id,
            )
            return existing_id
        raise


async def update_customer_celebration(
    session: aiohttp.ClientSession,
    access_token: str,
    celebration_id: str,
    *,
    title: str | None = None,
    date_from: str | None = None,
    date_until: str | None = None,
    date_id: str | None = None,
) -> None:
    """PATCH /customer-celebration/{id} — обновление праздника."""
    payload_data: dict[str, Any] = {
        "type": "customer-celebrations",
        "id": celebration_id,
    }
    if title is not None:
        payload_data["attributes"] = {"title": title.strip()}

    if date_from is not None:
        date_attrs = {
            "dateFrom": date_from,
            "dateUntil": date_until or date_from,
        }
        date_entry: dict[str, Any] = {
            "type": "celebration-dates",
            "attributes": date_attrs,
        }
        if date_id:
            date_entry["id"] = date_id
        payload_data["relationships"] = {
            "dates": {"data": [date_entry]},
        }

    await _api_request(
        session,
        "PATCH",
        f"/customer-celebration/{celebration_id}",
        access_token=access_token,
        json_body={"data": payload_data},
    )


async def delete_customer_celebrations(
    session: aiohttp.ClientSession,
    access_token: str,
    celebration_ids: list[str],
) -> None:
    """DELETE /customer-celebrations — удаление праздников из справочника."""
    if not celebration_ids:
        return
    await _api_request(
        session,
        "DELETE",
        "/customer-celebrations",
        access_token=access_token,
        json_body={
            "data": [
                {"id": celebration_id, "type": "customer-celebrations"}
                for celebration_id in celebration_ids
            ]
        },
    )


async def link_customer_celebration(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    celebration_id: str,
) -> bool:
    """
    Пытается привязать праздник к клиенту через PATCH /customers/{id}.
    На veresksalon связь в customer-celebration-dates через API не подтверждена.
    """
    try:
        await _api_request(
            session,
            "PATCH",
            f"/customers/{customer_id}",
            access_token=access_token,
            json_body={
                "data": {
                    "type": "customers",
                    "id": customer_id,
                    "relationships": {
                        "customerCelebrations": {
                            "data": [
                                {
                                    "type": "customer-celebrations",
                                    "id": celebration_id,
                                }
                            ],
                        }
                    },
                }
            },
        )
    except PosifloraAPIError:
        return False

    data = await list_customer_celebration_dates(
        session, access_token, customer_id=customer_id
    )
    linked = any(
        (item.get("relationships") or {})
        .get("celebration", {})
        .get("data", {})
        .get("id")
        == celebration_id
        for item in data.get("data") or []
    )
    return linked


async def create_customer_celebration_for_customer(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    *,
    title: str,
    date_from: str,
    date_until: str | None = None,
) -> str:
    """Создаёт праздник и пытается привязать его к клиенту."""
    celebration_id = await create_customer_celebration(
        session,
        access_token,
        title=title,
        date_from=date_from,
        date_until=date_until,
        customer_id=customer_id,
    )
    linked = await link_customer_celebration(
        session, access_token, customer_id, celebration_id
    )
    if linked:
        logger.info(
            "Posiflora: праздник #%s «%s» привязан к клиенту #%s",
            celebration_id,
            title,
            customer_id,
        )
    else:
        logger.info(
            "Posiflora: праздник #%s «%s» создан в справочнике "
            "(привязка к клиенту #%s через API не подтверждена)",
            celebration_id,
            title,
            customer_id,
        )
    return celebration_id


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

    Документация: CreateCustomerEventsAction
    https://posiflora.com/api/#tag/Customer-Events-API/operation/CreateCustomerEventsAction

    Важно: Posiflora требует client-generated UUID в data.id, иначе HTTP 500.
    """
    event_id = str(uuid.uuid4())
    try:
        data = await _api_request(
            session,
            "POST",
            "/customer-events",
            access_token=access_token,
            json_body={
                "data": {
                    "type": "customer-events",
                    "id": event_id,
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


async def create_customer_events_from_survey(
    session: aiohttp.ClientSession,
    access_token: str,
    customer_id: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str], int, int]:
    """
    Создаёт события анкеты в Posiflora — каждое отдельным POST /customer-events.

    Один клиент, N дат из бота → N отдельных запросов (свой UUID на каждый).
    """
    event_ids: list[str] = []
    celebration_ids: list[str] = []
    events_failed = 0
    celebrations_linked = 0
    total = len(events)

    for index, event in enumerate(events, start=1):
        relation = str(event.get("relation", ""))
        occasion = str(event.get("occasion", ""))
        title = _build_survey_event_title(relation, occasion)
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
                "Posiflora: событие %s/%s #%s «%s» (%s) для клиента #%s",
                index,
                total,
                event_id,
                title,
                date_iso,
                customer_id,
            )
            continue

        events_failed += 1
        celebration_title = _build_survey_celebration_title(
            relation, occasion, date_iso
        )
        try:
            celebration_id = await create_customer_celebration_for_customer(
                session,
                access_token,
                customer_id,
                title=celebration_title,
                date_from=date_iso,
                date_until=date_iso,
            )
            celebration_ids.append(celebration_id)
            linked_dates = await list_customer_celebration_dates(
                session,
                access_token,
                customer_id=customer_id,
            )
            if any(
                (item.get("relationships") or {})
                .get("celebration", {})
                .get("data", {})
                .get("id")
                == celebration_id
                for item in linked_dates.get("data") or []
            ):
                celebrations_linked += 1
            logger.info(
                "Posiflora: fallback праздник %s/%s #%s «%s» для клиента #%s",
                index,
                total,
                celebration_id,
                celebration_title,
                customer_id,
            )
        except PosifloraAPIError as exc:
            logger.warning(
                "Posiflora: событие %s/%s не создано для клиента #%s: %s",
                index,
                total,
                customer_id,
                exc,
            )

    return event_ids, celebration_ids, events_failed, celebrations_linked


async def create_customer_event_by_phone(
    session: aiohttp.ClientSession,
    access_token: str,
    phone: str,
    name: str,
    *,
    title: str,
    date_from: str,
    date_until: str,
    notes: str = "",
    source_id: str | None = None,
) -> tuple[str | None, str, bool]:
    """
    Создание события клиента: сначала клиент по телефону, затем POST /customer-events.

    1. GET /customers?search={phone} → id
    2. Если не найден — POST /customers → id
    3. POST /customer-events с полученным customer id

    Возвращает (event_id, customer_id, customer_created).
    """
    customer_id, created = await get_or_create_customer_id_by_phone(
        session,
        access_token,
        phone,
        name,
        notes=notes,
        source_id=source_id,
    )
    if not created and notes:
        await update_customer(
            session,
            access_token,
            customer_id,
            title=name,
            notes=notes,
            source_id=source_id,
        )
    event_id = await create_customer_event(
        session,
        access_token,
        customer_id,
        title=title,
        date_from=date_from,
        date_until=date_until,
    )
    return event_id, customer_id, created


async def sync_survey_profile_to_posiflora(
    profile: dict[str, Any],
    telegram_id: int,
) -> dict[str, Any]:
    """
    Синхронизация анкеты (7 вопросов) с Posiflora:
    1. Поиск клиента по телефону → id (или создание → id)
    2. Заполнение карточки: имя, источник, notes
    3. Каждая дата — отдельный POST /customer-events
    """
    name = str(profile.get("name", "")).strip()
    phone = str(profile.get("phone", "")).strip()
    events = list(profile.get("events") or [])

    if not name or not phone:
        raise PosifloraAPIError("Для синхронизации нужны имя и телефон клиента")

    notes = _build_profile_notes(profile, telegram_id)

    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)
        source_id = await get_or_create_customer_source(
            session,
            access_token,
            str(profile.get("source", "")),
        )
        customer_id, created = await get_or_create_customer_id_by_phone(
            session,
            access_token,
            phone,
            name,
            notes=notes,
            source_id=source_id,
        )
        await sync_customer_card_from_survey(
            session,
            access_token,
            customer_id,
            profile,
            telegram_id,
            source_id=source_id,
        )

        (
            event_ids,
            celebration_ids,
            events_failed,
            celebrations_linked,
        ) = await create_customer_events_from_survey(
            session,
            access_token,
            customer_id,
            events,
        )

    posiflora_ok = bool(customer_id)
    return {
        "customer_id": customer_id,
        "customer_created": created,
        "source_id": source_id,
        "event_ids": event_ids,
        "celebration_ids": celebration_ids,
        "events_total": len(events),
        "events_synced": len(event_ids),
        "events_failed": events_failed,
        "celebrations_synced": len(celebration_ids),
        "celebrations_linked": celebrations_linked,
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
    Find or Create клиента по телефону, событие в календаре и заказ в Posiflora.
    """
    delivery_at, _, _ = _delivery_window(delivery_date)
    event_title = _build_event_title(recipient, occasion)
    date_iso = delivery_at[:10]
    order_comment = _build_comment(
        customer_name,
        phone,
        recipient,
        occasion,
        relation,
        budget,
        delivery_date,
        telegram_id,
    )

    async with aiohttp.ClientSession() as session:
        access_token = await _get_access_token(session)

        event_id, customer_id, _ = await create_customer_event_by_phone(
            session,
            access_token,
            phone,
            customer_name,
            title=event_title,
            date_from=date_iso,
            date_until=date_iso,
            notes=order_comment,
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
