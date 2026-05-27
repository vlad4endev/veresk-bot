import asyncio
import logging
import os
import re
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, F
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

from config import BOT_TOKEN, FLORIST_CHAT_ID

try:
    from aiogram.fsm.storage.redis import RedisStorage

    REDIS_URL = os.getenv("REDIS_URL")
    storage = RedisStorage.from_url(REDIS_URL) if REDIS_URL else MemoryStorage()
except ImportError:
    storage = MemoryStorage()

from notifications import notify_florist, router as notifications_router
from order_store import save_order
from poller import start_polling
from posiflora import create_posiflora_order

logger = logging.getLogger(__name__)

PARSE_MODE = "Markdown"


class OrderForm(StatesGroup):
    name = State()
    phone = State()
    date = State()
    custom_date = State()
    recipient = State()
    occasion = State()
    custom_occasion = State()
    relation = State()
    custom_relation = State()
    budget = State()


DATE_OPTIONS = {
    "Сегодня",
    "Завтра",
    "Через 2–3 дня",
    "Через неделю",
    "Через 2 недели",
    "Другая дата",
}

OCCASION_OPTIONS = {
    "День рождения 🎂",
    "Годовщина 💍",
    "Свидание 💋",
    "Просто так 🌷",
    "Выздоровление 🤍",
    "Другое",
}

RELATION_OPTIONS = {
    "Девушка / Жена",
    "Мама",
    "Дочь",
    "Подруга",
    "Коллега",
    "Другое",
}

BUDGET_OPTIONS = {
    "до 5 000 ₽",
    "до 10 000 ₽",
    "до 15 000 ₽",
    "от 15 000 ₽",
}


def progress(step: int, total: int = 7) -> str:
    return "🟣" * step + "⚪️" * (total - step) + f"  {step}/{total}"


def kb_phone() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_date() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
            [
                KeyboardButton(text="Через 2–3 дня"),
                KeyboardButton(text="Через неделю"),
            ],
            [
                KeyboardButton(text="Через 2 недели"),
                KeyboardButton(text="Другая дата"),
            ],
        ],
        resize_keyboard=True,
    )


def kb_occasion() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="День рождения 🎂"),
                KeyboardButton(text="Годовщина 💍"),
            ],
            [
                KeyboardButton(text="Свидание 💋"),
                KeyboardButton(text="Просто так 🌷"),
            ],
            [
                KeyboardButton(text="Выздоровление 🤍"),
                KeyboardButton(text="Другое"),
            ],
        ],
        resize_keyboard=True,
    )


def kb_relation() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Девушка / Жена"),
                KeyboardButton(text="Мама"),
            ],
            [KeyboardButton(text="Дочь"), KeyboardButton(text="Подруга")],
            [KeyboardButton(text="Коллега"), KeyboardButton(text="Другое")],
        ],
        resize_keyboard=True,
    )


def kb_budget() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="до 5 000 ₽"),
                KeyboardButton(text="до 10 000 ₽"),
            ],
            [
                KeyboardButton(text="до 15 000 ₽"),
                KeyboardButton(text="от 15 000 ₽"),
            ],
        ],
        resize_keyboard=True,
    )


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🌿 *Добро пожаловать в Veresk*\n"
        "_trail of happiness_\n\n"
        "Я помогу подобрать идеальный букет для вашего особенного момента.\n\n"
        "Как вас зовут?",
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.name)


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


async def process_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пожалуйста, введите ваше имя.", parse_mode=PARSE_MODE)
        return

    await state.update_data(name=name)
    await message.answer(
        f"{progress(1)}\n\n"
        f"Приятно познакомиться, *{name}* 🌸\n\n"
        "Укажите ваш номер телефона — флорист позвонит, чтобы уточнить детали букета 📞\n\n"
        "_Нажмите кнопку ниже или введите номер вручную_",
        parse_mode=PARSE_MODE,
        reply_markup=kb_phone(),
    )
    await state.set_state(OrderForm.phone)


async def _save_phone_and_continue(
    message: Message, state: FSMContext, phone: str
) -> None:
    await state.update_data(phone=phone)
    await message.answer(
        f"{progress(2)}\n\n"
        "Отлично! Флорист сможет с вами связаться ✅\n\n"
        "Когда нужен букет?",
        parse_mode=PARSE_MODE,
        reply_markup=kb_date(),
    )
    await state.set_state(OrderForm.date)


async def process_phone_contact(message: Message, state: FSMContext) -> None:
    if not message.contact:
        return
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    await _save_phone_and_continue(message, state, phone)


async def process_phone_text(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    digits = "".join(c for c in raw if c.isdigit() or c == "+")
    if len(digits) < 10:
        await message.answer(
            "⚠️ Пожалуйста, введите корректный номер телефона\n"
            "Например: *+7 999 123-45-67*",
            parse_mode=PARSE_MODE,
        )
        return
    await _save_phone_and_continue(message, state, digits)


async def process_date(message: Message, state: FSMContext) -> None:
    if message.text == "Другая дата":
        await message.answer(
            f"{progress(3)}\n\n"
            "Введите дату в формате *ДД.ММ.ГГГГ* 📅\n"
            "_Например: 15.06.2025_",
            parse_mode=PARSE_MODE,
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(OrderForm.custom_date)
        return

    date_choice = message.text or ""
    if date_choice not in DATE_OPTIONS:
        await message.answer(
            "Выберите дату из кнопок ниже 👇",
            reply_markup=kb_date(),
            parse_mode=PARSE_MODE,
        )
        return

    await state.update_data(date=date_choice)
    await message.answer(
        f"{progress(3)}\n\n"
        "Как зовут счастливого получателя? 💌",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.recipient)


async def process_custom_date(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", raw):
        await message.answer(
            "⚠️ Пожалуйста, введите дату в формате *ДД.ММ.ГГГГ*\n"
            "_Например: 15.06.2025_",
            parse_mode=PARSE_MODE,
        )
        return

    await state.update_data(date=raw)
    await message.answer(
        f"{progress(4)}\n\n"
        "Как зовут счастливого получателя? 💌",
        parse_mode=PARSE_MODE,
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(OrderForm.recipient)


async def process_recipient(message: Message, state: FSMContext) -> None:
    recipient = (message.text or "").strip()
    if not recipient:
        await message.answer(
            "Пожалуйста, введите имя получателя.",
            parse_mode=PARSE_MODE,
        )
        return

    await state.update_data(recipient=recipient)
    await message.answer(
        f"{progress(5)}\n\n"
        "Какой особенный повод? ✨",
        reply_markup=kb_occasion(),
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.occasion)


async def process_occasion(message: Message, state: FSMContext) -> None:
    if message.text == "Другое":
        await message.answer(
            f"{progress(5)}\n\n"
            "Опишите повод своими словами ✏️",
            parse_mode=PARSE_MODE,
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(OrderForm.custom_occasion)
        return

    occasion = message.text or ""
    if occasion not in OCCASION_OPTIONS:
        await message.answer(
            "Выберите повод из кнопок ниже 👇",
            reply_markup=kb_occasion(),
            parse_mode=PARSE_MODE,
        )
        return

    data = await state.get_data()
    recipient = data["recipient"]
    await state.update_data(occasion=occasion)
    await message.answer(
        f"{progress(6)}\n\n"
        f"Кем приходится *{recipient}*? 🌺",
        reply_markup=kb_relation(),
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.relation)


async def process_custom_occasion(message: Message, state: FSMContext) -> None:
    await state.update_data(occasion=(message.text or "").strip())
    data = await state.get_data()
    await message.answer(
        f"{progress(5)}\n\n"
        f"Кем приходится *{data['recipient']}*? 🌺",
        parse_mode=PARSE_MODE,
        reply_markup=kb_relation(),
    )
    await state.set_state(OrderForm.relation)


async def process_relation(message: Message, state: FSMContext) -> None:
    if message.text == "Другое":
        await message.answer(
            f"{progress(6)}\n\n"
            "Опишите кем приходится получатель ✏️",
            parse_mode=PARSE_MODE,
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(OrderForm.custom_relation)
        return

    relation = message.text or ""
    if relation not in RELATION_OPTIONS:
        await message.answer(
            "Выберите вариант из кнопок ниже 👇",
            reply_markup=kb_relation(),
            parse_mode=PARSE_MODE,
        )
        return

    await state.update_data(relation=relation)
    await message.answer(
        f"{progress(6)}\n\n"
        "Последний шаг! 🎀\n\n"
        "Какой бюджет на букет?",
        reply_markup=kb_budget(),
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.budget)


async def process_custom_relation(message: Message, state: FSMContext) -> None:
    await state.update_data(relation=(message.text or "").strip())
    await message.answer(
        f"{progress(6)}\n\n"
        "Последний шаг! 🎀\n\n"
        "Какой бюджет на букет?",
        parse_mode=PARSE_MODE,
        reply_markup=kb_budget(),
    )
    await state.set_state(OrderForm.budget)


async def process_budget(message: Message, state: FSMContext, bot: Bot) -> None:
    budget = message.text or ""
    if budget not in BUDGET_OPTIONS:
        await message.answer(
            "Выберите бюджет из кнопок ниже 👇",
            reply_markup=kb_budget(),
            parse_mode=PARSE_MODE,
        )
        return

    await state.update_data(budget=budget)
    data = await state.get_data()
    client_tg_id = message.from_user.id
    name = data["name"]
    date = data["date"]
    recipient = data["recipient"]
    occasion = data["occasion"]
    relation = data["relation"]

    phone = data["phone"]

    summary = (
        f"{progress(7)}\n\n"
        "✅ *Заявка принята!*\n\n"
        "┌─────────────────────\n"
        f"│ 👤 Клиент:      *{name}*\n"
        f"│ 📞 Телефон:     *{phone}*\n"
        f"│ 📅 Дата:        *{date}*\n"
        f"│ 🎁 Получатель:  *{recipient}*\n"
        f"│ 🎉 Повод:       *{occasion}*\n"
        f"│ 💜 Кто:         *{relation}*\n"
        f"│ 💰 Бюджет:      *{budget}*\n"
        "└─────────────────────\n\n"
        "Наш флорист свяжется с вами в течение *15 минут* 🌷\n\n"
        "_Спасибо, что выбираете Veresk_"
    )

    await message.answer(
        summary,
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=PARSE_MODE,
    )

    order_id = "—"
    posiflora_ok = True
    try:
        order_id = await create_posiflora_order(
            customer_name=name,
            phone=phone,
            recipient=recipient,
            occasion=occasion,
            relation=relation,
            budget=budget,
            delivery_date=date,
            telegram_id=client_tg_id,
        )
        logger.info("✅ Заказ Posiflora: #%s", order_id)

        if hasattr(dp, "redis") and dp.redis:
            await save_order(dp.redis, order_id, client_tg_id, status="new")
    except Exception:
        logger.exception("❌ Ошибка Posiflora")
        posiflora_ok = False

    await notify_florist(
        bot=bot,
        florist_chat_id=FLORIST_CHAT_ID,
        data=data,
        order_id=str(order_id),
        client_tg_id=client_tg_id,
        posiflora_ok=posiflora_ok,
    )

    await state.clear()


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_cancel, Command("cancel"))
    dp.message.register(process_name, OrderForm.name)
    dp.message.register(process_phone_contact, OrderForm.phone, F.contact)
    dp.message.register(process_phone_text, OrderForm.phone, F.text)
    dp.message.register(process_date, OrderForm.date)
    dp.message.register(process_custom_date, OrderForm.custom_date)
    dp.message.register(process_recipient, OrderForm.recipient)
    dp.message.register(process_occasion, OrderForm.occasion)
    dp.message.register(process_custom_occasion, OrderForm.custom_occasion)
    dp.message.register(process_relation, OrderForm.relation)
    dp.message.register(process_custom_relation, OrderForm.custom_relation)
    dp.message.register(process_budget, OrderForm.budget)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)
dp.include_router(notifications_router)
register_handlers(dp)


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
    redis = getattr(dp.storage, "redis", None)
    if redis:
        dp.redis = redis
        asyncio.create_task(start_polling(bot, redis))
        logger.info("🔄 Polling задача запущена")
    else:
        logger.warning("⚠️ Redis недоступен — polling отключён")

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать заказ букета"),
            BotCommand(command="cancel", description="Отменить текущую анкету"),
        ]
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
