"""Отправка сообщений через MAX Bot API (рассылки).

MAX Bot API не умеет отправлять по номеру телефона — только по user_id,
и только тем, кто уже открыл диалог с ботом. Поэтому получатель ищется
по телефону в таблице max_profiles (заполняется анкетой MAX-бота).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3

import runtime_settings
from config import DATABASE_PATH, MAX_BOT_TOKEN
from senders.base import SendResult

logger = logging.getLogger(__name__)


def get_max_bot_token() -> str:
    """Токен MAX-бота: сначала из админ-панели, затем fallback на .env."""
    raw = runtime_settings.get("max_bot_token")
    if raw and str(raw).strip():
        return str(raw).strip()
    return MAX_BOT_TOKEN


def is_max_configured() -> bool:
    return bool(get_max_bot_token())


def _normalize_phone(phone: str) -> str:
    """Любой формат (+7(999)999-99-99, 8999…, 999…) → 10 цифр для сравнения."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]
    return digits


def _find_max_user_id(phone: str) -> int | None:
    target = _normalize_phone(phone)
    if not target:
        return None
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT max_user_id, phone FROM max_profiles"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("Таблица max_profiles недоступна", exc_info=True)
        return None
    for user_id, stored_phone in rows:
        if _normalize_phone(stored_phone or "") == target:
            return int(user_id)
    return None


class MaxBotSender:
    def __init__(self, token: str | None = None):
        self.token = (
            token.strip()
            if token is not None
            else get_max_bot_token()
        )

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def send(self, *, phone: str, name: str, text: str) -> SendResult:
        if not self.available:
            return SendResult(
                ok=False,
                status="failed",
                error="Токен MAX-бота не задан — укажите его в настройках",
            )

        user_id = await asyncio.to_thread(_find_max_user_id, phone)
        if user_id is None:
            return SendResult(
                ok=False,
                status="failed",
                error=(
                    "Клиент не найден среди пользователей MAX-бота "
                    "(отправка возможна только тем, кто прошёл анкету в MAX)"
                ),
            )

        from max_bot.api import MaxAPIError, MaxBotAPI

        api = MaxBotAPI(self.token)
        try:
            await api.send_message(user_id=user_id, text=text, markdown=False)
            logger.info("MAX рассылка: отправлено user_id=%s (%s)", user_id, name)
            return SendResult(ok=True, status="sent")
        except MaxAPIError as exc:
            logger.warning("MAX рассылка не доставлена user_id=%s: %s", user_id, exc)
            return SendResult(ok=False, status="failed", error=str(exc))
        finally:
            await api.close()
