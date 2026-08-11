"""Telethon userbot: отправка с личных Telegram-аккаунтов."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any

import runtime_settings
from config import SESSIONS_DIR, TELEGRAM_API_HASH, TELEGRAM_API_ID
from senders.base import SendResult

logger = logging.getLogger(__name__)

# phone → pending Telethon client during login flow
_pending_logins: dict[str, Any] = {}
# login_id → pending QR login ({client, qr, session_base, ...})
_pending_qr: dict[str, Any] = {}

TELETHON_MISSING_DETAIL = (
    "Библиотека Telethon не установлена в окружении сервера. "
    "Выполните: pip install telethon==1.36.0 и перезапустите админку."
)


def _telethon_missing_error(**extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "telethon_missing",
        "detail": TELETHON_MISSING_DETAIL,
        **extra,
    }


def is_telethon_installed() -> bool:
    try:
        import telethon  # noqa: F401

        return True
    except ImportError:
        return False


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
        return _telethon_missing_error()

    api_id, api_hash = get_api_credentials()
    phone_norm = _normalize_phone(phone)
    digits_only = re.sub(r"\D", "", phone_norm)
    session_name = sessions_path() / f"acc_{digits_only}"

    # Закрыть предыдущую незавершённую попытку для этого номера
    old = _pending_logins.pop(phone_norm, None)
    if old:
        try:
            await old["client"].disconnect()
        except Exception:
            pass

    client = TelegramClient(str(session_name), api_id, api_hash)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        session_file = str(sessions_path() / f"acc_{digits_only}.session")
        label = ""
        if me:
            label = " ".join(
                filter(
                    None,
                    [getattr(me, "first_name", None), getattr(me, "last_name", None)],
                )
            ) or phone_norm
        await client.disconnect()
        return {
            "ok": True,
            "phone": phone_norm,
            "already_authorized": True,
            "session_file": session_file,
            "label": label or phone_norm,
            "tg_id": getattr(me, "id", None) if me else None,
        }

    try:
        sent = await client.send_code_request(phone_norm)
    except Exception as exc:
        await client.disconnect()
        logger.exception("Telethon send_code_request failed")
        return {"ok": False, "error": _friendly_telethon_error(exc)}

    phone_code_hash = getattr(sent, "phone_code_hash", None) or ""
    code_type = type(getattr(sent, "type", None)).__name__ if sent else None
    _pending_logins[phone_norm] = {
        "client": client,
        "phone_code_hash": phone_code_hash,
        "code_type": code_type,
    }
    hint = _code_delivery_hint(code_type)
    logger.info("Telegram login code requested for %s via %s", phone_norm, code_type)
    return {
        "ok": True,
        "phone": phone_norm,
        "need_code": True,
        "code_type": code_type,
        "code_hint": hint,
        "detail": hint,
    }


def _code_delivery_hint(code_type: str | None, *, resent: bool = False) -> str:
    """Куда Telegram реально отправил код.

    С 18.02.2023 Telegram не шлёт SMS-коды в сторонние клиенты (Telethon) —
    только в официальное приложение. force_sms / ResendCodeRequest это не обходят.
    """
    name = (code_type or "").lower()
    prefix = "Код отправлен повторно. " if resent else ""
    if "app" in name:
        return (
            f"{prefix}"
            "Откройте Telegram на телефоне → чат «Telegram» (служебные сообщения) — "
            "код там. SMS для входа через админку Telegram больше не присылает "
            "(политика API с 2023 года)."
        )
    if "sms" in name or "fragment" in name or "firebase" in name:
        return f"{prefix}Код отправлен по SMS на этот номер. Проверьте SMS и папку «Спам»."
    if "call" in name or "flash" in name or "missed" in name:
        return (
            f"{prefix}"
            "Код придёт звонком / пропущенным вызовом — последние цифры номера и есть код."
        )
    if "email" in name:
        return f"{prefix}Код отправлен на email, привязанный к аккаунту Telegram."
    return (
        f"{prefix}"
        "Откройте приложение Telegram → чат «Telegram». "
        "SMS с кодом для сторонних клиентов (как эта админка) Telegram не отправляет."
    )


def _friendly_telethon_error(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc)
    msg_l = msg.lower()
    if (
        "SendCodeUnavailable" in name
        or "all available options" in msg_l
        or "caused by ResendCodeRequest" in msg
    ):
        return (
            "Другой способ доставки недоступен (SMS Telegram не шлёт в админку). "
            "Код уже должен быть в приложении Telegram → чат «Telegram». "
            "Закройте это окно и введите код оттуда — не нажимайте повторную отправку."
        )
    if "PhoneCodeInvalid" in name or "phone code entered was invalid" in msg_l:
        return (
            "Неверный код. Запросите новый кнопкой «Получить код» "
            "и введите свежий код из чата «Telegram» в приложении."
        )
    if "PhoneCodeExpired" in name or "expired" in msg_l:
        return "Код устарел. Нажмите «Получить код» ещё раз и введите новый."
    if "FloodWait" in name:
        wait = getattr(exc, "seconds", None)
        return (
            f"Слишком много попыток. Подождите {wait} сек. и попробуйте снова."
            if wait
            else "Слишком много попыток. Подождите немного и попробуйте снова."
        )
    if "PhoneNumberInvalid" in name:
        return "Неверный номер телефона. Укажите в формате +79001234567."
    if "PhoneNumberBanned" in name:
        return "Этот номер заблокирован в Telegram."
    if "SessionPasswordNeeded" in name:
        return "Нужен пароль 2FA"
    return msg


async def resend_telegram_login_code(phone: str) -> dict[str, Any]:
    """Повторная отправка кода (Telegram сам выбирает канал; SMS в сторонние клиенты обычно недоступны)."""
    if not is_telethon_configured():
        return {
            "ok": False,
            "error": "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы — укажите их в настройках",
        }
    phone_norm = _normalize_phone(phone)
    pending = _pending_logins.get(phone_norm)
    if not pending:
        return {
            "ok": False,
            "error": "Сначала запросите код для этого номера (кнопка «Получить код»)",
        }
    # Если код уже ушёл в App — повтор почти всегда даёт SendCodeUnavailable.
    prev_type = str(pending.get("code_type") or "").lower()
    if "app" in prev_type:
        return {
            "ok": False,
            "error": (
                "Код уже отправлен в приложение Telegram (чат «Telegram»). "
                "SMS недоступны — введите код из приложения. "
                "Если кода нет: подождите 1–2 минуты или нажмите «Получить код» заново."
            ),
            "code_type": pending.get("code_type"),
            "code_hint": _code_delivery_hint(pending.get("code_type")),
        }
    client = pending["client"]
    phone_code_hash = pending.get("phone_code_hash") or ""
    if not phone_code_hash:
        return {"ok": False, "error": "Нет phone_code_hash — запросите код заново"}
    try:
        from telethon.tl.functions.auth import ResendCodeRequest
    except ImportError:
        return _telethon_missing_error()
    try:
        sent = await client(ResendCodeRequest(phone_norm, phone_code_hash))
    except Exception as exc:
        logger.exception("Telethon ResendCodeRequest failed")
        return {"ok": False, "error": _friendly_telethon_error(exc)}

    new_hash = getattr(sent, "phone_code_hash", None) or phone_code_hash
    code_type = type(getattr(sent, "type", None)).__name__ if sent else None
    pending["phone_code_hash"] = new_hash
    pending["code_type"] = code_type
    hint = _code_delivery_hint(code_type, resent=True)
    logger.info("Telegram login code resent for %s via %s", phone_norm, code_type)
    return {
        "ok": True,
        "phone": phone_norm,
        "need_code": True,
        "code_type": code_type,
        "code_hint": hint,
        "detail": hint,
        "resent": True,
    }


async def confirm_telegram_login(
    phone: str,
    code: str,
    *,
    password: str | None = None,
) -> dict[str, Any]:
    """Шаг 2: подтвердить код (и 2FA пароль при необходимости)."""
    phone_norm = _normalize_phone(phone)
    pending = _pending_logins.get(phone_norm)
    if pending is None:
        return {
            "ok": False,
            "error": "Сначала запросите код для этого номера (кнопка «Получить код»)",
            "need_new_code": True,
        }

    # Совместимость: раньше хранили сам client
    if isinstance(pending, dict):
        client = pending["client"]
        phone_code_hash = pending.get("phone_code_hash") or None
    else:
        client = pending
        phone_code_hash = None

    try:
        from telethon.errors import (
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )
    except ImportError:
        return _telethon_missing_error()

    code_clean = re.sub(r"\D", "", (code or "").strip())
    if not code_clean and not password:
        return {"ok": False, "error": "Введите код из Telegram"}

    try:
        if password and not code_clean:
            await client.sign_in(password=password)
        else:
            kwargs: dict[str, Any] = {}
            if phone_code_hash:
                kwargs["phone_code_hash"] = phone_code_hash
            await client.sign_in(phone_norm, code_clean, **kwargs)
    except SessionPasswordNeededError:
        if not password:
            return {"ok": False, "need_2fa": True, "error": "Нужен пароль 2FA"}
        try:
            await client.sign_in(password=password)
        except Exception as exc:
            return {"ok": False, "error": f"2FA: {_friendly_telethon_error(exc)}"}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        return {
            "ok": False,
            "error": _friendly_telethon_error(exc),
            "need_new_code": True,
        }
    except Exception as exc:
        logger.exception("Telethon sign_in failed")
        return {
            "ok": False,
            "error": _friendly_telethon_error(exc),
            "need_new_code": "invalid" in str(exc).lower() or "expired" in str(exc).lower(),
        }

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
    pending = _pending_logins.pop(phone_norm, None)
    if not pending:
        return
    client = pending["client"] if isinstance(pending, dict) else pending
    try:
        await client.disconnect()
    except Exception:
        pass


def _qr_expires_iso(expires: Any) -> str | None:
    if expires is None:
        return None
    try:
        if getattr(expires, "tzinfo", None) is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(expires)


async def _finalize_authorized_client(
    client: Any,
    *,
    session_base: str,
) -> dict[str, Any]:
    """Сохранить авторизованный клиент как acc_{phone}.session и вернуть метаданные."""
    me = await client.get_me()
    raw_phone = getattr(me, "phone", None) if me else None
    phone_norm = _normalize_phone(f"+{raw_phone}" if raw_phone else "")
    digits_only = re.sub(r"\D", "", phone_norm) or "unknown"
    label = ""
    if me:
        label = " ".join(
            filter(
                None,
                [getattr(me, "first_name", None), getattr(me, "last_name", None)],
            )
        ) or phone_norm
    tg_id = getattr(me, "id", None) if me else None
    await client.disconnect()

    target_base = str(sessions_path() / f"acc_{digits_only}")
    session_file = f"{target_base}.session"
    if session_base != target_base:
        for suffix in (".session", ".session-journal"):
            src = Path(f"{session_base}{suffix}")
            dst = Path(f"{target_base}{suffix}")
            try:
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.replace(dst)
            except OSError:
                logger.warning("Не удалось переименовать session %s → %s", src, dst)

    return {
        "ok": True,
        "phone": phone_norm or None,
        "session_file": session_file,
        "label": label or phone_norm or "Telegram",
        "tg_id": tg_id,
    }


async def start_telegram_qr_login() -> dict[str, Any]:
    """Старт входа по QR (без SMS/кода) — как в Telegram Desktop."""
    if not is_telethon_configured():
        return {
            "ok": False,
            "error": "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы — укажите их в настройках",
        }
    try:
        from telethon import TelegramClient
    except ImportError:
        return _telethon_missing_error()

    api_id, api_hash = get_api_credentials()
    login_id = uuid.uuid4().hex[:16]
    session_base = str(sessions_path() / f"qr_{login_id}")
    client = TelegramClient(session_base, api_id, api_hash)
    try:
        await client.connect()
        if await client.is_user_authorized():
            result = await _finalize_authorized_client(client, session_base=session_base)
            return {**result, "already_authorized": True, "login_id": login_id}
        qr = await client.qr_login()
    except Exception as exc:
        try:
            await client.disconnect()
        except Exception:
            pass
        remove_session_file(session_base + ".session")
        logger.exception("Telegram QR login start failed")
        return {"ok": False, "error": _friendly_telethon_error(exc)}

    _pending_qr[login_id] = {
        "client": client,
        "qr": qr,
        "session_base": session_base,
        "need_2fa": False,
    }
    logger.info("Telegram QR login started id=%s", login_id)
    return {
        "ok": True,
        "login_id": login_id,
        "url": qr.url,
        "expires_at": _qr_expires_iso(getattr(qr, "expires", None)),
        "detail": (
            "Откройте Telegram на телефоне → Настройки → Устройства → "
            "Подключить устройство → наведите камеру на QR."
        ),
    }


async def refresh_telegram_qr_login(login_id: str) -> dict[str, Any]:
    """Обновить истёкший QR."""
    pending = _pending_qr.get(str(login_id or "").strip())
    if not pending:
        return {"ok": False, "error": "QR-сессия не найдена — нажмите «Войти по QR» снова"}
    qr = pending.get("qr")
    if not qr:
        return {"ok": False, "error": "QR-сессия повреждена — начните заново"}
    try:
        await qr.recreate()
    except Exception as exc:
        logger.exception("Telegram QR recreate failed")
        return {"ok": False, "error": _friendly_telethon_error(exc)}
    return {
        "ok": True,
        "login_id": login_id,
        "url": qr.url,
        "expires_at": _qr_expires_iso(getattr(qr, "expires", None)),
    }


async def poll_telegram_qr_login(login_id: str, *, timeout: float = 2.5) -> dict[str, Any]:
    """Короткое ожидание скана QR. Вызывать с фронта в цикле."""
    key = str(login_id or "").strip()
    pending = _pending_qr.get(key)
    if not pending:
        return {"ok": False, "error": "QR-сессия не найдена — нажмите «Войти по QR» снова"}
    if pending.get("need_2fa"):
        return {"ok": False, "need_2fa": True, "login_id": key, "error": "Нужен пароль 2FA"}

    client = pending["client"]
    qr = pending["qr"]
    try:
        from telethon.errors import SessionPasswordNeededError
    except ImportError:
        return _telethon_missing_error()

    try:
        await qr.wait(timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": True,
            "pending": True,
            "login_id": key,
            "url": getattr(qr, "url", None),
            "expires_at": _qr_expires_iso(getattr(qr, "expires", None)),
        }
    except SessionPasswordNeededError:
        pending["need_2fa"] = True
        return {
            "ok": False,
            "need_2fa": True,
            "login_id": key,
            "error": "Нужен пароль двухфакторной защиты Telegram",
        }
    except Exception as exc:
        logger.exception("Telegram QR wait failed")
        return {"ok": False, "error": _friendly_telethon_error(exc)}

    result = await _finalize_authorized_client(
        client, session_base=pending["session_base"]
    )
    _pending_qr.pop(key, None)
    return {**result, "login_id": key}


async def confirm_telegram_qr_2fa(login_id: str, password: str) -> dict[str, Any]:
    """Подтвердить 2FA после успешного скана QR."""
    key = str(login_id or "").strip()
    pending = _pending_qr.get(key)
    if not pending:
        return {"ok": False, "error": "QR-сессия не найдена — начните вход по QR заново"}
    pwd = (password or "").strip()
    if not pwd:
        return {"ok": False, "need_2fa": True, "error": "Введите пароль 2FA"}
    client = pending["client"]
    try:
        await client.sign_in(password=pwd)
    except Exception as exc:
        return {"ok": False, "need_2fa": True, "error": f"2FA: {_friendly_telethon_error(exc)}"}

    result = await _finalize_authorized_client(
        client, session_base=pending["session_base"]
    )
    _pending_qr.pop(key, None)
    return {**result, "login_id": key}


async def cancel_telegram_qr_login(login_id: str | None = None) -> None:
    """Отменить одну или все незавершённые QR-сессии."""
    if login_id:
        keys = [str(login_id).strip()]
    else:
        keys = list(_pending_qr.keys())
    for key in keys:
        pending = _pending_qr.pop(key, None)
        if not pending:
            continue
        client = pending.get("client")
        session_base = str(pending.get("session_base") or "")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if session_base:
            remove_session_file(session_base + ".session")


async def check_telegram_session(session_file: str) -> dict[str, Any]:
    """Проверить, что .session живая и авторизована (полный коннект)."""
    if not is_telethon_configured():
        return {
            "ok": False,
            "authorized": False,
            "error": "TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы",
        }
    if not session_file:
        return {"ok": False, "authorized": False, "error": "session_file пустой"}

    session_path = Path(session_file)
    base = str(session_path)
    if base.endswith(".session"):
        base = base[:-8]
    # Telethon пишет рядом .session; без файла коннекта нет
    if not Path(f"{base}.session").exists() and not session_path.exists():
        return {
            "ok": False,
            "authorized": False,
            "error": "Файл сессии не найден — переподключите аккаунт",
        }

    try:
        from senders.telegram_chat import telegram_session
    except ImportError:
        pass
    else:
        try:
            async with telegram_session(session_file) as client:
                me = await client.get_me()
                username = getattr(me, "username", None) if me else None
                first = getattr(me, "first_name", None) if me else None
                last = getattr(me, "last_name", None) if me else None
                label = " ".join(filter(None, [first, last])) or None
                return {
                    "ok": True,
                    "authorized": True,
                    "tg_id": getattr(me, "id", None) if me else None,
                    "username": username,
                    "label": label,
                    "phone": getattr(me, "phone", None) if me else None,
                }
        except asyncio.TimeoutError:
            return {"ok": False, "authorized": False, "error": "Таймаут подключения к Telegram"}
        except Exception as exc:
            logger.exception("check_telegram_session failed")
            return {"ok": False, "authorized": False, "error": str(exc)}

    try:
        from telethon import TelegramClient
    except ImportError:
        return {"ok": False, "authorized": False, "error": "telethon_missing", "detail": TELETHON_MISSING_DETAIL}

    api_id, api_hash = get_api_credentials()
    client = TelegramClient(base, api_id, api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
        authorized = await client.is_user_authorized()
        if not authorized:
            return {
                "ok": False,
                "authorized": False,
                "error": "Сессия не авторизована — переподключите аккаунт",
            }
        me = await client.get_me()
        username = getattr(me, "username", None) if me else None
        first = getattr(me, "first_name", None) if me else None
        last = getattr(me, "last_name", None) if me else None
        label = " ".join(filter(None, [first, last])) or None
        return {
            "ok": True,
            "authorized": True,
            "tg_id": getattr(me, "id", None) if me else None,
            "username": username,
            "label": label,
            "phone": getattr(me, "phone", None) if me else None,
        }
    except asyncio.TimeoutError:
        return {"ok": False, "authorized": False, "error": "Таймаут подключения к Telegram"}
    except Exception as exc:
        logger.exception("check_telegram_session failed")
        return {"ok": False, "authorized": False, "error": str(exc)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def remove_session_file(session_file: str) -> None:
    """Удалить файл сессии Telethon (и journal, если есть)."""
    if not session_file:
        return
    path = Path(session_file)
    candidates = [path]
    if path.suffix == ".session":
        candidates.append(Path(str(path) + "-journal"))
    else:
        candidates.append(Path(str(path) + ".session"))
        candidates.append(Path(str(path) + ".session-journal"))
    for p in candidates:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            logger.warning("Не удалось удалить session file %s", p)


class TelegramUserbotSender:
    """Отправка через одну Telethon-сессию."""

    def __init__(self, session_file: str, account_id: int | None = None):
        self.session_file = session_file
        self.account_id = account_id
        self._client = None

    @property
    def available(self) -> bool:
        return is_telethon_configured() and bool(self.session_file)

    async def send(
        self,
        *,
        phone: str,
        name: str,
        text: str,
        tg_user_id: int | None = None,
        media_path: str | None = None,
    ) -> SendResult:
        if not self.available:
            return SendResult(ok=False, status="failed", error="Telethon не настроен")
        try:
            from telethon.tl.functions.contacts import ImportContactsRequest
            from telethon.tl.types import InputPhoneContact
            from telethon.errors import FloodWaitError
            from senders.telegram_chat import telegram_session
        except ImportError:
            return SendResult(ok=False, status="failed", error=TELETHON_MISSING_DETAIL)

        phone_norm = _normalize_phone(phone) if phone else ""
        try:
            async with telegram_session(self.session_file, self.account_id) as client:
                user = None
                # 1) Прямая отправка по известному Telegram id из базы
                if tg_user_id is not None:
                    try:
                        user = await client.get_entity(int(tg_user_id))
                    except Exception:
                        logger.debug(
                            "get_entity(tg_user_id=%s) failed, fallback to phone",
                            tg_user_id,
                            exc_info=True,
                        )
                        user = None
                # 2) Импорт контакта по телефону → сообщение от имени аккаунта
                if user is None and phone_norm:
                    contact = InputPhoneContact(
                        client_id=0,
                        phone=phone_norm,
                        first_name=name.split()[0] if name else "Клиент",
                        last_name=(
                            " ".join(name.split()[1:])
                            if name and len(name.split()) > 1
                            else ""
                        ),
                    )
                    result = await client(ImportContactsRequest([contact]))
                    if result.users:
                        user = result.users[0]
                    if not user:
                        try:
                            user = await client.get_entity(phone_norm)
                        except Exception:
                            user = None
                if not user:
                    return SendResult(
                        ok=False,
                        status="failed",
                        error="Не удалось найти пользователя в Telegram",
                    )
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
                    # Telegram caption limit for photos ≈ 1024
                    caption = text or ""
                    if len(caption) <= 1024:
                        await client.send_file(user, str(path), caption=caption or None)
                    else:
                        await client.send_file(user, str(path))
                        await client.send_message(user, caption)
                else:
                    await client.send_message(user, text)
                # Допривяжем tg_user_id к карточке, если узнали
                if phone_norm and getattr(user, "id", None):
                    try:
                        from mailing_db import set_customer_tg_by_phone

                        await set_customer_tg_by_phone(phone_norm, int(user.id))
                    except Exception:
                        logger.debug(
                            "auto-bind tg_user_id after send failed",
                            exc_info=True,
                        )
            return SendResult(ok=True, status="sent")
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 60))
            logger.warning("FloodWait %ss for account %s", wait, self.account_id)
            await asyncio.sleep(min(wait, 120))
            # Не failed: диспетчер оставит pending и повторит позже
            return SendResult(
                ok=False,
                status="deferred",
                error=f"FloodWait {wait}s",
            )
        except Exception as exc:
            logger.exception(
                "Telethon send failed to %s / tg_id=%s", phone_norm, tg_user_id
            )
            return SendResult(ok=False, status="failed", error=str(exc))

    async def disconnect(self) -> None:
        from senders.telegram_chat import release_session

        await release_session(
            session_file=self.session_file,
            account_id=self.account_id,
        )
        self._client = None
