import os
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncEngine

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pomoshchnik")

ROOT = Path(__file__).resolve().parent
TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS = {1095057287, 7463822431}

bot = Bot(TOKEN) if TOKEN else None
dp = Dispatcher()
engine: AsyncEngine | None = None


def normalize_database_url(url: str) -> str:
    """Render may provide postgres:// or postgresql://; asyncpg needs postgresql+asyncpg://."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@dp.message(CommandStart())
async def start(message: Message):
    text_message = (
        "Привет! Это бот-помощник старосты: список группы, расписание и учёт "
        "посещаемости — всё в приложении."
        "Нажми кнопку ниже, чтобы открыть его. Там можно создать группу "
        "(как староста) или вступить по коду (как студент)."
        "Перед каждой парой бот будет присылать сюда опрос — просто отвечай на него."
    )
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await message.answer(text_message, reply_markup=kb)
    else:
        await message.answer(text_message + "\n\nУкажи WEBAPP_URL в настройках Render.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    poll_task = None
    if DATABASE_URL:
        try:
            engine = create_async_engine(
                normalize_database_url(DATABASE_URL),
                pool_pre_ping=True,
                connect_args={"ssl": "require"},
            )
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS app_users (
                        telegram_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS study_groups (
                        id BIGSERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        invite_code TEXT UNIQUE NOT NULL,
                        owner_telegram_id BIGINT NOT NULL REFERENCES app_users(telegram_id),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS group_members (
                        group_id BIGINT NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
                        telegram_id BIGINT NOT NULL REFERENCES app_users(telegram_id) ON DELETE CASCADE,
                        role TEXT NOT NULL DEFAULT 'student',
                        joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (group_id, telegram_id)
                    )
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS attendance (
                        id BIGSERIAL PRIMARY KEY,
                        group_id BIGINT NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
                        telegram_id BIGINT NOT NULL REFERENCES app_users(telegram_id) ON DELETE CASCADE,
                        lesson_date DATE NOT NULL,
                        status TEXT NOT NULL DEFAULT 'unknown',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (group_id, telegram_id, lesson_date)
                    )
                """))
            log.info("PostgreSQL connected; starter tables are ready.")
        except Exception:
            log.exception("Could not initialize PostgreSQL. Check DATABASE_URL and database status.")
            # Keep web service alive so logs and /health remain accessible.
    else:
        log.warning("DATABASE_URL is not set; database features are unavailable.")

    if bot and TOKEN:
        poll_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    if poll_task:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
    if bot:
        await bot.session.close()
    if engine:
        await engine.dispose()


app = FastAPI(title="Помощник — Староста Bot", lifespan=lifespan)
app.mount("/webapp", StaticFiles(directory=ROOT / "webapp"), name="webapp")


@app.get("/")
async def index():
    return FileResponse(ROOT / "webapp" / "index.html")


@app.get("/health")
async def health():
    db_ok = False
    if engine:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            log.exception("Database health check failed.")
    return {"status": "ok", "database_configured": bool(DATABASE_URL), "database_connected": db_ok}


@app.get("/api/status")
async def api_status():
    return await health()
