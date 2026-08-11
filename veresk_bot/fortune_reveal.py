"""Раскрытие sealed-билета колеса после анкеты."""

from __future__ import annotations

import logging
from typing import Any

from fortune_wheel import (
    format_customer_prize_note,
    format_prize_congrats_message,
    is_sealed_play,
    play_status,
)
from mailing_db import (
    append_customer_notes,
    claim_fortune_play_notified,
    get_customer_by_max_user_id,
    get_customer_by_phone,
    get_customer_by_tg_user_id,
    get_fortune_play,
    reveal_fortune_play,
    set_customer_max_by_phone,
    set_customer_tg_by_phone,
)

logger = logging.getLogger(__name__)


async def _link_customer(
    channel: str, uid: str, profile: dict[str, Any] | None
) -> dict[str, Any] | None:
    customer = None
    try:
        if channel == "telegram":
            customer = await get_customer_by_tg_user_id(int(uid))
        else:
            customer = await get_customer_by_max_user_id(int(uid))
    except Exception:
        customer = None

    phone = str((profile or {}).get("phone") or "").strip()
    if not customer and phone:
        try:
            if channel == "telegram":
                await set_customer_tg_by_phone(phone, int(uid))
                customer = await get_customer_by_tg_user_id(int(uid))
            else:
                await set_customer_max_by_phone(phone, int(uid))
                customer = await get_customer_by_max_user_id(int(uid))
            if not customer:
                customer = await get_customer_by_phone(phone)
        except Exception:
            logger.debug("reveal: customer link by phone failed", exc_info=True)
    return customer


async def reveal_sealed_ticket_after_survey(
    *,
    channel: str,
    user_id: str | int,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Если есть sealed-билет — раскрыть, записать в CRM, вернуть play.

    Поздравление в мессенджер отправляет вызывающий код (у бота есть сессия).
    """
    ch = str(channel or "").strip().lower()
    uid = str(user_id or "").strip()
    if ch not in ("telegram", "max") or not uid:
        return None

    play = await get_fortune_play(ch, uid)
    if not play or not is_sealed_play(play):
        return None

    customer = await _link_customer(ch, uid, profile)
    customer_id = (
        int(customer["id"])
        if customer and customer.get("id") is not None
        else None
    )
    revealed = await reveal_fortune_play(ch, uid, customer_id=customer_id)
    if not revealed:
        # Гонка / уже раскрыли
        play2 = await get_fortune_play(ch, uid)
        if play2 and play_status(play2) == "revealed":
            return play2
        return None

    prize_label = str(revealed.get("prize_label") or "")
    if customer_id is not None and prize_label:
        try:
            note = format_customer_prize_note(
                channel=ch,
                prize_label=prize_label,
                discount_pct=revealed.get("discount_pct"),
                created_at=str(
                    revealed.get("revealed_at") or revealed.get("created_at") or ""
                ),
            )
            await append_customer_notes(customer_id, note)
            pf_id = str((customer or {}).get("posiflora_id") or "").strip()
            if pf_id:
                try:
                    from posiflora import append_customer_prize_note_to_posiflora

                    await append_customer_prize_note_to_posiflora(pf_id, note)
                except Exception:
                    logger.exception(
                        "Не удалось записать раскрытый приз в Posiflora #%s", pf_id
                    )
        except Exception:
            logger.exception(
                "Не удалось записать раскрытый приз в карточку id=%s", customer_id
            )

    # Пометить notified заранее — иначе fallback /notify может продублировать
    try:
        await claim_fortune_play_notified(ch, uid)
    except Exception:
        logger.debug("reveal: claim notified failed", exc_info=True)

    return revealed


def congrats_text_for_play(play: dict[str, Any], *, markdown: bool = True) -> str:
    return format_prize_congrats_message(
        prize_label=str(play.get("prize_label") or ""),
        discount_pct=play.get("discount_pct"),
        markdown=markdown,
    )
