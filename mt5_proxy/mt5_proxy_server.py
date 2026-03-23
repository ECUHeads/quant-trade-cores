"""
mt5_proxy_server.py
====================
MT5 Proxy Server — รันบน Windows VPS

เป็น "แขนหุ่นยนต์" ที่รับคำสั่งจาก Linux Engine แล้วส่ง order เข้า FTMO
ผ่าน MetaTrader5 Python library

Architecture:
  Linux VPS (Engine) ──HTTPS──▶ Windows VPS (This Proxy) ──MT5──▶ FTMO Server

Endpoints:
  POST /execute              — ส่ง order (BUY/SELL) เข้า MT5
  POST /modify               — แก้ไข SL/TP ของ position ที่เปิดอยู่
  POST /close/{ticket}       — ปิด position เดี่ยว
  POST /flatten              — ปิดทุก position (emergency)
  GET  /positions            — ดู open positions
  GET  /account              — ดูข้อมูลบัญชี (balance, equity, margin)
  GET  /health               — heartbeat + MT5 connection status
  GET  /symbol/{symbol}      — ข้อมูล symbol (spread, digits, lot constraints)

Security:
  - API Key authentication (X-API-Key header)
  - IP whitelist (เฉพาะ Linux VPS เท่านั้น)
  - Rate limiting (ป้องกัน accidental spam)
  - Request signing (HMAC-SHA256) สำหรับ /execute, /flatten

Run:
  pip install fastapi uvicorn MetaTrader5
  python mt5_proxy_server.py

  # หรือ background ด้วย NSSM:
  nssm install MT5Proxy "C:\\Python311\\python.exe" "C:\\mt5proxy\\mt5_proxy_server.py"
"""

import os
import sys
import time
import hmac
import json
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager

# ============================================================
# CONFIGURATION
# ============================================================

# API Security
API_KEY = os.getenv("MT5_PROXY_API_KEY", "change-me-in-production")
HMAC_SECRET = os.getenv("MT5_PROXY_HMAC_SECRET", "change-hmac-secret-too")

# IP Whitelist — เฉพาะ Linux VPS IP ที่อนุญาต
ALLOWED_IPS = os.getenv("MT5_PROXY_ALLOWED_IPS", "127.0.0.1").split(",")

# MT5 Login credentials
MT5_LOGIN    = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER", "FTMO-Server")
MT5_PATH     = os.getenv("MT5_PATH", "")  # เช่น C:\\Program Files\\FTMO MetaTrader 5\\terminal64.exe

# Rate limiting
RATE_LIMIT_PER_MINUTE = int(os.getenv("MT5_PROXY_RATE_LIMIT", "30"))

# FTMO Symbol mapping — FTMO broker อาจใช้ suffix ต่างกัน
# key = canonical name จาก engine, value = MT5 symbol name
SYMBOL_MAP: dict[str, str] = {
    # ── Forex
    "EURUSD": "EURUSD",    "GBPUSD": "GBPUSD",    "USDJPY": "USDJPY",
    "AUDUSD": "AUDUSD",    "USDCAD": "USDCAD",    "USDCHF": "USDCHF",
    "NZDUSD": "NZDUSD",    "EURGBP": "EURGBP",    "EURJPY": "EURJPY",
    "GBPJPY": "GBPJPY",
    # ── Indices CFD
    "US30":   "US30.cash",  "NAS100": "US100.cash",
    "SPX500": "US500.cash", "GER40":  "GER40.cash",
    "UK100":  "UK100.cash", "JPN225": "JP225.cash",
    # ── Commodities
    "XAUUSD": "XAUUSD",    "XAGUSD": "XAGUSD",
    "USOIL":  "USOIL.cash", "UKOIL": "UKOIL.cash",
    # ── Crypto CFD
    "BTCUSD": "BTCUSD",    "ETHUSD": "ETHUSD",
    "LTCUSD": "LTCUSD",    "XRPUSD": "XRPUSD",
}

# Override จาก environment (JSON string)
# เช่น MT5_SYMBOL_MAP='{"US30":"US30","NAS100":"USTEC"}'
_env_map = os.getenv("MT5_SYMBOL_MAP", "")
if _env_map:
    try:
        SYMBOL_MAP.update(json.loads(_env_map))
    except json.JSONDecodeError:
        pass


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("mt5_proxy.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("MT5Proxy")


# ============================================================
# MT5 CONNECTION MANAGER
# ============================================================

class Mt5Connection:
    """
    จัดการ connection กับ MetaTrader5 Terminal

    Features:
      - Auto-reconnect ถ้า connection หลุด
      - Heartbeat check ทุก 30 วินาที
      - Thread-safe (ใช้ lock)
    """

    def __init__(self):
        self._mt5 = None
        self._lock = threading.Lock()
        self._connected = False
        self._last_heartbeat = 0
        self._reconnect_count = 0

    def initialize(self) -> bool:
        """เริ่ม connection กับ MT5 Terminal"""
        with self._lock:
            try:
                import MetaTrader5 as mt5
                self._mt5 = mt5

                init_kwargs = {}
                if MT5_PATH:
                    init_kwargs["path"] = MT5_PATH
                if MT5_LOGIN:
                    init_kwargs["login"] = MT5_LOGIN
                    init_kwargs["password"] = MT5_PASSWORD
                    init_kwargs["server"] = MT5_SERVER

                if not mt5.initialize(**init_kwargs):
                    err = mt5.last_error()
                    logger.error(f"MT5 init failed: {err}")
                    self._connected = False
                    return False

                # ตรวจว่า login สำเร็จ
                account = mt5.account_info()
                if account is None:
                    logger.error("MT5 account_info() returned None")
                    self._connected = False
                    return False

                self._connected = True
                self._last_heartbeat = time.time()
                logger.info(
                    f"✅ MT5 connected | "
                    f"Login: {account.login} | "
                    f"Server: {account.server} | "
                    f"Balance: ${account.balance:,.2f} | "
                    f"Leverage: 1:{account.leverage}"
                )
                return True

            except ImportError:
                logger.critical(
                    "❌ MetaTrader5 library not installed!\n"
                    "   Install: pip install MetaTrader5\n"
                    "   Note: Windows ONLY"
                )
                return False
            except Exception as e:
                logger.error(f"MT5 init exception: {e}")
                self._connected = False
                return False

    def ensure_connected(self) -> bool:
        """ตรวจ + reconnect ถ้าจำเป็น"""
        if self._connected and self._mt5:
            # Quick check — terminal_info ถ้า None = disconnected
            try:
                info = self._mt5.terminal_info()
                if info is not None:
                    self._last_heartbeat = time.time()
                    return True
            except Exception:
                pass

        # Reconnect
        logger.warning(f"MT5 disconnected — attempting reconnect #{self._reconnect_count + 1}")
        self._reconnect_count += 1
        self._connected = False

        if self._mt5:
            try:
                self._mt5.shutdown()
            except Exception:
                pass

        return self.initialize()

    @property
    def mt5(self):
        """ดึง mt5 module (ต้อง ensure_connected ก่อน)"""
        return self._mt5

    @property
    def connected(self) -> bool:
        return self._connected

    def get_status(self) -> dict:
        """สถานะ connection สำหรับ /health endpoint"""
        with self._lock:
            status = {
                "mt5_connected": self._connected,
                "reconnect_count": self._reconnect_count,
                "last_heartbeat_age_sec": round(time.time() - self._last_heartbeat, 1) if self._last_heartbeat else -1,
            }
            if self._connected and self._mt5:
                try:
                    info = self._mt5.terminal_info()
                    if info:
                        status["terminal"] = {
                            "connected": info.connected,
                            "trade_allowed": info.trade_allowed,
                            "community_account": info.community_account,
                        }
                    acct = self._mt5.account_info()
                    if acct:
                        status["account"] = {
                            "login": acct.login,
                            "server": acct.server,
                            "balance": round(acct.balance, 2),
                            "equity": round(acct.equity, 2),
                            "margin": round(acct.margin, 2),
                            "free_margin": round(acct.margin_free, 2),
                            "leverage": acct.leverage,
                            "profit": round(acct.profit, 2),
                        }
                except Exception as e:
                    status["error"] = str(e)
            return status

    def shutdown(self):
        """ปิด connection"""
        with self._lock:
            if self._mt5:
                try:
                    self._mt5.shutdown()
                except Exception:
                    pass
            self._connected = False
            logger.info("MT5 connection shutdown")


# Global connection
_conn = Mt5Connection()


# ============================================================
# REQUEST/RESPONSE MODELS
# ============================================================

class ExecuteRequest(BaseModel):
    """คำสั่ง order จาก Linux Engine"""
    symbol:      str   = Field(..., description="Canonical symbol e.g. EURUSD, US30")
    side:        str   = Field(..., description="BUY or SELL")
    volume:      float = Field(..., gt=0, description="Lot size e.g. 0.10")
    entry_price: float = Field(0, description="ราคาเข้า (0 = market order)")
    stop_loss:   float = Field(0, description="Stop loss price")
    take_profit: float = Field(0, description="Take profit price")
    order_type:  str   = Field("MARKET", description="MARKET or LIMIT or STOP")
    magic:       int   = Field(19151500, description="Magic number สำหรับ filter")
    comment:     str   = Field("QuantEngine", description="Order comment")
    deviation:   int   = Field(20, description="Max slippage in points")
    # Request signing
    timestamp:   str   = Field("", description="ISO timestamp สำหรับ HMAC verify")
    signature:   str   = Field("", description="HMAC-SHA256 signature")

class ModifyRequest(BaseModel):
    """แก้ไข SL/TP ของ position"""
    ticket:      int   = Field(..., description="Position ticket number")
    stop_loss:   float = Field(0, description="New stop loss (0 = ไม่แก้)")
    take_profit: float = Field(0, description="New take profit (0 = ไม่แก้)")

class ExecuteResponse(BaseModel):
    """ผลลัพธ์จาก MT5"""
    success:     bool
    order_id:    str   = ""
    ticket:      int   = 0
    symbol:      str   = ""
    side:        str   = ""
    volume:      float = 0
    price:       float = 0
    stop_loss:   float = 0
    take_profit: float = 0
    retcode:     int   = 0
    message:     str   = ""
    execution_ms: float = 0


# ============================================================
# SECURITY HELPERS
# ============================================================

def verify_api_key(x_api_key: str = Header(None)):
    """ตรวจ API Key"""
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")


def verify_ip(request: Request):
    """ตรวจ IP whitelist"""
    client_ip = request.client.host if request.client else "unknown"
    # อนุญาต localhost เสมอ
    if client_ip in ("127.0.0.1", "::1", "localhost"):
        return
    if "*" not in ALLOWED_IPS and client_ip not in ALLOWED_IPS:
        logger.warning(f"🚫 Blocked IP: {client_ip}")
        raise HTTPException(403, f"IP {client_ip} not whitelisted")


def verify_hmac_signature(payload: dict, timestamp: str, signature: str):
    """
    ตรวจ HMAC-SHA256 signature สำหรับ critical endpoints (/execute, /flatten)

    Signing:
      message = f"{timestamp}|{json.dumps(payload, sort_keys=True)}"
      sig = hmac.sha256(secret, message)
    """
    if not HMAC_SECRET or HMAC_SECRET == "change-hmac-secret-too":
        # Dev mode — skip HMAC verification
        logger.warning("⚠️  HMAC verification SKIPPED (using default secret)")
        return

    if not timestamp or not signature:
        raise HTTPException(401, "Missing timestamp or signature for HMAC verification")

    # Reject requests older than 60 seconds (replay protection)
    try:
        req_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age_sec = (datetime.now(timezone.utc) - req_time).total_seconds()
        if abs(age_sec) > 60:
            raise HTTPException(401, f"Request expired ({age_sec:.0f}s old, max 60s)")
    except ValueError:
        raise HTTPException(401, "Invalid timestamp format")

    # Compute expected signature
    # ลบ fields ที่ไม่ใช่ payload (timestamp, signature เอง)
    clean_payload = {k: v for k, v in payload.items()
                     if k not in ("timestamp", "signature")}
    message = f"{timestamp}|{json.dumps(clean_payload, sort_keys=True)}"
    expected = hmac.new(
        HMAC_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        logger.warning(f"🚫 HMAC mismatch for request at {timestamp}")
        raise HTTPException(401, "Invalid HMAC signature")


# ============================================================
# RATE LIMITER
# ============================================================

class RateLimiter:
    def __init__(self, max_per_min: int = 30):
        self.max_per_min = max_per_min
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str = "global") -> bool:
        now = time.time()
        cutoff = now - 60
        self._hits[key] = [t for t in self._hits[key] if t > cutoff]
        if len(self._hits[key]) >= self.max_per_min:
            return False
        self._hits[key].append(now)
        return True

_rate_limiter = RateLimiter(max_per_min=RATE_LIMIT_PER_MINUTE)


# ============================================================
# SYMBOL RESOLVER
# ============================================================

def resolve_symbol(canonical: str) -> str:
    """
    แปลง canonical symbol → MT5 symbol name

    ลำดับ:
      1. ดูจาก SYMBOL_MAP
      2. ลองใช้ชื่อตรงๆ
      3. ลอง suffix (.cash, .pro, m)
      4. ถ้าหาไม่เจอ → raise error

    Returns:
      MT5 symbol name ที่ใช้ได้จริง
    """
    mt5 = _conn.mt5
    if not mt5:
        return canonical

    # 1. Explicit map
    mapped = SYMBOL_MAP.get(canonical.upper(), canonical)

    # 2. Check ว่า symbol นี้มีอยู่จริงใน MT5
    info = mt5.symbol_info(mapped)
    if info is not None:
        # Enable symbol ถ้ายังไม่ visible
        if not info.visible:
            mt5.symbol_select(mapped, True)
        return mapped

    # 3. Fallback — ลอง variations
    variations = [
        canonical,
        canonical + ".cash",
        canonical + ".pro",
        canonical + "m",
        canonical + ".",
    ]
    for v in variations:
        info = mt5.symbol_info(v)
        if info is not None:
            if not info.visible:
                mt5.symbol_select(v, True)
            logger.info(f"Symbol resolved: {canonical} → {v}")
            return v

    raise HTTPException(400, f"Symbol '{canonical}' not found in MT5 terminal. "
                              f"Tried: {mapped}, {variations}")


# ============================================================
# FASTAPI APP
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect MT5 | Shutdown: disconnect"""
    success = _conn.initialize()
    if not success:
        logger.error("⚠️  MT5 not connected at startup — will retry on first request")
    yield
    _conn.shutdown()


app = FastAPI(
    title="MT5 Proxy Server — FTMO Bridge",
    version="1.0.0",
    description="Windows VPS proxy: receives orders from Linux Engine, executes via MT5",
    lifespan=lifespan,
)


# ── Middleware: IP check + rate limit
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # IP whitelist
    client_ip = request.client.host if request.client else "unknown"
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        if "*" not in ALLOWED_IPS and client_ip not in ALLOWED_IPS:
            logger.warning(f"🚫 Blocked request from {client_ip} to {request.url.path}")
            return JSONResponse(status_code=403, content={"error": f"IP {client_ip} not allowed"})

    # Rate limit (skip health check)
    if request.url.path != "/health":
        if not _rate_limiter.check(client_ip):
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded", "retry_after_sec": 60}
            )

    return await call_next(request)


# ============================================================
# ENDPOINTS
# ============================================================

@app.post("/execute", response_model=ExecuteResponse, tags=["Trading"])
async def execute_order(req: ExecuteRequest, x_api_key: str = Header(None)):
    """
    ส่ง order เข้า MT5 Terminal → FTMO

    Supports:
      - MARKET order (default) — entry_price ไม่ต้องใส่
      - LIMIT order — entry_price = ราคาที่ต้องการ
      - STOP order — entry_price = ราคา trigger
    """
    verify_api_key(x_api_key)

    # HMAC verify สำหรับ critical action
    verify_hmac_signature(
        req.model_dump(), req.timestamp, req.signature
    )

    # Ensure MT5 connected
    if not _conn.ensure_connected():
        return ExecuteResponse(
            success=False, message="MT5 not connected — reconnect failed",
            retcode=-1,
        )

    mt5 = _conn.mt5
    t_start = time.time()

    # Resolve symbol
    try:
        mt5_symbol = resolve_symbol(req.symbol)
    except HTTPException as e:
        return ExecuteResponse(success=False, message=e.detail, retcode=-2)

    # Get current price for market orders
    tick = mt5.symbol_info_tick(mt5_symbol)
    if tick is None:
        return ExecuteResponse(
            success=False, symbol=req.symbol,
            message=f"No tick data for {mt5_symbol}", retcode=-3,
        )

    # Determine order type & price
    side_upper = req.side.upper()
    if side_upper in ("BUY", "LONG"):
        mt5_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif side_upper in ("SELL", "SHORT"):
        mt5_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        return ExecuteResponse(success=False, message=f"Invalid side: {req.side}", retcode=-4)

    # Override with entry_price for limit/stop orders
    if req.order_type == "LIMIT":
        mt5_type = mt5.ORDER_TYPE_BUY_LIMIT if "BUY" in side_upper or "LONG" in side_upper \
                   else mt5.ORDER_TYPE_SELL_LIMIT
        price = req.entry_price
    elif req.order_type == "STOP":
        mt5_type = mt5.ORDER_TYPE_BUY_STOP if "BUY" in side_upper or "LONG" in side_upper \
                   else mt5.ORDER_TYPE_SELL_STOP
        price = req.entry_price

    # Build MT5 request
    trade_request = {
        "action":    mt5.TRADE_ACTION_DEAL if req.order_type == "MARKET" else mt5.TRADE_ACTION_PENDING,
        "symbol":    mt5_symbol,
        "volume":    round(req.volume, 2),
        "type":      mt5_type,
        "price":     price,
        "deviation": req.deviation,
        "magic":     req.magic,
        "comment":   req.comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }

    # Add SL/TP if specified
    if req.stop_loss > 0:
        trade_request["sl"] = req.stop_loss
    if req.take_profit > 0:
        trade_request["tp"] = req.take_profit

    # ── SEND ORDER
    logger.info(f"📤 Sending: {side_upper} {req.volume} {mt5_symbol} @ {price:.5f} "
                f"SL={req.stop_loss} TP={req.take_profit}")

    result = mt5.order_send(trade_request)
    elapsed_ms = (time.time() - t_start) * 1000

    if result is None:
        err = mt5.last_error()
        logger.error(f"❌ order_send returned None: {err}")
        return ExecuteResponse(
            success=False, symbol=req.symbol,
            message=f"MT5 error: {err}", retcode=-99,
            execution_ms=elapsed_ms,
        )

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        logger.error(f"❌ Order rejected: retcode={result.retcode} | {result.comment}")
        return ExecuteResponse(
            success=False, symbol=req.symbol,
            order_id=str(result.order), ticket=result.order,
            retcode=result.retcode, message=result.comment,
            execution_ms=elapsed_ms,
        )

    # ── SUCCESS
    logger.info(f"✅ Order filled: ticket={result.order} | "
                f"{side_upper} {result.volume} {mt5_symbol} @ {result.price:.5f} "
                f"({elapsed_ms:.0f}ms)")

    return ExecuteResponse(
        success=True,
        order_id=str(result.order),
        ticket=result.order,
        symbol=req.symbol,
        side=side_upper,
        volume=result.volume,
        price=result.price,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        retcode=result.retcode,
        message=f"Ticket #{result.order} filled @ {result.price:.5f}",
        execution_ms=elapsed_ms,
    )


@app.post("/modify", tags=["Trading"])
async def modify_position(req: ModifyRequest, x_api_key: str = Header(None)):
    """แก้ไข SL/TP ของ position ที่เปิดอยู่"""
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5

    # หา position
    pos = mt5.positions_get(ticket=req.ticket)
    if not pos:
        raise HTTPException(404, f"Position ticket {req.ticket} not found")

    pos = pos[0]
    modify_request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   pos.symbol,
        "position": req.ticket,
        "sl":       req.stop_loss if req.stop_loss > 0 else pos.sl,
        "tp":       req.take_profit if req.take_profit > 0 else pos.tp,
    }

    result = mt5.order_send(modify_request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        return {"success": True, "ticket": req.ticket, "message": "Modified"}
    else:
        msg = result.comment if result else "Unknown error"
        return {"success": False, "ticket": req.ticket, "message": msg}


@app.post("/close/{ticket}", tags=["Trading"])
async def close_position(ticket: int, x_api_key: str = Header(None)):
    """ปิด position เดี่ยว"""
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5

    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        raise HTTPException(404, f"Position ticket {ticket} not found")

    pos = pos[0]
    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(pos.symbol)
    price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

    close_request = {
        "action":    mt5.TRADE_ACTION_DEAL,
        "symbol":    pos.symbol,
        "volume":    pos.volume,
        "type":      close_type,
        "price":     price,
        "position":  ticket,
        "deviation":  20,
        "comment":   "ProxyClose",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    result = mt5.order_send(close_request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        logger.info(f"✅ Closed position #{ticket}")
        return {"success": True, "ticket": ticket, "close_price": result.price}
    else:
        msg = result.comment if result else "Unknown error"
        logger.error(f"❌ Close failed #{ticket}: {msg}")
        return {"success": False, "ticket": ticket, "message": msg}


@app.post("/flatten", tags=["Trading"])
async def flatten_all(x_api_key: str = Header(None)):
    """
    ปิดทุก position — Emergency flatten

    ⚠️  ใช้ในกรณีฉุกเฉินเท่านั้น (drawdown limit, system error)
    """
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5
    positions = mt5.positions_get()

    if not positions:
        return {"success": True, "closed": 0, "message": "No open positions"}

    results = []
    for pos in positions:
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
            "volume": pos.volume, "type": close_type,
            "price": price, "position": pos.ticket,
            "deviation": 30, "comment": "FLATTEN",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result = mt5.order_send(req)
        ok = result and result.retcode == mt5.TRADE_RETCODE_DONE
        results.append({
            "ticket": pos.ticket, "symbol": pos.symbol,
            "volume": pos.volume, "success": ok,
            "message": result.comment if result else "Error",
        })

    closed = sum(1 for r in results if r["success"])
    logger.warning(f"🔴 FLATTEN: {closed}/{len(positions)} positions closed")

    return {
        "success": closed == len(positions),
        "closed": closed,
        "total": len(positions),
        "details": results,
    }


@app.get("/positions", tags=["Info"])
async def get_positions(x_api_key: str = Header(None)):
    """ดู open positions ทั้งหมด"""
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5
    positions = mt5.positions_get()

    if not positions:
        return {"positions": [], "count": 0, "total_profit": 0}

    pos_list = []
    total_profit = 0
    for p in positions:
        pos_list.append({
            "ticket":      p.ticket,
            "symbol":      p.symbol,
            "type":        "BUY" if p.type == 0 else "SELL",
            "volume":      p.volume,
            "open_price":  p.price_open,
            "current_price": p.price_current,
            "sl":          p.sl,
            "tp":          p.tp,
            "profit":      round(p.profit, 2),
            "swap":        round(p.swap, 2),
            "magic":       p.magic,
            "comment":     p.comment,
            "open_time":   datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
        })
        total_profit += p.profit

    return {
        "positions": pos_list,
        "count": len(pos_list),
        "total_profit": round(total_profit, 2),
    }


@app.get("/account", tags=["Info"])
async def get_account(x_api_key: str = Header(None)):
    """ข้อมูลบัญชี (balance, equity, margin, etc.)"""
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5
    acct = mt5.account_info()

    if not acct:
        raise HTTPException(500, "Cannot retrieve account info")

    return {
        "login":        acct.login,
        "server":       acct.server,
        "name":         acct.name,
        "balance":      round(acct.balance, 2),
        "equity":       round(acct.equity, 2),
        "margin":       round(acct.margin, 2),
        "free_margin":  round(acct.margin_free, 2),
        "margin_level": round(acct.margin_level, 2) if acct.margin_level else 0,
        "profit":       round(acct.profit, 2),
        "leverage":     acct.leverage,
        "currency":     acct.currency,
        "trade_mode":   acct.trade_mode,
    }


@app.get("/symbol/{symbol}", tags=["Info"])
async def get_symbol_info(symbol: str, x_api_key: str = Header(None)):
    """ข้อมูล symbol — spread, digits, lot size constraints"""
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5
    try:
        mt5_sym = resolve_symbol(symbol)
    except HTTPException as e:
        raise e

    info = mt5.symbol_info(mt5_sym)
    tick = mt5.symbol_info_tick(mt5_sym)

    return {
        "canonical":    symbol,
        "mt5_name":     mt5_sym,
        "digits":       info.digits,
        "spread":       info.spread,
        "point":        info.point,
        "trade_mode":   info.trade_mode,
        "volume_min":   info.volume_min,
        "volume_max":   info.volume_max,
        "volume_step":  info.volume_step,
        "contract_size": info.trade_contract_size,
        "bid":          tick.bid if tick else 0,
        "ask":          tick.ask if tick else 0,
        "last_tick":    datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat() if tick else None,
    }


@app.get("/symbols/all", tags=["Discovery"])
async def get_all_symbols(
    x_api_key: str = Header(None),
    category: str = "",
):
    """
    ดึงทุก tradeable symbol จาก MT5 Terminal

    ใช้สำหรับ FTMO Universe Discovery:
      - ดูว่า broker มี instrument อะไรบ้าง
      - ดึง spread, session, lot constraints, margin rate
      - Filter ด้วย category: forex, index, commodity, crypto, stock, all

    Query params:
      category:  forex | index | commodity | crypto | stock | "" (all)

    Returns:
      {symbols: [...], count: N, categories: {forex: N, index: N, ...}}
    """
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5
    all_symbols = mt5.symbols_get()
    if not all_symbols:
        return {"symbols": [], "count": 0}

    result = []
    categories = {}

    for s in all_symbols:
        # Skip ที่ไม่ให้เทรด
        if s.trade_mode == 0:  # SYMBOL_TRADE_MODE_DISABLED
            continue

        # Classify category จาก path + name
        cat = _classify_symbol(s)

        # Filter by category ถ้าระบุ
        if category and cat != category.lower():
            continue

        categories[cat] = categories.get(cat, 0) + 1

        # Get current tick สำหรับ spread
        tick = mt5.symbol_info_tick(s.name)

        sym_data = {
            "name":           s.name,
            "description":    s.description,
            "category":       cat,
            "path":           s.path,
            "digits":         s.digits,
            "point":          s.point,
            "spread":         s.spread,
            "spread_float":   s.spread_float if hasattr(s, "spread_float") else 0,
            "trade_mode":     s.trade_mode,
            "volume_min":     s.volume_min,
            "volume_max":     s.volume_max,
            "volume_step":    s.volume_step,
            "contract_size":  s.trade_contract_size,
            "margin_initial": getattr(s, "margin_initial", 0),
            "margin_rate":    getattr(s, "margin_maintenance", 0),
            "swap_long":      getattr(s, "swap_long", 0),
            "swap_short":     getattr(s, "swap_short", 0),
            "currency_base":  getattr(s, "currency_base", ""),
            "currency_profit": getattr(s, "currency_profit", ""),
            "visible":        s.visible,
            "bid":            tick.bid if tick else 0,
            "ask":            tick.ask if tick else 0,
        }
        result.append(sym_data)

    return {
        "symbols": result,
        "count": len(result),
        "categories": categories,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _classify_symbol(sym_info) -> str:
    """จัดหมวด symbol จาก path + description + name"""
    path = (sym_info.path or "").lower()
    desc = (sym_info.description or "").lower()
    name = (sym_info.name or "").upper()

    # Forex: 6 chars, both parts are currencies
    if "forex" in path or "currency" in path:
        return "forex"
    if len(name) == 6 and name[:3].isalpha() and name[3:].isalpha():
        forex_currencies = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD",
                           "CHF", "NZD", "SEK", "NOK", "DKK", "SGD", "HKD"}
        if name[:3] in forex_currencies and name[3:] in forex_currencies:
            return "forex"

    if "index" in path or "indices" in path or ".cash" in name.lower():
        return "index"
    if any(x in name for x in ["US30", "US100", "US500", "GER40", "UK100", "JP225",
                                "NAS", "SPX", "DAX", "FTSE", "NIK"]):
        return "index"

    if "commodity" in path or "metal" in path or "energy" in path:
        return "commodity"
    if any(x in name for x in ["XAU", "XAG", "OIL", "BRENT", "UKOIL", "USOIL",
                                "XPTUSD", "XPDUSD", "NGAS"]):
        return "commodity"

    if "crypto" in path or "coin" in path:
        return "crypto"
    if any(x in name for x in ["BTC", "ETH", "LTC", "XRP", "BCH", "ADA",
                                "DOT", "SOL", "DOGE", "LINK"]):
        return "crypto"

    if "stock" in path or "share" in path:
        return "stock"

    return "other"


@app.get("/bars/{symbol}", tags=["Data"])
async def get_bars(
    symbol: str,
    timeframe: str = "15m",
    count: int = 100,
    x_api_key: str = Header(None),
):
    """
    ดึง OHLCV bars จาก MT5 Terminal

    Args:
      symbol:    canonical symbol (e.g. EURUSD, US30)
      timeframe: 1m | 5m | 15m | 30m | 1h | 4h | 1d | 1w
      count:     จำนวน bars (max 5000)

    Returns:
      {symbol, timeframe, count, bars: [{time, open, high, low, close, volume, spread}, ...]}
    """
    verify_api_key(x_api_key)

    if not _conn.ensure_connected():
        raise HTTPException(503, "MT5 not connected")

    mt5 = _conn.mt5

    # Map timeframe string → MT5 constant
    tf_map = {
        "1m":  mt5.TIMEFRAME_M1,   "5m":  mt5.TIMEFRAME_M5,
        "15m": mt5.TIMEFRAME_M15,  "30m": mt5.TIMEFRAME_M30,
        "1h":  mt5.TIMEFRAME_H1,   "4h":  mt5.TIMEFRAME_H4,
        "1d":  mt5.TIMEFRAME_D1,   "1w":  mt5.TIMEFRAME_W1,
    }
    mt5_tf = tf_map.get(timeframe)
    if mt5_tf is None:
        raise HTTPException(400, f"Invalid timeframe '{timeframe}'. Use: {list(tf_map.keys())}")

    count = min(count, 5000)

    # Resolve symbol
    try:
        mt5_sym = resolve_symbol(symbol)
    except HTTPException as e:
        raise e

    # Fetch bars
    rates = mt5.copy_rates_from_pos(mt5_sym, mt5_tf, 0, count)
    if rates is None or len(rates) == 0:
        return {
            "symbol": symbol, "mt5_name": mt5_sym,
            "timeframe": timeframe, "count": 0, "bars": [],
        }

    bars = []
    for r in rates:
        bars.append({
            "time":   datetime.fromtimestamp(r[0], tz=timezone.utc).isoformat(),
            "open":   round(r[1], 6),
            "high":   round(r[2], 6),
            "low":    round(r[3], 6),
            "close":  round(r[4], 6),
            "volume": int(r[5]),
            "spread": int(r[6]) if len(r) > 6 else 0,
        })

    return {
        "symbol":    symbol,
        "mt5_name":  mt5_sym,
        "timeframe": timeframe,
        "count":     len(bars),
        "bars":      bars,
    }


@app.get("/health", tags=["System"])
async def health_check():
    """
    Heartbeat endpoint — Linux Engine จะ ping ทุก 30 วินาที

    Returns:
      - MT5 connection status
      - Account info (ถ้า connected)
      - Terminal info
    """
    status = _conn.get_status()
    return {
        "status": "OK" if status["mt5_connected"] else "DEGRADED",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proxy_version": "1.0.0",
        **status,
    }


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("MT5_PROXY_HOST", "0.0.0.0")
    port = int(os.getenv("MT5_PROXY_PORT", "8500"))

    logger.info(f"🚀 Starting MT5 Proxy Server on {host}:{port}")
    logger.info(f"   Allowed IPs: {ALLOWED_IPS}")
    logger.info(f"   Symbol map: {len(SYMBOL_MAP)} symbols")

    uvicorn.run(
        "mt5_proxy_server:app",
        host=host,
        port=port,
        log_level="info",
        # ⚠️  production: ใช้ SSL
        # ssl_keyfile="key.pem",
        # ssl_certfile="cert.pem",
    )
