"""
platform/signal_api.py
======================
FastAPI API Gateway — Signal Distribution Middleware

Endpoints:
  POST /api/signals              — รับสัญญาณจาก Core Engine
  GET  /api/signals              — ดึงสัญญาณ (VIP = live, Guest = delayed)
  GET  /api/signals/{signal_id}  — ดึงสัญญาณเดี่ยว
  PUT  /api/signals/{signal_id}  — อัปเดต status (WON/LOST/CANCELLED)
  GET  /api/performance          — สถิติรวม (public)
  GET  /api/health               — Engine health check

  POST /api/auth/register        — สมัครสมาชิก
  POST /api/auth/login           — ล็อกอิน → JWT
  GET  /api/users/me             — ข้อมูล user ปัจจุบัน

  POST /admin/cancel/{signal_id} — Admin ยกเลิกสัญญาณ + แจ้ง LINE/Telegram
  GET  /admin/users              — Admin ดู users ทั้งหมด
  GET  /admin/stats              — Admin ดู MRR + active users
  GET  /admin/engine-status      — Admin ดู engine health

  POST /webhook/line             — LINE webhook callback
  POST /webhook/telegram         — Telegram webhook callback

Run:
  uvicorn platform.signal_api:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import time
import hmac
import json
import hashlib
import secrets
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
import base64

logger = logging.getLogger("SignalAPI")

# ── Database
from platform.models import init_db, Signal, User, Subscription, NotificationLog

DB_URL = os.getenv("DATABASE_URL", "sqlite:///platform/signals.db")
engine, SessionFactory = None, None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, SessionFactory
    engine, SessionFactory = init_db(DB_URL)
    logger.info(f"✅ DB connected: {DB_URL}")
    yield

app = FastAPI(
    title="Quant Agent SaaS — Signal API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Rate Limiting Middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """ป้องกัน DDoS — จำกัด request per IP per minute"""
    client_ip = request.client.host if request.client else "unknown"

    # ข้าม rate limit สำหรับ health check
    if request.url.path == "/api/health":
        return await call_next(request)

    if not _rate_limiter.check(client_ip):
        return JSONResponse(
            status_code=429,
            content={
                "error": "Rate limit exceeded",
                "detail": f"Max {RATE_LIMIT_PER_MINUTE} requests/minute",
                "retry_after_sec": 60,
            }
        )
    return await call_next(request)

# ── Simple API key auth (for Core Engine → API)
API_SECRET = os.getenv("SIGNAL_API_SECRET", "dev-secret-change-me")
ADMIN_KEY  = os.getenv("ADMIN_API_KEY", "admin-key-change-me")

# ── JWT Config
JWT_SECRET = os.getenv("JWT_SECRET", "change-me-in-production-" + secrets.token_hex(16))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 72       # token หมดอายุ 3 วัน

# ── Rate Limiting Config
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))

# ── Delayed signal hours for Guest
GUEST_DELAY_HOURS = 1


# ============================================================
# JWT IMPLEMENTATION (HMAC-SHA256)
# ============================================================

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (4 - len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_jwt(user_id: str, role: str, email: str = "") -> str:
    """
    สร้าง JWT token (HMAC-SHA256)

    Payload:
      sub: user_id
      role: GUEST/VIP/ADMIN
      email: user email
      exp: expiry timestamp
      iat: issued at
    """
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub":   user_id,
        "role":  role,
        "email": email,
        "exp":   now + JWT_EXPIRE_HOURS * 3600,
        "iat":   now,
    }

    h = _b64url_encode(json.dumps(header).encode())
    p = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{h}.{p}"
    sig = hmac.new(JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    s = _b64url_encode(sig)
    return f"{h}.{p}.{s}"


def verify_jwt(token: str) -> dict:
    """
    Verify JWT → คืน payload dict

    Raises:
      HTTPException 401 ถ้า token ไม่ valid หรือหมดอายุ
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid token format")

        h, p, s = parts
        # Verify signature
        signing_input = f"{h}.{p}"
        expected_sig = hmac.new(
            JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256
        ).digest()
        actual_sig = _b64url_decode(s)

        if not hmac.compare_digest(expected_sig, actual_sig):
            raise ValueError("Invalid signature")

        # Decode payload
        payload = json.loads(_b64url_decode(p))

        # Check expiry
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("Token expired")

        return payload

    except Exception as e:
        raise HTTPException(401, f"Invalid token: {e}")


def get_current_user(authorization: str = Header(None)):
    """
    FastAPI dependency: ดึง user จาก Authorization header

    Usage:
      @app.get("/api/protected")
      async def protected(user=Depends(get_current_user)):
          print(user["sub"], user["role"])
    """
    if not authorization:
        raise HTTPException(401, "Authorization header required")

    token = authorization
    if token.startswith("Bearer "):
        token = token[7:]

    return verify_jwt(token)


# ============================================================
# RATE LIMITER (In-Memory, per-IP)
# ============================================================

class RateLimiter:
    """
    Simple in-memory rate limiter (per IP address)

    Production: ใช้ Redis + slowapi แทน
    MVP: in-memory dict ก็เพียงพอ
    """

    def __init__(self, max_requests: int = 60, window_sec: int = 60):
        self.max_requests = max_requests
        self.window_sec   = window_sec
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, client_ip: str) -> bool:
        """True = ผ่าน, False = rate limited"""
        now = time.time()
        cutoff = now - self.window_sec

        # ลบ hits เก่า
        self._hits[client_ip] = [
            t for t in self._hits[client_ip] if t > cutoff
        ]

        if len(self._hits[client_ip]) >= self.max_requests:
            return False

        self._hits[client_ip].append(now)
        return True


_rate_limiter = RateLimiter(max_requests=RATE_LIMIT_PER_MINUTE, window_sec=60)


def get_db():
    session = SessionFactory()
    try:
        yield session
    finally:
        session.close()


def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_SECRET:
        raise HTTPException(401, "Invalid API key")


def verify_admin(x_admin_key: str = Header(None)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "Admin access required")


# ============================================================
# PYDANTIC MODELS
# ============================================================

class SignalCreate(BaseModel):
    signal_id:          str
    asset:              str
    timeframe:          str = "15m"
    action:             str                     # BUY / SELL / AVOID
    side:               str = "LONG"
    entry_low:          float = 0
    entry_high:         float = 0
    take_profit:        float = 0
    stop_loss:          float = 0
    risk_reward:        str = "1:2"
    technical_summary:  str = ""
    news_catalyst:      str = ""
    strategy_type:      str = "Trend Following"
    llm_cio_comment:    str = ""
    size:               float = 0
    sizing_unit:        str = "SHARES"
    sizing_multiplier:  float = 1.0
    ml_score:           int = 0
    regime_score:       float = 0
    confidence:         float = 0
    gate19_action:      str = ""
    profile_name:       str = ""
    metadata:           dict = Field(default_factory=dict)


class SignalUpdate(BaseModel):
    status:     Optional[str] = None
    exit_price: Optional[float] = None
    pnl_usd:   Optional[float] = None
    pnl_pct:   Optional[float] = None


class UserRegister(BaseModel):
    email:        str
    password:     str
    display_name: str = ""


class UserLogin(BaseModel):
    email:    str
    password: str


# ============================================================
# SIGNAL ENDPOINTS
# ============================================================

@app.post("/api/signals", tags=["Signals"])
async def create_signal(
    data: SignalCreate,
    db=Depends(get_db),
    _=Depends(verify_api_key),
):
    """
    รับสัญญาณจาก Core Engine (Gate 19)
    → บันทึกลง DB
    → Trigger LINE/Telegram notifications (async)
    """
    sig = Signal(
        signal_id=data.signal_id,
        asset=data.asset.upper(),
        timeframe=data.timeframe,
        action=data.action.upper(),
        side=data.side.upper(),
        entry_low=data.entry_low,
        entry_high=data.entry_high,
        take_profit=data.take_profit,
        stop_loss=data.stop_loss,
        risk_reward=data.risk_reward,
        technical_summary=data.technical_summary,
        news_catalyst=data.news_catalyst,
        strategy_type=data.strategy_type,
        llm_cio_comment=data.llm_cio_comment,
        size=data.size,
        sizing_unit=data.sizing_unit,
        sizing_multiplier=data.sizing_multiplier,
        ml_score=data.ml_score,
        regime_score=data.regime_score,
        confidence=data.confidence,
        gate19_action=data.gate19_action,
        profile_name=data.profile_name,
        raw_metadata=data.metadata,
        status="ACTIVE",
    )
    db.add(sig)
    db.commit()

    # ── Trigger notifications (fire-and-forget)
    _notify_all(sig, db)

    return {"ok": True, "signal_id": data.signal_id}


@app.get("/api/signals", tags=["Signals"])
async def list_signals(
    authorization: Optional[str] = Header(None),
    status: Optional[str] = None,
    asset: Optional[str] = None,
    limit: int = 20,
    db=Depends(get_db),
):
    """
    ดึงสัญญาณ:
      No token / Guest → delayed (>1 ชั่วโมง)
      VIP/Admin token   → ทั้งหมด including ACTIVE (live)

    Authorization: Bearer <jwt_token>
    """
    # ── Determine role from JWT (optional)
    role = "GUEST"
    if authorization:
        try:
            token = authorization.replace("Bearer ", "")
            payload = verify_jwt(token)
            role = payload.get("role", "GUEST")
        except HTTPException:
            role = "GUEST"   # invalid token → treat as guest

    q = db.query(Signal).order_by(Signal.timestamp.desc())

    if role not in ("VIP", "ADMIN"):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=GUEST_DELAY_HOURS)
        q = q.filter(Signal.timestamp < cutoff)

    if status:
        q = q.filter(Signal.status == status.upper())
    if asset:
        q = q.filter(Signal.asset == asset.upper())

    signals = q.limit(limit).all()
    return {"signals": [s.to_dict() for s in signals], "count": len(signals),
            "role": role}


@app.get("/api/signals/{signal_id}", tags=["Signals"])
async def get_signal(signal_id: str, db=Depends(get_db)):
    sig = db.query(Signal).filter(Signal.signal_id == signal_id).first()
    if not sig:
        raise HTTPException(404, "Signal not found")
    return sig.to_dict()


@app.put("/api/signals/{signal_id}", tags=["Signals"])
async def update_signal(
    signal_id: str,
    data: SignalUpdate,
    db=Depends(get_db),
    _=Depends(verify_api_key),
):
    """อัปเดต status เมื่อ trade ปิด (WON/LOST) หรือ admin cancel"""
    sig = db.query(Signal).filter(Signal.signal_id == signal_id).first()
    if not sig:
        raise HTTPException(404, "Signal not found")

    if data.status:
        sig.status = data.status.upper()
    if data.exit_price is not None:
        sig.exit_price = data.exit_price
    if data.pnl_usd is not None:
        sig.pnl_usd = data.pnl_usd
    if data.pnl_pct is not None:
        sig.pnl_pct = data.pnl_pct
    if data.status in ("WON", "LOST", "CANCELLED"):
        sig.closed_at = datetime.now(timezone.utc)

    db.commit()
    return {"ok": True, "signal_id": signal_id, "status": sig.status}


# ============================================================
# PERFORMANCE ENDPOINT (Public)
# ============================================================

@app.get("/api/performance", tags=["Performance"])
async def get_performance(db=Depends(get_db)):
    """สถิติรวม — Win Rate, Profit Factor, Max Drawdown (public)"""
    closed = db.query(Signal).filter(Signal.status.in_(["WON", "LOST"])).all()

    if not closed:
        return {"status": "no_data", "total_signals": 0}

    total   = len(closed)
    winners = [s for s in closed if s.status == "WON"]
    losers  = [s for s in closed if s.status == "LOST"]
    win_rate = round(len(winners) / total * 100, 1) if total else 0

    pnls = [s.pnl_usd or 0 for s in closed]
    total_pnl    = round(sum(pnls), 2)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999

    # Equity curve + max drawdown
    equity = 0.0; peak = 0.0; max_dd = 0.0
    equity_curve = []
    for s in sorted(closed, key=lambda x: x.timestamp or datetime.min):
        equity += (s.pnl_usd or 0)
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
        equity_curve.append({
            "date": s.timestamp.strftime("%Y-%m-%d") if s.timestamp else "",
            "equity": round(equity, 2),
            "pnl": round(s.pnl_usd or 0, 2),
        })

    # By asset breakdown
    by_asset = {}
    for s in closed:
        a = s.asset
        if a not in by_asset:
            by_asset[a] = {"total": 0, "wins": 0, "pnl": 0}
        by_asset[a]["total"] += 1
        by_asset[a]["wins"] += 1 if s.status == "WON" else 0
        by_asset[a]["pnl"] += (s.pnl_usd or 0)

    active_count = db.query(Signal).filter(Signal.status == "ACTIVE").count()

    return {
        "total_signals": total,
        "active_signals": active_count,
        "win_rate_pct": win_rate,
        "total_pnl_usd": total_pnl,
        "profit_factor": profit_factor,
        "max_drawdown_usd": round(max_dd, 2),
        "winners": len(winners),
        "losers": len(losers),
        "equity_curve": equity_curve[-100:],   # last 100
        "by_asset": by_asset,
    }


# ============================================================
# AUTH ENDPOINTS
# ============================================================

def _hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


@app.post("/api/auth/register", tags=["Auth"])
async def register(data: UserRegister, db=Depends(get_db)):
    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        raise HTTPException(400, "Email already registered")

    user = User(
        email=data.email,
        display_name=data.display_name or data.email.split("@")[0],
        password_hash=_hash_pw(data.password),
        role="GUEST",
    )
    db.add(user)
    db.commit()
    return {"ok": True, "user_id": user.id, "role": "GUEST"}


@app.post("/api/auth/login", tags=["Auth"])
async def login(data: UserLogin, db=Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()
    if not user or user.password_hash != _hash_pw(data.password):
        raise HTTPException(401, "Invalid credentials")

    # ── Real JWT (HMAC-SHA256)
    token = create_jwt(
        user_id=user.id,
        role=user.role if user.is_vip() else user.role,
        email=user.email or "",
    )

    return {
        "ok": True,
        "token": token,
        "token_type": "Bearer",
        "expires_in_hours": JWT_EXPIRE_HOURS,
        "user": user.to_dict(),
    }


@app.get("/api/users/me", tags=["Auth"])
async def get_me(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """ดึงข้อมูล user จาก JWT token (Authorization: Bearer <token>)"""
    user = db.query(User).filter(User.id == current_user["sub"]).first()
    if not user:
        raise HTTPException(404, "User not found")
    return user.to_dict()


# ============================================================
# ADMIN ENDPOINTS
# ============================================================

@app.post("/admin/cancel/{signal_id}", tags=["Admin"])
async def admin_cancel_signal(
    signal_id: str,
    db=Depends(get_db),
    _=Depends(verify_admin),
):
    """
    Admin ปุ่มฉุกเฉิน: Cancel signal + แจ้ง LINE/Telegram
    """
    sig = db.query(Signal).filter(Signal.signal_id == signal_id).first()
    if not sig:
        raise HTTPException(404, "Signal not found")

    sig.status = "CANCELLED"
    sig.closed_at = datetime.now(timezone.utc)
    db.commit()

    # ── แจ้งเตือน cancel
    _notify_cancel(sig, db)

    return {"ok": True, "signal_id": signal_id, "status": "CANCELLED"}


@app.get("/admin/users", tags=["Admin"])
async def admin_list_users(db=Depends(get_db), _=Depends(verify_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return {
        "users": [u.to_dict() for u in users],
        "total": len(users),
        "vip_count": sum(1 for u in users if u.is_vip()),
    }


@app.get("/admin/stats", tags=["Admin"])
async def admin_stats(db=Depends(get_db), _=Depends(verify_admin)):
    """Admin MRR + active users + signal stats"""
    total_users = db.query(User).count()
    vip_users = db.query(User).filter(User.role == "VIP").count()
    total_signals = db.query(Signal).count()
    active_signals = db.query(Signal).filter(Signal.status == "ACTIVE").count()

    # MRR from active subscriptions
    active_subs = db.query(Subscription).filter(
        Subscription.status == "ACTIVE"
    ).all()
    mrr = sum(s.amount_thb for s in active_subs)

    return {
        "total_users": total_users,
        "vip_users": vip_users,
        "total_signals": total_signals,
        "active_signals": active_signals,
        "mrr_thb": round(mrr, 2),
        "engine_status": _check_engine_health(),
    }


@app.get("/admin/engine-status", tags=["Admin"])
async def admin_engine_status(_=Depends(verify_admin)):
    return _check_engine_health()


def _check_engine_health() -> dict:
    """ตรวจว่า Python bot ยังทำงานอยู่ไหม (เช็คจาก signal ล่าสุด)"""
    try:
        _, SF = init_db(DB_URL)
        session = SF()
        latest = session.query(Signal).order_by(Signal.created_at.desc()).first()
        session.close()
        if latest:
            age_min = (datetime.now(timezone.utc) - latest.created_at).total_seconds() / 60
            return {
                "status": "HEALTHY" if age_min < 60 else "STALE",
                "last_signal_age_min": round(age_min, 1),
                "last_signal_id": latest.signal_id,
            }
    except Exception:
        pass
    return {"status": "UNKNOWN", "last_signal_age_min": -1}


# ============================================================
# WEBHOOK ENDPOINTS (LINE / Telegram)
# ============================================================

@app.post("/webhook/line", tags=["Webhooks"])
async def line_webhook(body: dict, db=Depends(get_db)):
    """LINE Messaging API webhook — รับ events จาก LINE"""
    events = body.get("events", [])
    for event in events:
        event_type = event.get("type", "")
        if event_type == "follow":
            line_uid = event["source"]["userId"]
            _register_line_user(line_uid, db)
        elif event_type == "message":
            # future: handle commands
            pass
    return {"ok": True}


@app.post("/webhook/telegram", tags=["Webhooks"])
async def telegram_webhook(body: dict, db=Depends(get_db)):
    """Telegram Bot webhook — รับ updates"""
    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "")

    if text == "/start":
        _register_telegram_user(chat_id, message.get("from", {}), db)
    elif text == "/status":
        pass  # future: send status
    return {"ok": True}


def _register_line_user(line_uid: str, db):
    existing = db.query(User).filter(User.line_user_id == line_uid).first()
    if not existing:
        user = User(line_user_id=line_uid, role="GUEST",
                     display_name=f"LINE-{line_uid[:8]}")
        db.add(user)
        db.commit()


def _register_telegram_user(chat_id: str, from_data: dict, db):
    existing = db.query(User).filter(User.telegram_chat_id == chat_id).first()
    if not existing:
        user = User(
            telegram_chat_id=chat_id,
            telegram_username=from_data.get("username", ""),
            display_name=from_data.get("first_name", f"TG-{chat_id[:8]}"),
            role="GUEST",
        )
        db.add(user)
        db.commit()


# ============================================================
# NOTIFICATION DISPATCHER
# ============================================================

def _notify_all(signal: Signal, db):
    """ส่ง notification ไปทุก VIP users ผ่าน LINE + Telegram"""
    try:
        from platform.notifier_line import send_signal_flex
        from platform.notifier_telegram import send_signal_telegram

        vip_users = db.query(User).filter(
            User.role.in_(["VIP", "ADMIN"]),
            User.is_active == True,
        ).all()

        for user in vip_users:
            # LINE
            if user.line_user_id:
                try:
                    send_signal_flex(user.line_user_id, signal.to_dict())
                    _log_notification(db, signal.signal_id, "LINE",
                                      user.line_user_id, "SENT")
                except Exception as e:
                    _log_notification(db, signal.signal_id, "LINE",
                                      user.line_user_id, "FAILED", str(e))

            # Telegram
            if user.telegram_chat_id:
                try:
                    send_signal_telegram(user.telegram_chat_id, signal.to_dict())
                    _log_notification(db, signal.signal_id, "TELEGRAM",
                                      user.telegram_chat_id, "SENT")
                except Exception as e:
                    _log_notification(db, signal.signal_id, "TELEGRAM",
                                      user.telegram_chat_id, "FAILED", str(e))
    except ImportError:
        logger.warning("Notifier modules not available")
    except Exception as e:
        logger.error(f"Notification dispatch error: {e}")


def _notify_cancel(signal: Signal, db):
    """แจ้ง cancel signal ไปทุก VIP"""
    try:
        from platform.notifier_line import send_cancel_message
        from platform.notifier_telegram import send_cancel_telegram

        vip_users = db.query(User).filter(
            User.role.in_(["VIP", "ADMIN"]), User.is_active == True,
        ).all()

        for user in vip_users:
            if user.line_user_id:
                try:
                    send_cancel_message(user.line_user_id, signal.to_dict())
                except Exception:
                    pass
            if user.telegram_chat_id:
                try:
                    send_cancel_telegram(user.telegram_chat_id, signal.to_dict())
                except Exception:
                    pass
    except ImportError:
        pass


def _log_notification(db, signal_id, channel, recipient, status, msg=""):
    log = NotificationLog(
        signal_id=signal_id, channel=channel,
        recipient=recipient, status=status, message=msg,
    )
    db.add(log)
    db.commit()


# ============================================================
# HEALTH
# ============================================================

@app.get("/api/health", tags=["System"])
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("platform.signal_api:app", host="0.0.0.0", port=8000, reload=True)
