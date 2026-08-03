"""Runtime-настройки, редактируемые из админ-панели.

Хранятся в JSON-файле рядом с БД, поэтому переживают перезапуск контейнера.
Значения отсюда имеют приоритет над переменными из .env; при их отсутствии
используется .env как fallback (см. senders/telegram_userbot.py).

Кэш сбрасывается при изменении mtime файла — так процесс max_bot
подхватывает токен, сохранённый в админке процессом bot, без рестарта.
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
_cache_mtime: float | None = None


def _file_mtime() -> float | None:
    try:
        return _SETTINGS_PATH.stat().st_mtime
    except OSError:
        return None


def _load() -> dict[str, Any]:
    global _cache, _cache_mtime
    mtime = _file_mtime()
    if _cache is not None and mtime is not None and mtime == _cache_mtime:
        return _cache
    if _cache is not None and mtime is None and _cache_mtime is None:
        return _cache
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        _cache = loaded if isinstance(loaded, dict) else {}
        _cache_mtime = mtime
    except FileNotFoundError:
        _cache = {}
        _cache_mtime = None
    except (json.JSONDecodeError, OSError):
        logger.exception("Не удалось прочитать %s, использую пустые настройки", _SETTINGS_PATH)
        _cache = {}
        _cache_mtime = mtime
    return _cache


def get(key: str, default: Any = None) -> Any:
    with _lock:
        return _load().get(key, default)


def set_many(values: dict[str, Any]) -> None:
    """Атомарно сохранить набор значений."""
    global _cache, _cache_mtime
    with _lock:
        data = dict(_load())
        data.update(values)
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SETTINGS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(_SETTINGS_PATH)
        _cache = data
        _cache_mtime = _file_mtime()


def delete_keys(*keys: str) -> None:
    """Удалить ключи из runtime-настроек."""
    global _cache, _cache_mtime
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
        _cache_mtime = _file_mtime()
