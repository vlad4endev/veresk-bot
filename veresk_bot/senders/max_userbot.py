"""PyMax userbot: отправка с личных MAX-аккаунтов (неофициальный API).

Логин по телефону + SMS (кастомный SmsCodeProvider для админки),
сессия в SQLite рядом с Telethon-сессиями.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from config import SESSIONS_DIR
from senders.base import SendResult

logger = logging.getLogger(__name__)

# phone_norm → pending login state
_pending_logins: dict[str, dict[str, Any]] = {}

PYMAX_MISSING_DETAIL = (
    "Библиотека maxapi-python (PyMax) не установлена. "
    "Выполните: pip install maxapi-python>=2.3.0 (нужен Python ≥3.10) "
    "и перезапустите сервис."
)


def is_pymax_installed() -> bool:
    try:
        import pymax  # noqa: F401

        return True
    except ImportError:
        return False


def _normalize_phone(phone: str) -> str:
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


def session_paths_for_phone(phone: str) -> tuple[str, str, Path]:
    """Возвращает (phone_norm, session_name, work_dir)."""
    phone_norm = _normalize_phone(phone)
    digits = re.sub(r"\D", "", phone_norm)
    work_dir = sessions_path()
    session_name = f"max_acc_{digits}.db"
    return phone_norm, session_name, work_dir


def session_file_for_phone(phone: str) -> str:
    phone_norm, session_name, work_dir = session_paths_for_phone(phone)
    return str(work_dir / session_name)


class _QueueSmsCodeProvider:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def set_code(self, code: str) -> None:
        await self._queue.put((code or "").strip())

    async def get_code(self, phone: str) -> str:
        return await self._queue.get()


class _QueuePasswordProvider:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.need_2fa = asyncio.Event()
        self.hint: str | None = None

    async def set_password(self, password: str) -> None:
        await self._queue.put((password or "").strip())

    async def get_password(self, hint: str | None = None) -> str:
        self.hint = hint
        self.need_2fa.set()
        return await self._queue.get()


def _user_label(me: Any) -> str | None:
    if me is None:
        return None
    contact = getattr(me, "contact", None) or me
    names = getattr(contact, "names", None) or []
    if names:
        first = names[0]
        name = getattr(first, "name", None) or getattr(first, "first_name", None)
        if name:
            return str(name)
    for attr in ("name", "first_name"):
        val = getattr(contact, attr, None)
        if val:
            return str(val)
    return None


def _user_id(me: Any) -> int | None:
    if me is None:
        return None
    contact = getattr(me, "contact", None)
    for obj in (contact, me):
        if obj is None:
            continue
        for attr in ("id", "user_id"):
            raw = getattr(obj, attr, None)
            if raw is not None:
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    pass
    return None


async def _run_client_once(
    *,
    phone: str,
    session_name: str,
    work_dir: Path,
    sms_provider: Any | None = None,
    password_provider: Any | None = None,
    on_ready: Any | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Поднять Client до on_start, выполнить on_ready(client), остановить."""
    try:
        from pymax import Client, ExtraConfig
    except ImportError:
        return {"ok": False, "error": "pymax_missing", "detail": PYMAX_MISSING_DETAIL}

    result_box: dict[str, Any] = {}
    error_box: list[BaseException] = []

    client = Client(
        phone=phone,
        work_dir=str(work_dir),
        session_name=session_name,
        sms_code_provider=sms_provider,
        password_provider=password_provider,
        extra_config=ExtraConfig(reconnect=False, log_level="WARNING"),
    )

    @client.on_start()
    async def _on_start(c: Any) -> None:
        try:
            if on_ready is not None:
                result_box["payload"] = await on_ready(c)
            else:
                result_box["payload"] = {
                    "max_user_id": _user_id(c.me),
                    "label": _user_label(c.me),
                    "phone": phone,
                }
            result_box["ok"] = True
        except Exception as exc:
            error_box.append(exc)
            result_box["ok"] = False
            result_box["error"] = str(exc)
        finally:
            try:
                await c.stop()
            except Exception:
                pass

    try:
        await asyncio.wait_for(client.start(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            await client.stop()
        except Exception:
            pass
        return {"ok": False, "error": "Таймаут подключения к MAX"}
    except Exception as exc:
        logger.exception("PyMax client.start failed")
        return {"ok": False, "error": str(exc)}

    if error_box:
        return {"ok": False, "error": str(error_box[0])}
    if result_box.get("ok"):
        out = {"ok": True, **(result_box.get("payload") or {})}
        return out
    return {
        "ok": False,
        "error": result_box.get("error") or "Не удалось авторизоваться в MAX",
    }


async def start_max_login(phone: str) -> dict[str, Any]:
    """Шаг 1: начать логин. Если сессия уже есть — сразу ок."""
    if not is_pymax_installed():
        return {"ok": False, "error": "pymax_missing", "detail": PYMAX_MISSING_DETAIL}

    phone_norm, session_name, work_dir = session_paths_for_phone(phone)
    session_file = str(work_dir / session_name)

    # Уже есть сессия — проверим без SMS
    if Path(session_file).exists():
        live = await check_max_session(session_file, phone=phone_norm)
        if live.get("ok") and live.get("authorized"):
            return {
                "ok": True,
                "phone": phone_norm,
                "already_authorized": True,
                "session_file": session_file,
                "label": live.get("label") or phone_norm,
                "max_user_id": live.get("max_user_id"),
            }

    # Отменить предыдущую попытку
    old = _pending_logins.pop(phone_norm, None)
    if old:
        task = old.get("task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    sms_provider = _QueueSmsCodeProvider()
    password_provider = _QueuePasswordProvider()
    done: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    async def _login_job() -> None:
        try:
            result = await _run_client_once(
                phone=phone_norm,
                session_name=session_name,
                work_dir=work_dir,
                sms_provider=sms_provider,
                password_provider=password_provider,
                timeout=300.0,
            )
            if not done.done():
                if result.get("ok"):
                    done.set_result(
                        {
                            "ok": True,
                            "phone": phone_norm,
                            "session_file": session_file,
                            "label": result.get("label") or phone_norm,
                            "max_user_id": result.get("max_user_id"),
                        }
                    )
                else:
                    done.set_result(result)
        except asyncio.CancelledError:
            if not done.done():
                done.set_result({"ok": False, "error": "cancelled"})
            raise
        except Exception as exc:
            logger.exception("MAX login job failed")
            if not done.done():
                done.set_result({"ok": False, "error": str(exc)})

    task = asyncio.create_task(_login_job(), name=f"max_login_{phone_norm}")
    _pending_logins[phone_norm] = {
        "sms": sms_provider,
        "password": password_provider,
        "task": task,
        "done": done,
        "session_file": session_file,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }

    # Дать Client запросить SMS (короткая пауза)
    await asyncio.sleep(1.5)
    if done.done():
        # Уже завершилось (сессия / ошибка)
        result = done.result()
        _pending_logins.pop(phone_norm, None)
        return result

    return {
        "ok": True,
        "phone": phone_norm,
        "need_code": True,
    }


async def confirm_max_login(
    phone: str,
    code: str,
    *,
    password: str | None = None,
) -> dict[str, Any]:
    """Шаг 2: передать SMS-код (и 2FA при необходимости)."""
    phone_norm = _normalize_phone(phone)
    pending = _pending_logins.get(phone_norm)
    if not pending:
        return {
            "ok": False,
            "error": "Сначала запросите код для этого номера",
            "need_new_code": True,
        }

    sms: _QueueSmsCodeProvider = pending["sms"]
    pwd: _QueuePasswordProvider = pending["password"]
    done: asyncio.Future = pending["done"]

    code_clean = re.sub(r"\D", "", (code or "").strip())
    if code_clean:
        await sms.set_code(code_clean)
    if password:
        await pwd.set_password(password)
    if not code_clean and not password:
        return {"ok": False, "error": "Введите код из SMS / MAX"}

    # Ждём либо 2FA, либо завершения логина
    for _ in range(90):  # до ~90 сек
        if done.done():
            break
        if pwd.need_2fa.is_set() and not password:
            return {
                "ok": False,
                "need_2fa": True,
                "error": "Нужен пароль 2FA",
                "hint": pwd.hint,
            }
        await asyncio.sleep(1.0)

    if not done.done():
        return {
            "ok": False,
            "error": "Таймаут подтверждения. Запросите код снова.",
            "need_new_code": True,
        }

    result = done.result()
    _pending_logins.pop(phone_norm, None)
    task = pending.get("task")
    if task and not task.done():
        task.cancel()
    return result


async def cancel_max_login(phone: str) -> None:
    phone_norm = _normalize_phone(phone)
    pending = _pending_logins.pop(phone_norm, None)
    if not pending:
        return
    task = pending.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def check_max_session(
    session_file: str,
    *,
    phone: str | None = None,
) -> dict[str, Any]:
    """Проверить, что SQLite-сессия PyMax живая."""
    if not is_pymax_installed():
        return {
            "ok": False,
            "authorized": False,
            "error": "pymax_missing",
            "detail": PYMAX_MISSING_DETAIL,
        }
    path = Path(session_file or "")
    if not path.exists():
        return {
            "ok": False,
            "authorized": False,
            "error": "Файл сессии не найден — переподключите аккаунт",
        }

    # session_file = .../max_acc_7999....db
    work_dir = path.parent
    session_name = path.name
    digits = re.sub(r"\D", "", path.stem.replace("max_acc_", ""))
    phone_norm = phone or (f"+{digits}" if digits else "")
    if not phone_norm:
        return {"ok": False, "authorized": False, "error": "Неизвестный телефон сессии"}

    result = await _run_client_once(
        phone=phone_norm,
        session_name=session_name,
        work_dir=work_dir,
        timeout=45.0,
    )
    if result.get("ok"):
        return {
            "ok": True,
            "authorized": True,
            "max_user_id": result.get("max_user_id"),
            "label": result.get("label"),
            "phone": phone_norm,
        }
    return {
        "ok": False,
        "authorized": False,
        "error": result.get("error") or "Сессия не авторизована",
    }


def remove_max_session_file(session_file: str) -> None:
    if not session_file:
        return
    path = Path(session_file)
    for p in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            logger.warning("Не удалось удалить MAX session %s", p)


async def _resolve_chat_id(
    client: Any,
    *,
    phone: str,
    name: str,
    max_user_id: int | None,
) -> tuple[int | None, int | None, str | None]:
    """Вернуть (chat_id, resolved_user_id, error)."""
    me_id = _user_id(client.me)
    target_uid = max_user_id

    if target_uid is None and phone:
        try:
            user = await client.search_by_phone(_normalize_phone(phone))
            target_uid = _user_id(user) or getattr(user, "id", None)
            if target_uid is not None:
                target_uid = int(target_uid)
        except Exception:
            logger.debug("search_by_phone failed for %s", phone, exc_info=True)
            target_uid = None

    if target_uid is None and phone:
        try:
            from pymax.types.domain.user import ContactInfo

            first = (name or "Клиент").split()[0] or "Клиент"
            last = " ".join((name or "").split()[1:]) or None
            imported = await client.import_contacts(
                [
                    ContactInfo(
                        phone=_normalize_phone(phone),
                        first_name=first,
                        last_name=last,
                    )
                ]
            )
            if imported:
                target_uid = _user_id(imported[0]) or getattr(imported[0], "id", None)
                if target_uid is not None:
                    target_uid = int(target_uid)
        except Exception:
            logger.debug("import_contacts failed for %s", phone, exc_info=True)

    if target_uid is None:
        return None, None, "Не удалось найти пользователя в MAX"

    try:
        if me_id is not None:
            chat_id = int(client.get_chat_id(int(me_id), int(target_uid)))
        else:
            # fallback: иногда chat_id == user_id для диалогов
            chat_id = int(target_uid)
        return chat_id, int(target_uid), None
    except Exception as exc:
        logger.debug("get_chat_id failed", exc_info=True)
        return None, int(target_uid), f"Не удалось открыть чат: {exc}"


class MaxUserbotSender:
    """Отправка через одну PyMax-сессию личного аккаунта."""

    def __init__(self, session_file: str, account_id: int | None = None, phone: str = ""):
        self.session_file = session_file
        self.account_id = account_id
        self.phone = phone

    @property
    def available(self) -> bool:
        return is_pymax_installed() and bool(self.session_file) and Path(self.session_file).exists()

    async def send(
        self,
        *,
        phone: str,
        name: str,
        text: str,
        max_user_id: int | None = None,
    ) -> SendResult:
        if not self.available:
            return SendResult(
                ok=False,
                status="failed",
                error="PyMax не настроен или нет файла сессии",
            )

        path = Path(self.session_file)
        work_dir = path.parent
        session_name = path.name
        digits = re.sub(r"\D", "", path.stem.replace("max_acc_", ""))
        account_phone = self.phone or (f"+{digits}" if digits else "")
        if not account_phone:
            return SendResult(ok=False, status="failed", error="Неизвестный телефон аккаунта")

        resolved_uid: list[int] = []

        async def _send(c: Any) -> dict[str, Any]:
            chat_id, uid, err = await _resolve_chat_id(
                c, phone=phone, name=name, max_user_id=max_user_id
            )
            if err or chat_id is None:
                raise RuntimeError(err or "chat_not_found")
            if uid is not None:
                resolved_uid.append(uid)
            await c.send_message(chat_id=int(chat_id), text=text)
            return {
                "max_user_id": _user_id(c.me),
                "label": _user_label(c.me),
                "target_uid": uid,
            }

        try:
            result = await _run_client_once(
                phone=account_phone,
                session_name=session_name,
                work_dir=work_dir,
                on_ready=_send,
                timeout=90.0,
            )
        except Exception as exc:
            logger.exception("MAX userbot send failed to %s", phone)
            return SendResult(ok=False, status="failed", error=str(exc))

        if not result.get("ok"):
            return SendResult(
                ok=False,
                status="failed",
                error=result.get("error") or "Ошибка отправки MAX",
            )

        # Допривязка max_user_id к карточке клиента
        target = resolved_uid[0] if resolved_uid else None
        if phone and target is not None:
            try:
                from mailing_db import set_customer_max_by_phone

                await set_customer_max_by_phone(phone, int(target))
            except Exception:
                logger.debug("auto-bind max_user_id after userbot send failed", exc_info=True)

        logger.info(
            "MAX userbot рассылка: отправлено %s (uid=%s)",
            name or phone,
            target,
        )
        return SendResult(ok=True, status="sent")
