"""
Сценарий анкеты Veresk для MAX — повторяет Telegram-бота (bot.py):

/start → имя → телефон (кнопка «Поделиться контактом» или вручную)
→ важная дата → повод → кем приходится → ещё даты? → бюджет → источник
→ сохранение в SQLite + синхронизация с Posiflora + уведомление флористу.

Отличие от Telegram: в MAX нет reply-клавиатур, поэтому все выборы —
inline-кнопки (callback), а телефон запрашивается кнопкой request_contact.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from max_bot.api import MaxBotAPI, btn_callback, btn_request_contact
from max_bot.storage import (
    extract_chat_id_from_update,
    extract_user_from_update,
    save_max_profile,
    upsert_dialog,
)
from survey_smart import (
    budget_hint_for_occasions,
    match_budget,
    match_occasion,
    match_relation,
    parse_date_input,
)

logger = logging.getLogger(__name__)

FORM_STEPS = 7

CUSTOM_PAYLOAD = "__custom__"
CUSTOM_LABEL = "✏️ Свой вариант"

OCCASION_PRESETS = ["День рождения 🎂", "Годовщина 💍"]
RELATION_PRESETS = ["Девушка", "Супруга", "Мама", "Дочь", "Коллега"]
BUDGET_PRESETS = ["до 5 000 ₽", "до 10 000 ₽", "до 15 000 ₽", "более 15 000 ₽"]
SOURCE_PRESETS = [
    "Instagram",
    "Рекомендация",
    "Google / поиск",
    "MAX",
    "Увидел вывеску",
]

# ── Состояния (FSM в памяти процесса) ─────────────────────────

STATE_NAME = "name"
STATE_PHONE = "phone"
STATE_DATE = "important_date"
STATE_OCCASION = "occasion"
STATE_RELATION = "relation"
STATE_ADD_MORE = "add_more_dates"
STATE_BUDGET = "budget"
STATE_SOURCE = "source"

_sessions: dict[int, dict[str, Any]] = {}


def _session(user_id: int) -> dict[str, Any]:
    return _sessions.setdefault(user_id, {"state": None, "data": {"events": []}})


def _reset(user_id: int) -> None:
    _sessions[user_id] = {"state": None, "data": {"events": []}}


# ── Вспомогательные ───────────────────────────────────────────


def progress(step: int, total: int = FORM_STEPS) -> str:
    return "🌸" * step + "⚪️" * (total - step) + f"  {step}/{total}"


def resolve_important_date(text: str) -> str | None:
    result = parse_date_input(text)
    return result["date"] if result.get("ok") else None


def _format_events_lines(events: list[dict]) -> str:
    lines = []
    for i, event in enumerate(events, start=1):
        lines.append(
            f"│ {i}. 📅 **{event['date']}** · {event['occasion']} · {event['relation']}"
        )
    return "\n".join(lines) if lines else "│ — нет дат"


def _choice_keyboard(field: str, presets: list[str], per_row: int = 2):
    rows = []
    for i in range(0, len(presets), per_row):
        rows.append(
            [btn_callback(p, f"{field}|{p}") for p in presets[i : i + per_row]]
        )
    rows.append([btn_callback(CUSTOM_LABEL, f"{field}|{CUSTOM_PAYLOAD}")])
    return rows


def kb_occasion():
    return _choice_keyboard("occasion", OCCASION_PRESETS)


def kb_relation():
    return _choice_keyboard("relation", RELATION_PRESETS)


def kb_source():
    return _choice_keyboard("source", SOURCE_PRESETS)


def kb_budget():
    return [
        [btn_callback(BUDGET_PRESETS[0], f"budget|{BUDGET_PRESETS[0]}"),
         btn_callback(BUDGET_PRESETS[1], f"budget|{BUDGET_PRESETS[1]}")],
        [btn_callback(BUDGET_PRESETS[2], f"budget|{BUDGET_PRESETS[2]}"),
         btn_callback(BUDGET_PRESETS[3], f"budget|{BUDGET_PRESETS[3]}")],
    ]


def kb_phone():
    return [[btn_request_contact("Поделиться номером")]]


def kb_add_more_dates():
    return [
        [btn_callback("➕ Добавить ещё дату", "more|yes")],
        [btn_callback("✅ Больше нет", "more|no")],
    ]


def _extract_phone_from_contact(attachment: dict[str, Any]) -> str | None:
    """Телефон из вложения type=contact (vcf_info или max_info)."""
    payload = attachment.get("payload") or {}
    vcf = payload.get("vcf_info") or ""
    m = re.search(r"TEL[^:]*:(\+?\d[\d\-\s()]{8,})", vcf)
    if m:
        digits = "".join(c for c in m.group(1) if c.isdigit() or c == "+")
        return digits if digits.startswith("+") else "+" + digits
    max_info = payload.get("max_info") or {}
    for key in ("phone", "phone_number"):
        value = str(max_info.get(key) or "").strip()
        if value:
            return value if value.startswith("+") else "+" + value
    return None


class SurveyBot:
    def __init__(self, api: MaxBotAPI, florist_chat_id: int = 0):
        self.api = api
        self.florist_chat_id = florist_chat_id

    async def _send(self, user_id: int, text: str, keyboard=None) -> None:
        await self.api.send_message(user_id=user_id, text=text, keyboard=keyboard)

    async def _index_dialog(
        self,
        update: dict[str, Any],
        *,
        last_text: str | None = None,
        last_out: bool = False,
    ) -> None:
        """Записать chat_id / user в индекс для инбокса админки."""
        chat_id = extract_chat_id_from_update(update)
        user_id, name = extract_user_from_update(update)
        if chat_id is None and user_id is None:
            return
        preview = (last_text or "").replace("\n", " ").strip()[:160] or None
        try:
            await upsert_dialog(
                chat_id=chat_id,
                max_user_id=user_id,
                name=name,
                last_text=preview,
                last_out=last_out if preview is not None else None,
            )
        except Exception:
            logger.debug("Не удалось обновить индекс MAX-диалога", exc_info=True)

    # ── Диспетчеризация обновлений ────────────────────────────

    async def handle_update(self, update: dict[str, Any]) -> None:
        update_type = update.get("update_type")
        if update_type == "bot_started":
            await self._index_dialog(update, last_text="Начал диалог с ботом")
            user_id = (update.get("user") or {}).get("user_id")
            if user_id:
                await self.cmd_start(int(user_id))
            return

        if update_type == "message_callback":
            await self._index_dialog(update, last_text="Нажал кнопку")
            await self._handle_callback(update)
            return

        if update_type == "message_created":
            await self._handle_message(update)
            return

    async def _handle_message(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        user_id = sender.get("user_id")
        if not user_id or sender.get("is_bot"):
            return
        user_id = int(user_id)

        body = message.get("body") or {}
        text = (body.get("text") or "").strip()
        attachments = body.get("attachments") or []
        preview = text or ("Медиа" if attachments else "Сообщение")
        await self._index_dialog(update, last_text=preview, last_out=False)

        # Команды
        lowered = text.lower()
        if lowered in ("/start", "start", "начать"):
            await self.cmd_start(user_id)
            return
        if lowered in ("/cancel", "отмена"):
            await self.cmd_cancel(user_id)
            return

        session = _session(user_id)
        state = session["state"]

        # Контакт (кнопка request_contact) на шаге телефона
        if state == STATE_PHONE:
            for att in attachments:
                if att.get("type") == "contact":
                    phone = _extract_phone_from_contact(att)
                    if phone:
                        await self._phone_done(user_id, phone)
                        return

        # Вне анкеты — не спамим /start: флорист ответит из «Чаты → MAX»
        if state is None:
            return

        handlers = {
            STATE_NAME: self.step_name,
            STATE_PHONE: self.step_phone_text,
            STATE_DATE: self.step_important_date,
            STATE_OCCASION: self.step_occasion_text,
            STATE_RELATION: self.step_relation_text,
            STATE_ADD_MORE: self.step_add_more_text,
            STATE_BUDGET: self.step_budget_text,
            STATE_SOURCE: self.step_source_text,
        }
        handler = handlers.get(state)
        if handler:
            await handler(user_id, text)

    async def _handle_callback(self, update: dict[str, Any]) -> None:
        callback = update.get("callback") or {}
        callback_id = callback.get("callback_id", "")
        payload = callback.get("payload") or ""
        user = callback.get("user") or {}
        user_id = user.get("user_id")
        if not user_id:
            return
        user_id = int(user_id)

        if callback_id:
            await self.api.answer_callback(callback_id)

        field, _, value = payload.partition("|")
        session = _session(user_id)
        state = session["state"]

        if field == "occasion" and state == STATE_OCCASION:
            await self._choice_selected(user_id, "occasion", value, self._ask_relation)
        elif field == "relation" and state == STATE_RELATION:
            await self._choice_selected(
                user_id, "relation", value, self._save_event_and_ask_more
            )
        elif field == "budget" and state == STATE_BUDGET:
            session["data"]["budget"] = value
            await self._ask_source(user_id)
        elif field == "source" and state == STATE_SOURCE:
            await self._choice_selected(user_id, "source", value, self._finish_survey)
        elif field == "more" and state == STATE_ADD_MORE:
            if value == "yes":
                next_num = len(session["data"].get("events", [])) + 1
                await self._send(
                    user_id,
                    f"Укажите **важную дату** для события {next_num} 📅\n\n"
                    "Можно: **15.06.2026**, **завтра**, **15 июня** или «мамин ДР 20 июля».",
                )
                session["data"]["pending_month"] = None
                session["data"]["hint_occasion"] = None
                session["data"]["hint_relation"] = None
                session["state"] = STATE_DATE
            else:
                await self._ask_budget(user_id)

    async def _choice_selected(
        self, user_id: int, field: str, value: str, on_done
    ) -> None:
        session = _session(user_id)
        if value == CUSTOM_PAYLOAD:
            session["data"][f"awaiting_custom_{field}"] = True
            await self._send(user_id, "Напишите свой вариант:")
            return
        session["data"][field] = value
        session["data"][f"awaiting_custom_{field}"] = False
        await on_done(user_id)

    # ── Шаги анкеты ───────────────────────────────────────────

    async def cmd_start(self, user_id: int) -> None:
        try:
            from bot_metrics import PLATFORM_MAX, record_bot_start

            await record_bot_start(PLATFORM_MAX, int(user_id))
        except Exception:
            logger.debug("Не удалось записать запуск MAX-бота", exc_info=True)

        _reset(user_id)
        _session(user_id)["state"] = STATE_NAME
        await self._send(
            user_id,
            "🩷 **Добро пожаловать в Veresk**\n"
            "_флористический салон · trail of happiness_\n\n"
            "Заполните короткую анкету — это поможет нам подобрать "
            "идеальный букет для вашего повода.\n\n"
            "Как вас зовут?",
        )

    async def cmd_cancel(self, user_id: int) -> None:
        session = _session(user_id)
        if session["state"] is None:
            await self._send(
                user_id, "Активной анкеты нет. Напишите /start чтобы начать 🌸"
            )
            return
        _reset(user_id)
        await self._send(
            user_id, "Анкета отменена 🌿\n\nНапишите /start чтобы начать заново."
        )

    async def step_name(self, user_id: int, text: str) -> None:
        name = text.strip()
        if not name:
            await self._send(user_id, "Пожалуйста, введите ваше имя.")
            return
        session = _session(user_id)
        session["data"]["name"] = name
        session["state"] = STATE_PHONE
        await self._send(
            user_id,
            f"{progress(1)}\n\n"
            f"Приятно познакомиться, **{name}**!\n\n"
            "Укажите номер телефона — нажмите кнопку или введите вручную:",
            keyboard=kb_phone(),
        )

    async def step_phone_text(self, user_id: int, text: str) -> None:
        digits = "".join(c for c in text.strip() if c.isdigit() or c == "+")
        if len(digits) < 10:
            await self._send(
                user_id,
                "Введите корректный номер.\nНапример: **+7 999 123-45-67**",
            )
            return
        await self._phone_done(user_id, digits)

    async def _phone_done(self, user_id: int, phone: str) -> None:
        session = _session(user_id)
        session["data"]["phone"] = phone
        session["data"]["pending_month"] = None
        session["state"] = STATE_DATE
        await self._send(
            user_id,
            f"{progress(2)}\n\n"
            "Укажите **важную дату** 📅\n\n"
            "Можно так: **15.06.2026**, **завтра**, **через неделю** или **15 июня**.\n"
            "_Можно сразу: «мамин ДР 15 июня»_",
        )

    async def step_important_date(self, user_id: int, text: str) -> None:
        session = _session(user_id)
        data = session["data"]
        pending = data.get("pending_month")
        try:
            pending_month = int(pending) if pending else None
        except (TypeError, ValueError):
            pending_month = None

        parsed = parse_date_input(text, pending_month=pending_month)
        if parsed.get("occasion"):
            data["hint_occasion"] = parsed["occasion"]
        if parsed.get("relation"):
            data["hint_relation"] = parsed["relation"]

        if parsed.get("ok") and parsed.get("date"):
            data["important_date"] = parsed["date"]
            data["pending_month"] = None
            event_num = len(data.get("events", [])) + 1
            note_bits = []
            if parsed.get("occasion"):
                note_bits.append(parsed["occasion"])
            if parsed.get("relation"):
                note_bits.append(parsed["relation"])
            note = (" · " + " · ".join(note_bits)) if note_bits else ""
            if event_num > 1 or note:
                await self._send(
                    user_id,
                    f"Дата **{parsed['date']}**{note} принята ✅"
                    + (f"\n\n**Событие {event_num}**" if event_num > 1 else ""),
                )
            await self._ask_occasion(user_id)
            return

        if parsed.get("pending_month"):
            data["pending_month"] = parsed["pending_month"]
            await self._send(
                user_id,
                parsed.get("message")
                or "Укажите число месяца — например **15**.",
            )
            return

        await self._send(
            user_id,
            parsed.get("message")
            or (
                "⚠️ Не распознал дату. Попробуйте **15.06.2026**, **завтра**, "
                "**через неделю** или **15 июня**."
            ),
        )

    async def _ask_occasion(self, user_id: int) -> None:
        session = _session(user_id)
        hint = (session["data"].get("hint_occasion") or "").strip()
        if hint:
            session["data"]["occasion"] = hint
            session["data"]["hint_occasion"] = None
            session["data"]["awaiting_custom_occasion"] = False
            await self._send(user_id, f"Повод: **{hint}** ✅")
            await self._ask_relation(user_id)
            return
        session["state"] = STATE_OCCASION
        await self._send(
            user_id,
            f"{progress(3)}\n\n"
            "**Какой повод?**\n\n"
            "_Выберите кнопку, «Свой вариант» или напишите текстом_",
            keyboard=kb_occasion(),
        )

    async def step_occasion_text(self, user_id: int, text: str) -> None:
        await self._custom_text_step(user_id, "occasion", text, self._ask_relation, kb_occasion)

    async def _ask_relation(self, user_id: int) -> None:
        session = _session(user_id)
        hint = (session["data"].get("hint_relation") or "").strip()
        if hint:
            session["data"]["relation"] = hint
            session["data"]["hint_relation"] = None
            session["data"]["awaiting_custom_relation"] = False
            await self._send(user_id, f"Получатель: **{hint}** ✅")
            await self._save_event_and_ask_more(user_id)
            return
        session["state"] = STATE_RELATION
        await self._send(
            user_id,
            f"{progress(4)}\n\n"
            "**Кем приходится получатель?** 🌺\n\n"
            "_Выберите кнопку, «Свой вариант» или напишите текстом_",
            keyboard=kb_relation(),
        )

    async def step_relation_text(self, user_id: int, text: str) -> None:
        await self._custom_text_step(
            user_id, "relation", text, self._save_event_and_ask_more, kb_relation
        )

    async def _custom_text_step(
        self, user_id: int, field: str, text: str, on_done, keyboard_fn
    ) -> None:
        """Текстовый ввод на шаге выбора: свой вариант, алиасы или свободный текст."""
        session = _session(user_id)
        if session["data"].get(f"awaiting_custom_{field}"):
            value = text.strip()
            if not value:
                await self._send(user_id, "Пожалуйста, введите текст.")
                return
            session["data"][field] = value
            session["data"][f"awaiting_custom_{field}"] = False
            await on_done(user_id)
            return

        smart = None
        if field == "occasion":
            smart = match_occasion(text)
        elif field == "relation":
            smart = match_relation(text)
        if smart:
            session["data"][field] = smart
            session["data"][f"awaiting_custom_{field}"] = False
            await self._send(user_id, f"Принято: **{smart}** ✅")
            await on_done(user_id)
            return

        value = text.strip()
        if field in ("occasion", "relation", "source") and len(value) >= 2:
            session["data"][field] = value
            session["data"][f"awaiting_custom_{field}"] = False
            await on_done(user_id)
            return

        await self._send(
            user_id,
            "Выберите вариант кнопкой, напишите текстом или нажмите «Свой вариант» 👇",
            keyboard=keyboard_fn(),
        )

    async def _save_event_and_ask_more(self, user_id: int) -> None:
        session = _session(user_id)
        data = session["data"]
        events = list(data.get("events", []))
        events.append(
            {
                "date": data["important_date"],
                "occasion": data["occasion"],
                "relation": data["relation"],
            }
        )
        data["events"] = events
        session["state"] = STATE_ADD_MORE
        await self._send(
            user_id,
            f"Событие сохранено ✅\n"
            f"📅 **{data['important_date']}** · {data['occasion']} · {data['relation']}\n\n"
            f"Всего важных дат: **{len(events)}**\n\n"
            "Хотите добавить ещё одну важную дату?",
            keyboard=kb_add_more_dates(),
        )

    async def step_add_more_text(self, user_id: int, text: str) -> None:
        await self._send(
            user_id,
            "Выберите: добавить ещё дату или завершить 👇",
            keyboard=kb_add_more_dates(),
        )

    async def _ask_budget(self, user_id: int) -> None:
        session = _session(user_id)
        session["state"] = STATE_BUDGET
        occasions = [e.get("occasion", "") for e in session["data"].get("events", [])]
        hint = budget_hint_for_occasions(occasions)
        # В MAX markdown **bold**; подсказку слегка упрощаем
        extra = ""
        if hint:
            extra = "\n\n💡 " + hint.replace("*", "**")
        await self._send(
            user_id,
            f"{progress(5)}\n\n"
            f"**Уровень бюджета букета?**{extra}\n\n"
            "_Выберите кнопку или напишите сумму, например «8000»_",
            keyboard=kb_budget(),
        )

    async def step_budget_text(self, user_id: int, text: str) -> None:
        mapped = match_budget(text) or (text.strip() if text.strip() in BUDGET_PRESETS else None)
        if not mapped:
            await self._send(
                user_id,
                "Выберите бюджет из кнопок или напишите сумму, например **8000** 👇",
                keyboard=kb_budget(),
            )
            return
        session = _session(user_id)
        session["data"]["budget"] = mapped
        if mapped != text.strip():
            await self._send(user_id, f"Бюджет: **{mapped}** ✅")
        await self._ask_source(user_id)

    async def _ask_source(self, user_id: int) -> None:
        _session(user_id)["state"] = STATE_SOURCE
        await self._send(
            user_id,
            f"{progress(6)}\n\n"
            "**Откуда вы узнали о нас?**\n\n"
            "_Выберите вариант или нажмите «Свой вариант»_",
            keyboard=kb_source(),
        )

    async def step_source_text(self, user_id: int, text: str) -> None:
        await self._custom_text_step(
            user_id, "source", text, self._finish_survey, kb_source
        )

    # ── Финал ─────────────────────────────────────────────────

    async def _finish_survey(self, user_id: int) -> None:
        session = _session(user_id)
        data = session["data"]
        events = list(data.get("events", []))

        profile = {
            "name": data.get("name", ""),
            "phone": data.get("phone", ""),
            "budget": data.get("budget", ""),
            "source": data.get("source", ""),
            "events": events,
        }

        logger.info(
            "MAX PROFILE user_id=%s | name=%s | phone=%s | budget=%s | source=%s | events=%s",
            user_id,
            profile["name"],
            profile["phone"],
            profile["budget"],
            profile["source"],
            json.dumps(events, ensure_ascii=False),
        )

        await self._send(
            user_id,
            f"{progress(7)}\n\n"
            "⏳ **Сохраняем анкету…**\n\n"
            "_Подождите несколько секунд — мы передаём ваши ответы флористу._",
        )

        posiflora_ok = False
        posiflora_meta: dict[str, Any] = {}

        await save_max_profile(user_id, profile)

        try:
            from posiflora import sync_survey_profile_to_posiflora

            posiflora_meta = await sync_survey_profile_to_posiflora(profile, user_id)
            posiflora_ok = bool(posiflora_meta.get("posiflora_ok"))
            logger.info(
                "Posiflora анкета (MAX): customer=%s, событий %s/%s",
                posiflora_meta.get("customer_id"),
                posiflora_meta.get("events_synced"),
                posiflora_meta.get("events_total"),
            )
        except Exception:
            logger.exception(
                "❌ Ошибка синхронизации анкеты MAX с Posiflora (user_id=%s)", user_id
            )

        await self._notify_florist(profile, user_id, posiflora_ok, posiflora_meta)

        events_block = _format_events_lines(events)
        posiflora_note = ""
        if not posiflora_ok:
            posiflora_note = (
                "\n\n⚠️ _Данные сохранены локально, но не удалось передать их в Posiflora. "
                "Флорист свяжется с вами вручную._"
            )
        elif posiflora_meta.get("events_failed") and not posiflora_meta.get(
            "celebrations_synced"
        ):
            posiflora_note = (
                "\n\n_Карточка клиента обновлена в Posiflora. "
                "Часть дат сохранена в заметках CRM._"
            )
        elif posiflora_meta.get("events_failed") and posiflora_meta.get(
            "celebrations_synced"
        ):
            posiflora_note = (
                "\n\n_Карточка клиента обновлена в Posiflora. "
                "Даты добавлены как праздники в CRM._"
            )

        await self._send(
            user_id,
            f"{progress(7)}\n\n"
            "✅ **Анкета сохранена!**\n\n"
            "┌─────────────────────\n"
            f"│ 👤 Клиент:  **{profile['name']}**\n"
            f"│ 📞 Телефон: **{profile['phone']}**\n"
            f"│ 💰 Бюджет:  **{profile['budget']}**\n"
            f"│ 📣 Источник: **{profile['source']}**\n"
            "│\n"
            "│ **Важные даты:**\n"
            f"{events_block}\n"
            "└─────────────────────\n\n"
            "Спасибо, что ответили на все вопросы! 🌷\n\n"
            "_Спасибо, что выбираете Veresk · trail of happiness_"
            f"{posiflora_note}",
        )
        _reset(user_id)

    async def _notify_florist(
        self,
        profile: dict[str, Any],
        user_id: int,
        posiflora_ok: bool,
        posiflora_meta: dict[str, Any],
    ) -> None:
        if not self.florist_chat_id:
            return
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        events_block = _format_events_lines(profile.get("events") or [])
        text = (
            "🌸 **Новая анкета — Veresk (MAX)**\n"
            f"_trail of happiness · {now}_\n\n"
            "┌─────────────────────\n"
            f"│ 👤 Клиент:      **{profile['name']}**\n"
            f"│ 📞 Телефон:     `{profile.get('phone', '—')}`\n"
            f"│ 📱 MAX user_id: `{user_id}`\n"
            f"│ 💰 Бюджет:      **{profile.get('budget', '—')}**\n"
            f"│ 📣 Источник:    **{profile.get('source', '—')}**\n"
            "│\n"
            "│ **Важные даты:**\n"
            f"{events_block}\n"
            "└─────────────────────"
        )
        meta = posiflora_meta or {}
        if posiflora_ok and meta.get("customer_id"):
            text += f"\n\n🆔 Posiflora клиент: `{meta['customer_id']}`"
            total = meta.get("events_total", 0)
            if total:
                text += f"\n📅 Событий в CRM: **{meta.get('events_synced', 0)}/{total}**"
        if not posiflora_ok:
            text += (
                "\n\n⚠️ **Анкета НЕ передана в Posiflora!**\n"
                "Создайте клиента и даты вручную по данным выше."
            )
        try:
            await self.api.send_message(chat_id=self.florist_chat_id, text=text)
            logger.info("🔔 Флорист уведомлён об анкете MAX user_id=%s", user_id)
        except Exception:
            logger.exception(
                "❌ Не удалось уведомить флориста в MAX chat_id=%s",
                self.florist_chat_id,
            )
