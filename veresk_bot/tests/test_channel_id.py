"""Нормализация id Telegram-канала: short ↔ −100… и матчинг событий."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="veresk-channel-")
os.environ["BOT_TOKEN"] = "123456789:AAE_test_token"
os.environ["POSIFLORA_USERNAME"] = "user"
os.environ["POSIFLORA_PASSWORD"] = "pass"
os.environ["POSIFLORA_STORE_ID"] = "1"
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "veresk.db")
os.environ["TELEGRAM_CHANNEL_ID"] = "0"
os.environ["TELEGRAM_CHANNEL_USERNAME"] = ""

import runtime_settings  # noqa: E402
from channel_subscriptions import (  # noqa: E402
    get_channel_config,
    matches_configured_channel,
    normalize_telegram_channel_id,
    remember_bot_admin_channel,
    same_telegram_channel_id,
    save_channel_config,
)


class ChannelIdNormalizeTests(unittest.TestCase):
    def test_short_to_full(self) -> None:
        self.assertEqual(normalize_telegram_channel_id(1234567890), -1001234567890)
        self.assertEqual(normalize_telegram_channel_id("1234567890"), -1001234567890)

    def test_full_stays(self) -> None:
        self.assertEqual(normalize_telegram_channel_id(-1001234567890), -1001234567890)

    def test_zero_and_bad(self) -> None:
        self.assertEqual(normalize_telegram_channel_id(0), 0)
        self.assertEqual(normalize_telegram_channel_id(None), 0)
        self.assertEqual(normalize_telegram_channel_id(""), 0)
        self.assertEqual(normalize_telegram_channel_id("abc"), 0)

    def test_same_short_and_full(self) -> None:
        self.assertTrue(same_telegram_channel_id(12345, -10012345))
        self.assertTrue(same_telegram_channel_id(-10012345, -10012345))
        self.assertFalse(same_telegram_channel_id(12345, -10099999))
        self.assertFalse(same_telegram_channel_id(0, -10012345))


class ChannelMatchAndRememberTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_settings.delete_keys(
            "telegram_channel_id",
            "telegram_channel_username",
            "telegram_channel_title",
        )

    def test_save_normalizes_short_id(self) -> None:
        cfg = save_channel_config(channel_id=555666777, channel_username="flowers")
        self.assertEqual(cfg["channel_id"], -100555666777)
        self.assertEqual(cfg["channel_username"], "flowers")
        self.assertTrue(cfg["configured"])

    def test_matches_short_config_against_bot_api_event(self) -> None:
        # В runtime намеренно короткий id — как после бага / ручного ввода
        runtime_settings.set_many({"telegram_channel_id": 555666777})
        self.assertTrue(matches_configured_channel(-100555666777, "flowers"))
        # get_channel_config должен вылечить id в −100…
        cfg = get_channel_config()
        self.assertEqual(cfg["channel_id"], -100555666777)

    def test_matches_by_username_when_id_stale(self) -> None:
        save_channel_config(channel_id=-100111, channel_username="veresk")
        # Событие с другого «сырого» id, но тот же @username — принимаем
        self.assertTrue(matches_configured_channel(-100999, "veresk"))
        self.assertFalse(matches_configured_channel(-100999, "other"))

    def test_remember_upgrades_short_id_instead_of_skip(self) -> None:
        runtime_settings.set_many(
            {
                "telegram_channel_id": 424242,
                "telegram_channel_username": "shop",
            }
        )
        result = remember_bot_admin_channel(
            channel_id=-100424242,
            channel_username="shop",
            channel_title="Shop channel",
            force=False,
        )
        self.assertFalse(result.get("skipped"))
        self.assertTrue(result.get("updated"))
        self.assertEqual(result["channel_id"], -100424242)
        self.assertEqual(result["channel_title"], "Shop channel")

    def test_remember_skips_other_channel(self) -> None:
        save_channel_config(channel_id=-100111, channel_username="a")
        result = remember_bot_admin_channel(
            channel_id=-100222,
            channel_username="b",
            force=False,
        )
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "other_channel_configured")


if __name__ == "__main__":
    unittest.main()
