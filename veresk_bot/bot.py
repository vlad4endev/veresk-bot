import asyncio
import json
import logging
import os
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler

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
from config import BOT_TOKEN, MINIAPP_URL, WEBAPP_HOST, WEBAPP_PORT

try:
    from aiogram.fsm.storage.redis import RedisStorage

    REDIS_URL = os.getenv("REDIS_URL")
    storage = RedisStorage.from_url(REDIS_URL) if REDIS_URL else MemoryStorage()
except ImportError:
    storage = MemoryStorage()

from client_db import get_client, get_orders_for_client
from notifications import router as notifications_router
from order_service import submit_order
from order_status import status_meta
from poller import start_polling
from order_store import get_active_order_by_tg
from webapp_buttons import (
    launch_keyboard,
    orders_list_keyboard,
    setup_bot_menu_button,
    tracker_reply_keyboard,
    tracking_keyboard,
)
from webapp_server import start_webapp_server

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


async def form_progress(state: FSMContext, logical_step: int) -> str:
    data = await state.get_data()
    if data.get("returning"):
        return progress(max(1, logical_step - 2), 5)
    return progress(logical_step, 7)


def _format_order_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso[:10] if iso else "—"


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


async def begin_order_dialog(message: Message, state: FSMContext, intro: str) -> None:
    """Анкета заказа в чате (кнопки клавиатуры)."""
    await state.clear()
    await message.answer(
        "Переходим к оформлению заказа 🌸",
        reply_markup=ReplyKeyboardRemove(),
    )
    tg_id = message.from_user.id
    client = await get_client(tg_id)

    if client:
        await state.update_data(
            name=client["name"],
            phone=client["phone"],
            returning=True,
        )
        await message.answer(
            f"{intro}\n\n"
            f"С возвращением, *{client['name']}* 🌸\n\n"
            "Когда нужен букет?",
            parse_mode=PARSE_MODE,
            reply_markup=kb_date(),
        )
        await state.set_state(OrderForm.date)
        return

    await state.update_data(returning=False)
    await message.answer(
        f"{intro}\n\nКак вас зовут?",
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.name)


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
    """Сообщение + inline и reply-кнопки Web App (для любого tg_id)."""
    inline_kb = launch_keyboard(order_id)
    reply_kb = tracker_reply_keyboard(order_id)

    if not inline_kb and not reply_kb:
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
    if reply_kb:
        await message.answer(
            "👇 *Откройте трекер* — кнопка внизу экрана "
            "(работает для всех клиентов, не только флориста).",
            parse_mode=PARSE_MODE,
            reply_markup=reply_kb,
        )


async def cmd_start(message: Message, state: FSMContext) -> None:
    """Открыть трекер статуса (Mini App). Заказ в чате — /order."""
    await state.clear()
    uid = message.from_user.id
    logger.info("/start tg_id=%s (@%s)", uid, message.from_user.username)

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
            "Новый букет — /order 🌸"
        )

    await _send_tracker_invite(message, intro, order_id)


async def cmd_status(message: Message, state: FSMContext) -> None:
    """Тот же трекер, что /start."""
    await cmd_start(message, state)


async def cmd_order(message: Message, state: FSMContext) -> None:
    intro = "🌿 *Заказ букета*\n_trail of happiness_"
    await begin_order_dialog(message, state, intro)


async def cmd_orders(message: Message) -> None:
    """История заказов клиента."""
    orders = await get_orders_for_client(message.from_user.id, limit=15)
    if not orders:
        await _send_tracker_invite(
            message,
            "📋 У вас пока нет заказов.\n\n"
            "Оформите букет: /order",
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
    lines.append("\n\n_Новый заказ: /order_")
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


async def process_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пожалуйста, введите ваше имя.", parse_mode=PARSE_MODE)
        return

    await state.update_data(name=name)
    await message.answer(
        f"{await form_progress(state, 1)}\n\n"
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
        f"{await form_progress(state, 2)}\n\n"
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
            f"{await form_progress(state, 3)}\n\n"
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
        f"{await form_progress(state, 3)}\n\n"
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
        f"{await form_progress(state, 3)}\n\n"
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
        f"{await form_progress(state, 4)}\n\n"
        "Какой особенный повод? ✨",
        reply_markup=kb_occasion(),
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.occasion)


async def process_occasion(message: Message, state: FSMContext) -> None:
    if message.text == "Другое":
        await message.answer(
            f"{await form_progress(state, 5)}\n\n"
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
        f"{await form_progress(state, 5)}\n\n"
        f"Кем приходится *{recipient}*? 🌺",
        reply_markup=kb_relation(),
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.relation)


async def process_custom_occasion(message: Message, state: FSMContext) -> None:
    await state.update_data(occasion=(message.text or "").strip())
    data = await state.get_data()
    await message.answer(
        f"{await form_progress(state, 5)}\n\n"
        f"Кем приходится *{data['recipient']}*? 🌺",
        parse_mode=PARSE_MODE,
        reply_markup=kb_relation(),
    )
    await state.set_state(OrderForm.relation)


async def process_relation(message: Message, state: FSMContext) -> None:
    if message.text == "Другое":
        await message.answer(
            f"{await form_progress(state, 6)}\n\n"
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
        f"{await form_progress(state, 6)}\n\n"
        "Последний шаг! 🎀\n\n"
        "Какой бюджет на букет?",
        reply_markup=kb_budget(),
        parse_mode=PARSE_MODE,
    )
    await state.set_state(OrderForm.budget)


async def process_custom_relation(message: Message, state: FSMContext) -> None:
    await state.update_data(relation=(message.text or "").strip())
    await message.answer(
        f"{await form_progress(state, 6)}\n\n"
        "Последний шаг! 🎀\n\n"
        "Какой бюджет на букет?",
        parse_mode=PARSE_MODE,
        reply_markup=kb_budget(),
    )
    await state.set_state(OrderForm.budget)


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
            "⚠️ Не удалось принять заявку. Напишите /order или попробуйте снова из приложения.",
            parse_mode=PARSE_MODE,
        )


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
        f"{await form_progress(state, 7)}\n\n"
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
        "Наш флорист свяжется с вами в течение *15 минут* 🌷"
    )
    summary += "\n\n_Спасибо, что выбираете Veresk_"

    redis = getattr(dp, "redis", None)
    order_id, posiflora_ok = await submit_order(bot, data, client_tg_id, redis=redis)

    if not posiflora_ok:
        summary += (
            "\n\n⚠️ _Заявка принята, но возникла задержка с CRM. "
            "Флорист свяжется с вами вручную._"
        )

    track_kb = tracking_keyboard(order_id)
    if track_kb:
        summary += "\n\n_Нажмите «Следить за заказом» — этапы и детали в приложении 💜_"

    await message.answer(
        summary,
        parse_mode=PARSE_MODE,
        reply_markup=track_kb or ReplyKeyboardRemove(),
    )

    track_reply = tracker_reply_keyboard(order_id)
    if track_reply:
        await message.answer(
            "👇 Или откройте трекер кнопкой внизу:",
            reply_markup=track_reply,
        )
    else:
        await message.answer("🌷", reply_markup=ReplyKeyboardRemove())
    await state.clear()


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(on_miniapp_order, F.web_app_data)
    dp.message.register(cmd_order, Command("order"))
    dp.message.register(cmd_orders, Command("orders"))
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
            await setup_bot_menu_button(bot)
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
