import os
import json
import hmac
import hashlib
import secrets
import string
import time
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEB_APP_URL = os.getenv("WEB_APP_URL", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "yuzerki.db")
PORT = int(os.getenv("PORT", "8000"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(BASE_DIR), "web")
INDEX_FILE = os.path.join(WEB_DIR, "index.html")

SPIN_COST = 1000
START_BALANCE = 10000
DEV_CHANCE = 1 / 2_000_000

db = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")

db.execute("""CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    photo_url TEXT,
    balance INTEGER NOT NULL DEFAULT 10000,
    main_username TEXT,
    created_at INTEGER NOT NULL
)""")

db.execute("""CREATE TABLE IF NOT EXISTS usernames(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    username TEXT NOT NULL UNIQUE,
    length INTEGER NOT NULL,
    rarity TEXT NOT NULL,
    created_at INTEGER NOT NULL
)""")

db.execute("""CREATE TABLE IF NOT EXISTS spins(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    rarity TEXT NOT NULL,
    cost INTEGER NOT NULL,
    created_at INTEGER NOT NULL
)""")

db.execute("""CREATE TABLE IF NOT EXISTS promo_codes(
    code TEXT PRIMARY KEY,
    amount INTEGER NOT NULL,
    max_uses INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0
)""")
db.commit()


class InitRequest(BaseModel):
    initData: str = ""


class PromoRequest(InitRequest):
    code: str


def verify_init_data(init_data: str):
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN не настроен на сервере")
    if not init_data:
        raise HTTPException(401, "Открой игру через Telegram")

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", None)
        auth_date = int(pairs.get("auth_date", "0"))

        if not received_hash or not auth_date:
            raise ValueError

        if time.time() - auth_date > 86400:
            raise HTTPException(401, "Сессия Telegram устарела. Открой игру заново.")

        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret_key, check.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            raise HTTPException(401, "Неверная подпись Telegram")

        return json.loads(pairs["user"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Не удалось проверить Telegram")


def ensure_user(u):
    now = int(time.time())
    row = db.execute("SELECT tg_id FROM users WHERE tg_id=?", (u["id"],)).fetchone()

    if row:
        db.execute("""UPDATE users
            SET username=?, first_name=?, last_name=?, photo_url=?
            WHERE tg_id=?""",
            (u.get("username"), u.get("first_name"), u.get("last_name"),
             u.get("photo_url"), u["id"]))
    else:
        db.execute("""INSERT INTO users
            (tg_id, username, first_name, last_name, photo_url, balance, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (u["id"], u.get("username"), u.get("first_name"),
             u.get("last_name"), u.get("photo_url"), START_BALANCE, now))
    db.commit()


def rarity_for_length(n):
    return {4: "LEGENDARY", 5: "EPIC", 6: "RARE"}[n]


def generate_username():
    alphabet = string.ascii_lowercase + string.digits

    # DEV is not advertised. It is simply an extremely rare result.
    if secrets.randbelow(2_000_000) == 0:
        if not db.execute("SELECT 1 FROM usernames WHERE username='dev'").fetchone():
            return "dev", "DEV"

    for _ in range(200):
        n = secrets.choice((4, 5, 6))
        name = "".join(secrets.choice(alphabet) for _ in range(n))
        if not db.execute("SELECT 1 FROM usernames WHERE username=?", (name,)).fetchone():
            return name, rarity_for_length(n)

    raise HTTPException(503, "Не удалось подобрать свободный юз")


def public_profile(tg_id):
    u = db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    items = db.execute("""SELECT username,length,rarity,created_at
                          FROM usernames WHERE tg_id=? ORDER BY id DESC""",
                       (tg_id,)).fetchall()

    return {
        "user": {
            "id": u["tg_id"],
            "username": u["username"],
            "first_name": u["first_name"],
            "last_name": u["last_name"],
            "photo_url": u["photo_url"]
        },
        "balance": u["balance"],
        "main_username": u["main_username"],
        "usernames": [dict(x) for x in items]
    }


async def telegram_api(method, payload=None):
    if not BOT_TOKEN:
        return {}
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload or {})
        r.raise_for_status()
        return r.json()


async def set_bot_menu():
    if not BOT_TOKEN or not WEB_APP_URL:
        return
    await telegram_api("setMyCommands", {
        "commands": [
            {"command": "start", "description": "Открыть игру ЮЗЕРКИ"},
            {"command": "help", "description": "Помощь"}
        ]
    })


app = FastAPI(title="ЮЗЕРКИ API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)


@app.get("/")
def index():
    if not os.path.exists(INDEX_FILE):
        raise HTTPException(404, "index.html не найден")
    return FileResponse(INDEX_FILE)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/profile")
def profile(req: InitRequest):
    u = verify_init_data(req.initData)
    ensure_user(u)
    return public_profile(u["id"])


@app.post("/api/spin")
def spin(req: InitRequest):
    u = verify_init_data(req.initData)
    ensure_user(u)
    tg_id = u["id"]

    user = db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if user["balance"] < SPIN_COST:
        raise HTTPException(400, "Недостаточно монет")

    name, rarity = generate_username()
    now = int(time.time())

    db.execute("UPDATE users SET balance=balance-? WHERE tg_id=?", (SPIN_COST, tg_id))
    db.execute("""INSERT INTO usernames
        (tg_id, username, length, rarity, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (tg_id, name, len(name), rarity, now))

    if not user["main_username"]:
        db.execute("UPDATE users SET main_username=? WHERE tg_id=?", (name, tg_id))

    db.execute("""INSERT INTO spins
        (tg_id, username, rarity, cost, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (tg_id, name, rarity, SPIN_COST, now))
    db.commit()

    new_balance = db.execute(
        "SELECT balance FROM users WHERE tg_id=?", (tg_id,)
    ).fetchone()["balance"]

    return {
        "username": "@" + name,
        "rarity": rarity,
        "balance": new_balance
    }


@app.get("/api/leaderboard")
def leaderboard():
    rows = db.execute("""
        SELECT u.username, COUNT(s.id) AS total
        FROM users u
        LEFT JOIN spins s ON s.tg_id=u.tg_id
        GROUP BY u.tg_id
        ORDER BY total DESC
        LIMIT 50
    """).fetchall()

    return [
        {"username": x["username"] or "без_username", "total": x["total"]}
        for x in rows
    ]


@app.post("/api/promo")
def promo(req: PromoRequest):
    u = verify_init_data(req.initData)
    ensure_user(u)
    code = req.code.strip().upper()

    row = db.execute(
        "SELECT * FROM promo_codes WHERE code=?", (code,)
    ).fetchone()

    if not row:
        raise HTTPException(404, "Промокод не найден")
    if row["uses"] >= row["max_uses"]:
        raise HTTPException(400, "Лимит промокода исчерпан")

    db.execute("UPDATE users SET balance=balance+? WHERE tg_id=?",
               (row["amount"], u["id"]))
    db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))
    db.commit()

    balance = db.execute(
        "SELECT balance FROM users WHERE tg_id=?", (u["id"],)
    ).fetchone()["balance"]

    return {"ok": True, "balance": balance}


async def polling_loop():
    # Lightweight bot polling so /start can open the Mini App.
    if not BOT_TOKEN:
        return

    offset = 0
    await set_bot_menu()

    while True:
        try:
            data = await telegram_api("getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["message"]
            })

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")

                if text.startswith("/start"):
                    chat_id = msg["chat"]["id"]

                    if WEB_APP_URL:
                        keyboard = {
                            "inline_keyboard": [[
                                {
                                    "text": "🎲 Открыть ЮЗЕРКИ",
                                    "web_app": {"url": WEB_APP_URL}
                                }
                            ]]
                        }
                        await telegram_api("sendMessage", {
                            "chat_id": chat_id,
                            "text": (
                                "Привет! 👋\n\n"
                                "Добро пожаловать в «ЮЗЕРКИ».\n"
                                "Это развлекательная игра со случайными юзами."
                            ),
                            "reply_markup": keyboard
                        })
                    else:
                        await telegram_api("sendMessage", {
                            "chat_id": chat_id,
                            "text": "Бот запущен, но WEB_APP_URL ещё не настроен."
                        })

                elif text == "/help":
                    await telegram_api("sendMessage", {
                        "chat_id": chat_id,
                        "text": "Открой игру кнопкой «Открыть ЮЗЕРКИ» после /start."
                    })

        except Exception as e:
            print("Telegram polling:", e)
            await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(polling_loop())
    yield
    task.cancel()


app.router.lifespan_context = lifespan

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
