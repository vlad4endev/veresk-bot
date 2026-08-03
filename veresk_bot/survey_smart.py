"""
Умный разбор ответов анкеты (Telegram + MAX).

Без LLM: относительные даты, «15 июня», свободный текст повода/отношения,
маппинг бюджета и мягкая подсказка по поводу.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any

OCCASION_DAY = "День рождения 🎂"
OCCASION_ANNIVERSARY = "Годовщина 💍"

OCCASION_ALIASES: dict[str, str] = {
    "день рождения": OCCASION_DAY,
    "др": OCCASION_DAY,
    "днюха": OCCASION_DAY,
    "birthday": OCCASION_DAY,
    "годовщина": OCCASION_ANNIVERSARY,
    "годовщину": OCCASION_ANNIVERSARY,
    "юбилей": OCCASION_ANNIVERSARY,
    "свадьба": OCCASION_ANNIVERSARY,
    "свадьбу": OCCASION_ANNIVERSARY,
    "свидание": "Свидание 💋",
    "просто так": "Просто так 🌷",
    "без повода": "Просто так 🌷",
    "выздоровление": "Выздоровление 🤍",
    "8 марта": "8 Марта 🌷",
    "14 февраля": "14 Февраля 💝",
    "валентин": "14 Февраля 💝",
}

RELATION_ALIASES: dict[str, str] = {
    "девушка": "Девушка",
    "подруга": "Девушка",
    "жена": "Супруга",
    "супруга": "Супруга",
    "супруге": "Супруга",
    "мама": "Мама",
    "маме": "Мама",
    "матушка": "Мама",
    "мам": "Мама",
    "мамин": "Мама",
    "мамины": "Мама",
    "дочь": "Дочь",
    "дочери": "Дочь",
    "коллега": "Коллега",
    "коллеге": "Коллега",
    "папа": "Папа",
    "отцу": "Папа",
    "бабушка": "Бабушка",
    "сестра": "Сестра",
    "брат": "Брат",
}

BUDGET_PRESETS = (
    "до 5 000 ₽",
    "до 10 000 ₽",
    "до 15 000 ₽",
    "более 15 000 ₽",
)

_MONTH_NAMES: dict[str, int] = {
    "января": 1,
    "январь": 1,
    "январе": 1,
    "янв": 1,
    "февраля": 2,
    "февраль": 2,
    "феврале": 2,
    "фев": 2,
    "марта": 3,
    "март": 3,
    "марте": 3,
    "мар": 3,
    "апреля": 4,
    "апрель": 4,
    "апреле": 4,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "мае": 5,
    "июня": 6,
    "июнь": 6,
    "июне": 6,
    "июн": 6,
    "июля": 7,
    "июль": 7,
    "июле": 7,
    "июл": 7,
    "августа": 8,
    "август": 8,
    "августе": 8,
    "авг": 8,
    "сентября": 9,
    "сентябрь": 9,
    "сентябре": 9,
    "сен": 9,
    "сент": 9,
    "октября": 10,
    "октябрь": 10,
    "октябре": 10,
    "окт": 10,
    "ноября": 11,
    "ноябрь": 11,
    "ноябре": 11,
    "ноя": 11,
    "нояб": 11,
    "декабря": 12,
    "декабрь": 12,
    "декабре": 12,
    "дек": 12,
}

_RELATIVE_DAYS: dict[str, int] = {
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}


def format_date(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def _clamp_day(year: int, month: int, day: int) -> date:
    last = monthrange(year, month)[1]
    return date(year, month, min(max(1, day), last))


def _next_occurrence(month: int, day: int, *, today: date | None = None) -> date:
    today = today or date.today()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        candidate = _clamp_day(today.year, month, day)
    if candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            candidate = _clamp_day(today.year + 1, month, day)
    return candidate


def match_occasion(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    for key, value in sorted(OCCASION_ALIASES.items(), key=lambda x: -len(x[0])):
        if key in low:
            return value
    # Уже выбран пресет с эмодзи
    for value in OCCASION_ALIASES.values():
        if value.lower() == low or value.split()[0].lower() in low:
            if value in (OCCASION_DAY, OCCASION_ANNIVERSARY) or " " in value:
                return value
    return None


def match_relation(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    # Целое слово / начало
    for key, value in sorted(RELATION_ALIASES.items(), key=lambda x: -len(x[0])):
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", low):
            return value
        if key in low and len(key) >= 4:
            return value
    # Точное совпадение пресета
    for value in set(RELATION_ALIASES.values()):
        if value.lower() == low:
            return value
    return None


def match_budget(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw in BUDGET_PRESETS:
        return raw
    low = raw.lower().replace("ё", "е")
    amount: int | None = None

    # «5к», «10 тыс» — раньше сырых цифр, иначе «8к» станет 8
    m = re.search(r"(\d+)\s*[kк]\b", low)
    if m:
        amount = int(m.group(1)) * 1000
    if amount is None:
        m = re.search(r"(\d+)\s*тыс", low)
        if m:
            amount = int(m.group(1)) * 1000
    if amount is None:
        digits = re.sub(r"[^\d]", "", raw)
        if digits:
            try:
                amount = int(digits)
            except ValueError:
                amount = None

    if amount is not None:
        if amount <= 5000:
            return BUDGET_PRESETS[0]
        if amount <= 10000:
            return BUDGET_PRESETS[1]
        if amount <= 15000:
            return BUDGET_PRESETS[2]
        return BUDGET_PRESETS[3]

    if any(x in low for x in ("до 5", "до5", "маленьк", "эконом")):
        return BUDGET_PRESETS[0]
    if any(x in low for x in ("до 10", "до10", "средн")):
        return BUDGET_PRESETS[1]
    if any(x in low for x in ("до 15", "до15")):
        return BUDGET_PRESETS[2]
    if any(x in low for x in ("более", "больше 15", "от 15", "премиум", "дорог")):
        return BUDGET_PRESETS[3]
    return None


def budget_hint_for_occasions(occasions: list[str]) -> str | None:
    """Мягкая подсказка бюджета по поводам сохранённых событий."""
    joined = " ".join(occasions).lower()
    if not joined:
        return None
    if "годовщин" in joined or "юбиле" in joined or "свадьб" in joined:
        return (
            "Для годовщины или свадьбы часто берут букеты "
            f"*{BUDGET_PRESETS[2]}* или *{BUDGET_PRESETS[3]}*."
        )
    if "день рождения" in joined or "др" in joined:
        return (
            "Для дня рождения обычно комфортный диапазон "
            f"*{BUDGET_PRESETS[1]}* — *{BUDGET_PRESETS[2]}*."
        )
    if "8 марта" in joined or "валентин" in joined or "14 февраля" in joined:
        return (
            "К праздничной дате чаще выбирают "
            f"*{BUDGET_PRESETS[1]}* или *{BUDGET_PRESETS[2]}*."
        )
    if "просто так" in joined or "свидан" in joined:
        return f"Для тёплого жеста часто хватает *{BUDGET_PRESETS[0]}* — *{BUDGET_PRESETS[1]}*."
    return None


def _extract_hints(text: str) -> tuple[str | None, str | None]:
    return match_occasion(text), match_relation(text)


def _parse_absolute_date(raw: str, *, today: date) -> date | None:
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    # ДД.ММ без года → ближайшая будущая
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})$", raw)
    if m:
        day_n, month_n = int(m.group(1)), int(m.group(2))
        if 1 <= month_n <= 12 and 1 <= day_n <= 31:
            return _next_occurrence(month_n, day_n, today=today)
    return None


def _parse_relative(raw: str, *, today: date) -> date | None:
    low = raw.lower().strip()
    if low in _RELATIVE_DAYS:
        return today + timedelta(days=_RELATIVE_DAYS[low])

    m = re.match(r"^через\s+(\d+)\s*д", low)
    if m:
        return today + timedelta(days=int(m.group(1)))
    m = re.match(r"^через\s+(\d+)\s*нед", low)
    if m:
        return today + timedelta(weeks=int(m.group(1)))
    if low in ("через неделю", "через нед"):
        return today + timedelta(weeks=1)
    if low in ("через две недели", "через 2 недели"):
        return today + timedelta(weeks=2)
    if "через пару дн" in low or low in ("через 2-3 дня", "через 2–3 дня"):
        return today + timedelta(days=3)
    return None


def _parse_month_day_words(raw: str, *, today: date) -> tuple[date | None, int | None]:
    """
    «15 июня», «июня 15», «в июне».
    Возвращает (дата | None, месяц_если_нет_дня | None).
    """
    low = raw.lower().replace("ё", "е")
    # 15 июня [2026]
    m = re.search(
        r"(\d{1,2})\s+([а-я]+?)(?:\s+(\d{4}))?\b",
        low,
    )
    if m and m.group(2) in _MONTH_NAMES:
        day_n = int(m.group(1))
        month_n = _MONTH_NAMES[m.group(2)]
        year = int(m.group(3)) if m.group(3) else None
        if year:
            return _clamp_day(year, month_n, day_n), None
        return _next_occurrence(month_n, day_n, today=today), None

    # июня 15
    m = re.search(r"([а-я]+)\s+(\d{1,2})(?:\s+(\d{4}))?\b", low)
    if m and m.group(1) in _MONTH_NAMES:
        month_n = _MONTH_NAMES[m.group(1)]
        day_n = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if year:
            return _clamp_day(year, month_n, day_n), None
        return _next_occurrence(month_n, day_n, today=today), None

    # в июне / июнь
    for name, month_n in _MONTH_NAMES.items():
        if len(name) < 3:
            continue
        if re.search(rf"(?:^|\s)(?:в\s+)?{re.escape(name)}\b", low):
            # Есть ли день рядом?
            dm = re.search(r"\b(\d{1,2})\b", low)
            if dm:
                day_n = int(dm.group(1))
                if 1 <= day_n <= 31:
                    return _next_occurrence(month_n, day_n, today=today), None
            return None, month_n
    return None, None


def parse_date_input(
    text: str,
    *,
    pending_month: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """
    Разбор ввода даты (и опционально повода/отношения в той же фразе).

    Возвращает словарь:
      ok: bool
      date: str | None  (ДД.ММ.ГГГГ)
      occasion: str | None
      relation: str | None
      pending_month: int | None  — нужен день месяца
      message: str | None — подсказка пользователю
    """
    today = today or date.today()
    raw = (text or "").strip()
    empty = {
        "ok": False,
        "date": None,
        "occasion": None,
        "relation": None,
        "pending_month": None,
        "message": None,
    }
    if not raw:
        return empty

    occasion, relation = _extract_hints(raw)

    # Добор дня к ранее понятому месяцу
    if pending_month and 1 <= pending_month <= 12:
        only_day = re.match(r"^(\d{1,2})\.?$", raw)
        if only_day:
            day_n = int(only_day.group(1))
            if 1 <= day_n <= 31:
                resolved = _next_occurrence(pending_month, day_n, today=today)
                return {
                    "ok": True,
                    "date": format_date(resolved),
                    "occasion": occasion,
                    "relation": relation,
                    "pending_month": None,
                    "message": None,
                }
        parsed = _parse_absolute_date(raw, today=today)
        if parsed:
            return {
                "ok": True,
                "date": format_date(parsed),
                "occasion": occasion,
                "relation": relation,
                "pending_month": None,
                "message": None,
            }

    # Чистая относительная / абсолютная дата
    low = raw.lower()
    for alias, offset in _RELATIVE_DAYS.items():
        if low == alias or low.startswith(alias + " "):
            resolved = today + timedelta(days=offset)
            return {
                "ok": True,
                "date": format_date(resolved),
                "occasion": occasion,
                "relation": relation,
                "pending_month": None,
                "message": None,
            }

    relative = _parse_relative(raw, today=today)
    if relative:
        return {
            "ok": True,
            "date": format_date(relative),
            "occasion": occasion,
            "relation": relation,
            "pending_month": None,
            "message": None,
        }

    absolute = _parse_absolute_date(raw, today=today)
    if absolute:
        return {
            "ok": True,
            "date": format_date(absolute),
            "occasion": occasion,
            "relation": relation,
            "pending_month": None,
            "message": None,
        }

    # Убрать подсказки повода, чтобы не мешали парсеру месяца
    cleaned = raw
    for key in list(OCCASION_ALIASES) + list(RELATION_ALIASES):
        cleaned = re.sub(rf"(?i)\b{re.escape(key)}\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")

    word_date, month_only = _parse_month_day_words(cleaned or raw, today=today)
    if word_date:
        return {
            "ok": True,
            "date": format_date(word_date),
            "occasion": occasion,
            "relation": relation,
            "pending_month": None,
            "message": None,
        }
    if month_only:
        month_label = next(
            (n for n, m in _MONTH_NAMES.items() if m == month_only and n.endswith("я")),
            str(month_only),
        )
        bits = []
        if occasion:
            bits.append(f"повод «{occasion}»")
        if relation:
            bits.append(f"получатель «{relation}»")
        remembered = ("Запомнил: " + ", ".join(bits) + ".\n") if bits else ""
        return {
            "ok": False,
            "date": None,
            "occasion": occasion,
            "relation": relation,
            "pending_month": month_only,
            "message": (
                f"{remembered}Понял: *{month_label}*. Укажите число месяца — например *15* "
                f"или полную дату *15.{month_only:02d}.{today.year}*."
            ),
        }

    # Если распознали только повод/отношение без даты
    if occasion or relation:
        bits = []
        if occasion:
            bits.append(f"повод «{occasion}»")
        if relation:
            bits.append(f"получатель «{relation}»")
        return {
            "ok": False,
            "date": None,
            "occasion": occasion,
            "relation": relation,
            "pending_month": None,
            "message": (
                "Запомнил: " + ", ".join(bits) + ".\n"
                "Теперь укажите дату — *ДД.ММ.ГГГГ*, *завтра* или *15 июня*."
            ),
        }

    return {
        **empty,
        "message": (
            "Не распознал дату. Попробуйте *15.06.2026*, *завтра*, "
            "*через неделю* или *15 июня*."
        ),
    }


def resolve_important_date(text: str) -> str | None:
    """Совместимость со старым API: только дата или None."""
    result = parse_date_input(text)
    return result["date"] if result.get("ok") else None
