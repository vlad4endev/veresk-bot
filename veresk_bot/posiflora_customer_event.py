#!/usr/bin/env python3
"""Ручной тест синхронизации анкеты с Posiflora."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from posiflora import PosifloraAPIError, sync_survey_profile_to_posiflora

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    profile = {
        "name": os.getenv("CUSTOMER_FIRST_NAME", "Тест Клиент").strip(),
        "phone": os.getenv("CUSTOMER_PHONE", "").strip(),
        "budget": os.getenv("CUSTOMER_BUDGET", "до 10 000 ₽").strip(),
        "source": os.getenv("CUSTOMER_SOURCE", "Telegram").strip(),
        "events": [
            {
                "date": os.getenv("EVENT_DATE_1", "15.06.2026"),
                "occasion": os.getenv("EVENT_OCCASION_1", "День рождения"),
                "relation": os.getenv("EVENT_RELATION_1", "Мама"),
            }
        ],
    }
    if not profile["phone"]:
        logger.error("Укажите CUSTOMER_PHONE в .env")
        sys.exit(1)

    result = await sync_survey_profile_to_posiflora(
        profile,
        telegram_id=int(os.getenv("TEST_TELEGRAM_ID", "0")),
    )
    logger.info("Результат: %s", result)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PosifloraAPIError:
        sys.exit(1)
