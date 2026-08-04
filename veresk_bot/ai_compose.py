"""
Генерация текстов рассылок через OpenAI-совместимый Chat Completions API.

Провайдеры (операторы): openai | openrouter | yandexgpt | custom.
Ключи и параметры: сначала из Настроек (runtime), иначе из .env.
"""

from __future__ import annotations

import logging
import re
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
    "personal": "личное сообщение одному клиенту (не массовая рассылка)",
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

PERSONAL_SYSTEM_PROMPT = """Ты копирайтер цветочного салона Veresk (букеты, доставка, поздравления).
Пиши короткие тёплые личные сообщения одному клиенту в Telegram / MAX.

Правила:
- Только текст сообщения, без кавычек, без пояснений и заголовков.
- Язык: русский; обращение на «вы».
- Это личное 1:1 сообщение, не массовая рассылка — тон ближе и персональнее.
- Если известно имя клиента — обращайся по имени напрямую (не плейсхолдер {имя}).
- Если имя неизвестно — можно «Здравствуйте!» без плейсхолдеров.
- Для скидки можно использовать конкретный процент из запроса или {скидка}.
- 2–5 коротких абзацев, без markdown и без эмодзи-спама (1–2 эмодзи максимум, по желанию).
- Без ссылок, кроме veresk.flowers если нужна ссылка на заказ.
- Не выдумывай акции/цены, которых нет в запросе пользователя."""

CHAT_SYSTEM_PROMPT = """Ты рабочий помощник сотрудников цветочного салона Veresk
(букеты, доставка, поздравления, CRM, личные сообщения и рассылки в Telegram / MAX).

Твоя задача — экономить время флориста и администратора: давать готовые ответы,
конкретные имена из CRM и тексты, которые можно сразу скопировать и отправить.

Помогаешь с:
- текстами рассылок и личных сообщений клиентам;
- кого поздравить (ДР, годовщины) и что написать;
- сегментами (regular / new / inactive / all) и идеями возврата;
- карточкой клиента: контакты, заказы, события — только из контекста;
- чек-листами перед рассылкой и мягкими формулировками без давления.

Как отвечать:
1) Сразу по делу (1–2 предложения).
2) Конкретика из CRM: имена, даты, цифры — только из блока «Данные CRM».
3) Если нужен текст сообщения — оберни его в блок:
```текст
...готовый текст...
```
4) В конце — 1–3 коротких следующих шага («что сделать в панели»).

Правила:
- Язык: русский; тон тёплый, деловой, без канцелярита и без сюсюканья.
- Не выдумывай клиентов, телефоны, суммы, скидки и акции — только из контекста или запроса.
- Если данных нет или мало — честно скажи и укажи, куда смотреть: Клиенты / События / Главная.
- Плейсхолдеры: {имя}, при необходимости {скидка} (только если скидку задал сотрудник).
- Можно: **жирный**, списки через «•» или «1.». Нельзя: markdown-таблицы, HTML, длинные простыни.
- Готовые тексты — короткие (2–5 абзацев), на «вы», без эмодзи-спама (0–2 эмодзи).
- Ссылка на заказ при необходимости: veresk.flowers
- Не раскрывай API-ключи, пароли и внутренние настройки сервера.
- Не обещай отправить сообщение сам — ты только готовишь текст и план."""

MAX_CHAT_HISTORY = 20
MAX_CHAT_MESSAGE_CHARS = 4000

# Стоп-слова для эвристического поиска клиента в сообщении сотрудника
CHAT_SEARCH_STOPWORDS = frozenset({
    "клиент", "клиенту", "клиента", "клиенты", "клиентов", "клиентом",
    "рассылка", "рассылку", "рассылки", "сегмент", "сегмента", "сегменте",
    "бюджет", "букет", "букета", "букеты", "текст", "текста", "текстом",
    "напиши", "написать", "сделай", "сделать", "привет", "здравствуйте",
    "пожалуйста", "сколько", "какой", "какая", "какие", "какого", "какую",
    "нужен", "нужна", "нужно", "помоги", "помощь", "идея", "идеи",
    "сегодня", "завтра", "неделе", "неделя", "месяц", "месяца",
    "событие", "события", "день", "рождения", "годовщина", "годовщины",
    "поздравить", "поздравление", "поздравления", "inactive", "regular",
    "new", "all", "telegram", "макс", "max", "veresk", "цветы", "цветочный",
    "салон", "магазин", "доставка", "заказ", "заказы", "заказов", "скидка",
    "акция", "акции", "промо", "сообщение", "сообщения", "черновик",
    "улучши", "перепиши", "короче", "теплее", "проверка", "сводка",
    "кратко", "подробно", "список", "покажи", "найди", "найти", "открой",
    "этот", "эта", "это", "там", "тут", "здесь", "очень", "просто",
    "можно", "нужны", "хочу", "дай", "давай", "будет", "была", "было",
    "известно", "расскажи", "подскажи", "проверь", "посмотри",
    "про", "для", "или", "если", "когда", "после", "перед", "через",
    "есть", "нет", "ещё", "еще", "также", "тоже", "только", "уже",
    "что", "кто", "как", "где", "почему", "зачем", "куда", "откуда",
    "дела", "добро", "утро", "вечер", "день", "ночь",
})


def detect_chat_intent(text: str) -> str:
    """Грубая цель запроса сотрудника — для подбора CRM-контекста."""
    t = (text or "").lower()
    if any(k in t for k in (
        "др", "день рождения", "годовщин", "поздравить", "событи",
        "на этой неделе", "ближайш", "кого поздрав",
    )):
        return "events"
    if any(k in t for k in (
        "inactive", "неактивн", "давно не", "вернуть", "реактив",
        "потерян", "уснувш",
    )):
        return "inactive"
    if any(k in t for k in (
        "рассылк", "текст", "напиши", "сообщени", "смс", "черновик",
        "улучши", "перепиши", "8 марта", "новый год", "валентин",
    )):
        return "copy"
    if any(k in t for k in (
        "клиент", "тел", "телефон", "+7", "кто такой", "карточка",
        "заказ", "тратил", "покупал", "известно про", "про клиент",
    )) or len(re.sub(r"\D", "", text or "")) >= 10:
        return "customer"
    if any(k in t for k in (
        "сводк", "статистик", "сколько клиент", "сегмент", "доставляем",
        "что проверить", "чек-лист", "чеклист",
    )):
        return "stats"
    return "general"


def suggest_chat_followups(intent: str, *, found_customers: bool = False) -> list[str]:
    """Короткие follow-up подсказки под кнопку в чате."""
    if intent == "events":
        return [
            "Напиши тёплый текст поздравления с {имя}",
            "Кому из списка писать в первую очередь?",
            "Сделай короткий чек-лист перед отправкой",
        ]
    if intent == "inactive":
        return [
            "Текст возврата для inactive без скидки",
            "Текст возврата с плейсхолдером {скидка}",
            "На что обратить внимание перед рассылкой inactive?",
        ]
    if intent == "copy":
        return [
            "Сделай вариант короче",
            "Сделай теплее и мягче",
            "Вариант для сегмента regular",
        ]
    if intent == "customer":
        tips = [
            "Черновик личного сообщения этому клиенту",
            "Кратко: что учесть перед звонком/перепиской",
        ]
        if found_customers:
            tips.insert(0, "Ещё раз: заказы и ближайшие события")
        return tips
    if intent == "stats":
        return [
            "Идея рассылки на эту неделю",
            "Кого поздравить в ближайшие 14 дней?",
            "Что проверить перед новой рассылкой?",
        ]
    return [
        "Кого поздравить на этой неделе?",
        "Идея рассылки для inactive",
        "Сводка по сегментам CRM",
    ]


def extract_customer_search_queries(text: str) -> list[str]:
    """Телефон, ФИО в кавычках, биграммы и значимые токены из текста сотрудника."""
    msg = (text or "").strip()
    if not msg:
        return []

    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        q = (q or "").strip()
        if len(q) < 2:
            return
        key = q.lower()
        if key in seen or key in CHAT_SEARCH_STOPWORDS:
            return
        seen.add(key)
        queries.append(q)

    phone_digits = re.sub(r"\D", "", msg)
    if len(phone_digits) >= 10:
        _add(phone_digits[-10:])

    for quoted in re.findall(r"[«\"']([^»\"']{2,40})[»\"']", msg):
        _add(quoted)

    # Имя Фамилия / Фамилия Имя (оба с заглавной)
    for pair in re.findall(
        r"\b([A-ZА-ЯЁ][a-zа-яё]{1,30})\s+([A-ZА-ЯЁ][a-zа-яё]{1,30})\b",
        msg,
    ):
        _add(f"{pair[0]} {pair[1]}")
        _add(pair[0])
        _add(pair[1])

    # Отдельные слова с заглавной (имена/фамилии), кроме начала предложения-мусора
    caps = re.findall(r"\b([A-ZА-ЯЁ][a-zа-яё]{2,30})\b", msg)
    for t in caps:
        if t.lower() in CHAT_SEARCH_STOPWORDS:
            continue
        _add(t)

    # Если по заглавным/телефону ничего полезного — длинные токены без стоп-слов
    has_phone = bool(phone_digits and len(phone_digits) >= 10)
    has_name = any(" " in q or (q[:1].isupper() and not q.isdigit()) for q in queries)
    if not has_phone and not has_name:
        tokens = re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", msg)
        tokens = [t for t in tokens if t.lower() not in CHAT_SEARCH_STOPWORDS]
        for t in sorted(set(tokens), key=len, reverse=True)[:3]:
            _add(t)

    return queries[:6]


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


def _build_user_content(
    *,
    prompt: str,
    current: str,
    segment: str,
    mode: str,
    client_name: str = "",
    occasion: str = "",
) -> str:
    audience = SEGMENT_HINTS.get(segment, SEGMENT_HINTS["all"])
    personal = segment == "personal"
    meta_parts: list[str] = []
    if personal and client_name:
        meta_parts.append(f"Имя клиента: {client_name}")
    if personal and occasion:
        meta_parts.append(f"Повод: {occasion}")
    meta = ("\n".join(meta_parts) + "\n") if meta_parts else ""

    if mode == "improve":
        kind = "личного сообщения" if personal else "рассылки"
        return (
            f"Аудитория: {audience}.\n"
            f"{meta}"
            f"Улучши или перепиши текст {kind}.\n"
            f"Пожелания: {prompt or 'сделай теплее и убедительнее'}.\n\n"
            f"Текущий текст:\n{current}"
        )
    user_content = f"Аудитория: {audience}.\n{meta}Запрос: {prompt}"
    if current:
        user_content += f"\n\nМожно опереться на черновик:\n{current}"
    return user_content


async def _chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 500,
) -> str:
    if not is_ai_configured():
        raise AiComposeError(
            "ai_not_configured",
            "Подключите ИИ в Настройках → Сервисы",
        )
    if not messages:
        raise AiComposeError("prompt_required", "Пустой запрос")

    provider = get_ai_provider()
    api_key = get_ai_api_key()
    url = f"{get_ai_api_base()}/chat/completions"
    payload: dict[str, Any] = {
        "model": resolve_model_uri(),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    headers = _request_headers(provider, api_key)

    timeout = aiohttp.ClientTimeout(total=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    detail = _extract_api_error(body) or f"HTTP {resp.status}"
                    logger.warning("AI chat failed (%s): %s", provider, detail)
                    raise AiComposeError("ai_provider_error", detail)
    except AiComposeError:
        raise
    except aiohttp.ClientError as exc:
        logger.warning("AI chat network error: %s", exc)
        raise AiComposeError("ai_network_error", "Не удалось связаться с ИИ") from exc
    except Exception as exc:
        logger.exception("AI chat unexpected error")
        raise AiComposeError("ai_error", "Ошибка генерации") from exc

    text = _extract_message(body)
    if not text:
        raise AiComposeError("ai_empty", "ИИ вернул пустой ответ")
    return text


async def generate_mailing_text(
    *,
    prompt: str,
    current_text: str = "",
    segment: str = "all",
    mode: str = "write",
    client_name: str = "",
    occasion: str = "",
) -> str:
    """
    mode: write — новый текст; improve — улучшить current_text с учётом prompt.
    segment=personal — личное 1:1 сообщение (имя/повод через client_name, occasion).
    """
    user_prompt = (prompt or "").strip()
    current = (current_text or "").strip()
    if mode == "improve" and not current and not user_prompt:
        raise AiComposeError("prompt_required", "Нет текста для улучшения")
    if mode != "improve" and not user_prompt:
        raise AiComposeError("prompt_required", "Опишите, какой текст нужен")

    seg = (segment or "all").strip() or "all"
    user_content = _build_user_content(
        prompt=user_prompt,
        current=current,
        segment=seg,
        mode=mode,
        client_name=(client_name or "").strip(),
        occasion=(occasion or "").strip(),
    )
    system = PERSONAL_SYSTEM_PROMPT if seg == "personal" else SYSTEM_PROMPT
    return await _chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.7,
        max_tokens=500,
    )


def normalize_chat_messages(raw: Any) -> list[dict[str, str]]:
    """Оставляет последние user/assistant сообщения в разумных пределах."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        out.append(
            {
                "role": role,
                "content": content[:MAX_CHAT_MESSAGE_CHARS],
            }
        )
    if len(out) > MAX_CHAT_HISTORY:
        out = out[-MAX_CHAT_HISTORY:]
    return out


async def admin_assistant_reply(
    *,
    messages: list[dict[str, str]],
    context: str = "",
    intent: str = "general",
) -> str:
    """Ответ внутреннего ИИ-чата админки с CRM-контекстом."""
    history = normalize_chat_messages(messages)
    if not history or history[-1]["role"] != "user":
        raise AiComposeError("prompt_required", "Напишите сообщение")

    system = CHAT_SYSTEM_PROMPT
    intent_hints = {
        "events": "Фокус запроса: ближайшие события и тексты поздравлений.",
        "inactive": "Фокус запроса: возврат неактивных клиентов, мягкий тон.",
        "copy": "Фокус запроса: готовый текст рассылки/сообщения в блоке ```текст.",
        "customer": "Фокус запроса: конкретный клиент из CRM-контекста.",
        "stats": "Фокус запроса: краткая сводка и практический чек-лист.",
        "general": "Фокус запроса: общий рабочий вопрос сотрудника.",
    }
    system += "\n\n" + intent_hints.get(intent, intent_hints["general"])
    ctx = (context or "").strip()
    if ctx:
        system += "\n\n=== Данные CRM (актуально сейчас) ===\n" + ctx[:10000]

    return await _chat_completion(
        [{"role": "system", "content": system}, *history],
        temperature=0.5,
        max_tokens=1600,
    )


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
