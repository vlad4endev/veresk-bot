import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        print(f"Ошибка: переменная {name} не задана. Скопируйте .env.example в .env", file=sys.stderr)
        sys.exit(1)
    return value.strip()


BOT_TOKEN = _require("BOT_TOKEN")
FLORIST_CHAT_ID = int(os.getenv("FLORIST_CHAT_ID", "0"))
POSIFLORA_USERNAME = _require("POSIFLORA_USERNAME")
POSIFLORA_PASSWORD = _require("POSIFLORA_PASSWORD")
POSIFLORA_STORE_ID = _require("POSIFLORA_STORE_ID")
POSIFLORA_BASE_URL = os.getenv("POSIFLORA_BASE_URL", "https://demo.posiflora.com/api").strip()
