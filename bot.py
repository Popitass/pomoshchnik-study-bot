import os
import asyncio
import logging
import secrets
import string
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
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
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def verify_init_data(init_data: str) -> dict:
    """Verify Telegram Mini App initData using the bot token."""
    if not TOKEN or not init_data:
        raise HTTPException(status_code=401, detail="Telegram initData is missing")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram initData hash is missing")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Telegram initData verification failed")
    try:
        user = json.loads(values.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Telegram user is unavailable")
    return {"id": user_id, "username": user.get("username"), "first_name": user.get("first_name", "")}


async def current_user(x_telegram_init_data: str = Header(default="")) -> dict:
    return verify_init_data(x_telegram_init_data)


async def query_one(sql: str, params: dict):
    if not engine:
        raise HTTPException(status_code=503, detail="Database is not configured")
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        row = result.mappings().first()
        return dict(row) if row else None


async def query_all(sql: str, params: dict):
    if not engine:
        raise HTTPException(status_code=503, detail="Database is not configured")
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return [dict(r) for r in result.mappings().all()]


async def ensure_user(user: dict):
    await query_one("""
        INSERT INTO app_users (telegram_id, username, first_name)
        VALUES (:id, :username, :first_name)
        ON CONFLICT (telegram_id) DO UPDATE SET
            username = EXCLUDED.username, first_name = EXCLUDED.first_name
        RETURNING telegram_id
    """, {"id": user["id"], "username": user.get("username"), "first_name": user.get("first_name", "")})


@dp.message(CommandStart())
async def start(message: Message):
    msg = (
        "Привет! Это бот-помощник старосты: список группы, расписание и учёт посещаемости — всё в приложении.\n\n"
        "Нажми кнопку ниже, чтобы открыть его. Там можно создать группу (как староста) или вступить по коду (как студент).\n\n"
        "Перед каждой парой бот будет присылать сюда опрос — просто отвечай на него."
    )
    if WEBAPP_URL:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
        await message.answer(msg, reply_markup=keyboard)
    else:
        await message.answer(msg + "\n\nУкажи WEBAPP_URL в настройках Render.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    poll_task = None
    if DATABASE_URL:
        try:
            engine = create_async_engine(normalize_database_url(DATABASE_URL), pool_pre_ping=True)
            async with engine.begin() as conn:
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS app_users (
                        telegram_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT NOT NULL DEFAULT '',
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
                        role TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('representative','student')),
                        joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (group_id, telegram_id)
                    )
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS lessons (
                        id BIGSERIAL PRIMARY KEY,
                        group_id BIGINT NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
                        subject TEXT NOT NULL,
                        starts_at TIMESTAMPTZ NOT NULL,
                        poll_minutes_before INTEGER NOT NULL DEFAULT 15,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS attendance (
                        id BIGSERIAL PRIMARY KEY,
                        group_id BIGINT NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
                        lesson_id BIGINT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
                        telegram_id BIGINT NOT NULL REFERENCES app_users(telegram_id) ON DELETE CASCADE,
                        status TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('present','absent','late','unknown')),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (lesson_id, telegram_id)
                    )
                """))
                # Миграция для баз, где attendance была создана старой версией кода.
                # CREATE TABLE IF NOT EXISTS не добавляет новые столбцы в существующую таблицу.
                await conn.execute(text("""
                    ALTER TABLE attendance
                    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS announcements (
                        id BIGSERIAL PRIMARY KEY,
                        group_id BIGINT NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
                        author_telegram_id BIGINT NOT NULL REFERENCES app_users(telegram_id),
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS materials (
                        id BIGSERIAL PRIMARY KEY,
                        group_id BIGINT NOT NULL REFERENCES study_groups(id) ON DELETE CASCADE,
                        author_telegram_id BIGINT NOT NULL REFERENCES app_users(telegram_id),
                        title TEXT NOT NULL,
                        url TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
            log.info("PostgreSQL ready.")
        except Exception:
            log.exception("Database initialization failed.")
    else:
        log.warning("DATABASE_URL is not set.")
    if bot:
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


app = FastAPI(title="Помощник старосты", lifespan=lifespan)
app.mount("/webapp", StaticFiles(directory=ROOT / "webapp"), name="webapp")


class CreateGroup(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class JoinGroup(BaseModel):
    invite_code: str = Field(min_length=4, max_length=32)


class CreateLesson(BaseModel):
    group_id: int
    subject: str = Field(min_length=1, max_length=150)
    starts_at: str
    poll_minutes_before: int = Field(default=15, ge=1, le=180)


class CreateTextItem(BaseModel):
    group_id: int
    title: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=5000)


class CreateMaterial(BaseModel):
    group_id: int
    title: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=1000)


async def require_member(group_id: int, user_id: int):
    row = await query_one(
        "SELECT role FROM group_members WHERE group_id=:gid AND telegram_id=:uid",
        {"gid": group_id, "uid": user_id}
    )
    if not row:
        raise HTTPException(status_code=403, detail="You are not a member of this group")
    return row["role"]


async def require_rep(group_id: int, user_id: int):
    role = await require_member(group_id, user_id)
    if role != "representative":
        raise HTTPException(status_code=403, detail="Representative permissions required")


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
            log.exception("Health check failed")
    return {"status": "ok", "database_configured": bool(DATABASE_URL), "database_connected": db_ok}


@app.get("/api/me")
async def me(user: dict = __import__("fastapi").Depends(current_user)):
    await ensure_user(user)
    groups = await query_all("""
        SELECT g.id, g.name, g.invite_code, gm.role
        FROM group_members gm JOIN study_groups g ON g.id=gm.group_id
        WHERE gm.telegram_id=:uid ORDER BY g.id DESC
    """, {"uid": user["id"]})
    return {"user": user, "admin": user["id"] in ADMIN_IDS, "groups": groups}


@app.post("/api/groups")
async def create_group(data: CreateGroup, user: dict = __import__("fastapi").Depends(current_user)):
    await ensure_user(user)
    code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    group = await query_one("""
        INSERT INTO study_groups(name, invite_code, owner_telegram_id)
        VALUES (:name, :code, :uid) RETURNING id, name, invite_code
    """, {"name": data.name.strip(), "code": code, "uid": user["id"]})
    await query_one("""
        INSERT INTO group_members(group_id, telegram_id, role)
        VALUES (:gid, :uid, 'representative') RETURNING group_id
    """, {"gid": group["id"], "uid": user["id"]})
    return {"group": group, "role": "representative"}


@app.post("/api/groups/join")
async def join_group(data: JoinGroup, user: dict = __import__("fastapi").Depends(current_user)):
    await ensure_user(user)
    group = await query_one(
        "SELECT id, name FROM study_groups WHERE invite_code=:code",
        {"code": data.invite_code.strip().upper()}
    )
    if not group:
        raise HTTPException(status_code=404, detail="Invite code not found")
    await query_one("""
        INSERT INTO group_members(group_id, telegram_id, role)
        VALUES (:gid, :uid, 'student')
        ON CONFLICT (group_id, telegram_id) DO NOTHING
        RETURNING group_id
    """, {"gid": group["id"], "uid": user["id"]})
    return {"group": group, "role": "student"}


@app.get("/api/groups/{group_id}")
async def group_details(group_id: int, user: dict = __import__("fastapi").Depends(current_user)):
    await require_member(group_id, user["id"])
    group = await query_one("SELECT id, name, invite_code FROM study_groups WHERE id=:gid", {"gid": group_id})
    members = await query_all("""
        SELECT u.telegram_id, u.username, u.first_name, gm.role
        FROM group_members gm JOIN app_users u ON u.telegram_id=gm.telegram_id
        WHERE gm.group_id=:gid ORDER BY gm.role DESC, u.first_name
    """, {"gid": group_id})
    lessons = await query_all("""
        SELECT id, subject, starts_at, poll_minutes_before
        FROM lessons WHERE group_id=:gid ORDER BY starts_at
    """, {"gid": group_id})
    announcements = await query_all("""
        SELECT id, title, body, created_at FROM announcements
        WHERE group_id=:gid ORDER BY created_at DESC
    """, {"gid": group_id})
    materials = await query_all("""
        SELECT id, title, url, created_at FROM materials
        WHERE group_id=:gid ORDER BY created_at DESC
    """, {"gid": group_id})
    return {"group": group, "members": members, "lessons": lessons,
            "announcements": announcements, "materials": materials}


@app.post("/api/lessons")
async def create_lesson(data: CreateLesson, user: dict = __import__("fastapi").Depends(current_user)):
    await require_rep(data.group_id, user["id"])
    lesson = await query_one("""
        INSERT INTO lessons(group_id, subject, starts_at, poll_minutes_before)
        VALUES (:gid, :subject, CAST(:starts_at AS timestamptz), :before)
        RETURNING id, group_id, subject, starts_at, poll_minutes_before
    """, {"gid": data.group_id, "subject": data.subject.strip(),
          "starts_at": data.starts_at, "before": data.poll_minutes_before})
    return {"lesson": lesson}


@app.post("/api/announcements")
async def create_announcement(data: CreateTextItem, user: dict = __import__("fastapi").Depends(current_user)):
    await require_rep(data.group_id, user["id"])
    item = await query_one("""
        INSERT INTO announcements(group_id, author_telegram_id, title, body)
        VALUES (:gid, :uid, :title, :body) RETURNING id, title, body, created_at
    """, {"gid": data.group_id, "uid": user["id"], "title": data.title.strip(), "body": data.body.strip()})
    return {"announcement": item}


@app.post("/api/materials")
async def create_material(data: CreateMaterial, user: dict = __import__("fastapi").Depends(current_user)):
    await require_rep(data.group_id, user["id"])
    item = await query_one("""
        INSERT INTO materials(group_id, author_telegram_id, title, url)
        VALUES (:gid, :uid, :title, :url) RETURNING id, title, url, created_at
    """, {"gid": data.group_id, "uid": user["id"], "title": data.title.strip(), "url": data.url.strip()})
    return {"material": item}


@app.post("/api/attendance")
async def set_attendance(data: dict, user: dict = __import__("fastapi").Depends(current_user)):
    group_id = int(data.get("group_id", 0))
    lesson_id = int(data.get("lesson_id", 0))
    target_id = int(data.get("telegram_id", 0))
    status = str(data.get("status", "unknown"))
    if status not in {"present", "absent", "late", "unknown"}:
        raise HTTPException(status_code=422, detail="Invalid attendance status")
    await require_rep(group_id, user["id"])
    lesson = await query_one("SELECT id FROM lessons WHERE id=:lid AND group_id=:gid",
                             {"lid": lesson_id, "gid": group_id})
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    member = await query_one("SELECT telegram_id FROM group_members WHERE group_id=:gid AND telegram_id=:uid",
                             {"gid": group_id, "uid": target_id})
    if not member:
        raise HTTPException(status_code=404, detail="Student not in group")
    row = await query_one("""
        INSERT INTO attendance(group_id, lesson_id, telegram_id, status)
        VALUES (:gid, :lid, :uid, :status)
        ON CONFLICT (lesson_id, telegram_id) DO UPDATE SET status=EXCLUDED.status, updated_at=NOW()
        RETURNING id, group_id, lesson_id, telegram_id, status
    """, {"gid": group_id, "lid": lesson_id, "uid": target_id, "status": status})
    return {"attendance": row}


@app.get("/api/groups/{group_id}/attendance")
async def attendance_list(group_id: int, user: dict = __import__("fastapi").Depends(current_user)):
    await require_member(group_id, user["id"])
    return {"attendance": await query_all("""
        SELECT a.lesson_id, a.telegram_id, a.status, a.updated_at, l.subject, l.starts_at
        FROM attendance a JOIN lessons l ON l.id=a.lesson_id
        WHERE a.group_id=:gid ORDER BY l.starts_at DESC
    """, {"gid": group_id})}
