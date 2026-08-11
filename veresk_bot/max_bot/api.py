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
            from max_bot.ssl_ctx import build_max_ssl_context

            ssl_ctx = build_max_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(
                headers={"Authorization": self.token},
                timeout=aiohttp.ClientTimeout(total=120),
                connector=connector,
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
        text: str = "",
        keyboard: list[list[dict[str, Any]]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        markdown: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if user_id is not None:
            params["user_id"] = user_id
        elif chat_id is not None:
            params["chat_id"] = chat_id
        else:
            raise ValueError("Нужен user_id или chat_id")

        body: dict[str, Any] = {"text": (text or "")[:4000]}
        if markdown and (text or "").strip():
            body["format"] = "markdown"
        atts: list[dict[str, Any]] = list(attachments or [])
        if keyboard:
            atts.append({"type": "inline_keyboard", "payload": {"buttons": keyboard}})
        if atts:
            body["attachments"] = atts
        return await self._request("POST", "/messages", params=params, json_body=body)

    async def create_upload(self, upload_type: str) -> dict[str, Any]:
        """POST /uploads?type=image|video|audio|file → {url, token?}."""
        kind = (upload_type or "file").strip().lower()
        if kind not in ("image", "video", "audio", "file"):
            kind = "file"
        return await self._request("POST", "/uploads", params={"type": kind})

    async def upload_file(
        self,
        upload_type: str,
        data: bytes,
        *,
        filename: str = "file",
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Получить слот загрузки и отправить байты. Возвращает payload для attachment."""
        slot = await self.create_upload(upload_type)
        upload_url = str(slot.get("url") or "").strip()
        if not upload_url:
            raise MaxAPIError(502, "upload slot without url")

        session = await self._get_session()
        uploaded: dict[str, Any] = {}
        text = ""
        # Сначала multipart (как в доках), затем raw PUT
        form = aiohttp.FormData()
        form.add_field(
            "data",
            data,
            filename=filename or "file",
            content_type=content_type or "application/octet-stream",
        )
        async with session.post(upload_url, data=form) as resp:
            text = await resp.text()
            if resp.status < 400:
                try:
                    uploaded = await resp.json(content_type=None)
                except Exception:
                    uploaded = {}
            else:
                headers = {"Content-Type": content_type or "application/octet-stream"}
                async with session.put(upload_url, data=data, headers=headers) as resp2:
                    text = await resp2.text()
                    if resp2.status >= 400:
                        raise MaxAPIError(resp2.status, text)
                    try:
                        uploaded = await resp2.json(content_type=None)
                    except Exception:
                        uploaded = {}

        token = (
            uploaded.get("token")
            or slot.get("token")
            or (uploaded.get("payload") or {}).get("token")
        )
        # image upload часто возвращает {photos: {…: {token}}}
        if not token and isinstance(uploaded.get("photos"), dict):
            for photo in uploaded["photos"].values():
                if isinstance(photo, dict) and photo.get("token"):
                    token = photo["token"]
                    break
        url = uploaded.get("url")
        payload: dict[str, Any] = {}
        if token:
            payload["token"] = token
        elif url:
            payload["url"] = url
        else:
            raise MaxAPIError(502, f"upload without token: {text[:200]}")

        kind = (upload_type or "file").strip().lower()
        att_type = {
            "image": "image",
            "video": "video",
            "audio": "audio",
            "file": "file",
        }.get(kind, "file")
        return {"type": att_type, "payload": payload}

    async def get_messages(
        self,
        *,
        chat_id: int | None = None,
        message_ids: list[str] | None = None,
        count: int = 50,
        from_ts: int | None = None,
        to_ts: int | None = None,
    ) -> dict[str, Any]:
        """GET /messages — история чата или конкретные mid."""
        params: dict[str, Any] = {
            "count": max(1, min(int(count or 50), 100)),
        }
        if chat_id is not None:
            params["chat_id"] = int(chat_id)
        elif message_ids:
            params["message_ids"] = ",".join(str(x) for x in message_ids)
        else:
            raise ValueError("Нужен chat_id или message_ids")
        if from_ts is not None:
            params["from"] = int(from_ts)
        if to_ts is not None:
            params["to"] = int(to_ts)
        return await self._request("GET", "/messages", params=params)

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        """GET /chats/{chatId} — карточка чата/диалога."""
        return await self._request("GET", f"/chats/{int(chat_id)}")

    async def subscribe_webhook(
        self,
        url: str,
        *,
        update_types: list[str] | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        """POST /subscriptions — доставка событий на HTTPS webhook."""
        body: dict[str, Any] = {"url": url.strip()}
        if update_types:
            body["update_types"] = list(update_types)
        if secret:
            body["secret"] = secret.strip()
        return await self._request("POST", "/subscriptions", json_body=body)

    async def list_subscriptions(self) -> dict[str, Any]:
        """GET /subscriptions — текущие webhook-подписки."""
        return await self._request("GET", "/subscriptions")

    async def unsubscribe_webhook(self, url: str) -> dict[str, Any]:
        """DELETE /subscriptions?url=… — отписка от webhook."""
        return await self._request(
            "DELETE", "/subscriptions", params={"url": url.strip()}
        )

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


def btn_link(text: str, url: str) -> dict[str, Any]:
    return {"type": "link", "text": text[:64], "url": str(url or "").strip()}


def btn_open_app(
    text: str,
    web_app: str,
    *,
    payload: str | None = None,
    contact_id: int | None = None,
) -> dict[str, Any]:
    """Кнопка открытия Mini App внутри MAX (не внешний браузер)."""
    btn: dict[str, Any] = {
        "type": "open_app",
        "text": str(text or "")[:64],
    }
    web = str(web_app or "").strip().lstrip("@/")
    if web:
        btn["web_app"] = web
    if payload:
        # start_param для Mini App (см. MAX Bridge initDataUnsafe.start_param)
        btn["payload"] = str(payload)[:512]
    if contact_id is not None:
        btn["contact_id"] = int(contact_id)
    return btn


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
