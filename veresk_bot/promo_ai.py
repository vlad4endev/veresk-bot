"""
ИИ-анализатор акций: сводка CRM + предложения скидок/кампаний для админки.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ai_compose import AiComposeError, _chat_completion, is_ai_configured
from mailing_db import (
    count_customers,
    get_stats,
    list_campaigns,
    list_fortune_plays,
    list_promotions,
    list_upcoming_events,
    promotions_overview,
)

logger = logging.getLogger(__name__)

ANALYZE_SYSTEM = """Ты маркетинговый аналитик цветочного салона Veresk (букеты, доставка, Telegram/MAX).
По данным CRM предложи конкретные акции, скидки и идеи авторассылок.

Ответь СТРОГО одним JSON-объектом (без markdown и пояснений вне JSON):
{
  "summary": "2–4 предложения: что видно по базе и что делать сейчас",
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
      "message_template": "Текст с {имя} и {скидка} для рассылки",
      "tags": ["тег"],
      "rationale": "Почему именно сейчас",
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
- 3–6 suggestions, реалистичные скидки 5–25% (подарок/доставка — без завышения).
- Учитывай уже активные акции: не дублируй, предлагай дополнения или паузу.
- Для ДР/годовщин — типы birthday/anniversary и use_in_auto_mail=true.
- Для «давно не заказывали» — reactivation + segment inactive.
- message_template: тёплый тон Veresk, 2–4 коротких абзаца, плейсхолдеры {имя}/{скидка}.
- dates в ISO YYYY-MM-DD или null.
- confidence от 0.4 до 0.95."""


def _clip(obj: Any, limit: int = 12000) -> str:
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
    try:
        confidence = float(raw.get("confidence") or 0.7)
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))
    try:
        priority = int(raw.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    return {
        "title": title[:120],
        "emoji": (str(raw.get("emoji") or "🎁").strip() or "🎁")[:8],
        "promo_type": promo_type,
        "discount_pct": discount_pct,
        "discount_text": str(raw.get("discount_text") or "").strip()[:40],
        "segment": segment,
        "priority": max(-100, min(100, priority)),
        "use_in_auto_mail": bool(raw.get("use_in_auto_mail", True)),
        "use_in_mailing": bool(raw.get("use_in_mailing", True)),
        "starts_at": (str(raw.get("starts_at")).strip()[:10] if raw.get("starts_at") else None),
        "ends_at": (str(raw.get("ends_at")).strip()[:10] if raw.get("ends_at") else None),
        "description": str(raw.get("description") or "").strip()[:2000],
        "message_template": str(raw.get("message_template") or "").strip()[:4000],
        "tags": tags[:12],
        "rationale": str(raw.get("rationale") or "").strip()[:800],
        "confidence": round(confidence, 2),
    }


async def build_promo_analysis_context() -> dict[str, Any]:
    """Собрать факты для анализатора (и для отображения в UI)."""
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

    events: list[dict[str, Any]] = []
    try:
        events = await list_upcoming_events(days=21, limit=25)
    except Exception:
        logger.debug("promo AI: events failed", exc_info=True)

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

    today_events = [e for e in events if int(e.get("days_until") or 0) == 0]
    week_events = [e for e in events if int(e.get("days_until") or 0) <= 7]
    bday_week = sum(
        1
        for e in week_events
        if (e.get("kind") or "").lower() in ("bday", "birthday")
    )
    anniv_week = sum(
        1
        for e in week_events
        if (e.get("kind") or "").lower() in ("anniv", "anniversary")
    )

    return {
        "stats": stats,
        "segments": segments,
        "events": {
            "horizon_days": 21,
            "total": len(events),
            "today": len(today_events),
            "week": len(week_events),
            "birthdays_week": bday_week,
            "anniversaries_week": anniv_week,
            "sample": [
                {
                    "name": (e.get("customer_name") or e.get("name") or "—"),
                    "kind": e.get("kind"),
                    "title": e.get("title"),
                    "days_until": e.get("days_until"),
                    "date": e.get("date_from"),
                }
                for e in events[:12]
            ],
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

    context = await build_promo_analysis_context()
    focus_text = (focus or "").strip()
    user_parts = [
        f"Горизонт планирования: {max(3, min(int(horizon_days or 14), 60))} дней.",
        "Данные салона:",
        _clip(context, 10000),
    ]
    if focus_text:
        user_parts.append(f"Фокус запроса администратора: {focus_text}")
    else:
        user_parts.append(
            "Сфокусируйся на ближайших возможностях: ДР/годовщины, inactive, "
            "дыры в календарных акциях, синергия с колесом фортуны."
        )

    messages = [
        {"role": "system", "content": ANALYZE_SYSTEM},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
    raw_text = await _chat_completion(messages, temperature=0.55, max_tokens=2500)
    parsed = _extract_json(raw_text)

    suggestions: list[dict[str, Any]] = []
    for item in parsed.get("suggestions") or []:
        norm = _normalize_suggestion(item)
        if norm:
            suggestions.append(norm)

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
        "suggestions": suggestions,
        "mailing_ideas": mailing_ideas[:8],
        "risks": risks,
        "context": {
            "segments": context.get("segments"),
            "events": {
                "today": (context.get("events") or {}).get("today"),
                "week": (context.get("events") or {}).get("week"),
                "birthdays_week": (context.get("events") or {}).get(
                    "birthdays_week"
                ),
                "anniversaries_week": (context.get("events") or {}).get(
                    "anniversaries_week"
                ),
            },
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
