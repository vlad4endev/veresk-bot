"""
Минимальный асинхронный клиент MAX Bot API (https://dev.max.ru/docs-api).

Отличия от Telegram Bot API:
- REST-архитектура: POST /messages вместо sendMessage и т.д.
- Токен передаётся в заголовке Authorization.
- Клавиатуры только inline (attachments -> inline_keyboard),
  reply-клавиатур нет.
- Обновления: GET /updates (long polling) либо webhook.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# С 19.07.2026 основной домен — platform-api2.max.ru
DEFAULT_API_BASE = "https://platform-api2.max.ru"


class MaxAPIError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"MAX API HTTP {status}: {body[:300]}")


class MaxBotAPI:
    def __init__(self, token: str, base_url: str = DEFAULT_API_BASE):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self.token},
                timeout=aiohttp.ClientTimeout(total=120),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        async with session.request(method, url, params=params, json=json_body) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise MaxAPIError(resp.status, text)
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {}

    # ── Методы API ────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        timeout: int = 30,
        limit: int = 100,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        return await self._request("GET", "/updates", params=params)

    async def send_message(
        self,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
        text: str,
        keyboard: list[list[dict[str, Any]]] | None = None,
        markdown: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if user_id is not None:
            params["user_id"] = user_id
        elif chat_id is not None:
            params["chat_id"] = chat_id
        else:
            raise ValueError("Нужен user_id или chat_id")

        body: dict[str, Any] = {"text": text[:4000]}
        if markdown:
            body["format"] = "markdown"
        if keyboard:
            body["attachments"] = [
                {"type": "inline_keyboard", "payload": {"buttons": keyboard}}
            ]
        return await self._request("POST", "/messages", params=params, json_body=body)

    async def answer_callback(
        self, callback_id: str, notification: str | None = None
    ) -> None:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification[:400]
        try:
            await self._request(
                "POST",
                "/answers",
                params={"callback_id": callback_id},
                json_body=body,
            )
        except MaxAPIError as exc:
            # Ответ на callback не критичен — не роняем сценарий
            logger.warning("answer_callback failed: %s", exc)


# ── Конструкторы кнопок ───────────────────────────────────────


def btn_callback(text: str, payload: str) -> dict[str, Any]:
    return {"type": "callback", "text": text, "payload": payload[:1024]}


def btn_request_contact(text: str) -> dict[str, Any]:
    return {"type": "request_contact", "text": text}


async def poll_updates_forever(api: MaxBotAPI, handler, *, types: list[str]) -> None:
    """Бесконечный long-polling цикл. handler(update: dict) — корутина."""
    marker: int | None = None
    while True:
        try:
            data = await api.get_updates(marker=marker, timeout=45, types=types)
        except (MaxAPIError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Ошибка получения обновлений MAX: %s", exc)
            await asyncio.sleep(5)
            continue

        marker = data.get("marker", marker)
        for update in data.get("updates", []):
            try:
                await handler(update)
            except Exception:
                logger.exception("Ошибка обработки обновления MAX: %s", update)
