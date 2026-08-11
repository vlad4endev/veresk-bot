"""
Инструменты ИИ-агента админки: досье клиента и tool-calling по CRM / ботам / рассылкам.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from mailing_db import (
    count_customers,
    get_customer,
    get_fortune_play,
    get_order_stats_for_customer,
    get_stats,
    list_campaigns,
    list_customers,
    list_events_for_customer,
    list_fortune_plays,
    list_messages_for_customer,
    list_orders_for_customer,
    list_upcoming_events,
)

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
MAX_TOOL_RESULT_CHARS = 6000

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]

AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "lookup_customer",
            "description": (
                "Найти клиента CRM и вернуть полное досье: контакты, заметки, "
                "заказы, события, личные/кампанейские сообщения, анкету бота TG/MAX, "
                "колесо фортуны, превью чата MAX. "
                "Вызывай, когда сотрудник спрашивает про конкретного человека "
                "или нужны детали перед текстом сообщения."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "integer",
                        "description": "ID клиента в CRM, если известен",
                    },
                    "query": {
                        "type": "string",
                        "description": "Имя, фамилия или телефон для поиска",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_upcoming_events",
            "description": (
                "Ближайшие ДР, годовщины и другие события клиентов на N дней вперёд. "
                "Включает auto_send и каналы — для акций автопоздравлений."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Горизонт в днях (1–90), по умолчанию 14",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимум записей (1–40), по умолчанию 20",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_segment_customers",
            "description": (
                "Примеры клиентов сегмента CRM: all / regular / new / inactive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "segment": {
                        "type": "string",
                        "enum": ["all", "regular", "new", "inactive"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Сколько примеров вернуть (1–20)",
                    },
                    "search": {
                        "type": "string",
                        "description": "Опциональный фильтр по имени/телефону",
                    },
                },
                "required": ["segment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shop_overview",
            "description": (
                "Сводка салона: число клиентов, сегменты, доставляемость, "
                "аккаунты рассылки, последние кампании, метрики ботов."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_campaigns",
            "description": "Последние рассылки: статус, сегмент, превью текста.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "1–15, по умолчанию 8",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_fortune_plays",
            "description": "Недавние розыгрыши колеса фортуны (Telegram / MAX).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "1–30, по умолчанию 15",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_promotions",
            "description": (
                "Список акций и скидок салона: статус, сегмент, процент, "
                "использование в авторассылке и плейсхолдере {скидка}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "1–40, по умолчанию 20",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_promotions",
            "description": (
                "ИИ-анализ CRM и событий клиентов: предложить акции для автопоздравлений "
                "(birthday/anniversary), скидки и идеи рассылок. "
                "Используй для «какие акции сделать», «автопоздравления», «ДР на неделю»."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "description": (
                            "Фокус: автопоздравления, ДР, годовщины, inactive, весна…"
                        ),
                    },
                    "horizon_days": {
                        "type": "integer",
                        "description": "Горизонт 3–60 дней, по умолчанию 14",
                    },
                },
            },
        },
    },
]


def provider_supports_tools(provider: str) -> bool:
    """YandexGPT часто без function calling — остаёмся на обогащённом контексте."""
    return (provider or "").strip().lower() in (
        "openai",
        "openrouter",
        "deepseek",
        "custom",
    )


def _clip(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 20] + "\n… (обрезано)"


def _safe_json(obj: Any) -> str:
    try:
        return _clip(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return _clip(str(obj))


async def _bot_survey_bits(customer: dict[str, Any]) -> dict[str, Any]:
    """Анкеты TG/MAX-ботов и заказы опроса, связанные с клиентом."""
    out: dict[str, Any] = {}
    phone = (customer.get("phone") or "").strip()
    tg_id = customer.get("tg_user_id")
    max_uid = customer.get("max_user_id")

    try:
        from client_db import (
            get_client_profile,
            get_orders_for_client,
            get_profile_by_phone,
        )

        profile = None
        if tg_id:
            profile = await get_client_profile(int(tg_id))
        if not profile and phone:
            profile = await get_profile_by_phone(phone)
        if profile:
            events = profile.get("events") or []
            out["tg_survey"] = {
                "tg_id": profile.get("tg_id"),
                "name": profile.get("name"),
                "phone": profile.get("phone"),
                "budget": profile.get("budget") or "",
                "source": profile.get("source") or "",
                "events": events[:8],
                "updated_at": profile.get("updated_at"),
            }
            survey_tg = profile.get("tg_id") or tg_id
            if survey_tg:
                bot_orders = await get_orders_for_client(int(survey_tg), limit=8)
                if bot_orders:
                    out["tg_bot_orders"] = [
                        {
                            "posiflora_id": o.get("posiflora_order_id"),
                            "status": o.get("status"),
                            "delivery_date": o.get("delivery_date"),
                            "recipient": o.get("recipient"),
                            "occasion": o.get("occasion"),
                            "budget": o.get("budget"),
                            "created_at": o.get("created_at"),
                        }
                        for o in bot_orders
                    ]
    except Exception:
        logger.debug("AI agent: tg survey failed", exc_info=True)

    max_profile: dict[str, Any] | None = None
    try:
        from max_bot.storage import get_dialog, get_max_profile, get_max_profile_by_phone

        if max_uid:
            max_profile = await get_max_profile(int(max_uid))
        if not max_profile and phone:
            max_profile = await get_max_profile_by_phone(phone)
        if max_profile:
            events_raw = max_profile.get("events_json") or "[]"
            try:
                events = (
                    json.loads(events_raw)
                    if isinstance(events_raw, str)
                    else (events_raw or [])
                )
            except json.JSONDecodeError:
                events = []
            out["max_survey"] = {
                "max_user_id": max_profile.get("max_user_id"),
                "name": max_profile.get("name"),
                "phone": max_profile.get("phone"),
                "budget": max_profile.get("budget") or "",
                "source": max_profile.get("source") or "",
                "events": (events or [])[:8],
                "updated_at": max_profile.get("updated_at"),
            }
        dialog_uid = max_uid or (max_profile or {}).get("max_user_id")
        if dialog_uid:
            dialog = await get_dialog(max_user_id=int(dialog_uid))
            if dialog:
                out["max_chat_preview"] = {
                    "chat_id": dialog.get("chat_id"),
                    "last_text": (dialog.get("last_text") or "")[:300],
                    "last_at": dialog.get("last_at"),
                    "last_out": bool(dialog.get("last_out")),
                    "username": dialog.get("username") or "",
                }
    except Exception:
        logger.debug("AI agent: max survey failed", exc_info=True)

    return out


async def build_customer_dossier(customer_id: int) -> dict[str, Any] | None:
    """Полная карточка клиента для ИИ (CRM + боты + фортуна)."""
    c = await get_customer(int(customer_id))
    if not c:
        return None

    cid = int(c["id"])
    dossier: dict[str, Any] = {
        "id": cid,
        "name": c.get("name") or "",
        "phone": c.get("phone") or "",
        "segment": c.get("segment") or "",
        "tg_user_id": c.get("tg_user_id"),
        "max_user_id": c.get("max_user_id"),
        "notes": (c.get("notes") or "").strip(),
        "last_order_at": c.get("last_order_at"),
        "created_in_pf_at": c.get("created_in_pf_at"),
        "posiflora_id": c.get("posiflora_id") or "",
    }

    try:
        dossier["order_stats"] = await get_order_stats_for_customer(cid)
    except Exception:
        dossier["order_stats"] = {}

    try:
        orders = await list_orders_for_customer(cid, limit=12)
        dossier["orders"] = [
            {
                "ordered_at": o.get("ordered_at"),
                "amount": o.get("amount") or 0,
                "number": o.get("number") or "",
                "status": o.get("status") or "",
                "comment": (o.get("comment") or "")[:120],
                "delivery_at": o.get("delivery_at"),
            }
            for o in (orders or [])
        ]
    except Exception:
        dossier["orders"] = []

    try:
        events = await list_events_for_customer(cid)
        dossier["events"] = [
            {
                "kind": ev.get("kind") or "",
                "title": ev.get("title") or "",
                "date_from": ev.get("date_from") or "",
                "auto_send": bool(ev.get("auto_send")),
            }
            for ev in (events or [])[:10]
        ]
    except Exception:
        dossier["events"] = []

    try:
        messages = await list_messages_for_customer(cid, limit=12)
        dossier["messages"] = [
            {
                "kind": m.get("kind"),
                "title": (m.get("title") or "")[:200],
                "channel": m.get("channel"),
                "status": m.get("status"),
                "date": m.get("date"),
            }
            for m in (messages or [])
        ]
    except Exception:
        dossier["messages"] = []

    fortune: list[dict[str, Any]] = []
    try:
        if c.get("tg_user_id"):
            play = await get_fortune_play("telegram", str(c["tg_user_id"]))
            if play:
                fortune.append(
                    {
                        "channel": "telegram",
                        "prize": play.get("prize_label") or play.get("prize") or "",
                        "created_at": play.get("created_at"),
                    }
                )
        if c.get("max_user_id"):
            play = await get_fortune_play("max", str(c["max_user_id"]))
            if play:
                fortune.append(
                    {
                        "channel": "max",
                        "prize": play.get("prize_label") or play.get("prize") or "",
                        "created_at": play.get("created_at"),
                    }
                )
    except Exception:
        logger.debug("AI agent: fortune failed", exc_info=True)
    if fortune:
        dossier["fortune"] = fortune

    extra = await _bot_survey_bits(c)
    dossier.update(extra)
    return dossier


def format_dossier_text(dossier: dict[str, Any]) -> str:
    """Человекочитаемое досье для system/tool result."""
    lines: list[str] = []
    lines.append(
        f"Клиент id={dossier.get('id')} «{dossier.get('name') or '—'}» "
        f"тел={dossier.get('phone') or '—'} сегмент={dossier.get('segment') or '—'} "
        f"tg={dossier.get('tg_user_id') or '—'} max={dossier.get('max_user_id') or '—'}"
    )
    notes = (dossier.get("notes") or "").strip()
    if notes:
        lines.append(f"Заметки: {notes[:500]}")
    ost = dossier.get("order_stats") or {}
    if ost:
        lines.append(
            f"Заказы CRM: count={ost.get('orders_count', 0)}, "
            f"sum={ost.get('total_spent', 0)}, avg={ost.get('avg_order', 0)}, "
            f"last={ost.get('last_order_at') or '—'}"
        )
    for o in dossier.get("orders") or []:
        lines.append(
            f"  заказ {o.get('ordered_at') or '—'} сумма={o.get('amount') or 0} "
            f"№{o.get('number') or '—'} [{o.get('status') or ''}] "
            f"{(o.get('comment') or '')[:80]}"
        )
    for ev in dossier.get("events") or []:
        auto = " [авто]" if ev.get("auto_send") else ""
        lines.append(
            f"  событие: {ev.get('date_from') or '—'} "
            f"{ev.get('title') or ev.get('kind') or ''}{auto}"
        )
    for m in dossier.get("messages") or []:
        lines.append(
            f"  сообщ. [{m.get('kind')}] {m.get('date') or '—'} "
            f"{m.get('channel')}/{m.get('status')}: {(m.get('title') or '')[:100]}"
        )
    for f in dossier.get("fortune") or []:
        lines.append(
            f"  колесо ({f.get('channel')}): {f.get('prize') or '—'} "
            f"@ {f.get('created_at') or '—'}"
        )
    tg_s = dossier.get("tg_survey")
    if tg_s:
        lines.append(
            f"Анкета TG-бота: бюджет={tg_s.get('budget') or '—'}, "
            f"источник={tg_s.get('source') or '—'}, "
            f"событий={len(tg_s.get('events') or [])}"
        )
        for ev in (tg_s.get("events") or [])[:5]:
            if isinstance(ev, dict):
                lines.append(
                    f"  TG-событие: {ev.get('title') or ev.get('kind') or ev}"
                )
            else:
                lines.append(f"  TG-событие: {ev}")
    for o in dossier.get("tg_bot_orders") or []:
        lines.append(
            f"  заказ бота: {o.get('delivery_date') or '—'} "
            f"кому={o.get('recipient') or '—'} повод={o.get('occasion') or '—'} "
            f"бюджет={o.get('budget') or '—'} [{o.get('status') or ''}]"
        )
    max_s = dossier.get("max_survey")
    if max_s:
        lines.append(
            f"Анкета MAX-бота: бюджет={max_s.get('budget') or '—'}, "
            f"источник={max_s.get('source') or '—'}, "
            f"событий={len(max_s.get('events') or [])}"
        )
    chat = dossier.get("max_chat_preview")
    if chat and chat.get("last_text"):
        who = "мы" if chat.get("last_out") else "клиент"
        preview = (chat.get("last_text") or "")[:200]
        lines.append(
            f"Чат MAX ({who}, {chat.get('last_at') or '—'}): {preview}"
        )
    return "\n".join(lines)


async def tool_lookup_customer(args: dict[str, Any]) -> str:
    cid = args.get("customer_id")
    query = str(args.get("query") or "").strip()

    if cid is not None and str(cid).strip() != "":
        try:
            dossier = await build_customer_dossier(int(cid))
        except (TypeError, ValueError):
            return "Некорректный customer_id"
        if not dossier:
            return f"Клиент id={cid} не найден"
        return format_dossier_text(dossier)

    if not query:
        return "Укажите customer_id или query (имя/телефон)"

    rows, total = await list_customers(search=query, page=1, page_size=5)
    if not rows:
        return f"По запросу «{query}» клиенты не найдены"

    parts = [f"Найдено {total} по «{query}». Полные досье (до 3):"]
    for row in rows[:3]:
        dossier = await build_customer_dossier(int(row["id"]))
        if dossier:
            parts.append(format_dossier_text(dossier))
            parts.append("---")
    return _clip("\n".join(parts))


async def tool_list_upcoming_events(args: dict[str, Any]) -> str:
    try:
        days = int(args.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 40))

    try:
        from promo_ai import list_events_for_agent

        return await list_events_for_agent(days=days, limit=limit)
    except Exception:
        logger.debug("promo events helper failed, fallback", exc_info=True)

    events = await list_upcoming_events(days=days, limit=limit)
    if not events:
        return f"Событий на ближайшие {days} дн. нет"

    lines = [f"События на {days} дн. (показано {len(events)}):"]
    for ev in events:
        name = (ev.get("customer_name") or ev.get("name") or "—").strip()
        title = (ev.get("title") or ev.get("kind") or "событие").strip()
        when = (ev.get("next_date") or ev.get("event_date") or "").strip()
        phone = (ev.get("customer_phone") or "").strip()
        cust_id = ev.get("cust_id") or ev.get("customer_id") or ""
        auto = "auto✓" if ev.get("auto_send") else "auto✗"
        lines.append(
            f"• через {ev.get('days_until')}д ({when}): {name} — {title} "
            f"[{auto}] id={cust_id} тел={phone or '—'}"
        )
    return _clip("\n".join(lines))


async def tool_list_segment_customers(args: dict[str, Any]) -> str:
    segment = str(args.get("segment") or "all").strip().lower()
    if segment not in ("all", "regular", "new", "inactive"):
        return "segment: all | regular | new | inactive"
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 20))
    search = str(args.get("search") or "").strip()
    seg_arg = None if segment == "all" else segment
    rows, total = await list_customers(
        search=search, segment=seg_arg, page=1, page_size=limit
    )
    if not rows:
        return f"В сегменте {segment} никого не найдено"
    lines = [f"Сегмент {segment}: всего {total}, примеры:"]
    for c in rows:
        lines.append(
            f"• id={c.get('id')} {c.get('name') or '—'} "
            f"тел={c.get('phone') or '—'} last_order={c.get('last_order_at') or '—'}"
        )
    return _clip("\n".join(lines))


async def tool_get_shop_overview(_args: dict[str, Any]) -> str:
    lines: list[str] = []
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
    except Exception as exc:
        lines.append(f"Сводка недоступна: {exc}")

    try:
        lines.append(
            "Сегменты: all={a}, regular={r}, new={n}, inactive={i}".format(
                a=await count_customers(),
                r=await count_customers("regular"),
                n=await count_customers("new"),
                i=await count_customers("inactive"),
            )
        )
    except Exception:
        pass

    try:
        from bot_metrics import get_bot_metrics

        metrics = await get_bot_metrics()
        if metrics:
            lines.append(f"Метрики ботов: {_safe_json(metrics)[:800]}")
    except Exception:
        logger.debug("AI agent: bot metrics failed", exc_info=True)

    try:
        campaigns = await list_campaigns(limit=5)
        if campaigns:
            lines.append("Последние рассылки:")
            for c in campaigns:
                msg_preview = (c.get("message") or "")[:80]
                lines.append(
                    f"• #{c.get('id')} [{c.get('status')}] "
                    f"сегмент={c.get('segment')} «{msg_preview}»"
                )
    except Exception:
        pass

    lines.append(
        "Панель: Клиенты, События, Главная (рассылки), Акции, Чаты (TG/MAX), "
        "Колесо. Сайт: veresk.flowers"
    )
    return _clip("\n".join(lines))


async def tool_list_recent_campaigns(args: dict[str, Any]) -> str:
    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 15))
    campaigns = await list_campaigns(limit=limit)
    if not campaigns:
        return "Рассылок пока нет"
    lines = ["Последние рассылки:"]
    for c in campaigns:
        msg_preview = (c.get("message") or "")[:120]
        lines.append(
            f"• #{c.get('id')} [{c.get('status')}] сегмент={c.get('segment')} "
            f"каналы={c.get('channels') or '—'} "
            f"sent={c.get('sent_count')}/{c.get('total_count')} «{msg_preview}»"
        )
    return _clip("\n".join(lines))


async def tool_list_fortune_plays(args: dict[str, Any]) -> str:
    try:
        limit = int(args.get("limit") or 15)
    except (TypeError, ValueError):
        limit = 15
    limit = max(1, min(limit, 30))
    data = await list_fortune_plays(limit=limit, offset=0)
    items = data.get("items") or []
    if not items:
        return "Розыгрышей колеса пока нет"
    lines = [
        f"Колесо фортуны: всего={data.get('total', len(items))}, "
        f"tg={data.get('telegram', '—')}, max={data.get('max', '—')}"
    ]
    for p in items[:limit]:
        lines.append(
            f"• {p.get('channel')} user={p.get('user_id')} "
            f"{p.get('full_name') or p.get('username') or '—'} "
            f"приз={p.get('prize_label') or p.get('prize') or '—'} "
            f"@ {p.get('created_at') or '—'}"
        )
    return _clip("\n".join(lines))


async def tool_list_promotions(args: dict[str, Any]) -> str:
    from promo_ai import list_promotions_for_agent

    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    return await list_promotions_for_agent(limit=limit)


async def tool_analyze_promotions(args: dict[str, Any]) -> str:
    from promo_ai import analyze_promotions

    focus = str(args.get("focus") or "").strip()
    try:
        horizon = int(args.get("horizon_days") or 14)
    except (TypeError, ValueError):
        horizon = 14
    try:
        result = await analyze_promotions(focus=focus, horizon_days=horizon)
    except Exception as exc:
        return f"Анализ акций недоступен: {exc}"
    lines = [
        f"Сводка: {result.get('summary') or '—'}",
        "Инсайты:",
    ]
    for tip in result.get("insights") or []:
        lines.append(f"• {tip}")
    lines.append("Предложения акций:")
    for s in result.get("suggestions") or []:
        lines.append(
            f"• {s.get('emoji') or ''} «{s.get('title')}» "
            f"type={s.get('promo_type')} скидка={s.get('discount_text') or s.get('discount_pct') or '—'} "
            f"сегмент={s.get('segment')} · {s.get('rationale') or ''}"
        )
    if result.get("mailing_ideas"):
        lines.append("Идеи рассылок:")
        for m in result["mailing_ideas"]:
            lines.append(
                f"• [{m.get('segment')}] {m.get('title')}: {m.get('hook') or ''}"
            )
    if result.get("risks"):
        lines.append("Риски: " + "; ".join(result["risks"]))
    return _clip("\n".join(lines))


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "lookup_customer": tool_lookup_customer,
    "list_upcoming_events": tool_list_upcoming_events,
    "list_segment_customers": tool_list_segment_customers,
    "get_shop_overview": tool_get_shop_overview,
    "list_recent_campaigns": tool_list_recent_campaigns,
    "list_fortune_plays": tool_list_fortune_plays,
    "list_promotions": tool_list_promotions,
    "analyze_promotions": tool_analyze_promotions,
}


async def execute_tool(name: str, arguments: dict[str, Any] | str | None) -> str:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return f"Неизвестный инструмент: {name}"
    args: dict[str, Any]
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
            args = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            args = {}
    elif isinstance(arguments, dict):
        args = arguments
    else:
        args = {}
    try:
        return await handler(args)
    except Exception as exc:
        logger.warning("AI tool %s failed: %s", name, exc, exc_info=True)
        return f"Ошибка инструмента {name}: {exc}"
