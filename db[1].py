"""
Работа с базой данных (SQLite) для бота-планировщика.
"""

import aiosqlite
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_PATH = "planner.db"
TZ = ZoneInfo("Europe/Moscow")

DEFAULT_REMIND_MINUTES = 60

# Категории матрицы Эйзенхауэра
CATEGORIES = {
    "urgent_important": "🔴 Срочно и важно",
    "important_not_urgent": "🟡 Важно, не срочно",
    "urgent_not_important": "🟠 Срочно, не важно",
    "not_urgent_not_important": "⚪ Не срочно и не важно",
}


def now() -> datetime:
    return datetime.now(TZ)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                due_at TEXT NOT NULL,
                category TEXT NOT NULL,
                remind_at TEXT NOT NULL,
                reminded INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                remind_minutes_before INTEGER NOT NULL DEFAULT 60
            )
            """
        )
        await db.commit()


async def get_remind_minutes(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT remind_minutes_before FROM settings WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if row:
            return row[0]
        return DEFAULT_REMIND_MINUTES


async def set_remind_minutes(user_id: int, minutes: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (user_id, remind_minutes_before)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET remind_minutes_before = excluded.remind_minutes_before
            """,
            (user_id, minutes),
        )
        await db.commit()


async def add_task(
    user_id: int,
    chat_id: int,
    title: str,
    due_at: datetime,
    category: str,
    remind_minutes_before: int,
):
    remind_at = due_at - timedelta(minutes=remind_minutes_before)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tasks (user_id, chat_id, title, due_at, category, remind_at, reminded, done, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
            """,
            (
                user_id,
                chat_id,
                title,
                due_at.isoformat(),
                category,
                remind_at.isoformat(),
                now().isoformat(),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def get_pending_tasks(user_id: int, category: str | None = None):
    query = "SELECT id, title, due_at, category FROM tasks WHERE user_id = ? AND done = 0"
    params = [user_id]
    if category:
        query += " AND category = ?"
        params.append(category)
    query += " ORDER BY due_at ASC"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, params)
        return await cur.fetchall()


async def get_today_tasks(user_id: int):
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, title, due_at, category FROM tasks
            WHERE user_id = ? AND done = 0 AND due_at >= ? AND due_at < ?
            ORDER BY due_at ASC
            """,
            (user_id, start.isoformat(), end.isoformat()),
        )
        return await cur.fetchall()


async def get_task(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, chat_id, title, due_at, category, done FROM tasks WHERE id = ?",
            (task_id,),
        )
        return await cur.fetchone()


async def mark_done(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
        await db.commit()


async def delete_task(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()


async def get_due_reminders():
    """Задачи, для которых пора отправить напоминание."""
    current = now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT id, chat_id, title, due_at, category FROM tasks
            WHERE done = 0 AND reminded = 0 AND remind_at <= ?
            """,
            (current,),
        )
        return await cur.fetchall()


async def mark_reminded(task_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET reminded = 1 WHERE id = ?", (task_id,))
        await db.commit()
