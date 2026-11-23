"""Ключ Posiflora и текст ошибки 403 вместо сырого JSON:API."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="veresk-pf-")
os.environ["BOT_TOKEN"] = "123456789:AAE_test_token"
os.environ["POSIFLORA_USERNAME"] = "user"
os.environ["POSIFLORA_PASSWORD"] = "pass"
os.environ["POSIFLORA_STORE_ID"] = "1"
os.environ["POSIFLORA_API_KEY"] = ""
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "veresk.db")

from posiflora import (  # noqa: E402
    build_posiflora_headers,
    clear_posiflora_api_key,
    explain_posiflora_error,
    normalize_posiflora_api_key,
    posiflora_api_key,
    posiflora_settings_public,
    save_posiflora_api_key,
)

_BODY = json.dumps(
    {
        "errors": [
            {
                "id": "2036107973",
                "status": "403",
                "code": "1da6c9fa-6024-477e-830d-ff1c91866da5",
                "title": "Admin panel credentials are not supported",
            }
        ]
    }
)


class PosifloraAuthTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_posiflora_api_key()

    def test_403_without_key_is_a_short_hint(self) -> None:
        text = explain_posiflora_error(403, _BODY, has_api_key=False)
        self.assertIn("API-ключ", text)
        self.assertNotIn("2036107973", text)
        self.assertNotIn("1da6c9fa", text)

    def test_403_with_key_points_at_permissions(self) -> None:
        text = explain_posiflora_error(403, _BODY, has_api_key=True)
        self.assertIn("без права", text)
        self.assertNotIn("вставьте его ниже", text)

    def test_other_error_keeps_the_title(self) -> None:
        body = json.dumps({"errors": [{"title": "Store is closed", "code": "x"}]})
        text = explain_posiflora_error(400, body, has_api_key=False)
        self.assertEqual(text, "HTTP 400: Store is closed")

    def test_key_header_replaces_bearer(self) -> None:
        headers = build_posiflora_headers("pf-live-key-1234", "session-token")
        self.assertEqual(headers["X-Api-Key"], "pf-live-key-1234")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["Accept"], "application/vnd.api+json")

    def test_bearer_when_no_key(self) -> None:
        headers = build_posiflora_headers("", "session-token")
        self.assertEqual(headers["Authorization"], "Bearer session-token")
        self.assertNotIn("X-Api-Key", headers)

    def test_saved_key_is_masked_and_preferred(self) -> None:
        secret = "pf-live-key-1234"
        save_posiflora_api_key(secret)
        public = posiflora_settings_public()
        self.assertEqual(posiflora_api_key(), secret)
        self.assertEqual(public["source"], "panel")
        self.assertTrue(public["api_key_set"])
        self.assertNotIn(secret, json.dumps(public))
        self.assertTrue(public["api_key_masked"].startswith("pf-l"))
        self.assertTrue(public["api_key_masked"].endswith("1234"))

    def test_rejects_key_with_spaces(self) -> None:
        with self.assertRaises(ValueError):
            normalize_posiflora_api_key("short key")
        with self.assertRaises(ValueError):
            normalize_posiflora_api_key("short")


if __name__ == "__main__":
    unittest.main()
