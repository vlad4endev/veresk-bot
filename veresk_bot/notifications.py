from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router

import app_context
from order_store import update_order_status
from webapp_buttons import tracking_keyboard
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

logger = logging.getLogger(__name__)
router = Router()

PARSE_MODE = "Markdown"


def florist_keyboard(
    order_id: str, client_tg_id: int, phone: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять заказ",
                    callback_data=f"order:accept:{order_id}:{client_tg_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📞 Позвонить {phone}",
                    url=f"tel:{phone}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Написать в Telegram",
                    url=f"tg://user?id={client_tg_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"order:decline:{order_id}:{client_tg_id}",
                )
            ],
        ]
    )


def florist_message(
    data: dict, order_id: str, client_tg_id: int, posiflora_ok: bool = True
) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = (
        "🌸 *Новый заказ — Veresk*\n"
        f"_trail of happiness · {now}_\n\n"
        "┌─────────────────────\n"
        f"│ 👤 Клиент:      *{data['name']}*\n"
        f"│ 📞 Телефон:     `{data.get('phone', '—')}`\n"
        f"│ 📱 Telegram:    [написать](tg://user?id={client_tg_id})\n"
        f"│ 📅 Дата:        *{data['date']}*\n"
        f"│ 🎁 Получатель:  *{data['recipient']}*\n"
        f"│ 🎉 Повод:       *{data['occasion']}*\n"
        f"│ 💜 Кто:         *{data['relation']}*\n"
        f"│ 💰 Бюджет:      *{data['budget']}*\n"
        "└─────────────────────\n\n"
        f"🆔 Заказ Posiflora: `#{order_id}`"
    )
    if not posiflora_ok:
        text += (
            "\n\n⚠️ *Заказ НЕ создан в Posiflora\\!*\n"
            "Создайте вручную по данным выше\\."
        )
    return text


async def notify_florist(
    bot: Bot,
    florist_chat_id: int,
    data: dict,
    order_id: str,
    client_tg_id: int,
    posiflora_ok: bool = True,
) -> None:
    if not florist_chat_id:
        logger.warning("FLORIST_CHAT_ID не задан — уведомление флористу пропущено")
        return
    phone = data.get("phone", "—")
    try:
        await bot.send_message(
            chat_id=florist_chat_id,
            text=florist_message(
                data, order_id, client_tg_id, posiflora_ok=posiflora_ok
            ),
            parse_mode=PARSE_MODE,
            reply_markup=florist_keyboard(order_id, client_tg_id, phone),
        )
        logger.info("🔔 Флорист уведомлён о заказе #%s", order_id)
    except Exception:
        logger.exception(
            "❌ Не удалось уведомить флориста chat_id=%s", florist_chat_id
        )


def _parse_order_callback(callback: CallbackQuery) -> tuple[str, int] | None:
    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        return None
    _, _, order_id, client_tg_id_str = parts
    try:
        return order_id, int(client_tg_id_str)
    except ValueError:
        return None


@router.callback_query(F.data.startswith("order:accept:"))
async def on_accept(callback: CallbackQuery, bot: Bot) -> None:
    parsed = _parse_order_callback(callback)
    if not parsed:
        await callback.answer("Некорректные данные заказа", show_alert=True)
        return

    order_id, client_tg_id = parsed

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"✅ *Заказ #{order_id} принят*\nКлиент получит уведомление.",
            parse_mode=PARSE_MODE,
        )

    if app_context.redis_client:
        await update_order_status(app_context.redis_client, order_id, "confirmed")

    try:
        await bot.send_message(
            chat_id=client_tg_id,
            text=(
                "🌷 *Ваш заказ подтверждён!*\n\n"
                "Флорист уже приступает к созданию вашего букета.\n"
                "Статус можно смотреть в трекере заказа.\n\n"
                "_Veresk · trail of happiness_"
            ),
            parse_mode=PARSE_MODE,
            reply_markup=tracking_keyboard(order_id),
        )
    except Exception:
        logger.exception(
            "❌ Не удалось уведомить клиента client_tg_id=%s", client_tg_id
        )

    await callback.answer("Заказ принят ✅")


@router.callback_query(F.data.startswith("order:decline:"))
async def on_decline(callback: CallbackQuery, bot: Bot) -> None:
    parsed = _parse_order_callback(callback)
    if not parsed:
        await callback.answer("Некорректные данные заказа", show_alert=True)
        return

    order_id, client_tg_id = parsed

    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"❌ *Заказ #{order_id} отклонён.*\nКлиент получит уведомление.",
            parse_mode=PARSE_MODE,
        )

    if app_context.redis_client:
        await update_order_status(app_context.redis_client, order_id, "cancelled")

    try:
        await bot.send_message(
            chat_id=client_tg_id,
            text=(
                "😔 *К сожалению, мы не можем выполнить этот заказ*\n\n"
                "Возможно, выбранная дата или бюджет не совпали с доступностью.\n\n"
                "Пожалуйста, свяжитесь с нами напрямую:\n"
                "📞 *+7 994 004 99 83*\n"
                "💬 @verba_flowers\n\n"
                "_Veresk · trail of happiness_"
            ),
            parse_mode=PARSE_MODE,
            reply_markup=tracking_keyboard(order_id),
        )
    except Exception:
        logger.exception(
            "❌ Не удалось уведомить клиента client_tg_id=%s", client_tg_id
        )

    await callback.answer("Заказ отклонён")
