"""
Проверка подписи Telegram Web App initData.
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_sec: int = 86400) -> dict | None:
    if not init_data or not bot_token:
        return None

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    auth_date = parsed.get("auth_date")
    if auth_date:
        try:
            if time.time() - int(auth_date) > max_age_sec:
                return None
        except ValueError:
            return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        return None

    user_raw = parsed.get("user")
    if user_raw:
        try:
            parsed["user"] = json.loads(user_raw)
        except json.JSONDecodeError:
            return None

    return parsed


def tg_user_id_from_init(init_data: str, bot_token: str) -> int | None:
    validated = validate_init_data(init_data, bot_token)
    if not validated:
        return None
    user = validated.get("user")
    if isinstance(user, dict) and "id" in user:
        return int(user["id"])
    return None
