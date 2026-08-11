"""
ИИ-анализатор акций: сводка CRM + события клиентов + предложения для автопоздравлений.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ai_compose import AiComposeError, _chat_completion, is_ai_configured
from mailing_db import (
    count_customers,
    events_overview,
    get_stats,
    list_campaigns,
    list_fortune_plays,
    list_live_promotions,
    list_promotions,
    promotions_overview,
)

logger = logging.getLogger(__name__)

ANALYZE_SYSTEM = """Ты маркетинговый аналитик цветочного салона Veresk (букеты, доставка, Telegram/MAX).
Тебе доступны события клиентов (ДР, годовщины), сегменты CRM, текущие акции и рассылки.

Главная задача: предложить акции, которые попадут в АВТОПОЗДРАВЛЕНИЯ
(ежедневная авторассылка по событиям с auto_send) и в плейсхолдер {скидка}.

Ответь СТРОГО одним JSON-объектом (без markdown и пояснений вне JSON):
{
  "summary": "2–4 предложения: что видно по событиям/базе и что делать сейчас",
  "insights": ["короткий вывод 1", "вывод 2", "вывод 3"],
  "suggestions": [
    {
      "title": "Название акции",
      "emoji": "🌷",
      "promo_type": "discount|gift|seasonal|welcome|reactivation|birthday|anniversary|other",
      "discount_pct": 15,
      "discount_text": "15%",
      "segment": "all|regular|new|inactive",
      "priority": 0,
      "use_in_auto_mail": true,
      "use_in_mailing": true,
      "starts_at": null,
      "ends_at": null,
      "description": "Зачем акция и кому",
      "message_template": "Текст с {имя} и {скидка} для рассылки/автопоздравления",
      "tags": ["тег"],
      "rationale": "Почему именно сейчас (сошлись на событиях)",
      "confidence": 0.0
    }
  ],
  "mailing_ideas": [
    {
      "title": "Идея рассылки",
      "segment": "inactive",
      "hook": "Короткий посыл",
      "why": "Почему сработает"
    }
  ],
  "risks": ["на что обратить внимание"]
}

Правила:
- Язык: русский.
- 3–6 suggestions, реалистичные скидки 5–25%.
- ОБЯЗАТЕЛЬНО минимум 1 акция promo_type=birthday и 1 promo_type=anniversary,
  если в данных есть соответствующие события (или события в базе вообще).
  Для них: use_in_auto_mail=true, tags включают birthday/др или anniversary/годовщина,
  priority выше обычных (5–20), message_template — тёплое поздравление с {имя} и {скидка}.
- Если нет живой birthday/anniversary акции в promotions — предложи создать их в первую очередь.
- Смотри sample событий: имена, days_until, auto_send on/off — опирайся на них в rationale.
- Для «давно не заказывали» — reactivation + segment inactive.
- message_template: тёплый тон Veresk, 2–4 коротких абзаца, плейсхолдеры {имя}/{скидка}.
- dates в ISO YYYY-MM-DD или null.
- confidence от 0.4 до 0.95."""


def _clip(obj: Any, limit: int = 14000) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        text = str(obj)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n… (обрезано)"


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise AiComposeError("ai_empty", "ИИ вернул пустой ответ")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    raise AiComposeError("ai_parse_error", "Не удалось разобрать ответ ИИ")


def _default_auto_template(promo_type: str, title: str = "") -> str:
    if promo_type == "birthday":
        return (
            "С днём рождения, {имя}! 🎂💐\n\n"
            "От всей души поздравляем и дарим скидку {скидка} на любой букет.\n\n"
            "Ваш Veresk 🌷"
        )
    if promo_type == "anniversary":
        return (
            "{имя}, поздравляем с годовщиной! 💍\n\n"
            "Отметьте этот день красивым букетом — дарим скидку {скидка}.\n\n"
            "Ваш Veresk 🌷"
        )
    label = (title or "специальное предложение").strip()
    return (
        f"Здравствуйте, {{имя}}!\n\n"
        f"{label}: для вас скидка {{скидка}} на любой букет.\n\n"
        "Ваш Veresk 🌷"
    )


def _normalize_suggestion(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    promo_type = str(raw.get("promo_type") or "discount").strip().lower() or "discount"
    allowed_types = {
        "discount",
        "gift",
        "seasonal",
        "welcome",
        "reactivation",
        "birthday",
        "anniversary",
        "other",
    }
    if promo_type not in allowed_types:
        promo_type = "discount"
    segment = str(raw.get("segment") or "all").strip() or "all"
    if segment not in ("all", "regular", "new", "inactive"):
        segment = "all"
    pct = raw.get("discount_pct")
    try:
        discount_pct = float(pct) if pct is not None and pct != "" else None
    except (TypeError, ValueError):
        discount_pct = None
    if discount_pct is not None:
        discount_pct = max(0.0, min(100.0, discount_pct))
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,;]+", tags) if t.strip()]
    elif isinstance(tags, list):
        tags = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tags = []

    use_in_auto_mail = bool(raw.get("use_in_auto_mail", True))
    use_in_mailing = bool(raw.get("use_in_mailing", True))
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0

    # ДР/годовщины всегда для автопоздравлений
    if promo_type in ("birthday", "anniversary"):
        use_in_auto_mail = True
        priority = max(priority, 10 if promo_type == "birthday" else 8)
        need_tag = "birthday" if promo_type == "birthday" else "anniversary"
        tags_l = {t.lower() for t in tags}
        if need_tag not in tags_l and need_tag[:3] not in tags_l:
            tags = [need_tag, *tags]

    message_template = str(raw.get("message_template") or "").strip()
    if not message_template:
        message_template = _default_auto_template(promo_type, title)

    discount_text = str(raw.get("discount_text") or "").strip()[:40]
    if not discount_text and discount_pct is not None:
        if abs(discount_pct - round(discount_pct)) < 1e-9:
            discount_text = f"{int(round(discount_pct))}%"
        else:
            discount_text = f"{discount_pct:g}%"

    try:
        confidence = float(raw.get("confidence") or 0.7)
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))

    return {
        "title": title[:120],
        "emoji": (str(raw.get("emoji") or "🎁").strip() or "🎁")[:8],
        "promo_type": promo_type,
        "discount_pct": discount_pct,
        "discount_text": discount_text,
        "segment": segment,
        "priority": max(-100, min(100, priority)),
        "use_in_auto_mail": use_in_auto_mail,
        "use_in_mailing": use_in_mailing,
        "starts_at": (str(raw.get("starts_at")).strip()[:10] if raw.get("starts_at") else None),
        "ends_at": (str(raw.get("ends_at")).strip()[:10] if raw.get("ends_at") else None),
        "description": str(raw.get("description") or "").strip()[:2000],
        "message_template": message_template[:4000],
        "tags": tags[:12],
        "rationale": str(raw.get("rationale") or "").strip()[:800],
        "confidence": round(confidence, 2),
    }


def _fallback_auto_suggestions(events_ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Если ИИ не предложил ДР/годовщину — добавим шаблоны по событиям."""
    by_kind = (events_ctx or {}).get("by_kind") or {}
    db = (events_ctx or {}).get("db") or {}
    out: list[dict[str, Any]] = []
    if int(by_kind.get("birthday") or 0) or int(db.get("birthdays_total") or 0):
        week_n = int((events_ctx or {}).get("week") or 0)
        out.append(
            _normalize_suggestion(
                {
                    "title": "Автопоздравление с Днём рождения",
                    "emoji": "🎂",
                    "promo_type": "birthday",
                    "discount_pct": 15,
                    "discount_text": "15%",
                    "segment": "all",
                    "priority": 15,
                    "use_in_auto_mail": True,
                    "use_in_mailing": True,
                    "description": "Базовая акция для ежедневных автопоздравлений с ДР",
                    "message_template": _default_auto_template("birthday"),
                    "tags": ["birthday", "др", "auto"],
                    "rationale": (
                        f"В горизонте есть ДР клиентов"
                        + (f" (на неделе событий: {week_n})" if week_n else "")
                        + ". Нужна акция birthday для авторассылки."
                    ),
                    "confidence": 0.85,
                }
            )
        )
    if int(by_kind.get("anniversary") or 0) or int(db.get("anniversaries_total") or 0):
        out.append(
            _normalize_suggestion(
                {
                    "title": "Автопоздравление с годовщиной",
                    "emoji": "💍",
                    "promo_type": "anniversary",
                    "discount_pct": 15,
                    "discount_text": "15%",
                    "segment": "all",
                    "priority": 12,
                    "use_in_auto_mail": True,
                    "use_in_mailing": True,
                    "description": "Базовая акция для автопоздравлений с годовщиной",
                    "message_template": _default_auto_template("anniversary"),
                    "tags": ["anniversary", "годовщина", "auto"],
                    "rationale": "В базе есть годовщины — нужна акция anniversary для авторассылки.",
                    "confidence": 0.82,
                }
            )
        )
    return [x for x in out if x]


async def build_promo_analysis_context(*, horizon_days: int = 21) -> dict[str, Any]:
    """Собрать факты для анализатора (и для отображения в UI)."""
    horizon = max(3, min(int(horizon_days or 21), 60))

    stats = {}
    try:
        stats = await get_stats()
    except Exception:
        logger.debug("promo AI: stats failed", exc_info=True)

    segments = {}
    try:
        segments = {
            "all": await count_customers(),
            "regular": await count_customers("regular"),
            "new": await count_customers("new"),
            "inactive": await count_customers("inactive"),
        }
    except Exception:
        logger.debug("promo AI: segments failed", exc_info=True)

    events_ctx: dict[str, Any] = {}
    try:
        events_ctx = await events_overview(days=horizon)
    except Exception:
        logger.debug("promo AI: events overview failed", exc_info=True)
        events_ctx = {"horizon_days": horizon, "sample": [], "by_kind": {}}

    campaigns: list[dict[str, Any]] = []
    try:
        campaigns = await list_campaigns(limit=8)
    except Exception:
        logger.debug("promo AI: campaigns failed", exc_info=True)

    fortune = {}
    try:
        fortune = await list_fortune_plays(limit=15, offset=0)
    except Exception:
        logger.debug("promo AI: fortune failed", exc_info=True)

    overview = {}
    try:
        overview = await promotions_overview()
    except Exception:
        logger.debug("promo AI: promotions overview failed", exc_info=True)

    live_auto: list[dict[str, Any]] = []
    try:
        live = await list_live_promotions(for_auto_mail=True)
        live_auto = [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "promo_type": p.get("promo_type"),
                "discount": p.get("discount_display") or p.get("discount_text"),
                "priority": p.get("priority"),
            }
            for p in live[:10]
        ]
    except Exception:
        logger.debug("promo AI: live auto promos failed", exc_info=True)

    has_bday_promo = any(p.get("promo_type") == "birthday" for p in live_auto)
    has_anniv_promo = any(p.get("promo_type") == "anniversary" for p in live_auto)

    return {
        "stats": stats,
        "segments": segments,
        "events": events_ctx,
        "auto_greeting_gap": {
            "has_live_birthday_promo": has_bday_promo,
            "has_live_anniversary_promo": has_anniv_promo,
            "live_auto_promos": live_auto,
            "instruction": (
                "Если has_live_birthday_promo=false — предложи birthday-акцию. "
                "Если has_live_anniversary_promo=false — предложи anniversary-акцию. "
                "Обе с use_in_auto_mail=true."
            ),
        },
        "campaigns": [
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "status": c.get("status"),
                "segment": c.get("segment"),
                "sent": c.get("sent_count"),
                "total": c.get("total_count"),
                "preview": (c.get("message") or "")[:100],
            }
            for c in campaigns
        ],
        "fortune": {
            "total": fortune.get("total"),
            "telegram": fortune.get("telegram"),
            "max": fortune.get("max"),
            "recent_prizes": [
                {
                    "prize": p.get("prize_label"),
                    "discount_pct": p.get("discount_pct"),
                    "channel": p.get("channel"),
                }
                for p in (fortune.get("items") or [])[:8]
            ],
        },
        "promotions": overview,
    }


async def analyze_promotions(
    *,
    focus: str = "",
    horizon_days: int = 14,
) -> dict[str, Any]:
    """Запуск ИИ-анализа. Возвращает context + suggestions."""
    if not is_ai_configured():
        raise AiComposeError(
            "ai_not_configured",
            "Подключите ИИ в Настройках → Сервисы",
        )

    horizon = max(3, min(int(horizon_days or 14), 60))
    context = await build_promo_analysis_context(horizon_days=horizon)
    focus_text = (focus or "").strip()
    focus_l = focus_text.lower()

    user_parts = [
        f"Горизонт планирования: {horizon} дней.",
        "Данные салона (включая события клиентов и автопоздравления):",
        _clip(context, 12000),
    ]
    if focus_text:
        user_parts.append(f"Фокус запроса администратора: {focus_text}")
    else:
        user_parts.append(
            "Сфокусируйся на событиях клиентов (ДР/годовщины) и акциях "
            "для автопоздравлений, затем inactive и календарные дыры."
        )

    if any(
        k in focus_l
        for k in (
            "авто поздр",
            "авто поздрав",
            "автопоздрав",
            "др",
            "день рождения",
            "годовщин",
            "событи",
            "birthday",
            "anniversary",
        )
    ) or not focus_text:
        user_parts.append(
            "Приоритет: создать/обновить акции birthday и anniversary "
            "с use_in_auto_mail=true и готовым message_template для авторассылки."
        )

    messages = [
        {"role": "system", "content": ANALYZE_SYSTEM},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    raw_text = await _chat_completion(messages, temperature=0.5, max_tokens=2800)
    parsed = _extract_json(raw_text)

    suggestions: list[dict[str, Any]] = []
    for item in parsed.get("suggestions") or []:
        norm = _normalize_suggestion(item)
        if norm:
            suggestions.append(norm)

    # Гарантируем предложения ДР/годовщины по событиям, если ИИ их пропустил
    events_ctx = context.get("events") or {}
    have_types = {s.get("promo_type") for s in suggestions}
    for fb in _fallback_auto_suggestions(events_ctx):
        if fb and fb.get("promo_type") not in have_types:
            suggestions.insert(0, fb)
            have_types.add(fb.get("promo_type"))

    mailing_ideas = []
    for item in parsed.get("mailing_ideas") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        mailing_ideas.append(
            {
                "title": title[:120],
                "segment": str(item.get("segment") or "all").strip()[:40],
                "hook": str(item.get("hook") or "").strip()[:400],
                "why": str(item.get("why") or "").strip()[:400],
            }
        )

    insights = [
        str(x).strip()
        for x in (parsed.get("insights") or [])
        if str(x).strip()
    ][:8]
    risks = [
        str(x).strip() for x in (parsed.get("risks") or []) if str(x).strip()
    ][:6]

    return {
        "ok": True,
        "summary": str(parsed.get("summary") or "").strip()[:1200],
        "insights": insights,
        "suggestions": suggestions[:8],
        "mailing_ideas": mailing_ideas[:8],
        "risks": risks,
        "context": {
            "segments": context.get("segments"),
            "events": {
                "horizon_days": events_ctx.get("horizon_days"),
                "upcoming_total": events_ctx.get("upcoming_total"),
                "today": events_ctx.get("today"),
                "week": events_ctx.get("week"),
                "by_kind": events_ctx.get("by_kind"),
                "auto_send_on": events_ctx.get("auto_send_on"),
                "auto_send_off": events_ctx.get("auto_send_off"),
                "with_messenger": events_ctx.get("with_messenger"),
                "db": events_ctx.get("db"),
                "sample": (events_ctx.get("sample") or [])[:15],
            },
            "auto_greeting_gap": context.get("auto_greeting_gap"),
            "promotions_counts": (context.get("promotions") or {}).get("counts"),
            "stats": context.get("stats"),
        },
    }


async def list_promotions_for_agent(limit: int = 20) -> str:
    data = await list_promotions(limit=max(1, min(limit, 40)))
    items = data.get("items") or []
    if not items:
        return "Акций пока нет"
    lines = [
        f"Акции: всего={data.get('total')}, "
        f"active={(data.get('counts') or {}).get('active', 0)}, "
        f"draft={(data.get('counts') or {}).get('draft', 0)}"
    ]
    for p in items:
        live = "live" if p.get("is_live") else p.get("status")
        disc = p.get("discount_display") or "—"
        lines.append(
            f"• #{p.get('id')} {p.get('emoji') or ''} «{p.get('title')}» "
            f"[{live}] type={p.get('promo_type')} скидка={disc} "
            f"сегмент={p.get('segment')} auto={int(bool(p.get('use_in_auto_mail')))} "
            f"mail={int(bool(p.get('use_in_mailing')))}"
        )
    return "\n".join(lines)


async def list_events_for_agent(*, days: int = 21, limit: int = 30) -> str:
    """Текстовая сводка событий для ИИ-чата / инструментов."""
    overview = await events_overview(days=days)
    sample = overview.get("sample") or []
    if not sample and not (overview.get("db") or {}).get("total_events"):
        return f"Событий клиентов на {days} дн. нет"
    db = overview.get("db") or {}
    by_kind = overview.get("by_kind") or {}
    lines = [
        f"События на {overview.get('horizon_days')} дн.: "
        f"ближайших={overview.get('upcoming_total')}, сегодня={overview.get('today')}, "
        f"неделя={overview.get('week')}",
        f"По типам (горизонт): ДР={by_kind.get('birthday', 0)}, "
        f"годовщины={by_kind.get('anniversary', 0)}, другие={by_kind.get('other', 0)}",
        f"Автоотправка в горизонте: on={overview.get('auto_send_on')}, "
        f"off={overview.get('auto_send_off')}, с мессенджером={overview.get('with_messenger')}",
        f"В базе всего: events={db.get('total_events')}, "
        f"auto_send={db.get('auto_send_enabled')}, "
        f"ДР={db.get('birthdays_total')}, годовщины={db.get('anniversaries_total')}",
        "Ближайшие:",
    ]
    for ev in sample[: max(1, min(limit, 40))]:
        auto = "auto✓" if ev.get("auto_send") else "auto✗"
        ch = []
        if ev.get("has_tg"):
            ch.append("tg")
        if ev.get("has_max"):
            ch.append("max")
        lines.append(
            f"• через {ev.get('days_until')}д ({ev.get('date')}): "
            f"{ev.get('name')} — {ev.get('kind') or ev.get('title') or 'событие'} "
            f"[{auto}] сегмент={ev.get('segment')} каналы={','.join(ch) or '—'}"
        )
    return _clip("\n".join(lines))
