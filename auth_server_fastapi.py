#!/opt/data/flytracker/backend/.venv/bin/python3
"""Auth API — FastAPI + PostgreSQL + Redis."""
import os, hashlib, hmac, secrets, asyncio
from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime

LOG_FORMAT = "%Y-%m-%d %H:%M:%S"

def log(level: str, msg: str):
    print(f"[{datetime.now().strftime(LOG_FORMAT)}][{level}] {msg}")

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, text
import redis.asyncio as aioredis

# ── Config ────────────────
PORT = int(os.getenv("PORT", "8000"))
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

engine = create_engine(DATABASE_URL, pool_size=2, max_overflow=2)
redis_client = None

# ── Password hashing ──────
def hash_pw(password: str) -> str:
    salt = os.urandom(32)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return salt.hex() + ":" + dk.hex()

def verify_pw(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False

# ── Pydantic models ────────
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class ChangePasswordRequest(BaseModel):
    token: str
    current_password: str
    new_password: str

class UpdatePhoneRequest(BaseModel):
    token: str
    telefone: str

class UserOut(BaseModel):
    id: int
    name: str
    email: str
    telefone: Optional[str] = None
    created_at: str

class AuthResponse(BaseModel):
    token: str
    user: UserOut

# ── Lifespan ───────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    # Verify DB on startup
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        log("INFO", "PostgreSQL connected")
    except Exception as e:
        log("ERROR", f"PostgreSQL: {e}")
    # Start alert checker in background
    try:
        import alert_checker
        await alert_checker.init_alertas_table(engine)
        asyncio.create_task(alert_checker.alert_loop(redis_client, engine))
        log("INFO", "Alert checker started — will check Gmail every 2h")
        if not os.getenv("COMPOSIO_API_KEY"):
            log("ALERTA", "COMPOSIO_API_KEY not set — alert checker will skip actual API calls")
    except Exception as e:
        log("ALERTA", f"Alert checker not available: {e}")
    yield
    await redis_client.close()
    await close_pg_pool()

app = FastAPI(title="Auth API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Endpoints ──────────────
@app.get("/health")
async def health():
    db_ok = False
    redis_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    try:
        await redis_client.ping()
        redis_ok = True
    except Exception:
        pass
    return {"status": "ok" if db_ok and redis_ok else "degraded", "db": db_ok, "redis": redis_ok}

@app.post("/api/register", response_model=AuthResponse)
async def register(req: RegisterRequest):
    name = req.name.strip()
    email = req.email.strip().lower()
    password = req.password

    if len(password) < 6:
        raise HTTPException(400, "Senha precisa ter no mínimo 6 caracteres")
    if not name or len(name) < 2:
        raise HTTPException(400, "Nome precisa ter no mínimo 2 caracteres")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Email inválido")

    with engine.begin() as conn:
        existing = conn.execute(text("SELECT id FROM usuarios WHERE email = :e"), {"e": email}).fetchone()
        if existing:
            raise HTTPException(409, "Este email já está cadastrado")

        pwhash = hash_pw(password)
        row = conn.execute(
            text("INSERT INTO usuarios (nome, email, senha_hash) VALUES (:n, :e, :h) RETURNING id, criado_em"),
            {"n": name, "e": email, "h": pwhash}
        ).fetchone()

    token = secrets.token_urlsafe(48)
    await redis_client.setex(f"session:{token}", 86400 * 30, str(row[0]))

    return AuthResponse(
        token=token,
        user=UserOut(id=row[0], name=name, email=email, telefone=None, created_at=str(row[1]))
    )

@app.post("/api/login", response_model=AuthResponse)
async def login(req: LoginRequest):
    email = req.email.strip().lower()
    password = req.password

    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Email inválido")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, nome, email, senha_hash, telefone, criado_em FROM usuarios WHERE email = :e"),
            {"e": email}
        ).fetchone()

    if not row:
        raise HTTPException(401, "Email não encontrado. Crie uma conta primeiro.")
    if not verify_pw(password, row[3]):
        raise HTTPException(401, "Senha incorreta.")

    token = secrets.token_urlsafe(48)
    await redis_client.setex(f"session:{token}", 86400 * 30, str(row[0]))

    return AuthResponse(
        token=token,
        user=UserOut(id=row[0], name=row[1], email=row[2], telefone=row[4], created_at=str(row[5]))
    )

@app.get("/api/me")
async def me(token: str):
    uid = await redis_client.get(f"session:{token}")
    if not uid:
        raise HTTPException(401, "Token inválido ou expirado")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, nome, email, telefone, criado_em FROM usuarios WHERE id = :id"),
            {"id": int(uid)}
        ).fetchone()

    if not row:
        raise HTTPException(404, "Usuário não encontrado")

    return UserOut(id=row[0], name=row[1], email=row[2], telefone=row[3], created_at=str(row[4]))

@app.post("/api/logout")
async def logout(token: str):
    await redis_client.delete(f"session:{token}")
    return {"ok": True}

@app.post("/api/change-password")
async def change_password(req: ChangePasswordRequest):
    uid = await redis_client.get(f"session:{req.token}")
    if not uid:
        raise HTTPException(401, "Token inválido ou expirado")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT senha_hash FROM usuarios WHERE id = :id"),
            {"id": int(uid)}
        ).fetchone()

    if not row:
        raise HTTPException(404, "Usuário não encontrado")

    if not verify_pw(req.current_password, row[0]):
        raise HTTPException(400, "Senha atual incorreta")

    if len(req.new_password) < 6:
        raise HTTPException(400, "Nova senha precisa ter no mínimo 6 caracteres")

    new_hash = hash_pw(req.new_password)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE usuarios SET senha_hash = :h WHERE id = :id"),
            {"h": new_hash, "id": int(uid)}
        )

    return {"ok": True, "message": "Senha alterada com sucesso"}

@app.post("/api/update-phone")
async def update_phone(req: UpdatePhoneRequest):
    uid = await redis_client.get(f"session:{req.token}")
    if not uid:
        raise HTTPException(401, "Token inválido ou expirado")

    phone = req.telefone.strip()
    if phone and len(phone) < 8:
        raise HTTPException(400, "Telefone inválido")

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE usuarios SET telefone = :t WHERE id = :id"),
            {"t": phone or None, "id": int(uid)}
        )

    return {"ok": True, "message": "Telefone atualizado com sucesso"}

from fastapi.responses import FileResponse
from award_scraper import fetch_award_data, format_for_frontend, close_pg_pool

import os
STATIC_DIR = os.path.join(os.path.dirname(__file__), "frontend")
INDEX_HTML = os.path.join(STATIC_DIR, "index.html")

@app.get("/api/search")
async def search_award(origin: str, dest: str, cabin: str = "economy"):
    """Search award and commercial data for a route."""
    data = await fetch_award_data(redis_client)
    result = format_for_frontend(data, origin.upper(), dest.upper(), cabin)
    return result

@app.get("/api/deals")
async def get_deals():
    """Get curated deals."""
    data = await fetch_award_data(redis_client)
    return {"deals": data.get("deals", []), "updated_at": data.get("updated_at")}

@app.get("/api/programs")
async def get_programs():
    """Get supported programs list."""
    return {"programs": [
        "Smiles (Gol)", "TudoAzul (Azul)", "LATAM Pass", "AAdvantage",
        "Delta SkyMiles", "United MileagePlus", "Flying Blue",
        "BA Avios", "Iberia Plus", "TAP Miles&Go"
    ]}

@app.get("/api/all-data")
async def get_all_data():
    """Get all award/commercial data (for frontend initialization)."""
    data = await fetch_award_data(redis_client)
    return data

@app.get("/api/alertas")
async def get_alertas():
    """Get stored alerts from Gmail parsing."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, origem, destino, milhas_max, programa, classe, data_viagem, criado_em FROM alertas ORDER BY criado_em DESC LIMIT 50")
            ).fetchall()
        return {
            "alertas": [
                {
                    "id": r[0],
                    "origem": r[1],
                    "destino": r[2],
                    "milhas_max": r[3],
                    "programa": r[4],
                    "classe": r[5],
                    "data_viagem": str(r[6]) if r[6] else None,
                    "criado_em": str(r[7]),
                }
                for r in rows
            ]
        }
    except Exception as e:
        return {"alertas": [], "error": str(e)}

@app.get("/")
@app.head("/")
async def serve_frontend():
    if not os.path.exists(INDEX_HTML):
        raise HTTPException(404, "Frontend not built yet")
    return FileResponse(INDEX_HTML, media_type="text/html")

# ── Main ────────────────────
if __name__ == "__main__":
    port = PORT
    log("INFO", "Buscador de Passagens — Auth API")
    log("INFO", f"Port: {port}")
    log("INFO", f"DB: {DATABASE_URL.split('@')[1].split('?')[0] if DATABASE_URL and '@' in DATABASE_URL else 'configured'}")
    log("INFO", f"Redis: {REDIS_URL.split('@')[1] if REDIS_URL and '@' in REDIS_URL else REDIS_URL or 'configured'}")
    has_key = bool(os.getenv("COMPOSIO_API_KEY"))
    log("INFO", f"Alert checker: {'composio-core installed' if has_key else '⚠️ COMPOSIO_API_KEY not set'}")
    uvicorn.run("auth_server_fastapi:app", host="0.0.0.0", port=port, log_level="info", access_log=True)
