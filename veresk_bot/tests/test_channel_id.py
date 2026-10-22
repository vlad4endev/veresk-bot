"""Нормализация id Telegram-канала: short ↔ −100… и матчинг событий."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    _channel_full_id,
    _channel_limitation_note,
    _is_admin_member_status,
    _is_chat_admin_required,
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

    def test_channel_full_id_from_broadcast_entity(self) -> None:
        entity = SimpleNamespace(id=555666777, broadcast=True, megagroup=False)
        self.assertEqual(_channel_full_id(entity), -100555666777)

    def test_admin_status_helper(self) -> None:
        self.assertTrue(_is_admin_member_status("administrator"))
        self.assertTrue(_is_admin_member_status("creator"))
        self.assertFalse(_is_admin_member_status("member"))
        self.assertFalse(_is_admin_member_status("left"))
        self.assertFalse(_is_admin_member_status(None))


class ChannelMatchAndRememberTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime_settings.delete_keys(
            "telegram_channel_id",
            "telegram_channel_username",
            "telegram_channel_title",
            "telegram_channel_participants",
        )

    def test_save_normalizes_short_id(self) -> None:
        cfg = save_channel_config(channel_id=555666777, channel_username="flowers")
        self.assertEqual(cfg["channel_id"], -100555666777)
        self.assertEqual(cfg["channel_username"], "flowers")
        self.assertTrue(cfg["configured"])

    def test_matches_short_config_against_bot_api_event(self) -> None:
        runtime_settings.set_many({"telegram_channel_id": 555666777})
        self.assertTrue(matches_configured_channel(-100555666777, "flowers"))
        cfg = get_channel_config()
        self.assertEqual(cfg["channel_id"], -100555666777)

    def test_matches_by_username_when_id_stale(self) -> None:
        save_channel_config(channel_id=-100111, channel_username="veresk")
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

    def test_detect_chat_admin_required_error(self) -> None:
        class ChatAdminRequiredError(Exception):
            pass

        self.assertTrue(_is_chat_admin_required(ChatAdminRequiredError("x")))
        self.assertTrue(
            _is_chat_admin_required(
                RuntimeError(
                    "Chat admin privileges are required to do that in the "
                    "specified chat (caused by GetParticipantsRequest)"
                )
            )
        )
        self.assertFalse(_is_chat_admin_required(RuntimeError("flood wait")))

    def test_migrates_short_channel_ids_in_db(self) -> None:
        from channel_subscriptions import (
            _connect,
            _ensure_channel_subscriptions_schema,
            _normalize_stored_subscription_channel_ids,
        )

        with _connect() as db:
            _ensure_channel_subscriptions_schema(db)
            db.execute(
                """
                INSERT INTO channel_subscriptions (
                    tg_user_id, channel_id, status, username, first_name, last_name,
                    first_seen_at, updated_at, source
                ) VALUES (1, 424242, 'member', '', '', '',
                          '2026-01-01T00:00:00', '2026-01-01T00:00:00', 'sync')
                """
            )
            n = _normalize_stored_subscription_channel_ids(db)
            db.commit()
            row = db.execute(
                "SELECT channel_id FROM channel_subscriptions WHERE tg_user_id = 1"
            ).fetchone()
        self.assertEqual(n, 1)
        self.assertEqual(int(row[0]), -100424242)

    def test_bot_api_admin_check_uses_username_then_id(self) -> None:
        from channel_subscriptions import _bot_is_admin_of_channel

        calls: list[object] = []

        async def fake_status(chat_id, user_id):
            calls.append((chat_id, user_id))
            if str(chat_id).startswith("@"):
                return "administrator"
            return "left"

        async def _run() -> bool | None:
            with patch(
                "channel_subscriptions._bot_api_member_status",
                new=AsyncMock(side_effect=fake_status),
            ):
                return await _bot_is_admin_of_channel(
                    {
                        "channel_id": -100555666777,
                        "channel_username": "veresk_flowers",
                    },
                    123456789,
                )

        self.assertTrue(asyncio.run(_run()))
        self.assertEqual(calls[0][0], "@veresk_flowers")

    def test_save_participants_count(self) -> None:
        cfg = save_channel_config(channel_id=-100555, participants_count=126)
        self.assertEqual(cfg["participants_count"], 126)
        self.assertIn("~126", _channel_limitation_note(participants_count=126, tracked=0))
        note = _channel_limitation_note(
            participants_count=126, tracked=3, list_mode="admins"
        )
        self.assertIn("ограничение API", note)
        self.assertIn("администраторы (3)", note)
        self.assertNotIn("не админ канала", note)


if __name__ == "__main__":
    unittest.main()
