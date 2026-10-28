import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from fastapi import FastAPI
from fastapi.responses import FileResponse
import uvicorn

TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "pomoshchnik.db")
WEB_DIR = Path(__file__).parent / "webapp"

dp = Dispatcher()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                group_name TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

@dp.message(CommandStart())
async def start(message: Message):
    if WEBAPP_URL:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(
                text="Открыть Помощник",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )]],
            resize_keyboard=True
        )
        await message.answer(
            "Привет! 👋 Это «Помощник» — рабочая основа приложения для старосты.\\n"
            "Нажми кнопку ниже, чтобы открыть меню.",
            reply_markup=keyboard
        )
    else:
        await message.answer(
            "Привет! 👋 Бот «Помощник» создан.\\n\\n"
            "Чтобы открыть Mini App, добавь переменную WEBAPP_URL с HTTPS-ссылкой "
            "на опубликованное приложение."
        )

@dp.message(F.text == "/health")
async def health(message: Message):
    await message.answer("Бот работает ✅")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if TOKEN:
        bot = Bot(TOKEN)
        app.state.bot = bot
        polling_task = asyncio.create_task(dp.start_polling(bot))
    else:
        bot = None
        polling_task = None

    yield

    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
    if bot:
        await bot.session.close()

app = FastAPI(title="Помощник", lifespan=lifespan)

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")

@app.get("/health")
async def healthcheck():
    return {"status": "ok", "app": "Помощник"}

@app.get("/api/students")
async def get_students():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id, full_name, group_name FROM students ORDER BY full_name").fetchall()
    return [dict(row) for row in rows]

if __name__ == "__main__":
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT)
