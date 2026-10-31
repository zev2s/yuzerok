import os, json, hmac, hashlib, sqlite3, secrets, string, time
from urllib.parse import parse_qsl
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEB_APP_URL = os.getenv("WEB_APP_URL", "")
DB_PATH = os.getenv("DATABASE_PATH", str(BASE / "yuzerki.db"))

app = FastAPI(title="Yuzerki API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        tg_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        photo_url TEXT,
        balance INTEGER NOT NULL DEFAULT 10000,
        main_username TEXT,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usernames(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL,
        username TEXT NOT NULL UNIQUE,
        length INTEGER NOT NULL,
        rarity TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS spins(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        rarity TEXT NOT NULL,
        cost INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS promo_codes(
        code TEXT PRIMARY KEY,
        amount INTEGER NOT NULL,
        max_uses INTEGER NOT NULL,
        uses INTEGER NOT NULL DEFAULT 0
    );
    """)
    con.commit()
    con.close()

init_db()

def verify_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        raise HTTPException(401, "Telegram authorization is not configured")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    auth_date = int(pairs.get("auth_date", "0"))
    if not received or time.time() - auth_date > 86400:
        raise HTTPException(401, "Invalid or expired Telegram data")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received):
        raise HTTPException(401, "Invalid Telegram signature")
    user = json.loads(pairs["user"])
    return user

def get_user(init_data: str):
    tg = verify_init_data(init_data)
    con = db()
    row = con.execute("SELECT * FROM users WHERE tg_id=?", (tg["id"],)).fetchone()
    if not row:
        con.execute(
            "INSERT INTO users(tg_id,username,first_name,last_name,photo_url,created_at) VALUES(?,?,?,?,?,?)",
            (tg["id"], tg.get("username"), tg.get("first_name",""), tg.get("last_name",""),
             tg.get("photo_url"), int(time.time()))
        )
        con.commit()
        row = con.execute("SELECT * FROM users WHERE tg_id=?", (tg["id"],)).fetchone()
    else:
        con.execute(
            "UPDATE users SET username=?,first_name=?,last_name=?,photo_url=? WHERE tg_id=?",
            (tg.get("username"), tg.get("first_name",""), tg.get("last_name",""),
             tg.get("photo_url"), tg["id"])
        )
        con.commit()
        row = con.execute("SELECT * FROM users WHERE tg_id=?", (tg["id"],)).fetchone()
    con.close()
    return tg, row

class Auth(BaseModel):
    initData: str

class AddUsername(BaseModel):
    initData: str
    username: str

class Promo(BaseModel):
    initData: str
    code: str

@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/profile")
def profile(a: Auth):
    tg, row = get_user(a.initData)
    con = db()
    items = con.execute(
        "SELECT username,rarity,length FROM usernames WHERE tg_id=? ORDER BY created_at DESC",
        (tg["id"],)
    ).fetchall()
    con.close()
    return {
        "user": {
            "id": tg["id"],
            "username": tg.get("username"),
            "first_name": tg.get("first_name",""),
            "last_name": tg.get("last_name",""),
            "photo_url": tg.get("photo_url"),
        },
        "balance": row["balance"],
        "main_username": row["main_username"] or ("@" + tg["username"] if tg.get("username") else None),
        "usernames": [dict(x) for x in items]
    }

def generate_username():
    # @dev — специальный скрытый сверхредкий результат.
    if secrets.randbelow(2_000_000) == 0:
        return "dev", "DEV"
    length = secrets.choice([4,5,6])
    chars = string.ascii_lowercase + string.digits
    name = "".join(secrets.choice(chars) for _ in range(length))
    rarity = {4:"LEGENDARY",5:"EPIC",6:"RARE"}[length]
    return name, rarity

@app.post("/api/spin")
def spin(a: Auth):
    tg, _ = get_user(a.initData)
    cost = 1000
    con = db()
    row = con.execute("SELECT balance FROM users WHERE tg_id=?", (tg["id"],)).fetchone()
    if row["balance"] < cost:
        con.close()
        raise HTTPException(400, "Недостаточно монет")
    name, rarity = generate_username()
    try:
        con.execute("UPDATE users SET balance=balance-? WHERE tg_id=?", (cost,tg["id"]))
        con.execute(
            "INSERT INTO usernames(tg_id,username,length,rarity,created_at) VALUES(?,?,?,?,?)",
            (tg["id"], name, len(name), rarity, int(time.time()))
        )
        con.execute(
            "INSERT INTO spins(tg_id,username,rarity,cost,created_at) VALUES(?,?,?,?,?)",
            (tg["id"], name, rarity, cost, int(time.time()))
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback()
        con.close()
        return spin(a)
    balance = con.execute("SELECT balance FROM users WHERE tg_id=?", (tg["id"],)).fetchone()["balance"]
    con.close()
    return {"username":"@"+name, "rarity":rarity, "balance":balance}

@app.post("/api/add-username")
def add_username(a: AddUsername):
    tg, _ = get_user(a.initData)
    u = a.username.strip().lower()
    if not u.startswith("@"):
        u = "@"+u
    if len(u) < 2 or len(u) > 33:
        raise HTTPException(400, "Неверный юз")
    con = db()
    try:
        con.execute(
            "INSERT INTO usernames(tg_id,username,length,rarity,created_at) VALUES(?,?,?,?,?)",
            (tg["id"],u[1:],max(0,len(u)-1),"ADDED",int(time.time()))
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        raise HTTPException(400, "Этот юз уже есть")
    con.close()
    return {"ok":True,"username":u}

@app.post("/api/promo")
def promo(a: Promo):
    tg, _ = get_user(a.initData)
    code = a.code.strip().upper()
    con = db()
    row = con.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
    if not row or row["uses"] >= row["max_uses"]:
        con.close()
        raise HTTPException(400, "Промокод недействителен")
    con.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (code,))
    con.execute("UPDATE users SET balance=balance+? WHERE tg_id=?", (row["amount"],tg["id"]))
    con.commit()
    balance=con.execute("SELECT balance FROM users WHERE tg_id=?", (tg["id"],)).fetchone()["balance"]
    con.close()
    return {"ok":True,"balance":balance}

@app.get("/api/leaderboard")
def leaderboard():
    con=db()
    rows=con.execute("""
      SELECT COALESCE(username,'user') username, COUNT(*) total
      FROM usernames GROUP BY tg_id ORDER BY total DESC LIMIT 20
    """).fetchall()
    con.close()
    return [dict(x) for x in rows]

@app.get("/")
def index():
    return FileResponse(ROOT/"web"/"index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT","8000")))
