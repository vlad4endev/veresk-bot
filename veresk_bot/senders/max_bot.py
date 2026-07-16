"""Заглушка MAX-бота. Реальная интеграция — когда будет MAX_BOT_TOKEN и API."""

from __future__ import annotations

import logging

from config import MAX_BOT_TOKEN
from senders.base import SendResult

logger = logging.getLogger(__name__)


class MaxBotSender:
    def __init__(self, token: str | None = None):
        self.token = (token if token is not None else MAX_BOT_TOKEN).strip()

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def send(self, *, phone: str, name: str, text: str) -> SendResult:
        if not self.available:
            return SendResult(
                ok=False,
                status="failed",
                error="MAX_BOT_TOKEN не задан — бот MAX ещё не подключён",
            )
        # TODO: реализовать вызов MAX Bot API, когда документация/токен будут готовы
        logger.warning(
            "MAX send stub: phone=%s name=%s text_len=%s",
            phone,
            name,
            len(text),
        )
        return SendResult(
            ok=False,
            status="failed",
            error="Отправка в MAX пока не реализована (заглушка)",
        )
