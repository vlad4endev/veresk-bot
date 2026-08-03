"""
Генерация текстов рассылок через OpenAI-совместимый Chat Completions API.

Провайдеры (операторы): openai | openrouter | yandexgpt | custom.
Ключи и параметры: сначала из Настроек (runtime), иначе из .env.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

import runtime_settings
from config import (
    AI_API_BASE,
    AI_API_KEY,
    AI_FOLDER_ID,
    AI_MODEL,
    AI_PROVIDER,
)

logger = logging.getLogger(__name__)

PROVIDERS = ("openai", "openrouter", "yandexgpt", "custom")

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "Ключ с platform.openai.com",
    },
    "openrouter": {
        "label": "OpenRouter",
        "api_base": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "hint": "Ключ с openrouter.ai/keys · модель вида provider/model",
    },
    "yandexgpt": {
        "label": "YandexGPT",
        "api_base": "https://llm.api.cloud.yandex.net/v1",
        "model": "yandexgpt-lite/latest",
        "hint": "API-ключ и Folder ID из Yandex Cloud → AI Studio",
    },
    "custom": {
        "label": "Свой API",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint": "Любой OpenAI-совместимый endpoint",
    },
}

SEGMENT_HINTS = {
    "regular": "постоянные клиенты цветочного магазина",
    "all": "все клиенты цветочного магазина",
    "new": "новые клиенты, недавно оставившие контакты",
    "inactive": "клиенты, которые давно не заказывали",
}

SYSTEM_PROMPT = """Ты копирайтер цветочного салона Veresk (букеты, доставка, поздравления).
Пиши короткие тёплые сообщения для рассылки в Telegram / MAX.

Правила:
- Только текст сообщения, без кавычек, без пояснений и заголовков.
- Язык: русский.
- Обращение на «вы», можно начать с «Здравствуйте, {имя}!».
- Плейсхолдер имени клиента — строго {имя} (если уместно).
- Для скидки можно использовать {скидка}.
- 2–5 коротких абзацев, без markdown и без эмодзи-спама (1–2 эмодзи максимум, по желанию).
- Без ссылок, кроме veresk.flowers если нужна ссылка на заказ.
- Не выдумывай акции/цены, которых нет в запросе пользователя."""


def get_ai_provider() -> str:
    raw = runtime_settings.get("ai_provider")
    if raw and str(raw).strip() in PROVIDERS:
        return str(raw).strip()
    if AI_PROVIDER in PROVIDERS:
        return AI_PROVIDER
    return "openai"


def get_ai_api_key() -> str:
    raw = runtime_settings.get("ai_api_key")
    if raw and str(raw).strip():
        return str(raw).strip()
    return AI_API_KEY


def get_ai_folder_id() -> str:
    raw = runtime_settings.get("ai_folder_id")
    if raw and str(raw).strip():
        return str(raw).strip()
    return AI_FOLDER_ID


def get_ai_api_base() -> str:
    provider = get_ai_provider()
    raw = runtime_settings.get("ai_api_base")
    if raw and str(raw).strip():
        return str(raw).strip().rstrip("/")
    if AI_API_BASE and provider in ("custom", "openai"):
        return AI_API_BASE.rstrip("/")
    preset = PROVIDER_PRESETS.get(provider) or PROVIDER_PRESETS["openai"]
    return preset["api_base"].rstrip("/")


def get_ai_model() -> str:
    raw = runtime_settings.get("ai_model")
    if raw and str(raw).strip():
        return str(raw).strip()
    if AI_MODEL:
        return AI_MODEL
    preset = PROVIDER_PRESETS.get(get_ai_provider()) or PROVIDER_PRESETS["openai"]
    return preset["model"]


def is_ai_configured() -> bool:
    if not get_ai_api_key():
        return False
    if get_ai_provider() == "yandexgpt" and not get_ai_folder_id():
        return False
    return True


def resolve_model_uri() -> str:
    """Для YandexGPT модель должна быть gpt://folder_id/name."""
    model = get_ai_model()
    if get_ai_provider() != "yandexgpt":
        return model
    folder = get_ai_folder_id()
    if model.startswith("gpt://") or model.startswith("emb://"):
        return model
    # yandexgpt-lite/latest → gpt://<folder>/yandexgpt-lite/latest
    name = model.lstrip("/")
    if folder:
        return f"gpt://{folder}/{name}"
    return model


def ai_settings_public() -> dict[str, Any]:
    """Статус для админки (без полного ключа)."""
    key = get_ai_api_key()
    provider = get_ai_provider()
    from_panel = bool(runtime_settings.get("ai_api_key"))
    folder = get_ai_folder_id()
    return {
        "configured": is_ai_configured(),
        "provider": provider,
        "providers": [
            {
                "id": pid,
                "label": meta["label"],
                "api_base": meta["api_base"],
                "model": meta["model"],
                "hint": meta["hint"],
                "needs_folder": pid == "yandexgpt",
            }
            for pid, meta in PROVIDER_PRESETS.items()
        ],
        "api_key_set": bool(key),
        "api_key_masked": _mask_key(key) if key else None,
        "api_base": get_ai_api_base(),
        "model": get_ai_model(),
        "folder_id": folder or None,
        "folder_id_set": bool(folder),
        "from_env": bool(key) and not from_panel,
        "from_panel": from_panel,
    }


def _mask_key(key: str) -> str:
    if len(key) <= 10:
        return "••••••••"
    return key[:4] + "…" + key[-4:]


def _build_user_content(*, prompt: str, current: str, segment: str, mode: str) -> str:
    audience = SEGMENT_HINTS.get(segment, SEGMENT_HINTS["all"])
    if mode == "improve":
        return (
            f"Аудитория: {audience}.\n"
            f"Улучши или перепиши текст рассылки.\n"
            f"Пожелания: {prompt or 'сделай теплее и убедительнее'}.\n\n"
            f"Текущий текст:\n{current}"
        )
    user_content = f"Аудитория: {audience}.\nЗапрос: {prompt}"
    if current:
        user_content += f"\n\nМожно опереться на черновик:\n{current}"
    return user_content


async def generate_mailing_text(
    *,
    prompt: str,
    current_text: str = "",
    segment: str = "all",
    mode: str = "write",
) -> str:
    """
    mode: write — новый текст; improve — улучшить current_text с учётом prompt.
    """
    if not is_ai_configured():
        raise AiComposeError(
            "ai_not_configured",
            "Подключите ИИ в Настройках → Сервисы",
        )

    user_prompt = (prompt or "").strip()
    current = (current_text or "").strip()
    if mode == "improve" and not current and not user_prompt:
        raise AiComposeError("prompt_required", "Нет текста для улучшения")
    if mode != "improve" and not user_prompt:
        raise AiComposeError("prompt_required", "Опишите, какой текст нужен")

    user_content = _build_user_content(
        prompt=user_prompt,
        current=current,
        segment=segment,
        mode=mode,
    )
    provider = get_ai_provider()
    api_key = get_ai_api_key()
    url = f"{get_ai_api_base()}/chat/completions"
    payload: dict[str, Any] = {
        "model": resolve_model_uri(),
        "temperature": 0.7,
        "max_tokens": 500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    headers = _request_headers(provider, api_key)

    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    detail = _extract_api_error(body) or f"HTTP {resp.status}"
                    logger.warning("AI compose failed (%s): %s", provider, detail)
                    raise AiComposeError("ai_provider_error", detail)
    except AiComposeError:
        raise
    except aiohttp.ClientError as exc:
        logger.warning("AI compose network error: %s", exc)
        raise AiComposeError("ai_network_error", "Не удалось связаться с ИИ") from exc
    except Exception as exc:
        logger.exception("AI compose unexpected error")
        raise AiComposeError("ai_error", "Ошибка генерации") from exc

    text = _extract_message(body)
    if not text:
        raise AiComposeError("ai_empty", "ИИ вернул пустой ответ")
    return text


def _request_headers(provider: str, api_key: str) -> dict[str, str]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if provider == "yandexgpt":
        # API-ключ сервисного аккаунта → Api-Key; IAM-токен (t1.… ) → Bearer
        if api_key.startswith("t1."):
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            headers["Authorization"] = f"Api-Key {api_key}"
        folder = get_ai_folder_id()
        if folder:
            headers["x-folder-id"] = folder
    elif provider == "openrouter":
        headers["HTTP-Referer"] = "https://veresk.flowers"
        headers["X-OpenRouter-Title"] = "Veresk Admin"
        headers["X-Title"] = "Veresk Admin"
    return headers


def _extract_message(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    text = str(content or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'«»":
        text = text[1:-1].strip()
    return text


def _extract_api_error(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "")
    if isinstance(err, str):
        return err
    return str(body.get("message") or "")


class AiComposeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
