"""Runtime-настройки, редактируемые из админ-панели.

Хранятся в JSON-файле рядом с БД, поэтому переживают перезапуск контейнера.
Значения отсюда имеют приоритет над переменными из .env; при их отсутствии
используется .env как fallback (см. senders/telegram_userbot.py).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from config import DATABASE_PATH

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path(DATABASE_PATH).resolve().parent / "runtime_settings.json"
_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        _cache = loaded if isinstance(loaded, dict) else {}
    except FileNotFoundError:
        _cache = {}
    except (json.JSONDecodeError, OSError):
        logger.exception("Не удалось прочитать %s, использую пустые настройки", _SETTINGS_PATH)
        _cache = {}
    return _cache


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def set_many(values: dict[str, Any]) -> None:
    """Атомарно сохранить набор значений."""
    global _cache
    with _lock:
        data = dict(_load())
        data.update(values)
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_SETTINGS_PATH)
        _cache = data


def delete_keys(*keys: str) -> None:
    """Удалить ключи из runtime-настроек."""
    global _cache
    with _lock:
        data = dict(_load())
        changed = False
        for key in keys:
            if key in data:
                del data[key]
                changed = True
        if not changed:
            return
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_SETTINGS_PATH)
        _cache = data
