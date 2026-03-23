"""
universal_order_executor.py
===========================
Universal Order Executor — Level 3

Multi-platform adapter:
  1. MT5:        MetaTrader 5 library (FTMO, ICMarkets)
  2. API_REST:   HTTP REST API (Tradovate/Topstep/Alpaca)
  3. JSON_DUMP:  Write JSON file for OpenClaw Agent to read

ระบบ Retry/Fallback:
  - ส่งคำสั่ง → timeout? → retry 1 ครั้ง → fail? → log + abort
  - FIFO Queue ยังอยู่ (ป้องกัน HFT pattern)

ทุก adapter คืน ExecutionResult ที่เป็น unified format
"""

import os
import json
import time
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Empty
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("UniversalExecutor")


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class ExecutionResult:
    """Unified result จากทุก adapter"""
    success:    bool  = False
    order_id:   str   = ""
    symbol:     str   = ""
    side:       str   = ""
    size:       float = 0
    sizing_unit: str  = "SHARES"
    entry_price: float = 0
    stop_loss:  float = 0
    take_profit: float = 0
    method:     str   = ""          # MT5 | API_REST | JSON_DUMP
    message:    str   = ""
    metadata:   dict  = field(default_factory=dict)


# ============================================================
# ADAPTER: JSON_DUMP (for OpenClaw)
# ============================================================

class JsonDumpAdapter:
    """เขียน JSON signal ลง folder สำหรับ OpenClaw Agent"""

    def __init__(self, signal_dir: str = "./signals/"):
        self.signal_dir = Path(signal_dir)
        self.signal_dir.mkdir(parents=True, exist_ok=True)
        self._counter = 0

    def submit(self, symbol: str, side: str, size: float, sizing_unit: str,
               entry_price: float, stop_loss: float, take_profit: float,
               metadata: dict = None) -> ExecutionResult:
        self._counter += 1
        now = datetime.now(timezone.utc)
        sig_id = f"SIG-{now.strftime('%Y%m%d')}-{self._counter:04d}"

        payload = {
            "signal_id":   sig_id,
            "timestamp":   now.isoformat(),
            "action":      "OPEN",
            "ticker":      symbol,
            "side":        side.upper(),
            "size":        size,
            "sizing_unit": sizing_unit,
            "entry_price": round(entry_price, 6),
            "stop_loss":   round(stop_loss, 6),
            "take_profit": round(take_profit, 6),
            "timeframe":   "15m",
            "source":      "UniversalEngine-v3",
            "metadata":    metadata or {},
        }

        fname = f"order_{symbol}_{now.strftime('%Y%m%d_%H%M%S')}.json"
        fpath = self.signal_dir / fname
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        logger.info(f"📤 JSON Signal: {fname}")
        return ExecutionResult(
            success=True, order_id=sig_id, symbol=symbol, side=side,
            size=size, sizing_unit=sizing_unit, entry_price=entry_price,
            stop_loss=stop_loss, take_profit=take_profit,
            method="JSON_DUMP", message=f"Written to {fpath}",
        )

    def flatten_all(self):
        now = datetime.now(timezone.utc)
        payload = {
            "signal_id": f"FLAT-{now.strftime('%Y%m%d_%H%M%S')}",
            "timestamp": now.isoformat(),
            "action": "FLATTEN_ALL",
            "ticker": "*", "side": "CLOSE", "size": 0,
            "entry_price": 0, "stop_loss": 0, "take_profit": 0,
            "source": "UniversalEngine-v3",
        }
        fpath = self.signal_dir / f"flatten_{now.strftime('%Y%m%d_%H%M%S')}.json"
        with open(fpath, "w") as f:
            json.dump(payload, f, indent=2)
        logger.warning(f"📤 FLATTEN signal: {fpath.name}")


# ============================================================
# ADAPTER: MT5 (MetaTrader 5)
# ⚠️ IMPORTANT: MetaTrader5 Python library supports Windows ONLY
#    Linux users: ต้องรัน MT5 ผ่าน WINE + REST API wrapper
#    หรือเช่า Windows VPS สำหรับ FTMO execution
# ============================================================

class Mt5Adapter:
    """
    MetaTrader 5 execution (สำหรับ FTMO, ICMarkets)

    Platform Requirements:
      - Windows: ใช้ได้ตรง (pip install MetaTrader5)
      - Linux:   ❌ ImportError — ต้องใช้วิธีอ้อม:
        1. รัน MT5 Terminal ผ่าน WINE + ใช้ REST API wrapper (เช่น mt5-rest)
        2. เช่า Windows VPS (เช่น Contabo Windows VPS ~$8/เดือน)
        3. ใช้ JSON_DUMP adapter แทน + ให้ OpenClaw/MT5 EA อ่าน JSON
      - macOS:   ❌ ไม่รองรับ (เหมือน Linux)
    """

    def __init__(self):
        import platform as _platform

        self._mt5 = None
        self._os = _platform.system()  # 'Windows', 'Linux', 'Darwin'

        if self._os != "Windows":
            logger.warning(
                f"⚠️  MT5 adapter: OS={self._os} — MetaTrader5 library "
                f"supports Windows ONLY.\n"
                f"   Options for {self._os}:\n"
                f"   1. Use JSON_DUMP adapter + OpenClaw/MT5 EA\n"
                f"   2. Run MT5 via WINE + REST API wrapper\n"
                f"   3. Use Windows VPS for FTMO execution\n"
                f"   → MT5 adapter will return failure for all orders."
            )
            return

        try:
            import MetaTrader5 as mt5
            if mt5.initialize():
                self._mt5 = mt5
                logger.info(f"✅ MT5 initialized on {self._os}")
            else:
                err = mt5.last_error()
                logger.error(f"MT5 init failed: {err}")
        except ImportError:
            logger.warning(
                "MetaTrader5 library not installed.\n"
                "   Install: pip install MetaTrader5  (Windows only)\n"
                "   MT5 adapter disabled — orders will fail."
            )

    def submit(self, symbol: str, side: str, size: float, sizing_unit: str,
               entry_price: float, stop_loss: float, take_profit: float,
               metadata: dict = None) -> ExecutionResult:
        if not self._mt5:
            reason = (f"MT5 not available (OS={self._os}). "
                      f"Use JSON_DUMP or Windows VPS." if hasattr(self, '_os')
                      else "MT5 not initialized")
            return ExecutionResult(success=False, method="MT5", message=reason)

        mt5 = self._mt5
        order_type = mt5.ORDER_TYPE_BUY if side.upper() in ("LONG", "BUY") else mt5.ORDER_TYPE_SELL

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    symbol,
            "volume":    float(size),
            "type":      order_type,
            "price":     entry_price,
            "sl":        stop_loss,
            "tp":        take_profit,
            "deviation": 20,
            "magic":     19_151_500,      # unique magic number
            "comment":   "UniversalEngine",
            "type_time": mt5.ORDER_TIME_DAY,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            msg = f"MT5 error: {result.comment if result else 'None'}"
            logger.error(msg)
            return ExecutionResult(success=False, method="MT5", message=msg)

        return ExecutionResult(
            success=True, order_id=str(result.order), symbol=symbol,
            side=side, size=size, sizing_unit=sizing_unit,
            entry_price=result.price, stop_loss=stop_loss, take_profit=take_profit,
            method="MT5", message=f"Ticket #{result.order}",
        )

    def flatten_all(self):
        if not self._mt5:
            return
        mt5 = self._mt5
        positions = mt5.positions_get()
        if not positions:
            return
        for pos in positions:
            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
                "volume": pos.volume, "type": close_type,
                "position": pos.ticket, "deviation": 20,
                "comment": "Flatten", "type_time": mt5.ORDER_TIME_DAY,
            }
            mt5.order_send(request)
        logger.warning(f"MT5 FLATTEN: closed {len(positions)} positions")


# ============================================================
# ADAPTER: API_REST (Generic HTTP)
# ============================================================

class ApiRestAdapter:
    """HTTP REST API execution (Tradovate, Alpaca, etc.)"""

    def __init__(self, base_url: str = "", api_key: str = "", secret: str = ""):
        self.base_url = base_url
        self.api_key  = api_key
        self.secret   = secret
        # Alpaca-specific init
        self._alpaca_client = None
        if api_key and "alpaca" in base_url.lower() if base_url else True:
            try:
                from alpaca.trading.client import TradingClient
                paper = "paper" in base_url.lower() if base_url else True
                self._alpaca_client = TradingClient(api_key, secret, paper=paper)
                logger.info("✅ Alpaca REST adapter ready")
            except Exception as e:
                logger.warning(f"Alpaca init failed: {e}")

    def submit(self, symbol: str, side: str, size: float, sizing_unit: str,
               entry_price: float, stop_loss: float, take_profit: float,
               metadata: dict = None) -> ExecutionResult:
        import requests as req

        if self._alpaca_client:
            # Alpaca: ส่ง fractional ได้ → ไม่ int() ถ้า allow_fractional
            # (Config check อยู่ที่ risk_manager แล้ว → ที่นี่แค่ส่งตาม)
            alpaca_size = round(size, 4) if size != int(size) else int(size)
            return self._submit_alpaca(symbol, side, alpaca_size, entry_price,
                                       stop_loss, take_profit)

        # ── Generic REST fallback
        payload = {
            "symbol": symbol, "side": side, "qty": size,
            "price": entry_price, "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"}
        try:
            resp = req.post(f"{self.base_url}/orders", json=payload,
                            headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return ExecutionResult(
                success=True, order_id=str(data.get("id", "")),
                symbol=symbol, side=side, size=size, sizing_unit=sizing_unit,
                entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
                method="API_REST", message="OK",
            )
        except Exception as e:
            return ExecutionResult(success=False, method="API_REST", message=str(e))

    def _submit_alpaca(self, symbol, side, shares, entry, sl, tp) -> ExecutionResult:
        from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        order_side = OrderSide.BUY if side.upper() in ("LONG", "BUY") else OrderSide.SELL
        req = LimitOrderRequest(
            symbol=symbol, qty=shares, side=order_side,
            limit_price=round(entry, 2), time_in_force=TimeInForce.DAY,
            take_profit=TakeProfitRequest(limit_price=round(tp, 2)),
            stop_loss=StopLossRequest(stop_price=round(sl, 2)),
        )
        try:
            order = self._alpaca_client.submit_order(order_data=req)
            return ExecutionResult(
                success=True, order_id=str(order.id), symbol=symbol,
                side=side, size=shares, sizing_unit="SHARES",
                entry_price=entry, stop_loss=sl, take_profit=tp,
                method="API_REST", message=f"Alpaca order {order.status}",
            )
        except Exception as e:
            return ExecutionResult(success=False, method="API_REST", message=str(e))

    def flatten_all(self):
        if self._alpaca_client:
            try:
                self._alpaca_client.cancel_orders()
                self._alpaca_client.close_all_positions(cancel_orders=True)
                logger.warning("API_REST FLATTEN via Alpaca")
            except Exception as e:
                logger.error(f"Alpaca flatten error: {e}")


# ============================================================
# UNIVERSAL EXECUTOR — routing + retry + FIFO queue
# ============================================================

class UniversalOrderExecutor:
    """
    Main executor — routes to correct adapter based on Config.EXECUTION_METHOD

    Features:
      - Adapter routing: MT5 / API_REST / JSON_DUMP
      - FIFO Queue + 3.5s delay (HFT prevention)
      - Retry: 1 retry on failure
      - Flatten signal: flatten_all() → adapter-specific
    """

    ORDER_QUEUE_DELAY_SEC = 3.5
    MAX_RETRIES           = 1

    def __init__(self, execution_method: str = "JSON_DUMP", **kwargs):
        self.method = execution_method

        if execution_method == "JSON_DUMP":
            self.adapter = JsonDumpAdapter(
                signal_dir=kwargs.get("signal_dir", "./signals/")
            )
        elif execution_method == "MT5_PROXY":
            # ── Linux → Windows VPS proxy → MT5 → FTMO
            from mt5_proxy_client import Mt5ProxyAdapter
            self.adapter = Mt5ProxyAdapter(
                proxy_url=kwargs.get("mt5_proxy_url", ""),
                api_key=kwargs.get("mt5_proxy_api_key", ""),
                hmac_secret=kwargs.get("mt5_proxy_hmac_secret", ""),
                unhealthy_callback=kwargs.get("mt5_unhealthy_cb"),
                recovery_callback=kwargs.get("mt5_recovery_cb"),
            )
        elif execution_method == "MT5":
            self.adapter = Mt5Adapter()
        elif execution_method == "API_REST":
            self.adapter = ApiRestAdapter(
                base_url=kwargs.get("base_url", ""),
                api_key=kwargs.get("api_key", ""),
                secret=kwargs.get("secret", ""),
            )
        else:
            logger.warning(f"Unknown method '{execution_method}' → JSON_DUMP fallback")
            self.adapter = JsonDumpAdapter()

        # FIFO Queue
        self._queue: Queue = Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="ExecWorker")
        self._worker.start()

        # Health check adapter (Alpaca)
        self._health_client = None

        logger.info(f"✅ UniversalExecutor ready | method={self.method}")

    @classmethod
    def from_config(cls, cfg) -> "UniversalOrderExecutor":
        """สร้างจาก Config"""
        kwargs = {"signal_dir": cfg.SIGNAL_DIR}
        if cfg.EXECUTION_METHOD == "API_REST":
            key, secret = cfg.get_alpaca_keys()
            kwargs["api_key"] = key
            kwargs["secret"]  = secret
        elif cfg.EXECUTION_METHOD == "MT5_PROXY":
            proxy_cfg = cfg.get_mt5_proxy_config()
            kwargs.update(proxy_cfg)
            kwargs["mt5_unhealthy_cb"] = getattr(cfg, "_mt5_unhealthy_cb", None)
            kwargs["mt5_recovery_cb"]  = getattr(cfg, "_mt5_recovery_cb", None)
        return cls(execution_method=cfg.EXECUTION_METHOD, **kwargs)

    # ------------------------------------------
    # SUBMIT ORDER (with retry)
    # ------------------------------------------

    def submit_order(
        self, symbol: str, side: str, size: float, sizing_unit: str,
        entry_price: float, stop_loss: float, take_profit: float,
        metadata: dict = None,
    ) -> ExecutionResult:
        """Submit order with 1 retry on failure"""
        for attempt in range(1 + self.MAX_RETRIES):
            result = self.adapter.submit(
                symbol, side, size, sizing_unit,
                entry_price, stop_loss, take_profit, metadata
            )
            if result.success:
                return result
            if attempt < self.MAX_RETRIES:
                logger.warning(f"[Retry] {symbol} attempt {attempt+1} failed: {result.message}")
                time.sleep(1.0)

        logger.error(f"[Executor] {symbol} FAILED after {1+self.MAX_RETRIES} attempts")
        return result

    # ── Compatibility alias
    def submit_bracket_order(self, symbol, shares, side,
                              stop_loss_price, take_profit_price,
                              entry_price=None, metadata=None):
        """Compatibility with old main.py interface"""
        r = self.submit_order(
            symbol, side, shares, "SHARES",
            entry_price or 0, stop_loss_price, take_profit_price, metadata
        )
        # Return mock order object for journal compatibility
        class _Mock:
            def __init__(self, oid, st):
                self.id = oid; self.status = st
        return _Mock(r.order_id, "filled" if r.success else "failed") if r.success else None

    # ------------------------------------------
    # FLATTEN
    # ------------------------------------------

    def flatten_all_positions(self):
        self.flush_queue()
        self.adapter.flatten_all()

    # ------------------------------------------
    # FIFO QUEUE
    # ------------------------------------------

    def enqueue_order(self, **kwargs):
        self._queue.put(kwargs)

    def _worker_loop(self):
        while not self._stop.is_set():
            try:
                spec = self._queue.get(timeout=1.0)
            except Empty:
                continue
            try:
                self.submit_order(**spec)
            except Exception as e:
                logger.error(f"[Worker] {e}")
            finally:
                self._queue.task_done()
            if not self._queue.empty():
                time.sleep(self.ORDER_QUEUE_DELAY_SEC)

    def flush_queue(self) -> int:
        n = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                n += 1
            except Empty:
                break
        if n:
            logger.warning(f"Flushed {n} pending orders")
        return n

    def stop(self):
        self._stop.set()
        self._worker.join(timeout=5)

    # ------------------------------------------
    # HEALTH CHECK (via Alpaca for equities)
    # ------------------------------------------

    def check_system_health(self, max_daily_loss: float = 700.0) -> dict:
        """ตรวจ equity จาก Alpaca — ถ้าไม่มี → return OK"""
        client = getattr(self.adapter, "_alpaca_client", None)
        if not client:
            return {"status": "OK", "today_pnl": 0.0, "buying_power": 80_000.0}
        try:
            acct = client.get_account()
            pnl = float(acct.equity) - float(acct.last_equity)
            if pnl <= -(max_daily_loss - 100):
                return {"status": "HALT", "today_pnl": pnl}
            return {"status": "OK", "today_pnl": pnl,
                    "buying_power": float(acct.buying_power),
                    "equity": float(acct.equity)}
        except Exception:
            return {"status": "OK", "today_pnl": 0.0, "buying_power": 80_000.0}

    def get_open_positions(self) -> list:
        client = getattr(self.adapter, "_alpaca_client", None)
        if client:
            try:
                return client.get_all_positions()
            except Exception:
                pass
        return []


# ============================================================
if __name__ == "__main__":
    import tempfile
    logging.basicConfig(level=logging.INFO)
    print("=== UniversalOrderExecutor Test ===")

    with tempfile.TemporaryDirectory() as tmp:
        ex = UniversalOrderExecutor("JSON_DUMP", signal_dir=tmp)
        r = ex.submit_order("NVDA", "LONG", 34, "SHARES", 182.30, 179.56, 187.75)
        print(f"  {r.method}: success={r.success} id={r.order_id}")
        ex.flatten_all_positions()
        print(f"  Signals: {list(Path(tmp).glob('*.json'))}")
        ex.stop()
    print("✅ Done")