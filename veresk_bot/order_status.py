"""
Единая модель статусов заказа для бота, polling и Mini App.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusStep:
    key: str
    title: str
    subtitle: str
    icon: str


# Порядок шагов на таймлайне (ключи Posiflora + внутренние)
STATUS_PIPELINE: tuple[StatusStep, ...] = (
    StatusStep("new", "Заявка принята", "Мы получили вашу анкету", "📝"),
    StatusStep("confirmed", "Заказ подтверждён", "Флорист готовит композицию", "🌸"),
    StatusStep("in_progress", "Букет собирается", "Подбираем идеальные цветы", "💐"),
    StatusStep("delivering", "В пути", "Курьер уже едет к получателю", "🚗"),
    StatusStep("delivered", "Доставлено", "Пусть этот день будет особенным", "🎉"),
)

PIPELINE_KEYS: tuple[str, ...] = tuple(s.key for s in STATUS_PIPELINE)

STATUS_ALIASES: dict[str, str] = {
    "unknown": "new",
    "pending": "new",
    "created": "new",
    "accepted": "confirmed",
    "processing": "in_progress",
    "ready": "in_progress",
    "on_delivery": "delivering",
    "delivery": "delivering",
    "completed": "delivered",
    "done": "delivered",
    "returned": "cancelled",
}

TERMINAL_STATUSES = frozenset({"delivered", "cancelled"})


def normalize_status(raw: str | None) -> str:
    if not raw:
        return "new"
    key = raw.strip().lower()
    if key in PIPELINE_KEYS:
        return key
    return STATUS_ALIASES.get(key, "new")


def status_index(status: str) -> int:
    norm = normalize_status(status)
    if norm == "cancelled":
        return -1
    try:
        return PIPELINE_KEYS.index(norm)
    except ValueError:
        return 0


def status_meta(raw_status: str) -> dict:
    """Данные для Mini App: текущий шаг, прогресс, подписи."""
    norm = normalize_status(raw_status)
    if norm == "cancelled":
        return {
            "status": "cancelled",
            "label": "Заказ отменён",
            "subtitle": "Свяжитесь с нами, если это ошибка",
            "icon": "😔",
            "progress": 0,
            "step_index": -1,
            "steps": [_step_dict(s, "pending") for s in STATUS_PIPELINE],
            "is_terminal": True,
        }

    idx = status_index(norm)
    progress = int(((idx + 1) / len(STATUS_PIPELINE)) * 100)

    steps = []
    for i, step in enumerate(STATUS_PIPELINE):
        if i < idx:
            state = "done"
        elif i == idx:
            state = "active"
        else:
            state = "pending"
        steps.append(_step_dict(step, state))

    current = STATUS_PIPELINE[idx]
    return {
        "status": norm,
        "label": current.title,
        "subtitle": current.subtitle,
        "icon": current.icon,
        "progress": progress,
        "step_index": idx,
        "steps": steps,
        "is_terminal": norm in TERMINAL_STATUSES,
    }


# Подписи для Mini App (таймлайн на главной)
MINIAPP_TIMELINE: tuple[tuple[str, str], ...] = (
    ("new", "Заказ принят"),
    ("confirmed", "Флорист подтвердил"),
    ("in_progress", "Букет собирается"),
    ("delivering", "Передан курьеру"),
    ("delivered", "Доставлен"),
)


def miniapp_status_payload(raw_status: str, created_at: str | None = None) -> dict:
    """Ответ API /api/order-status для status.js."""
    norm = normalize_status(raw_status)
    cur_idx = status_index(norm) if norm != "cancelled" else -1

    steps = []
    for i, (key, label) in enumerate(MINIAPP_TIMELINE):
        if norm == "cancelled":
            state = "wait"
        elif i < cur_idx:
            state = "done"
        elif i == cur_idx:
            state = "current"
        else:
            state = "wait"
        time = ""
        if state == "done" and key == "new" and created_at:
            time = _format_time(created_at)
        elif state == "wait":
            time = "Ожидается"
        steps.append({"key": key, "label": label, "state": state, "time": time})

    badge = MINIAPP_TIMELINE[cur_idx][1] if cur_idx >= 0 else "Отменён"
    if norm == "in_progress":
        badge += " 💐"
    elif norm == "delivering":
        badge += " 🚗"
    elif norm == "delivered":
        badge += " 🎉"

    return {
        "status": norm,
        "badge": badge,
        "steps": steps,
        "step_index": cur_idx,
    }


def _format_time(iso: str) -> str:
    try:
        from datetime import datetime

        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        months = (
            "янв", "фев", "мар", "апр", "май", "июн",
            "июл", "авг", "сен", "окт", "ноя", "дек",
        )
        return f"{dt.day} {months[dt.month - 1]}, {dt.strftime('%H:%M')}"
    except Exception:
        return ""


def _step_dict(step: StatusStep, state: str) -> dict:
    return {
        "key": step.key,
        "title": step.title,
        "subtitle": step.subtitle,
        "icon": step.icon,
        "state": state,
    }
