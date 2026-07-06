import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app_context import set_redis
from config import BOT_TOKEN, FLORIST_CHAT_ID, MINIAPP_URL, WEBAPP_HOST, WEBAPP_PORT

try:
    from aiogram.fsm.storage.redis import RedisStorage

    REDIS_URL = os.getenv("REDIS_URL")
    storage = RedisStorage.from_url(REDIS_URL) if REDIS_URL else MemoryStorage()
except ImportError:
    storage = MemoryStorage()

from client_db import get_orders_for_client, save_client_profile
from notifications import notify_florist_profile
from notifications import router as notifications_router
from order_status import status_meta
from poller import start_polling
from order_store import get_active_order_by_tg
from webapp_buttons import (
    launch_keyboard,
    orders_list_keyboard,
    reset_bot_menu_button,
)
from webapp_server import start_webapp_server

logger = logging.getLogger(__name__)

PARSE_MODE = "Markdown"


class ProfileForm(StatesGroup):
    name = State()
    phone = State()
    important_date = State()
    occasion = State()
    relation = State()
    add_more_dates = State()
    budget = State()
    source = State()


FORM_STEPS = 7

CUSTOM_OPTION = "✏️ Свой вариант"
ADD_MORE_YES = "➕ Добавить ещё дату"
ADD_MORE_NO = "✅ Больше нет"

OCCASION_PRESETS = {"День рождения 🎂", "Годовщина 💍"}
RELATION_PRESETS = {"Девушка", "Супруга", "Мама", "Дочь", "Коллега"}
BUDGET_PRESETS = {"до 5 000 ₽", "до 10 000 ₽", "до 15 000 ₽", "более 15 000 ₽"}
SOURCE_PRESETS = {
    "Instagram",
    "Рекомендация",
    "Google / поиск",
    "Telegram",
    "Увидел вывеску",
}

def progress(step: int, total: int = FORM_STEPS) -> str:
    return "🟣" * step + "⚪️" * (total - step) + f"  {step}/{total}"


def _choice_keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text=text) for text in row] for row in rows]
    keyboard.append([KeyboardButton(text=CUSTOM_OPTION)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def _format_date(day) -> str:
    return day.strftime("%d.%m.%Y")


def resolve_important_date(text: str) -> str | None:
    """Проверяет ввод и возвращает дату в формате ДД.ММ.ГГГГ."""
    raw = text.strip()
    if not raw:
        return None

    today = datetime.now().date()
    aliases = {"сегодня": 0, "завтра": 1}
    alias = aliases.get(raw.lower())
    if alias is not None:
        return _format_date(today + timedelta(days=alias))

    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", raw):
        try:
            datetime.strptime(raw, "%d.%m.%Y")
        except ValueError:
            return None
        return raw

    return None


def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Поделиться номером", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_occasion() -> ReplyKeyboardMarkup:
    return _choice_keyboard([["День рождения 🎂", "Годовщина 💍"]])


def kb_relation() -> ReplyKeyboardMarkup:
    return _choice_keyboard(
        [
            ["Девушка", "Супруга"],
            ["Мама", "Дочь"],
            ["Коллега"],
        ]
    )


def kb_budget() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="до 5 000 ₽"), KeyboardButton(text="до 10 000 ₽")],
            [KeyboardButton(text="до 15 000 ₽"), KeyboardButton(text="более 15 000 ₽")],
        ],
        resize_keyboard=True,
    )


def kb_source() -> ReplyKeyboardMarkup:
    return _choice_keyboard(
        [
            ["Instagram", "Рекомендация"],
            ["Google / поиск", "Telegram"],
            ["Увидел вывеску"],
        ]
    )


def kb_add_more_dates() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADD_MORE_YES)],
            [KeyboardButton(text=ADD_MORE_NO)],
        ],
        resize_keyboard=True,
    )


def _format_events_lines(events: list[dict]) -> str:
    lines = []
    for i, event in enumerate(events, start=1):
        lines.append(
            f"│ {i}. 📅 *{event['date']}* · {event['occasion']} · {event['relation']}"
        )
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# ВРЕМЕННО ОТКЛЮЧЕНО — старый сценарий заказа букета
# Раскомментировать когда вернёмся к нему
# ═══════════════════════════════════════════════════════════
#
# class OrderForm(StatesGroup):
#     name = State()
#     phone = State()
#     date = State()
#     custom_date = State()
#     recipient = State()
#     occasion = State()
#     custom_occasion = State()
#     relation = State()
#     custom_relation = State()
#     budget = State()
#
#
# DATE_OPTIONS = {
#     "Сегодня",
#     "Завтра",
#     "Через 2–3 дня",
#     "Через неделю",
#     "Через 2 недели",
#     "Другая дата",
# }
#
# OCCASION_OPTIONS = {
#     "День рождения 🎂",
#     "Годовщина 💍",
#     "Свидание 💋",
#     "Просто так 🌷",
#     "Выздоровление 🤍",
#     "Другое",
# }
#
# RELATION_OPTIONS = {
#     "Девушка / Жена",
#     "Мама",
#     "Дочь",
#     "Подруга",
#     "Коллега",
#     "Другое",
# }
#
# BUDGET_OPTIONS = {
#     "до 5 000 ₽",
#     "до 10 000 ₽",
#     "до 15 000 ₽",
#     "от 15 000 ₽",
# }
#
#
# def progress(step: int, total: int = 7) -> str:
#     return "🟣" * step + "⚪️" * (total - step) + f"  {step}/{total}"
#
#
# async def form_progress(state: FSMContext, logical_step: int) -> str:
#     data = await state.get_data()
#     if data.get("returning"):
#         return progress(max(1, logical_step - 2), 5)
#     return progress(logical_step, 7)
#
#
# def kb_date() -> ReplyKeyboardMarkup:
#     return ReplyKeyboardMarkup(
#         keyboard=[
#             [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
#             [
#                 KeyboardButton(text="Через 2–3 дня"),
#                 KeyboardButton(text="Через неделю"),
#             ],
#             [
#                 KeyboardButton(text="Через 2 недели"),
#                 KeyboardButton(text="Другая дата"),
#             ],
#         ],
#         resize_keyboard=True,
#     )
#
#
# def kb_occasion() -> ReplyKeyboardMarkup:
#     return ReplyKeyboardMarkup(
#         keyboard=[
#             [
#                 KeyboardButton(text="День рождения 🎂"),
#                 KeyboardButton(text="Годовщина 💍"),
#             ],
#             [
#                 KeyboardButton(text="Свидание 💋"),
#                 KeyboardButton(text="Просто так 🌷"),
#             ],
#             [
#                 KeyboardButton(text="Выздоровление 🤍"),
#                 KeyboardButton(text="Другое"),
#             ],
#         ],
#         resize_keyboard=True,
#     )
#
#
# def kb_relation() -> ReplyKeyboardMarkup:
#     return ReplyKeyboardMarkup(
#         keyboard=[
#             [
#                 KeyboardButton(text="Девушка / Жена"),
#                 KeyboardButton(text="Мама"),
#             ],
#             [KeyboardButton(text="Дочь"), KeyboardButton(text="Подруга")],
#             [KeyboardButton(text="Коллега"), KeyboardButton(text="Другое")],
#         ],
#         resize_keyboard=True,
#     )
#
#
# def kb_budget() -> ReplyKeyboardMarkup:
#     return ReplyKeyboardMarkup(
#         keyboard=[
#             [
#                 KeyboardButton(text="до 5 000 ₽"),
#                 KeyboardButton(text="до 10 000 ₽"),
#             ],
#             [
#                 KeyboardButton(text="до 15 000 ₽"),
#                 KeyboardButton(text="от 15 000 ₽"),
#             ],
#         ],
#         resize_keyboard=True,
#     )
#
#
# async def begin_order_dialog(message: Message, state: FSMContext, intro: str) -> None:
#     """Анкета заказа в чате (кнопки клавиатуры)."""
#     await state.clear()
#     await message.answer(
#         "Переходим к оформлению заказа 🌸",
#         reply_markup=ReplyKeyboardRemove(),
#     )
#     tg_id = message.from_user.id
#     client = await get_client(tg_id)
#
#     if client:
#         await state.update_data(
#             name=client["name"],
#             phone=client["phone"],
#             returning=True,
#         )
#         await message.answer(
#             f"{intro}\n\n"
#             f"С возвращением, *{client['name']}* 🌸\n\n"
#             "Когда нужен букет?",
#             parse_mode=PARSE_MODE,
#             reply_markup=kb_date(),
#         )
#         await state.set_state(OrderForm.date)
#         return
#
#     await state.update_data(returning=False)
#     await message.answer(
#         f"{intro}\n\nКак вас зовут?",
#         parse_mode=PARSE_MODE,
#     )
#     await state.set_state(OrderForm.name)
#
#
# async def process_name(message: Message, state: FSMContext) -> None:
#     name = (message.text or "").strip()
#     if not name:
#         await message.answer("Пожалуйста, введите ваше имя.", parse_mode=PARSE_MODE)
#         return
#
#     await state.update_data(name=name)
#     await message.answer(
#         f"{await form_progress(state, 1)}\n\n"
#         f"Приятно познакомиться, *{name}* 🌸\n\n"
#         "Укажите ваш номер телефона — флорист позвонит, чтобы уточнить детали букета 📞\n\n"
#         "_Нажмите кнопку ниже или введите номер вручную_",
#         parse_mode=PARSE_MODE,
#         reply_markup=kb_phone(),
#     )
#     await state.set_state(OrderForm.phone)
#
#
# async def _save_phone_and_continue(
#     message: Message, state: FSMContext, phone: str
# ) -> None:
#     await state.update_data(phone=phone)
#     await message.answer(
#         f"{await form_progress(state, 2)}\n\n"
#         "Отлично! Флорист сможет с вами связаться ✅\n\n"
#         "Когда нужен букет?",
#         parse_mode=PARSE_MODE,
#         reply_markup=kb_date(),
#     )
#     await state.set_state(OrderForm.date)
#
#
# async def process_phone_contact(message: Message, state: FSMContext) -> None:
#     if not message.contact:
#         return
#     phone = message.contact.phone_number
#     if not phone.startswith("+"):
#         phone = "+" + phone
#     await _save_phone_and_continue(message, state, phone)
#
#
# async def process_phone_text(message: Message, state: FSMContext) -> None:
#     raw = (message.text or "").strip()
#     digits = "".join(c for c in raw if c.isdigit() or c == "+")
#     if len(digits) < 10:
#         await message.answer(
#             "⚠️ Пожалуйста, введите корректный номер телефона\n"
#             "Например: *+7 999 123-45-67*",
#             parse_mode=PARSE_MODE,
#         )
#         return
#     await _save_phone_and_continue(message, state, digits)
#
#
# async def process_date(message: Message, state: FSMContext) -> None:
#     if message.text == "Другая дата":
#         await message.answer(
#             f"{await form_progress(state, 3)}\n\n"
#             "Введите дату в формате *ДД.ММ.ГГГГ* 📅\n"
#             "_Например: 15.06.2025_",
#             parse_mode=PARSE_MODE,
#             reply_markup=ReplyKeyboardRemove(),
#         )
#         await state.set_state(OrderForm.custom_date)
#         return
#
#     date_choice = message.text or ""
#     if date_choice not in DATE_OPTIONS:
#         await message.answer(
#             "Выберите дату из кнопок ниже 👇",
#             reply_markup=kb_date(),
#             parse_mode=PARSE_MODE,
#         )
#         return
#
#     await state.update_data(date=date_choice)
#     await message.answer(
#         f"{await form_progress(state, 3)}\n\n"
#         "Как зовут счастливого получателя? 💌",
#         reply_markup=ReplyKeyboardRemove(),
#         parse_mode=PARSE_MODE,
#     )
#     await state.set_state(OrderForm.recipient)
#
#
# async def process_custom_date(message: Message, state: FSMContext) -> None:
#     raw = (message.text or "").strip()
#     if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", raw):
#         await message.answer(
#             "⚠️ Пожалуйста, введите дату в формате *ДД.ММ.ГГГГ*\n"
#             "_Например: 15.06.2025_",
#             parse_mode=PARSE_MODE,
#         )
#         return
#
#     await state.update_data(date=raw)
#     await message.answer(
#         f"{await form_progress(state, 3)}\n\n"
#         "Как зовут счастливого получателя? 💌",
#         parse_mode=PARSE_MODE,
#         reply_markup=ReplyKeyboardRemove(),
#     )
#     await state.set_state(OrderForm.recipient)
#
#
# async def process_recipient(message: Message, state: FSMContext) -> None:
#     recipient = (message.text or "").strip()
#     if not recipient:
#         await message.answer(
#             "Пожалуйста, введите имя получателя.",
#             parse_mode=PARSE_MODE,
#         )
#         return
#
#     await state.update_data(recipient=recipient)
#     await message.answer(
#         f"{await form_progress(state, 4)}\n\n"
#         "Какой особенный повод? ✨",
#         reply_markup=kb_occasion(),
#         parse_mode=PARSE_MODE,
#     )
#     await state.set_state(OrderForm.occasion)
#
#
# async def process_occasion(message: Message, state: FSMContext) -> None:
#     if message.text == "Другое":
#         await message.answer(
#             f"{await form_progress(state, 5)}\n\n"
#             "Опишите повод своими словами ✏️",
#             parse_mode=PARSE_MODE,
#             reply_markup=ReplyKeyboardRemove(),
#         )
#         await state.set_state(OrderForm.custom_occasion)
#         return
#
#     occasion = message.text or ""
#     if occasion not in OCCASION_OPTIONS:
#         await message.answer(
#             "Выберите повод из кнопок ниже 👇",
#             reply_markup=kb_occasion(),
#             parse_mode=PARSE_MODE,
#         )
#         return
#
#     data = await state.get_data()
#     recipient = data["recipient"]
#     await state.update_data(occasion=occasion)
#     await message.answer(
#         f"{await form_progress(state, 5)}\n\n"
#         f"Кем приходится *{recipient}*? 🌺",
#         reply_markup=kb_relation(),
#         parse_mode=PARSE_MODE,
#     )
#     await state.set_state(OrderForm.relation)
#
#
# async def process_custom_occasion(message: Message, state: FSMContext) -> None:
#     await state.update_data(occasion=(message.text or "").strip())
#     data = await state.get_data()
#     await message.answer(
#         f"{await form_progress(state, 5)}\n\n"
#         f"Кем приходится *{data['recipient']}*? 🌺",
#         parse_mode=PARSE_MODE,
#         reply_markup=kb_relation(),
#     )
#     await state.set_state(OrderForm.relation)
#
#
# async def process_relation(message: Message, state: FSMContext) -> None:
#     if message.text == "Другое":
#         await message.answer(
#             f"{await form_progress(state, 6)}\n\n"
#             "Опишите кем приходится получатель ✏️",
#             parse_mode=PARSE_MODE,
#             reply_markup=ReplyKeyboardRemove(),
#         )
#         await state.set_state(OrderForm.custom_relation)
#         return
#
#     relation = message.text or ""
#     if relation not in RELATION_OPTIONS:
#         await message.answer(
#             "Выберите вариант из кнопок ниже 👇",
#             reply_markup=kb_relation(),
#             parse_mode=PARSE_MODE,
#         )
#         return
#
#     await state.update_data(relation=relation)
#     await message.answer(
#         f"{await form_progress(state, 6)}\n\n"
#         "Последний шаг! 🎀\n\n"
#         "Какой бюджет на букет?",
#         reply_markup=kb_budget(),
#         parse_mode=PARSE_MODE,
#     )
#     await state.set_state(OrderForm.budget)
#
#
# async def process_custom_relation(message: Message, state: FSMContext) -> None:
#     await state.update_data(relation=(message.text or "").strip())
#     await message.answer(
#         f"{await form_progress(state, 6)}\n\n"
#         "Последний шаг! 🎀\n\n"
#         "Какой бюджет на букет?",
#         parse_mode=PARSE_MODE,
#         reply_markup=kb_budget(),
#     )
#     await state.set_state(OrderForm.budget)
#
#
# async def process_budget(message: Message, state: FSMContext, bot: Bot) -> None:
#     budget = message.text or ""
#     if budget not in BUDGET_OPTIONS:
#         await message.answer(
#             "Выберите бюджет из кнопок ниже 👇",
#             reply_markup=kb_budget(),
#             parse_mode=PARSE_MODE,
#         )
#         return
#
#     await state.update_data(budget=budget)
#     data = await state.get_data()
#     client_tg_id = message.from_user.id
#     name = data["name"]
#     date = data["date"]
#     recipient = data["recipient"]
#     occasion = data["occasion"]
#     relation = data["relation"]
#
#     phone = data["phone"]
#
#     summary = (
#         f"{await form_progress(state, 7)}\n\n"
#         "✅ *Заявка принята!*\n\n"
#         "┌─────────────────────\n"
#         f"│ 👤 Клиент:      *{name}*\n"
#         f"│ 📞 Телефон:     *{phone}*\n"
#         f"│ 📅 Дата:        *{date}*\n"
#         f"│ 🎁 Получатель:  *{recipient}*\n"
#         f"│ 🎉 Повод:       *{occasion}*\n"
#         f"│ 💜 Кто:         *{relation}*\n"
#         f"│ 💰 Бюджет:      *{budget}*\n"
#         "└─────────────────────\n\n"
#         "Наш флорист свяжется с вами в течение *15 минут* 🌷"
#     )
#     summary += "\n\n_Спасибо, что выбираете Veresk_"
#
#     redis = getattr(dp, "redis", None)
#     order_id, posiflora_ok = await submit_order(bot, data, client_tg_id, redis=redis)
#
#     if not posiflora_ok:
#         summary += (
#             "\n\n⚠️ _Заявка принята, но возникла задержка с CRM. "
#             "Флорист свяжется с вами вручную._"
#         )
#
#     track_kb = tracking_keyboard(order_id)
#     if track_kb:
#         summary += "\n\n_Нажмите «Следить за заказом» — этапы и детали в приложении 💜_"
#
#     await message.answer(
#         summary,
#         parse_mode=PARSE_MODE,
#         reply_markup=track_kb or ReplyKeyboardRemove(),
#     )
#
#     await message.answer("🌷", reply_markup=ReplyKeyboardRemove())
#     await state.clear()
#
# ═══════════════════════════════════════════════════════════


def _format_order_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso[:10] if iso else "—"


async def _latest_order_id(tg_id: int) -> str | None:
    redis = getattr(dp, "redis", None)
    if redis:
        order = await get_active_order_by_tg(redis, tg_id)
        if order:
            return str(order["order_id"])
    orders = await get_orders_for_client(tg_id, limit=1)
    if orders:
        return str(orders[0]["posiflora_order_id"])
    return None


async def _send_tracker_invite(
    message: Message,
    intro: str,
    order_id: str | None = None,
) -> None:
    """Сообщение с inline-кнопкой Web App."""
    inline_kb = launch_keyboard(order_id)
    if not inline_kb:
        await message.answer(
            intro
            + "\n\n_Трекер недоступен: на сервере не задан MINIAPP_URL (HTTPS)._",
            parse_mode=PARSE_MODE,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await message.answer(
        intro,
        parse_mode=PARSE_MODE,
        reply_markup=inline_kb,
    )


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(events=[])
    await message.answer(
        "🌸 *Добро пожаловать в Veresk*\n"
        "_флористический салон · trail of happiness_\n\n"
        "Заполните короткую анкету — это поможет нам подобрать "
        "идеальный букет для вашего повода.\n\n"
        "Как вас зовут?",
        parse_mode=PARSE_MODE,
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(ProfileForm.name)


async def cmd_status(message: Message, state: FSMContext) -> None:
    """Трекер статуса заказа (Mini App)."""
    await state.clear()
    uid = message.from_user.id
    order_id = await _latest_order_id(uid)
    intro = "🌿 *Veresk*\n_trail of happiness_\n\n"
    if order_id:
        intro += (
            f"Ваш заказ *№{order_id}* — откройте трекер, "
            "чтобы видеть этапы и детали."
        )
    else:
        intro += (
            "Следите за заказом после оформления.\n"
            "Профильная анкета — /start 🌸"
        )
    await _send_tracker_invite(message, intro, order_id)


async def cmd_order(message: Message, state: FSMContext) -> None:
    await message.answer(
        "Сейчас основной сценарий — профильная анкета.\n"
        "Напишите /start 🌸",
        parse_mode=PARSE_MODE,
        reply_markup=ReplyKeyboardRemove(),
    )


async def cmd_orders(message: Message) -> None:
    """История заказов клиента."""
    orders = await get_orders_for_client(message.from_user.id, limit=15)
    if not orders:
        await _send_tracker_invite(
            message,
            "📋 У вас пока нет заказов.\n\n"
            "Заполните профиль: /start",
        )
        return

    lines = ["📋 *Ваши заказы*\n"]
    for o in orders:
        title = status_meta(o.get("status", "new")).get("label", "—")
        created = _format_order_date(o.get("created_at", ""))
        lines.append(
            f"\n• *№{o['posiflora_order_id']}* · {created}\n"
            f"  🎁 {o['recipient']} · 📅 {o['delivery_date']}\n"
            f"  _{title}_"
        )
    lines.append("\n\n_Профиль: /start_")
    kb = orders_list_keyboard(orders)
    await message.answer("".join(lines), parse_mode=PARSE_MODE, reply_markup=kb)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer(
            "Активной анкеты нет. Напишите /start чтобы начать 🌸",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await state.clear()
    await message.answer(
        "Анкета отменена 🌿\n\nНапишите /start чтобы начать заново.",
        parse_mode=PARSE_MODE,
        reply_markup=ReplyKeyboardRemove(),
    )


async def step_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пожалуйста, введите ваше имя.", parse_mode=PARSE_MODE)
        return
    await state.update_data(name=name)
    await message.answer(
        f"{progress(1)}\n\n"
        f"Приятно познакомиться, *{name}*!\n\n"
        "Укажите номер телефона — нажмите кнопку или введите вручную:",
        parse_mode=PARSE_MODE,
        reply_markup=kb_phone(),
    )
    await state.set_state(ProfileForm.phone)


async def step_phone_contact(message: Message, state: FSMContext) -> None:
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    await _phone_done(message, state, phone)


async def step_phone_text(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    digits = "".join(c for c in raw if c.isdigit() or c == "+")
    if len(digits) < 10:
        await message.answer(
            "Введите корректный номер.\nНапример: *+7 999 123-45-67*",
            parse_mode=PARSE_MODE,
        )
        return
    await _phone_done(message, state, digits)


async def _phone_done(message: Message, state: FSMContext, phone: str) -> None:
    await state.update_data(phone=phone)
    await message.answer(
        f"{progress(2)}\n\n"
        "Укажите *важную дату* 📅\n\n"
        "Введите дату в формате *ДД.ММ.ГГГГ*\n"
        "_Например: 15.06.2025_",
        parse_mode=PARSE_MODE,
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(ProfileForm.important_date)


async def _ask_occasion(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{progress(4)}\n\n"
        "*Какой повод?*\n\n"
        "_Выберите вариант или нажмите «Свой вариант»_",
        parse_mode=PARSE_MODE,
        reply_markup=kb_occasion(),
    )
    await state.set_state(ProfileForm.occasion)


async def _ask_relation(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{progress(5)}\n\n"
        "*Кем приходится получатель?* 🌺\n\n"
        "_Выберите вариант или нажмите «Свой вариант»_",
        parse_mode=PARSE_MODE,
        reply_markup=kb_relation(),
    )
    await state.set_state(ProfileForm.relation)


async def _save_event_and_ask_more(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    events = list(data.get("events", []))
    events.append(
        {
            "date": data["important_date"],
            "occasion": data["occasion"],
            "relation": data["relation"],
        }
    )
    await state.update_data(events=events)
    count = len(events)
    await message.answer(
        f"Событие сохранено ✅\n"
        f"📅 *{data['important_date']}* · {data['occasion']} · {data['relation']}\n\n"
        f"Всего важных дат: *{count}*\n\n"
        "Хотите добавить ещё одну важную дату?",
        parse_mode=PARSE_MODE,
        reply_markup=kb_add_more_dates(),
    )
    await state.set_state(ProfileForm.add_more_dates)


async def _ask_budget(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{progress(6)}\n\n"
        "*Уровень бюджета букета?*\n\n"
        "_Выберите вариант из кнопок ниже_",
        parse_mode=PARSE_MODE,
        reply_markup=kb_budget(),
    )
    await state.set_state(ProfileForm.budget)


async def _ask_source(message: Message, state: FSMContext) -> None:
    await message.answer(
        f"{progress(7)}\n\n"
        "*Откуда вы узнали о нас?*\n\n"
        "_Выберите вариант или нажмите «Свой вариант»_",
        parse_mode=PARSE_MODE,
        reply_markup=kb_source(),
    )
    await state.set_state(ProfileForm.source)


async def _finish_survey(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    tg_id = message.from_user.id
    events = list(data.get("events", []))

    profile = {
        "name": data.get("name", ""),
        "phone": data.get("phone", ""),
        "budget": data.get("budget", ""),
        "source": data.get("source", ""),
        "events": events,
    }

    logger.info(
        "PROFILE tg_id=%s | name=%s | phone=%s | budget=%s | source=%s | events=%s",
        tg_id,
        profile["name"],
        profile["phone"],
        profile["budget"],
        profile["source"],
        json.dumps(events, ensure_ascii=False),
    )

    await save_client_profile(tg_id, profile)

    posiflora_ok = False
    posiflora_meta: dict[str, Any] = {}
    try:
        from posiflora import sync_survey_profile_to_posiflora

        posiflora_meta = await sync_survey_profile_to_posiflora(profile, tg_id)
        posiflora_ok = bool(posiflora_meta.get("posiflora_ok"))
        logger.info(
            "Posiflora анкета: customer=%s, событий %s/%s",
            posiflora_meta.get("customer_id"),
            posiflora_meta.get("events_synced"),
            posiflora_meta.get("events_total"),
        )
    except Exception:
        logger.exception("❌ Ошибка синхронизации анкеты с Posiflora (tg_id=%s)", tg_id)

    await notify_florist_profile(
        message.bot,
        FLORIST_CHAT_ID,
        profile,
        tg_id,
        posiflora_ok=posiflora_ok,
        posiflora_meta=posiflora_meta,
    )

    events_block = _format_events_lines(events)
    posiflora_note = ""
    if not posiflora_ok:
        posiflora_note = (
            "\n\n⚠️ _Данные сохранены локально, но не удалось передать их в Posiflora\\. "
            "Флорист свяжется с вами вручную\\._"
        )
    elif posiflora_meta.get("events_failed"):
        posiflora_note = (
            "\n\n_Карточка клиента обновлена в Posiflora\\. "
            "Часть дат сохранена в заметках CRM\\._"
        )

    await message.answer(
        f"{progress(7)}\n\n"
        "✅ *Анкета сохранена!*\n\n"
        "┌─────────────────────\n"
        f"│ 👤 Клиент:  *{profile['name']}*\n"
        f"│ 📞 Телефон: *{profile['phone']}*\n"
        f"│ 💰 Бюджет:  *{profile['budget']}*\n"
        f"│ 📣 Источник: *{profile['source']}*\n"
        "│\n"
        "│ *Важные даты:*\n"
        f"{events_block}\n"
        "└─────────────────────\n\n"
        "Спасибо, что ответили на все вопросы! 🌷\n\n"
        "_Спасибо, что выбираете Veresk · trail of happiness_"
        f"{posiflora_note}",
        parse_mode=PARSE_MODE,
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()


async def _handle_choice_step(
    message: Message,
    state: FSMContext,
    *,
    field: str,
    presets: set[str],
    keyboard,
    on_done,
) -> None:
    text = (message.text or "").strip()
    awaiting_key = f"awaiting_custom_{field}"
    data = await state.get_data()

    if text == CUSTOM_OPTION:
        await state.update_data(**{awaiting_key: True})
        await message.answer(
            "Напишите свой вариант:",
            parse_mode=PARSE_MODE,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if data.get(awaiting_key):
        if not text:
            await message.answer(
                "Пожалуйста, введите текст.",
                parse_mode=PARSE_MODE,
            )
            return
        await state.update_data(**{field: text, awaiting_key: False})
        await on_done(message, state)
        return

    if text in presets:
        await state.update_data(**{field: text, awaiting_key: False})
        await on_done(message, state)
        return

    await message.answer(
        "Выберите вариант из кнопок или нажмите «Свой вариант» 👇",
        reply_markup=keyboard(),
        parse_mode=PARSE_MODE,
    )


async def step_important_date(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    resolved = resolve_important_date(text)
    if resolved:
        await state.update_data(important_date=resolved)
        data = await state.get_data()
        event_num = len(data.get("events", [])) + 1
        if event_num > 1:
            await message.answer(
                f"Дата *{resolved}* принята ✅\n\n"
                f"*Событие {event_num}* — какой повод?",
                parse_mode=PARSE_MODE,
            )
        await _ask_occasion(message, state)
        return

    await message.answer(
        "⚠️ Введите корректную дату в формате *ДД.ММ.ГГГГ*\n"
        "_Например: 15.06.2025_",
        parse_mode=PARSE_MODE,
    )


async def step_add_more_dates(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == ADD_MORE_YES:
        data = await state.get_data()
        next_num = len(data.get("events", [])) + 1
        await message.answer(
            f"Укажите *важную дату* для события {next_num} 📅\n\n"
            "Введите дату в формате *ДД.ММ.ГГГГ*\n"
            "_Например: 15.06.2025_",
            parse_mode=PARSE_MODE,
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(ProfileForm.important_date)
        return

    if text == ADD_MORE_NO:
        await _ask_budget(message, state)
        return

    await message.answer(
        "Выберите: добавить ещё дату или завершить 👇",
        reply_markup=kb_add_more_dates(),
        parse_mode=PARSE_MODE,
    )


async def step_occasion(message: Message, state: FSMContext) -> None:
    await _handle_choice_step(
        message,
        state,
        field="occasion",
        presets=OCCASION_PRESETS,
        keyboard=kb_occasion,
        on_done=_ask_relation,
    )


async def step_relation(message: Message, state: FSMContext) -> None:
    await _handle_choice_step(
        message,
        state,
        field="relation",
        presets=RELATION_PRESETS,
        keyboard=kb_relation,
        on_done=_save_event_and_ask_more,
    )


async def step_budget(message: Message, state: FSMContext) -> None:
    budget = (message.text or "").strip()
    if budget not in BUDGET_PRESETS:
        await message.answer(
            "Выберите бюджет из кнопок ниже 👇",
            reply_markup=kb_budget(),
            parse_mode=PARSE_MODE,
        )
        return
    await state.update_data(budget=budget, awaiting_custom_budget=False)
    await _ask_source(message, state)


async def step_source(message: Message, state: FSMContext) -> None:
    await _handle_choice_step(
        message,
        state,
        field="source",
        presets=SOURCE_PRESETS,
        keyboard=kb_source,
        on_done=_finish_survey,
    )


async def on_miniapp_order(message: Message, bot: Bot) -> None:
    """Заказ из Mini App через tg.sendData."""
    raw = message.web_app_data.data if message.web_app_data else ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await message.answer(
            "⚠️ Не удалось разобрать данные заказа. Попробуйте отправить ещё раз из приложения.",
            parse_mode=PARSE_MODE,
        )
        return
    if not isinstance(data, dict) or not data.get("name") or not data.get("phone"):
        await message.answer(
            "⚠️ В заявке не хватает имени или телефона.",
            parse_mode=PARSE_MODE,
        )
        return

    from order_service import finalize_miniapp_order

    redis = getattr(dp, "redis", None)
    try:
        await finalize_miniapp_order(bot, data, message.from_user.id, redis=redis)
    except Exception:
        logger.exception("Mini App sendData failed for tg_id=%s", message.from_user.id)
        await message.answer(
            "⚠️ Не удалось принять заявку. Напишите /start или попробуйте снова из приложения.",
            parse_mode=PARSE_MODE,
        )


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(on_miniapp_order, F.web_app_data)
    dp.message.register(cmd_order, Command("order"))
    dp.message.register(cmd_orders, Command("orders"))
    dp.message.register(cmd_cancel, Command("cancel"))
    dp.message.register(step_name, ProfileForm.name)
    dp.message.register(step_phone_contact, ProfileForm.phone, F.contact)
    dp.message.register(step_phone_text, ProfileForm.phone, F.text)
    dp.message.register(step_important_date, ProfileForm.important_date)
    dp.message.register(step_occasion, ProfileForm.occasion)
    dp.message.register(step_relation, ProfileForm.relation)
    dp.message.register(step_add_more_dates, ProfileForm.add_more_dates)
    dp.message.register(step_budget, ProfileForm.budget)
    dp.message.register(step_source, ProfileForm.source)


BOT_COMMANDS = [
    BotCommand(command="start", description="Статус заказа"),
    BotCommand(command="status", description="Открыть трекер"),
    BotCommand(command="order", description="Новый заказ"),
    BotCommand(command="orders", description="История ваших заказов"),
    BotCommand(command="cancel", description="Отменить текущую анкету"),
]

bot = Bot(token=BOT_TOKEN, session=AiohttpSession(timeout=120))
dp = Dispatcher(storage=storage)
dp.include_router(notifications_router)
register_handlers(dp)


async def validate_bot_token() -> None:
    """Проверка BOT_TOKEN до запуска polling."""
    try:
        me = await bot.get_me(request_timeout=90)
    except TelegramUnauthorizedError:
        logger.error(
            "BOT_TOKEN неверный или отозван. Откройте @BotFather → /mybots → ваш бот → "
            "API Token, скопируйте токен в .env на сервере (без кавычек и пробелов)."
        )
        await bot.session.close()
        raise SystemExit(1) from None
    except TelegramNetworkError as exc:
        logger.error("Не удалось связаться с Telegram API: %s", exc)
        await bot.session.close()
        raise SystemExit(1) from None

    logger.info("Бот авторизован: @%s (id=%s)", me.username, me.id)


async def setup_menu_commands() -> None:
    """Регистрация /start и /cancel в меню Telegram. Сбой сети не останавливает бота."""
    for attempt in range(1, 4):
        try:
            await bot.set_my_commands(BOT_COMMANDS, request_timeout=90)
            await reset_bot_menu_button(bot)
            logger.info("Меню команд бота обновлено")
            return
        except TelegramUnauthorizedError:
            raise
        except TelegramNetworkError as exc:
            logger.warning(
                "Не удалось обновить меню команд (попытка %s/3): %s",
                attempt,
                exc,
            )
            if attempt < 3:
                await asyncio.sleep(3 * attempt)
    logger.warning(
        "Меню команд не обновлено — бот продолжит работу; проверьте доступ к api.telegram.org"
    )


async def main() -> None:
    os.makedirs("/app/logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                "/app/logs/bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            ),
        ],
    )
    await validate_bot_token()

    from client_db import init_db

    await init_db()

    redis = getattr(dp.storage, "redis", None)
    if redis:
        dp.redis = redis
        set_redis(redis)
        asyncio.create_task(start_polling(bot, redis))
        logger.info("🔄 Polling задача запущена")
    else:
        logger.warning("⚠️ Redis недоступен — polling статусов отключён")

    if MINIAPP_URL:
        await start_webapp_server(redis, WEBAPP_HOST, WEBAPP_PORT, bot=bot)
        logger.info("🌐 Mini App URL: %s (доступен всем клиентам)", MINIAPP_URL)
    else:
        logger.warning(
            "⚠️ MINIAPP_URL не задан — Web App не откроется (задайте HTTPS в .env)"
        )

    await setup_menu_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
