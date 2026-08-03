"""Отправка сообщений через MAX Bot API (рассылки).

MAX Bot API не умеет отправлять по номеру телефона — только по user_id,
и только тем, кто уже открыл диалог с ботом.

Поиск получателя (по приоритету):
1. явный max_user_id (из карточки клиента / сверки);
2. customers.max_user_id по телефону;
3. max_profiles по телефону (анкета MAX-бота).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import runtime_settings
from config import DATABASE_PATH, MAX_BOT_TOKEN
from senders.base import SendResult
from senders.matching import phone_digits, resolve_max_user_id_sync

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
    return phone_digits(phone)


def _find_max_user_id_in_customers(phone: str) -> int | None:
    target = _normalize_phone(phone)
    if not target:
        return None
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT max_user_id, phone FROM customers WHERE max_user_id IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.debug("customers.max_user_id lookup failed", exc_info=True)
        return None
    for user_id, stored_phone in rows:
        if _normalize_phone(stored_phone or "") == target:
            try:
                return int(user_id)
            except (TypeError, ValueError):
                continue
    return None


def _find_max_user_id(phone: str, *, max_user_id: int | None = None) -> int | None:
    found = resolve_max_user_id_sync(max_user_id=max_user_id, phone=phone)
    if found is not None:
        return found
    return _find_max_user_id_in_customers(phone)


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

    async def send(
        self,
        *,
        phone: str,
        name: str,
        text: str,
        max_user_id: int | None = None,
        media_path: str | None = None,
        media_filename: str | None = None,
        media_mime: str | None = None,
    ) -> SendResult:
        if not self.available:
            return SendResult(
                ok=False,
                status="failed",
                error="Токен MAX-бота не задан — укажите его в настройках",
            )

        user_id = await asyncio.to_thread(
            _find_max_user_id, phone, max_user_id=max_user_id
        )
        if user_id is None:
            return SendResult(
                ok=False,
                status="failed",
                error=(
                    "Клиент не найден в MAX "
                    "(нужен max_user_id в базе или анкета в MAX-боте)"
                ),
            )

        # Если нашли по профилю/телефону — допривяжем к карточке
        if phone and max_user_id is None:
            try:
                from mailing_db import set_customer_max_by_phone

                await set_customer_max_by_phone(phone, int(user_id))
            except Exception:
                logger.debug("auto-bind max_user_id after send resolve failed", exc_info=True)

        from max_bot.api import MaxAPIError, MaxBotAPI

        api = MaxBotAPI(self.token)
        try:
            attachments = None
            media = (media_path or "").strip()
            if media:
                from pathlib import Path

                path = Path(media)
                if not path.is_file():
                    return SendResult(
                        ok=False,
                        status="failed",
                        error="Файл вложения не найден",
                    )
                raw = path.read_bytes()
                filename = media_filename or path.name
                mime = media_mime or "image/jpeg"
                upload_type = "image" if str(mime).startswith("image/") else "file"
                attachments = [
                    await api.upload_file(
                        upload_type,
                        raw,
                        filename=filename,
                        content_type=mime,
                    )
                ]
            await api.send_message(
                user_id=user_id,
                text=text,
                attachments=attachments,
                markdown=False,
            )
            logger.info("MAX рассылка: отправлено user_id=%s (%s)", user_id, name)
            return SendResult(ok=True, status="sent")
        except MaxAPIError as exc:
            logger.warning("MAX рассылка не доставлена user_id=%s: %s", user_id, exc)
            return SendResult(ok=False, status="failed", error=str(exc))
        finally:
            await api.close()
