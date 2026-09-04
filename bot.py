"""
Telegram-бот "Планировщик" — напоминания о делах с разбивкой по матрице Эйзенхауэра.

Запуск: python bot.py
Токен бота берётся из файла .env (переменная BOT_TOKEN).
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiohttp import web

import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

router = Router()

# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить дело")],
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📋 Все дела")],
        [KeyboardButton(text="⚙️ Настройки")],
    ],
    resize_keyboard=True,
)

CATEGORY_ORDER = [
    "urgent_important",
    "important_not_urgent",
    "urgent_not_important",
    "not_urgent_not_important",
]


def category_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key in CATEGORY_ORDER:
        rows.append(
            [InlineKeyboardButton(text=db.CATEGORIES[key], callback_data=f"cat:{key}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard() -> InlineKeyboardMarkup:
    options = [15, 30, 60, 120, 180, 1440]
    rows = []
    row = []
    for m in options:
        label = f"{m} мин" if m < 1440 else "1 день"
        row.append(InlineKeyboardButton(text=label, callback_data=f"remind:{m}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


REMIND_OPTIONS = [15, 30, 60, 120, 180, 1440]


def remind_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 1440:
        hours = minutes // 60
        return f"{hours} ч" if minutes % 60 == 0 else f"{minutes} мин"
    days = minutes // 1440
    return f"{days} день" if days == 1 else f"{days} дн."


def task_remind_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for m in REMIND_OPTIONS:
        mark = "✅ " if m in selected else ""
        row.append(
            InlineKeyboardButton(text=f"{mark}{remind_label(m)}", callback_data=f"toggle:{m}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="Готово ✔️", callback_data="remindsdone")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_actions_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done:{task_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{task_id}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Разбор даты/времени, введённых пользователем
# ---------------------------------------------------------------------------

TIME_RE = r"(?P<h>\d{1,2}):(?P<m>\d{2})"


def parse_datetime(text: str) -> datetime | None:
    text = text.strip().lower()
    time_match = re.search(TIME_RE, text)
    if not time_match:
        return None
    hour = int(time_match.group("h"))
    minute = int(time_match.group("m"))
    if hour > 23 or minute > 59:
        return None

    rest = text[: time_match.start()].strip()
    base = db.now()

    if rest.startswith("сегодня") or rest == "":
        target_date = base.date()
    elif rest.startswith("завтра"):
        target_date = (base + timedelta(days=1)).date()
    elif rest.startswith("послезавтра"):
        target_date = (base + timedelta(days=2)).date()
    else:
        # Форматы: ДД.ММ или ДД.ММ.ГГГГ
        date_match = re.match(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", rest)
        if not date_match:
            return None
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year_str = date_match.group(3)
        if year_str:
            year = int(year_str)
            if year < 100:
                year += 2000
        else:
            year = base.year
        try:
            target_date = datetime(year, month, day).date()
        except ValueError:
            return None
        if not year_str:
            candidate = datetime.combine(target_date, datetime.min.time(), tzinfo=db.TZ)
            if candidate.date() < base.date():
                target_date = target_date.replace(year=year + 1)

    try:
        result = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            hour,
            minute,
            tzinfo=db.TZ,
        )
    except ValueError:
        return None
    return result


def format_dt(dt_str: str) -> str:
    dt = datetime.fromisoformat(dt_str)
    return dt.strftime("%d.%m.%Y %H:%M")


# ---------------------------------------------------------------------------
# Состояния добавления дела
# ---------------------------------------------------------------------------


class AddTask(StatesGroup):
    title = State()
    when = State()
    category = State()
    remind = State()


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я помогу не забывать о делах и встречах.\n\n"
        "Каждое дело можно отнести к одной из категорий матрицы Эйзенхауэра:\n"
        f"{db.CATEGORIES['urgent_important']}\n"
        f"{db.CATEGORIES['important_not_urgent']}\n"
        f"{db.CATEGORIES['urgent_not_important']}\n"
        f"{db.CATEGORIES['not_urgent_not_important']}\n\n"
        "Нажми «➕ Добавить дело», чтобы создать первую задачу.",
        reply_markup=MAIN_MENU,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=MAIN_MENU)


@router.message(F.text == "➕ Добавить дело")
@router.message(Command("add"))
async def add_task_start(message: Message, state: FSMContext):
    await state.set_state(AddTask.title)
    await message.answer(
        "Как называется дело? (например: «Звонок клиенту»)\n\nОтменить — /cancel"
    )


@router.message(AddTask.title)
async def add_task_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if not title:
        await message.answer("Название не может быть пустым. Попробуй ещё раз.")
        return
    await state.update_data(title=title)
    await state.set_state(AddTask.when)
    await message.answer(
        "Когда это нужно сделать?\n\n"
        "Примеры:\n"
        "• сегодня 18:00\n"
        "• завтра 09:30\n"
        "• 25.12 14:00\n"
        "• 25.12.2026 14:00"
    )


@router.message(AddTask.when)
async def add_task_when(message: Message, state: FSMContext):
    dt = parse_datetime(message.text)
    if dt is None:
        await message.answer(
            "Не удалось распознать дату и время. Попробуй в формате:\n"
            "«сегодня 18:00», «завтра 09:30» или «25.12 14:00»."
        )
        return
    if dt < db.now():
        await message.answer(
            "Это время уже прошло. Укажи дату/время в будущем."
        )
        return
    await state.update_data(due_at=dt.isoformat())
    await state.set_state(AddTask.category)
    await message.answer(
        "К какой категории отнести это дело?", reply_markup=category_keyboard()
    )


@router.callback_query(AddTask.category, F.data.startswith("cat:"))
async def add_task_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]
    default_minutes = await db.get_remind_minutes(callback.from_user.id)
    await state.update_data(category=category, selected_reminds=[default_minutes])
    await state.set_state(AddTask.remind)

    await callback.message.edit_text(
        f"{db.CATEGORIES[category]}\n\n"
        "За сколько напомнить об этом деле? Можно выбрать сразу несколько "
        "вариантов — например «1 день» и «3 ч», чтобы не забыть накануне "
        "и незадолго до начала. Нажимайте варианты, потом «Готово».",
        reply_markup=task_remind_keyboard({default_minutes}),
    )
    await callback.answer()


@router.callback_query(AddTask.remind, F.data.startswith("toggle:"))
async def add_task_toggle_remind(callback: CallbackQuery, state: FSMContext):
    minutes = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    selected = set(data.get("selected_reminds", []))
    if minutes in selected:
        selected.discard(minutes)
    else:
        selected.add(minutes)
    await state.update_data(selected_reminds=list(selected))
    await callback.message.edit_reply_markup(reply_markup=task_remind_keyboard(selected))
    await callback.answer()


@router.callback_query(AddTask.remind, F.data == "remindsdone")
async def add_task_remind_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_reminds", [])
    if not selected:
        await callback.answer("Выберите хотя бы один вариант.", show_alert=True)
        return

    title = data["title"]
    due_at = datetime.fromisoformat(data["due_at"])
    category = data["category"]

    task_id = await db.add_task(
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        title=title,
        due_at=due_at,
        category=category,
    )
    remind_ats = sorted({due_at - timedelta(minutes=m) for m in selected})
    await db.add_reminders(task_id, remind_ats)
    await state.clear()

    labels = ", ".join(remind_label(m) for m in sorted(selected, reverse=True))
    await callback.message.edit_text(
        f"Готово! Дело «{title}» добавлено.\n"
        f"📌 {db.CATEGORIES[category]}\n"
        f"🕒 {due_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"🔔 Напомню за: {labels} до дела."
    )
    await callback.answer()


@router.message(F.text == "📅 Сегодня")
@router.message(Command("today"))
async def list_today(message: Message):
    tasks = await db.get_today_tasks(message.from_user.id)
    if not tasks:
        await message.answer("На сегодня дел нет 🎉")
        return
    await message.answer("Дела на сегодня:")
    for task_id, title, due_at, category in tasks:
        text = (
            f"{db.CATEGORIES[category]}\n"
            f"🕒 {format_dt(due_at)}\n"
            f"{title}"
        )
        await message.answer(text, reply_markup=task_actions_keyboard(task_id))


@router.message(F.text == "📋 Все дела")
@router.message(Command("list"))
async def list_all(message: Message):
    tasks = await db.get_pending_tasks(message.from_user.id)
    if not tasks:
        await message.answer("Дел пока нет. Нажми «➕ Добавить дело».")
        return

    by_category: dict[str, list] = {key: [] for key in CATEGORY_ORDER}
    for task_id, title, due_at, category in tasks:
        by_category.setdefault(category, []).append((task_id, title, due_at))

    for key in CATEGORY_ORDER:
        items = by_category.get(key, [])
        if not items:
            continue
        await message.answer(f"<b>{db.CATEGORIES[key]}</b>")
        for task_id, title, due_at in items:
            text = f"🕒 {format_dt(due_at)}\n{title}"
            await message.answer(text, reply_markup=task_actions_keyboard(task_id))


@router.callback_query(F.data.startswith("done:"))
async def task_done(callback: CallbackQuery):
    task_id = int(callback.data.split(":", 1)[1])
    await db.mark_done(task_id)
    await callback.message.edit_text(callback.message.text + "\n\n✅ Выполнено")
    await callback.answer("Отмечено как выполненное")


@router.callback_query(F.data.startswith("del:"))
async def task_delete(callback: CallbackQuery):
    task_id = int(callback.data.split(":", 1)[1])
    await db.delete_task(task_id)
    await callback.message.edit_text(callback.message.text + "\n\n🗑 Удалено")
    await callback.answer("Удалено")


@router.message(F.text == "⚙️ Настройки")
@router.message(Command("settings"))
async def settings_cmd(message: Message):
    current = await db.get_remind_minutes(message.from_user.id)
    await message.answer(
        f"Сейчас я напоминаю о делах за {current} мин. до срока.\n"
        "Выбери новое значение по умолчанию:",
        reply_markup=settings_keyboard(),
    )


@router.callback_query(F.data.startswith("remind:"))
async def settings_set(callback: CallbackQuery):
    minutes = int(callback.data.split(":", 1)[1])
    await db.set_remind_minutes(callback.from_user.id, minutes)
    await callback.message.edit_text(
        f"Готово! Теперь буду напоминать за {minutes} мин. до дела "
        "(для уже созданных дел время напоминания не меняется)."
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Фоновая проверка напоминаний
# ---------------------------------------------------------------------------


async def reminder_loop(bot: Bot):
    while True:
        try:
            due = await db.get_due_reminders()
            for reminder_id, chat_id, title, due_at, category, remind_at in due:
                dt = datetime.fromisoformat(due_at)
                left = dt - datetime.fromisoformat(remind_at)
                left_minutes = round(left.total_seconds() / 60)
                text = (
                    "🔔 Напоминание!\n\n"
                    f"{db.CATEGORIES[category]}\n"
                    f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} "
                    f"(через {remind_label(left_minutes)})\n"
                    f"{title}"
                )
                try:
                    await bot.send_message(chat_id, text)
                except Exception as e:
                    logger.warning("Не удалось отправить напоминание: %s", e)
                await db.mark_reminder_sent(reminder_id)
        except Exception:
            logger.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Мини веб-сервер (нужен только для бесплатного хостинга типа Render —
# такие сервисы ожидают, что приложение слушает HTTP-порт, и "усыпляют"
# сервис, если к нему никто не обращается; внешний "будильник" вроде
# UptimeRobot стучится сюда, чтобы бот не засыпал).
# ---------------------------------------------------------------------------


async def health(request):
    return web.Response(text="Бот работает ✅")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Веб-сервер запущен на порту %s", port)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


async def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Не найден BOT_TOKEN. Задай переменную окружения BOT_TOKEN "
            "(в Render — в разделе Environment; локально — в файле .env)."
        )

    await db.init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await start_web_server()
    asyncio.create_task(reminder_loop(bot))

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
