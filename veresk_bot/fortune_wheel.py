"""Конфиг колеса фортуны — хранится в runtime_settings.json."""

from __future__ import annotations

import re
from typing import Any

import runtime_settings

SETTINGS_KEY = "fortune_wheel"

DEFAULT_COLORS = (
    "#E879B0",
    "#3D2A55",
    "#F3C4DC",
    "#6B4C8A",
    "#D4569A",
    "#52406A",
    "#F0A8CB",
    "#2A1B3D",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "title": "Весенний розыгрыш",
    "note": "Крутите колесо — получите подарок от Veresk",
    "segments": [
        {"id": "s1", "label": "Скидка 10%", "color": "#E879B0", "weight": 30},
        {"id": "s2", "label": "Скидка 15%", "color": "#3D2A55", "weight": 18},
        {"id": "s3", "label": "Бесплатная доставка", "color": "#F3C4DC", "weight": 22},
        {"id": "s4", "label": "Попробуйте ещё", "color": "#6B4C8A", "weight": 20},
        {"id": "s5", "label": "Мини-букет", "color": "#D4569A", "weight": 10},
    ],
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _norm_color(raw: Any, fallback: str) -> str:
    s = str(raw or "").strip()
    if not _HEX_RE.match(s):
        return fallback
    return s if s.startswith("#") else f"#{s}"


def _norm_id(raw: Any, index: int) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw or "").strip())
    return s[:40] if s else f"s{index + 1}"


def normalize_config(raw: Any) -> dict[str, Any]:
    """Привести произвольный payload к безопасному конфигу."""
    data = raw if isinstance(raw, dict) else {}
    title = str(data.get("title") or "").strip()[:80]
    note = str(data.get("note") or "").strip()[:240]
    segs_in = data.get("segments")
    if not isinstance(segs_in, list):
        segs_in = []

    segments: list[dict[str, Any]] = []
    for i, item in enumerate(segs_in[:24]):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:48]
        if not label:
            continue
        try:
            weight = float(item.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0
        if weight < 0:
            weight = 0
        color = _norm_color(item.get("color"), DEFAULT_COLORS[i % len(DEFAULT_COLORS)])
        segments.append(
            {
                "id": _norm_id(item.get("id"), i),
                "label": label,
                "color": color,
                "weight": weight,
                "order": i,
            }
        )

    if not title:
        title = DEFAULT_CONFIG["title"]
    if not note:
        note = DEFAULT_CONFIG["note"]
    if len(segments) < 2:
        segments = [dict(s) for s in DEFAULT_CONFIG["segments"]]

    total = sum(float(s["weight"]) for s in segments)
    for s in segments:
        w = float(s["weight"])
        s["chance_pct"] = round((w / total) * 1000) / 10 if total > 0 else 0

    return {"title": title, "note": note, "segments": segments}


def validate_config(raw: Any) -> str:
    """Пустая строка = ок, иначе текст ошибки для UI."""
    data = raw if isinstance(raw, dict) else {}
    title = str(data.get("title") or "").strip()
    if not title:
        return "Укажите название колеса"
    segs = data.get("segments")
    if not isinstance(segs, list) or len(segs) < 2:
        return "Нужно минимум 2 сектора"
    if len(segs) > 24:
        return "Не больше 24 секторов"
    total = 0.0
    for item in segs:
        if not isinstance(item, dict):
            return "Некорректный сектор"
        if not str(item.get("label") or "").strip():
            return "У каждого сектора должно быть название"
        try:
            w = float(item.get("weight") or 0)
        except (TypeError, ValueError):
            return "Вес сектора должен быть числом"
        if w < 0:
            return "Вес сектора не может быть отрицательным"
        total += w
        color = str(item.get("color") or "").strip()
        if color and not _HEX_RE.match(color):
            return "Некорректный цвет сектора"
    if total <= 0:
        return "Сумма весов должна быть больше 0"
    return ""


def get_config() -> dict[str, Any]:
    stored = runtime_settings.get(SETTINGS_KEY)
    if not stored:
        return normalize_config(DEFAULT_CONFIG)
    return normalize_config(stored)


def save_config(raw: Any) -> dict[str, Any]:
    err = validate_config(raw)
    if err:
        raise ValueError(err)
    cfg = normalize_config(raw)
    # chance_pct — производное, в файл можно не класть, но оставляем для удобства
    runtime_settings.set_many({SETTINGS_KEY: cfg})
    return cfg


def extract_discount_pct(label: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", str(label or ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def pick_winner(segments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Взвешенный выбор сектора. Возвращает {index, segment}."""
    import random

    segs = list(segments or get_config()["segments"])
    if not segs:
        raise ValueError("Нет секторов")
    weights = [max(0.0, float(s.get("weight") or 0)) for s in segs]
    total = sum(weights)
    if total <= 0:
        idx = random.randrange(len(segs))
    else:
        r = random.uniform(0, total)
        acc = 0.0
        idx = len(segs) - 1
        for i, w in enumerate(weights):
            acc += w
            if r <= acc:
                idx = i
                break
    seg = dict(segs[idx])
    return {
        "index": idx,
        "segment": seg,
        "discount_pct": extract_discount_pct(str(seg.get("label") or "")),
    }
