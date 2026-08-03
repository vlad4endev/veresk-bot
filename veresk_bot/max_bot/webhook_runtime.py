"""
Webhook MAX → локальный SurveyBot + SSE-инбокс админки.

При активном POST /subscriptions long polling не работает — анкета и чаты
обслуживаются здесь, в процессе bot/webapp.

URL и секрет: сначала runtime_settings (админка), затем .env.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import runtime_settings
from max_bot.api import DEFAULT_API_BASE, MaxAPIError, MaxBotAPI
from max_bot.hub import WEBHOOK_UPDATE_TYPES, publish_update_event
from max_bot.storage import init_max_db
from max_bot.survey import SurveyBot
from senders.max_bot import get_max_bot_token

logger = logging.getLogger(__name__)

_survey: SurveyBot | None = None
_api: MaxBotAPI | None = None

_SECRET_RE = re.compile(r"^[a-zA-Z0-9_-]{5,256}$")


def webhook_url() -> str:
    raw = runtime_settings.get("max_webhook_url")
    if raw and str(raw).strip():
        return str(raw).strip()
    return (os.getenv("MAX_WEBHOOK_URL") or "").strip()


def webhook_secret() -> str:
    raw = runtime_settings.get("max_webhook_secret")
    if raw and str(raw).strip():
        return str(raw).strip()
    return (os.getenv("MAX_WEBHOOK_SECRET") or "").strip()


def florist_chat_id() -> int:
    raw = runtime_settings.get("max_florist_chat_id")
    if raw not in (None, ""):
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    try:
        return int(os.getenv("MAX_FLORIST_CHAT_ID", "0") or "0")
    except (TypeError, ValueError):
        return 0


def webhook_enabled() -> bool:
    return bool(webhook_url() and get_max_bot_token())


def webhook_url_source() -> str | None:
    if runtime_settings.get("max_webhook_url"):
        return "panel"
    if (os.getenv("MAX_WEBHOOK_URL") or "").strip():
        return "env"
    return None


def webhook_secret_source() -> str | None:
    if runtime_settings.get("max_webhook_secret"):
        return "panel"
    if (os.getenv("MAX_WEBHOOK_SECRET") or "").strip():
        return "env"
    return None


def validate_webhook_url(url: str) -> str | None:
    u = (url or "").strip()
    if not u:
        return None
    if not u.startswith("https://"):
        return "URL должен начинаться с https://"
    if "://" in u[8:] and not u.startswith("https://"):
        return "Только HTTPS"
    # Max требует порт 443 без явного указания
    if re.search(r":\d+", u.split("://", 1)[-1].split("/", 1)[0]):
        return "Не указывайте порт в URL (нужен HTTPS :443)"
    return None


def validate_webhook_secret(secret: str) -> str | None:
    s = (secret or "").strip()
    if not s:
        return None
    if not _SECRET_RE.match(s):
        return "Секрет: 5–256 символов (A–Z, a–z, 0–9, _, -)"
    return None


def reset_runtime_cache() -> None:
    """Сбросить кэш SurveyBot после смены токена/настроек."""
    global _survey, _api
    _survey = None
    _api = None


async def ensure_runtime() -> SurveyBot | None:
    """Подготовить API + SurveyBot (лениво, один раз на процесс)."""
    global _survey, _api
    token = get_max_bot_token()
    if not token:
        return None
    await init_max_db()
    if _api is None or _api.token != token:
        if _api is not None:
            try:
                await _api.close()
            except Exception:
                pass
        base = os.getenv("MAX_API_BASE", DEFAULT_API_BASE)
        _api = MaxBotAPI(token, base_url=base)
        _survey = SurveyBot(_api, florist_chat_id=florist_chat_id())
        logger.info("MAX SurveyBot готов (webhook/runtime)")
    return _survey


async def register_webhook_subscription() -> dict[str, Any] | None:
    """POST /subscriptions на Max, если webhook URL задан."""
    url = webhook_url()
    token = get_max_bot_token()
    if not url or not token:
        return None
    err = validate_webhook_url(url)
    if err:
        logger.error("Некорректный webhook URL: %s", err)
        return {"success": False, "error": err}

    await ensure_runtime()
    assert _api is not None
    secret = webhook_secret() or None
    if secret:
        sec_err = validate_webhook_secret(secret)
        if sec_err:
            return {"success": False, "error": sec_err}
    try:
        result = await _api.subscribe_webhook(
            url,
            update_types=WEBHOOK_UPDATE_TYPES,
            secret=secret,
        )
        logger.info("MAX webhook подписан: %s → %s", url, result)
        return result if isinstance(result, dict) else {"success": True, "raw": result}
    except MaxAPIError as exc:
        logger.error("Не удалось подписаться на MAX webhook: %s", exc)
        return {"success": False, "error": str(exc)}


async def unregister_webhook_subscription(url: str | None = None) -> dict[str, Any] | None:
    target = (url or webhook_url() or "").strip()
    token = get_max_bot_token()
    if not target or not token:
        return None
    await ensure_runtime()
    assert _api is not None
    try:
        result = await _api.unsubscribe_webhook(target)
        logger.info("MAX webhook отписан: %s → %s", target, result)
        return result if isinstance(result, dict) else {"success": True}
    except MaxAPIError as exc:
        logger.error("Не удалось отписаться от MAX webhook: %s", exc)
        return {"success": False, "error": str(exc)}


async def handle_incoming_update(update: dict[str, Any]) -> None:
    """Один Update: индекс чатов → SSE → анкета."""
    if not isinstance(update, dict):
        return
    try:
        await publish_update_event(update)
    except Exception:
        logger.exception("publish_update_event failed")

    survey = await ensure_runtime()
    if survey is None:
        return
    try:
        await survey.handle_update(update)
    except Exception:
        logger.exception("SurveyBot.handle_update failed")
