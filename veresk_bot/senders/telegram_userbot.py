"""Telethon userbot: отправка с личных Telegram-аккаунтов."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import runtime_settings
from config import SESSIONS_DIR, TELEGRAM_API_HASH, TELEGRAM_API_ID
from senders.base import SendResult

logger = logging.getLogger(__name__)

# phone → pending Telethon client during login flow
_pending_logins: dict[str, Any] = {}


def get_api_credentials() -> tuple[int, str]:
    """API ID/Hash: сначала значения из админ-панели, затем fallback на .env."""
    raw_id = runtime_settings.get("telegram_api_id")
    raw_hash = runtime_settings.get("telegram_api_hash")
    api_id = 0
    if raw_id:
        try:
            api_id = int(raw_id)
        except (TypeError, ValueError):
            api_id = 0
    api_hash = str(raw_hash).strip() if raw_hash else ""
    if api_id and api_hash:
        return api_id, api_hash
    return TELEGRAM_API_ID, TELEGRAM_API_HASH


def _normalize_phone(phone: str) -> str:
    """Любой формат (в т.ч. +7(999)999-99-99 из базы) → +79999999999."""
    digits = re.sub(r"\D", "", phone.strip())
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return f"+{digits}" if digits else phone.strip()


def sessions_path() -> Path:
    path = Path(SESSIONS_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_telethon_configured() -> bool:
    api_id, api_hash = get_api_credentials()
    return bool(api_id and api_hash)


async def start_telegram_login(phone: str) -> dict[str, Any]:
    """Шаг 1: отправить код на номер. Возвращает {ok, phone} или {ok:false, error}."""
    if not is_telethon_configured():
        return {
            "ok": False,
            "error": "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы — укажите их в настройках",
        }
    try:
        from telethon import TelegramClient
    except ImportError:
        return {"ok": False, "error": "telethon не установлен"}

    api_id, api_hash = get_api_credentials()
    phone_norm = _normalize_phone(phone)
    digits_only = re.sub(r"\D", "", phone_norm)
    session_name = sessions_path() / f"acc_{digits_only}"
    client = TelegramClient(str(session_name), api_id, api_hash)
    await client.connect()
    try:
        await client.send_code_request(phone_norm)
    except Exception as exc:
        await client.disconnect()
        logger.exception("Telethon send_code_request failed")
        return {"ok": False, "error": str(exc)}

    _pending_logins[phone_norm] = client
    return {"ok": True, "phone": phone_norm, "need_code": True}


async def confirm_telegram_login(
    phone: str,
    code: str,
    *,
    password: str | None = None,
) -> dict[str, Any]:
    """Шаг 2: подтвердить код (и 2FA пароль при необходимости)."""
    phone_norm = _normalize_phone(phone)
    client = _pending_logins.get(phone_norm)
    if client is None:
        return {"ok": False, "error": "Сначала запросите код для этого номера"}

    try:
        from telethon.errors import SessionPasswordNeededError
    except ImportError:
        return {"ok": False, "error": "telethon не установлен"}

    try:
        await client.sign_in(phone_norm, code.strip())
    except SessionPasswordNeededError:
        if not password:
            return {"ok": False, "need_2fa": True, "error": "Нужен пароль 2FA"}
        try:
            await client.sign_in(password=password)
        except Exception as exc:
            return {"ok": False, "error": f"2FA: {exc}"}
    except Exception as exc:
        logger.exception("Telethon sign_in failed")
        return {"ok": False, "error": str(exc)}

    me = await client.get_me()
    digits_only = re.sub(r"\D", "", phone_norm)
    session_file = str(sessions_path() / f"acc_{digits_only}.session")
    label = ""
    if me:
        label = " ".join(
            filter(None, [getattr(me, "first_name", None), getattr(me, "last_name", None)])
        ) or phone_norm
    await client.disconnect()
    _pending_logins.pop(phone_norm, None)

    return {
        "ok": True,
        "phone": phone_norm,
        "session_file": session_file,
        "label": label or phone_norm,
        "tg_id": getattr(me, "id", None) if me else None,
    }


async def cancel_telegram_login(phone: str) -> None:
    phone_norm = _normalize_phone(phone)
    client = _pending_logins.pop(phone_norm, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


class TelegramUserbotSender:
    """Отправка через одну Telethon-сессию."""

    def __init__(self, session_file: str, account_id: int | None = None):
        self.session_file = session_file
        self.account_id = account_id
        self._client = None

    @property
    def available(self) -> bool:
        return is_telethon_configured() and bool(self.session_file)

    async def _get_client(self):
        if self._client and self._client.is_connected():
            return self._client
        from telethon import TelegramClient

        # session_file может быть с .session или без
        base = self.session_file
        if base.endswith(".session"):
            base = base[:-8]
        api_id, api_hash = get_api_credentials()
        client = TelegramClient(base, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Сессия не авторизована — переподключите аккаунт")
        self._client = client
        return client

    async def send(self, *, phone: str, name: str, text: str) -> SendResult:
        if not self.available:
            return SendResult(ok=False, status="failed", error="Telethon не настроен")
        try:
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact
            from telethon.errors import FloodWaitError
        except ImportError:
            return SendResult(ok=False, status="failed", error="telethon не установлен")

        phone_norm = _normalize_phone(phone)
        try:
            client = await self._get_client()
            contact = InputPhoneContact(
                client_id=0,
                phone=phone_norm,
                first_name=name.split()[0] if name else "Клиент",
                last_name=" ".join(name.split()[1:]) if name and len(name.split()) > 1 else "",
            )
            result = await client(ImportContactsRequest([contact]))
            user = None
            if result.users:
                user = result.users[0]
            if not user:
                # Попробуем напрямую по телефону
                try:
                    user = await client.get_entity(phone_norm)
                except Exception:
                    return SendResult(
                        ok=False,
                        status="failed",
                        error="Не удалось найти пользователя по телефону",
                    )
            await client.send_message(user, text)
            return SendResult(ok=True, status="sent")
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 60))
            logger.warning("FloodWait %ss for account %s", wait, self.account_id)
            await asyncio.sleep(min(wait, 120))
            return SendResult(ok=False, status="failed", error=f"FloodWait {wait}s")
        except Exception as exc:
            logger.exception("Telethon send failed to %s", phone_norm)
            return SendResult(ok=False, status="failed", error=str(exc))

    async def disconnect(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
