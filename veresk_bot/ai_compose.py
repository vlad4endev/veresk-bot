"""
Генерация текстов рассылок через OpenAI-совместимый Chat Completions API.

Провайдеры (операторы): openai | openrouter | deepseek | yandexgpt | custom.
Ключи и параметры: сначала из Настроек (runtime), иначе из .env.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

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

PROVIDERS = ("openai", "openrouter", "deepseek", "yandexgpt", "custom")

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
        "model": "deepseek/deepseek-v4-flash-0731",
        "hint": (
            "Ключ с openrouter.ai/keys · без OpenAI/Anthropic/Google "
            "(санкции) — DeepSeek/Qwen/Mistral/Llama"
        ),
    },
    "deepseek": {
        "label": "DeepSeek",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-pro",
        "hint": "Ключ с platform.deepseek.com · deepseek-v4-pro (умная) / deepseek-v4-flash",
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

# Устаревшие ID DeepSeek → актуальные (chat/reasoner сняты после июля 2026).
DEEPSEEK_MODEL_ALIASES = {
    "deepseek-chat": "deepseek-v4-pro",
    "deepseek-reasoner": "deepseek-v4-pro",
    "deepseek-v3": "deepseek-v4-pro",
    "deepseek-v3.1": "deepseek-v4-pro",
    "deepseek-v3.2": "deepseek-v4-pro",
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

У тебя есть доступ ко всему сервису через инструменты (tools):
- lookup_customer — полное досье клиента (заметки, заказы, события, сообщения,
  анкеты TG/MAX-ботов, колесо фортуны, превью чата MAX);
- list_upcoming_events — кого поздравить;
- list_segment_customers — примеры сегментов regular/new/inactive/all;
- get_shop_overview — сводка салона, сегменты, метрики ботов;
- list_recent_campaigns — последние рассылки;
- list_fortune_plays — розыгрыши колеса.

Когда не хватает данных в блоке «Данные CRM» — сначала вызови нужный tool,
потом отвечай. Для конкретного человека почти всегда начинай с lookup_customer.

Помогаешь с:
- текстами рассылок и личных сообщений клиентам;
- кого поздравить (ДР, годовщины) и что написать;
- сегментами и идеями возврата;
- карточкой клиента: контакты, заказы, события, заметки, история переписки;
- чек-листами перед рассылкой и мягкими формулировками без давления.

Как отвечать:
1) Сразу по делу (1–2 предложения).
2) Конкретика из CRM/tools: имена, даты, цифры — только из фактов.
3) Если нужен текст сообщения — оберни его в блок:
```текст
...готовый текст...
```
4) В конце — 1–3 коротких следующих шага («что сделать в панели»).

Правила:
- Язык: русский; тон тёплый, деловой, без канцелярита и без сюсюканья.
- Не выдумывай клиентов, телефоны, суммы, скидки и акции — только из данных или запроса.
- Если данных нет или мало — честно скажи и укажи, куда смотреть: Клиенты / События / Главная / Чаты.
- Плейсхолдеры: {имя}, при необходимости {скидка} (только если скидку задал сотрудник).
- Можно: **жирный**, списки через «•» или «1.». Нельзя: markdown-таблицы, HTML, длинные простыни.
- Готовые тексты — короткие (2–5 абзацев), на «вы», без эмодзи-спама (0–2 эмодзи).
- Ссылка на заказ при необходимости: veresk.flowers
- Не раскрывай API-ключи, пароли и внутренние настройки сервера.
- Не обещай отправить сообщение сам — ты только готовишь текст и план."""

# Ключи runtime_settings для кастомных системных промптов
AI_PROMPT_MAILING_KEY = "ai_prompt_mailing"
AI_PROMPT_PERSONAL_KEY = "ai_prompt_personal"
AI_PROMPT_CHAT_KEY = "ai_prompt_chat"
MAX_SYSTEM_PROMPT_CHARS = 12000

DEFAULT_PROMPTS: dict[str, str] = {
    "mailing": SYSTEM_PROMPT,
    "personal": PERSONAL_SYSTEM_PROMPT,
    "chat": CHAT_SYSTEM_PROMPT,
}

PROMPT_SETTING_KEYS = {
    "mailing": AI_PROMPT_MAILING_KEY,
    "personal": AI_PROMPT_PERSONAL_KEY,
    "chat": AI_PROMPT_CHAT_KEY,
}

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


AI_BY_PROVIDER_KEY = "ai_by_provider"


def _mask_key(key: str) -> str:
    if len(key) <= 10:
        return "••••••••"
    return key[:4] + "…" + key[-4:]


def get_ai_provider() -> str:
    raw = runtime_settings.get("ai_provider")
    if raw and str(raw).strip() in PROVIDERS:
        return str(raw).strip()
    if AI_PROVIDER in PROVIDERS:
        return AI_PROVIDER
    return "openai"


def _legacy_provider_slot() -> dict[str, Any]:
    """Старые плоские ключи → слот активного (или env) провайдера."""
    slot: dict[str, Any] = {}
    if runtime_settings.get("ai_api_key"):
        slot["api_key"] = str(runtime_settings.get("ai_api_key")).strip()
    if runtime_settings.get("ai_model"):
        slot["model"] = str(runtime_settings.get("ai_model")).strip()
    if runtime_settings.get("ai_api_base"):
        slot["api_base"] = str(runtime_settings.get("ai_api_base")).strip().rstrip("/")
    if runtime_settings.get("ai_folder_id"):
        slot["folder_id"] = str(runtime_settings.get("ai_folder_id")).strip()
    return slot


def _preset_api_base(provider: str) -> str:
    preset = PROVIDER_PRESETS.get(provider) or PROVIDER_PRESETS["openai"]
    return str(preset["api_base"]).rstrip("/")


def _sanitize_provider_slot(pid: str, slot: dict[str, Any]) -> dict[str, Any]:
    """
    Чинит слот после миграции/переключения операторов.

    Раньше плоский ai_api_base от Yandex мог попасть в слот OpenRouter —
    тогда запросы шли на llm.api.cloud.yandex.net → Access denied by security policy.
    """
    out = dict(slot)
    if pid != "custom":
        out["api_base"] = _preset_api_base(pid)
    else:
        base = str(out.get("api_base") or "").strip().rstrip("/")
        out["api_base"] = base or _preset_api_base("custom")
    if pid != "yandexgpt":
        out.pop("folder_id", None)
    return out


def get_ai_by_provider() -> dict[str, dict[str, Any]]:
    """Словарь настроек по операторам; мигрирует legacy-ключи при первом чтении."""
    raw = runtime_settings.get(AI_BY_PROVIDER_KEY)
    data: dict[str, dict[str, Any]] = {}
    dirty = False
    if isinstance(raw, dict):
        for pid, slot in raw.items():
            if pid in PROVIDERS and isinstance(slot, dict):
                cleaned = _sanitize_provider_slot(pid, slot)
                data[pid] = cleaned
                if cleaned.get("api_base") != str(slot.get("api_base") or "").rstrip("/"):
                    dirty = True
                if pid != "yandexgpt" and "folder_id" in slot:
                    dirty = True

    # Миграция: только ключ (+ model) в слот активного провайдера.
    # api_base/folder_id чужого оператора не копируем — иначе OpenRouter
    # начинает бить в Yandex endpoint.
    legacy = _legacy_provider_slot()
    if legacy.get("api_key"):
        active = get_ai_provider()
        cur = data.get(active) or {}
        if not (cur.get("api_key") or "").strip():
            migrated: dict[str, Any] = {
                "api_key": legacy["api_key"],
                "api_base": _preset_api_base(active),
            }
            model = str(legacy.get("model") or "").strip()
            if model:
                migrated["model"] = model
            if active == "yandexgpt" and legacy.get("folder_id"):
                migrated["folder_id"] = legacy["folder_id"]
            data[active] = {**migrated, **{k: v for k, v in cur.items() if v}}
            data[active] = _sanitize_provider_slot(active, data[active])
            dirty = True

    if dirty:
        try:
            runtime_settings.set_many({AI_BY_PROVIDER_KEY: data})
        except Exception:
            logger.debug("AI by-provider migrate/sanitize failed", exc_info=True)
    return data


def get_provider_slot(provider: str | None = None) -> dict[str, Any]:
    pid = (provider or get_ai_provider()).strip().lower()
    if pid not in PROVIDERS:
        pid = get_ai_provider()
    return dict(get_ai_by_provider().get(pid) or {})


def _env_fallback_key(provider: str) -> str:
    """Ключ из .env только если активный провайдер совпадает с AI_PROVIDER."""
    if provider == (AI_PROVIDER if AI_PROVIDER in PROVIDERS else "openai"):
        return AI_API_KEY or ""
    return ""


def get_ai_api_key(provider: str | None = None) -> str:
    pid = (provider or get_ai_provider()).strip().lower()
    slot = get_provider_slot(pid)
    key = str(slot.get("api_key") or "").strip()
    if key:
        return key
    # Legacy flat (до миграции в рантайме)
    if pid == get_ai_provider():
        legacy = str(runtime_settings.get("ai_api_key") or "").strip()
        if legacy:
            return legacy
    return _env_fallback_key(pid)


def get_ai_folder_id(provider: str | None = None) -> str:
    pid = (provider or get_ai_provider()).strip().lower()
    slot = get_provider_slot(pid)
    folder = str(slot.get("folder_id") or "").strip()
    if folder:
        return folder
    if pid == get_ai_provider():
        legacy = str(runtime_settings.get("ai_folder_id") or "").strip()
        if legacy:
            return legacy
    if pid == "yandexgpt" or pid == (AI_PROVIDER if AI_PROVIDER in PROVIDERS else ""):
        return AI_FOLDER_ID or ""
    return ""


def get_ai_api_base(provider: str | None = None) -> str:
    pid = (provider or get_ai_provider()).strip().lower()
    # У известных операторов endpoint фиксирован — не берём чужой legacy base
    # (после переключения Yandex→OpenRouter иначе остаётся yandex.net).
    if pid != "custom":
        return _preset_api_base(pid if pid in PROVIDERS else "openai")

    slot = get_provider_slot(pid)
    raw = str(slot.get("api_base") or "").strip().rstrip("/")
    if raw:
        return raw
    if pid == get_ai_provider():
        legacy = str(runtime_settings.get("ai_api_base") or "").strip().rstrip("/")
        if legacy:
            return legacy
    if AI_API_BASE and pid == (AI_PROVIDER if AI_PROVIDER in PROVIDERS else "openai"):
        return AI_API_BASE.rstrip("/")
    return _preset_api_base("custom")


def get_ai_model(provider: str | None = None) -> str:
    pid = (provider or get_ai_provider()).strip().lower()
    preset = PROVIDER_PRESETS.get(pid) or PROVIDER_PRESETS["openai"]
    slot = get_provider_slot(pid)
    model = str(slot.get("model") or "").strip()
    if model:
        return model
    if pid == get_ai_provider():
        legacy = str(runtime_settings.get("ai_model") or "").strip()
        if legacy:
            return legacy
    if AI_MODEL and pid == (AI_PROVIDER if AI_PROVIDER in PROVIDERS else "openai"):
        return AI_MODEL
    return preset["model"]


def is_provider_configured(provider: str) -> bool:
    pid = (provider or "").strip().lower()
    if pid not in PROVIDERS:
        return False
    if not get_ai_api_key(pid):
        return False
    if pid == "yandexgpt" and not get_ai_folder_id(pid):
        return False
    return True


def is_ai_configured() -> bool:
    return is_provider_configured(get_ai_provider())


def resolve_model_uri(provider: str | None = None) -> str:
    """Для YandexGPT модель должна быть gpt://folder_id/name."""
    pid = (provider or get_ai_provider()).strip().lower()
    model = get_ai_model(pid)
    if pid == "deepseek":
        return DEEPSEEK_MODEL_ALIASES.get(model.lower(), model)
    if pid != "yandexgpt":
        return model
    folder = get_ai_folder_id(pid)
    if model.startswith("gpt://") or model.startswith("emb://"):
        return model
    name = model.lstrip("/")
    if folder:
        return f"gpt://{folder}/{name}"
    return model


def save_provider_settings(
    provider: str,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    model: str | None = None,
    folder_id: str | None = None,
    activate: bool = True,
) -> None:
    """Сохранить настройки одного оператора, не затирая остальные."""
    pid = (provider or "").strip().lower()
    if pid not in PROVIDERS:
        raise ValueError("invalid_provider")

    preset = PROVIDER_PRESETS.get(pid) or PROVIDER_PRESETS["openai"]
    all_slots = get_ai_by_provider()
    slot = dict(all_slots.get(pid) or {})

    if api_key is not None and str(api_key).strip():
        slot["api_key"] = str(api_key).strip()

    if model is not None:
        m = str(model).strip()
        if pid == "deepseek":
            m = DEEPSEEK_MODEL_ALIASES.get(m.lower(), m) if m else preset["model"]
        slot["model"] = m or preset["model"]
    elif not slot.get("model"):
        slot["model"] = preset["model"]

    if pid == "custom":
        if api_base is not None:
            base = str(api_base).strip().rstrip("/")
            slot["api_base"] = base or preset["api_base"]
        elif not slot.get("api_base"):
            slot["api_base"] = preset["api_base"]
    else:
        # Всегда preset — игнорируем чужой base из формы/legacy
        slot["api_base"] = preset["api_base"]

    if pid == "yandexgpt":
        if folder_id is not None and str(folder_id).strip():
            slot["folder_id"] = str(folder_id).strip()
    else:
        slot.pop("folder_id", None)

    slot = _sanitize_provider_slot(pid, slot)
    all_slots[pid] = slot
    values: dict[str, Any] = {AI_BY_PROVIDER_KEY: all_slots}
    if activate:
        values["ai_provider"] = pid
        # Зеркало в плоские ключи — совместимость со старым кодом/логами
        if slot.get("api_key"):
            values["ai_api_key"] = slot["api_key"]
        if slot.get("model"):
            values["ai_model"] = slot["model"]
        if slot.get("api_base"):
            values["ai_api_base"] = slot["api_base"]
        if pid == "yandexgpt" and slot.get("folder_id"):
            values["ai_folder_id"] = slot["folder_id"]
        else:
            # Сбрасываем плоский folder, чтобы не «прилипал» к OpenRouter/DeepSeek
            values["ai_folder_id"] = ""

    runtime_settings.set_many(values)


def clear_ai_settings() -> None:
    """Сбросить все сохранённые ключи ИИ в панели (промпты не трогаем)."""
    runtime_settings.delete_keys(
        "ai_provider",
        "ai_api_key",
        "ai_api_base",
        "ai_model",
        "ai_folder_id",
        AI_BY_PROVIDER_KEY,
    )


def _norm_prompt(text: str) -> str:
    """Нормализация для сравнения/хранения: единый \\n, без краёв."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def get_system_prompt(kind: str) -> str:
    """Системный промпт: из настроек панели или дефолт из кода."""
    key = PROMPT_SETTING_KEYS.get(kind)
    default = DEFAULT_PROMPTS.get(kind, "")
    if not key:
        return default
    custom = _norm_prompt(str(runtime_settings.get(key) or ""))
    return custom if custom else default


def prompts_public() -> dict[str, Any]:
    """Промпты для формы настроек: текущие тексты + флаги кастомизации."""
    out: dict[str, Any] = {}
    for kind, setting_key in PROMPT_SETTING_KEYS.items():
        custom = _norm_prompt(str(runtime_settings.get(setting_key) or ""))
        default = DEFAULT_PROMPTS[kind]
        out[kind] = {
            "text": custom or default,
            "customized": bool(custom),
            "default": default,
        }
    return out


def save_ai_prompts(prompts: dict[str, Any] | None) -> dict[str, str]:
    """
    Сохранить системные промпты. Пустая строка = вернуть к дефолту.
    Возвращает словарь ошибок kind -> detail (пустой = ок).
    """
    if not isinstance(prompts, dict):
        return {}
    errors: dict[str, str] = {}
    values: dict[str, Any] = {}
    clear_keys: list[str] = []
    for kind, setting_key in PROMPT_SETTING_KEYS.items():
        if kind not in prompts:
            continue
        raw = prompts.get(kind)
        text = _norm_prompt(str(raw if raw is not None else ""))
        if len(text) > MAX_SYSTEM_PROMPT_CHARS:
            errors[kind] = f"Слишком длинный промпт (макс. {MAX_SYSTEM_PROMPT_CHARS})"
            continue
        default_n = _norm_prompt(DEFAULT_PROMPTS[kind])
        if not text or text == default_n:
            clear_keys.append(setting_key)
        else:
            values[setting_key] = text
    if values:
        runtime_settings.set_many(values)
        logger.info(
            "AI prompts saved: %s",
            ", ".join(k for k, sk in PROMPT_SETTING_KEYS.items() if sk in values),
        )
    if clear_keys:
        runtime_settings.delete_keys(*clear_keys)
    return errors


def reset_ai_prompts() -> None:
    """Вернуть все системные промпты к значениям из кода."""
    runtime_settings.delete_keys(*PROMPT_SETTING_KEYS.values())


def ai_settings_public() -> dict[str, Any]:
    """Статус для админки (без полных ключей), с настройками по каждому оператору."""
    provider = get_ai_provider()
    key = get_ai_api_key(provider)
    folder = get_ai_folder_id(provider)
    slot = get_provider_slot(provider)
    from_panel = bool(str(slot.get("api_key") or "").strip()) or bool(
        runtime_settings.get("ai_api_key")
    )

    providers_out: list[dict[str, Any]] = []
    for pid, meta in PROVIDER_PRESETS.items():
        p_slot = get_provider_slot(pid)
        p_key = get_ai_api_key(pid)
        p_model = get_ai_model(pid)
        if pid == "deepseek":
            p_model = DEEPSEEK_MODEL_ALIASES.get(p_model.lower(), p_model)
        p_folder = get_ai_folder_id(pid) if pid == "yandexgpt" else ""
        configured = is_provider_configured(pid)
        providers_out.append(
            {
                "id": pid,
                "label": meta["label"],
                "api_base": get_ai_api_base(pid),
                "model": p_model,
                "hint": meta["hint"],
                "needs_folder": pid == "yandexgpt",
                "configured": configured,
                "api_key_set": bool(p_key),
                "api_key_masked": _mask_key(p_key) if p_key else None,
                "folder_id": p_folder or None,
                "from_panel": bool(str(p_slot.get("api_key") or "").strip()),
            }
        )

    return {
        "configured": is_ai_configured(),
        "provider": provider,
        "providers": providers_out,
        "api_key_set": bool(key),
        "api_key_masked": _mask_key(key) if key else None,
        "api_base": get_ai_api_base(provider),
        "model": resolve_model_uri(provider) if provider == "deepseek" else get_ai_model(provider),
        "folder_id": folder or None,
        "folder_id_set": bool(folder),
        "from_env": bool(key) and not from_panel,
        "from_panel": from_panel,
        "prompts": prompts_public(),
    }


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


async def _chat_completion_raw(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1200,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    """Сырой ответ chat/completions (включая tool_calls)."""
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
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    # DeepSeek V4: thinking по умолчанию включён и съедает max_tokens —
    # ответ обрывается. Для админки отключаем CoT, весь лимит идёт в текст.
    if provider == "deepseek":
        payload["thinking"] = {"type": "disabled"}

    headers = _request_headers(provider, api_key)

    timeout_sec = 120 if provider == "deepseek" else 90
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    detail = _extract_api_error(body) or f"HTTP {resp.status}"
                    logger.warning(
                        "AI chat failed (%s / %s → %s): %s",
                        provider,
                        resolve_model_uri(),
                        url,
                        detail,
                    )
                    low = detail.lower()
                    if "access denied by security policy" in low:
                        host = urlparse(url).netloc or "?"
                        detail = (
                            f"Отказ доступа ({host}). "
                            "Если видите yandex.net при выбранном OpenRouter — "
                            "на сервере старый код: сделайте ./deploy.sh. "
                            "Иначе проверьте ключ/кредиты OpenRouter или переключитесь на DeepSeek."
                        )
                    elif provider == "openrouter" and (
                        "user not found" in low or resp.status == 401
                    ):
                        detail = (
                            "OpenRouter: ключ недействителен (User not found). "
                            "Создайте новый ключ на openrouter.ai/keys и вставьте в настройки. "
                            "Модель: deepseek/… — не openai/anthropic/google (санкции)."
                        )
                    elif provider == "openrouter" and (
                        "not available" in low
                        or "sanction" in low
                        or "region" in low
                        or "openai/" in (resolve_model_uri() or "").lower()
                        or "anthropic/" in (resolve_model_uri() or "").lower()
                        or "google/" in (resolve_model_uri() or "").lower()
                    ) and resp.status in (400, 403, 404):
                        detail = (
                            f"{detail} · В вашем регионе OpenRouter не даёт OpenAI/Anthropic/Google. "
                            "Поставьте deepseek/deepseek-v4-flash-0731 (или Qwen/Mistral) и сохраните."
                        )
                    raise AiComposeError("ai_provider_error", detail)
    except AiComposeError:
        raise
    except aiohttp.ClientError as exc:
        logger.warning("AI chat network error: %s", exc)
        raise AiComposeError("ai_network_error", "Не удалось связаться с ИИ") from exc
    except Exception as exc:
        logger.exception("AI chat unexpected error")
        raise AiComposeError("ai_error", "Ошибка генерации") from exc

    if not isinstance(body, dict):
        raise AiComposeError("ai_error", "Некорректный ответ ИИ")
    return body


async def _chat_completion(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.7,
    max_tokens: int = 1200,
) -> str:
    body = await _chat_completion_raw(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    text = _extract_message(body)
    if not text:
        raise AiComposeError("ai_empty", "ИИ вернул пустой ответ")

    finish = ""
    try:
        finish = str(((body.get("choices") or [{}])[0] or {}).get("finish_reason") or "")
    except Exception:
        finish = ""
    if finish == "length":
        logger.warning(
            "AI reply truncated by max_tokens=%s (%s / %s)",
            max_tokens,
            get_ai_provider(),
            resolve_model_uri(),
        )

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
    if seg != "personal":
        try:
            from mailing_db import get_active_discount

            disc = await get_active_discount(segment=seg)
        except Exception:
            disc = None
        if disc:
            extra = ""
            if disc.get("text"):
                extra = f"Актуальная скидка для этой аудитории: {disc['text']}."
            kind = str(disc.get("promo_type") or "")
            if kind in ("welcome", "new", "channel_subscribers_new") or disc.get("source") in (
                "welcome",
                "new",
                "channel_subscribers_new",
            ):
                extra += (
                    " Это акция для новых клиентов — "
                    "не подставляй другую скидку."
                )
            elif kind in ("reactivation", "inactive"):
                extra += " Это акция для клиентов, которые давно не заказывали."
            tpl = str(disc.get("message_template") or "").strip()
            if tpl:
                extra += (
                    " Шаблон сообщения из этой акции (можно опереться, сохрани {имя} и {скидка}):\n"
                    + tpl
                )
            extra += (
                " Если упоминаешь скидку, используй плейсхолдер {скидка}, "
                "не пиши процент цифрами."
            )
            user_content += "\n" + extra
    system = get_system_prompt("personal" if seg == "personal" else "mailing")
    return await _chat_completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        temperature=0.7,
        max_tokens=1200,
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
    enable_tools: bool = True,
) -> str:
    """Ответ внутреннего ИИ-чата админки с CRM-контекстом и tool-calling."""
    history = normalize_chat_messages(messages)
    if not history or history[-1]["role"] != "user":
        raise AiComposeError("prompt_required", "Напишите сообщение")

    system = get_system_prompt("chat")
    intent_hints = {
        "events": "Фокус запроса: ближайшие события и тексты поздравлений. При необходимости вызови list_upcoming_events.",
        "inactive": "Фокус запроса: возврат неактивных клиентов, мягкий тон. Можно list_segment_customers(segment=inactive).",
        "copy": "Фокус запроса: готовый текст рассылки/сообщения в блоке ```текст.",
        "customer": "Фокус запроса: конкретный клиент — вызови lookup_customer, если досье неполное.",
        "stats": "Фокус запроса: краткая сводка — get_shop_overview и практический чек-лист.",
        "general": "Фокус запроса: общий рабочий вопрос сотрудника. Используй tools при нехватке фактов.",
    }
    system += "\n\n" + intent_hints.get(intent, intent_hints["general"])
    ctx = (context or "").strip()
    if ctx:
        system += "\n\n=== Данные CRM (актуально сейчас) ===\n" + ctx[:14000]

    convo: list[dict[str, Any]] = [{"role": "system", "content": system}, *history]

    from ai_agent import (
        AGENT_TOOLS,
        MAX_TOOL_ROUNDS,
        execute_tool,
        provider_supports_tools,
    )

    use_tools = enable_tools and provider_supports_tools(get_ai_provider())
    if not use_tools:
        return await _chat_completion(convo, temperature=0.5, max_tokens=4096)

    for _round in range(MAX_TOOL_ROUNDS):
        body = await _chat_completion_raw(
            convo,
            temperature=0.4,
            max_tokens=4096,
            tools=AGENT_TOOLS,
            tool_choice="auto",
        )
        choice = ((body.get("choices") or [{}])[0] or {})
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        finish = str(choice.get("finish_reason") or "")

        if tool_calls and finish in ("tool_calls", "function_call", ""):
            # Сохраняем ответ ассистента с tool_calls
            content = msg.get("content")
            if content is None:
                content = ""
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
            convo.append(assistant_msg)
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = str(fn.get("name") or "").strip()
                raw_args = fn.get("arguments") or "{}"
                call_id = str(call.get("id") or name or "tool")
                result = await execute_tool(name, raw_args)
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": result,
                    }
                )
            continue

        text = _extract_message(body)
        if text:
            if finish == "length":
                logger.warning(
                    "AI reply truncated by max_tokens (tools on, %s)",
                    resolve_model_uri(),
                )
            return text
        # Пустой текст без tool_calls — пробуем ещё раз без tools
        break

    # Финальный ответ без tools (после цикла или если модель молчала)
    return await _chat_completion(convo, temperature=0.5, max_tokens=4096)


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
