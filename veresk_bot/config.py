import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        print(f"Ошибка: переменная {name} не задана. Скопируйте .env.example в .env", file=sys.stderr)
        sys.exit(1)
    return value.strip()


def _normalize_token(value: str) -> str:
    token = value.strip().strip('"').strip("'")
    if ":" not in token:
        print(
            "Ошибка: BOT_TOKEN должен быть вида 123456789:ABCdef... (получите у @BotFather)",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


BOT_TOKEN = _normalize_token(_require("BOT_TOKEN"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


FLORIST_CHAT_ID = int(os.getenv("FLORIST_CHAT_ID", "0") or "0")
FLORIST_NOTIFICATIONS_ENABLED = _env_bool("FLORIST_NOTIFICATIONS_ENABLED", False)
POSIFLORA_USERNAME = _require("POSIFLORA_USERNAME")
POSIFLORA_PASSWORD = _require("POSIFLORA_PASSWORD")
POSIFLORA_STORE_ID = _require("POSIFLORA_STORE_ID")
POSIFLORA_BASE_URL = os.getenv("POSIFLORA_BASE_URL", "https://demo.posiflora.com/api").strip()
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))  # секунды между проверками

# Mini App (HTTPS, путь /miniapp/)
_raw_miniapp = os.getenv("MINIAPP_URL", os.getenv("WEBAPP_URL", "")).strip()
MINIAPP_URL = _raw_miniapp if _raw_miniapp.endswith("/") else f"{_raw_miniapp}/" if _raw_miniapp else ""
WEBAPP_URL = MINIAPP_URL.rstrip("/")  # обратная совместимость
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "3005"))

_default_db = Path(__file__).resolve().parent / "data" / "veresk.db"
DATABASE_PATH = os.getenv("DATABASE_PATH", str(_default_db))

# Админ-панель рассылок
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
# Обратная совместимость: если ADMIN_PASSWORD пуст — берём ADMIN_TOKEN
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
if not ADMIN_PASSWORD and ADMIN_TOKEN:
    ADMIN_PASSWORD = ADMIN_TOKEN
POSIFLORA_SYNC_INTERVAL = int(os.getenv("POSIFLORA_SYNC_INTERVAL", "3600"))
MAILING_SEND_INTERVAL = float(os.getenv("MAILING_SEND_INTERVAL", "3.0"))
MAILING_BATCH_SIZE = int(os.getenv("MAILING_BATCH_SIZE", "10"))
# Текст подстановки для плейсхолдера {скидка} в рассылках (напр. «15%», «−20%»)
MAILING_DISCOUNT_TEXT = os.getenv("MAILING_DISCOUNT_TEXT", "15%").strip() or "15%"

# Telethon (userbot для рассылок с личных номеров)
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
_default_sessions = Path(__file__).resolve().parent / "data" / "sessions"
SESSIONS_DIR = os.getenv("SESSIONS_DIR", str(_default_sessions))

# MAX-бот (токен выдаёт @MasterBot в MAX)
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
# Публичный username бота в MAX (без @ /) — для кнопки open_app
MAX_BOT_USERNAME = os.getenv("MAX_BOT_USERNAME", "").strip().lstrip("@/").strip()
# Чат флориста в MAX для уведомлений об анкетах (0 — выключено)
MAX_FLORIST_CHAT_ID = int(os.getenv("MAX_FLORIST_CHAT_ID", "0") or "0")
# Публичный HTTPS-домен (админка + miniapp + API)
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL", "https://admin.veresk-flowers.ru"
).strip().rstrip("/")
# HTTPS URL webhook (порт 443), напр. https://admin.veresk-flowers.ru/api/max/webhook
# Если задан — Max шлёт события на bot-сервис; long polling у max_bot отключается.
MAX_WEBHOOK_URL = os.getenv("MAX_WEBHOOK_URL", "").strip()
# Секрет для заголовка X-Max-Bot-Api-Secret (5–256: A-Z a-z 0-9 _ -)
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "").strip()
# База MAX Bot API
MAX_API_BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru").strip()

# ИИ для текстов рассылок (OpenAI / OpenRouter / DeepSeek / YandexGPT / свой API)
# Удобнее задать в админке: Настройки → Сервисы → ИИ-редактор
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai").strip().lower() or "openai"
AI_API_KEY = os.getenv("AI_API_KEY", os.getenv("OPENAI_API_KEY", "")).strip()
AI_API_BASE = os.getenv(
    "AI_API_BASE",
    os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
).strip() or "https://api.openai.com/v1"
AI_MODEL = os.getenv("AI_MODEL", os.getenv("OPENAI_MODEL", "")).strip()
AI_FOLDER_ID = os.getenv("AI_FOLDER_ID", os.getenv("YANDEX_FOLDER_ID", "")).strip()
