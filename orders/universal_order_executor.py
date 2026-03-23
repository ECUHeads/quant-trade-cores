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
    # ── Enhancement: Fill Quality & Cost Tracking
    order_type:      str   = "MARKET"     # MARKET | LIMIT | STOP | STOP_LIMIT
    intended_price:  float = 0            # price we wanted
    actual_fill:     float = 0            # price we got
    slippage_usd:    float = 0            # |actual - intended| × size
    spread_at_entry: float = 0            # bid-ask spread at execution time
    is_requote:      bool  = False        # True if requoted before fill
    retcode:         int   = 0            # MT5 retcode / HTTP status
    fill_latency_ms: int   = 0           # time from submit to fill


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
               metadata: dict = None, order_type: str = "MARKET") -> ExecutionResult:
        self._counter += 1
        now = datetime.now(timezone.utc)
        sig_id = f"SIG-{now.strftime('%Y%m%d')}-{self._counter:04d}"

        payload = {
            "signal_id":   sig_id,
            "timestamp":   now.isoformat(),
            "action":      "OPEN",
            "order_type":  order_type,       # MARKET | LIMIT | STOP | STOP_LIMIT
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

        logger.info(f"📤 JSON Signal: {fname} [{order_type}]")
        return ExecutionResult(
            success=True, order_id=sig_id, symbol=symbol, side=side,
            size=size, sizing_unit=sizing_unit, entry_price=entry_price,
            stop_loss=stop_loss, take_profit=take_profit,
            method="JSON_DUMP", message=f"Written to {fpath}",
            order_type=order_type, intended_price=entry_price,
            actual_fill=entry_price,
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
               metadata: dict = None, order_type: str = "MARKET") -> ExecutionResult:
        if not self._mt5:
            reason = (f"MT5 not available (OS={self._os}). "
                      f"Use JSON_DUMP or Windows VPS." if hasattr(self, '_os')
                      else "MT5 not initialized")
            return ExecutionResult(success=False, method="MT5", message=reason)

        mt5 = self._mt5
        t0 = time.time()

        # ── Map order_type to MT5 action + type
        if order_type == "MARKET":
            action = mt5.TRADE_ACTION_DEAL
            mt5_type = mt5.ORDER_TYPE_BUY if side.upper() in ("LONG", "BUY") else mt5.ORDER_TYPE_SELL
        elif order_type == "LIMIT":
            action = mt5.TRADE_ACTION_PENDING
            mt5_type = mt5.ORDER_TYPE_BUY_LIMIT if side.upper() in ("LONG", "BUY") else mt5.ORDER_TYPE_SELL_LIMIT
        elif order_type == "STOP":
            action = mt5.TRADE_ACTION_PENDING
            mt5_type = mt5.ORDER_TYPE_BUY_STOP if side.upper() in ("LONG", "BUY") else mt5.ORDER_TYPE_SELL_STOP
        elif order_type == "STOP_LIMIT":
            action = mt5.TRADE_ACTION_PENDING
            mt5_type = mt5.ORDER_TYPE_BUY_STOP_LIMIT if side.upper() in ("LONG", "BUY") else mt5.ORDER_TYPE_SELL_STOP_LIMIT
        else:
            action = mt5.TRADE_ACTION_DEAL
            mt5_type = mt5.ORDER_TYPE_BUY if side.upper() in ("LONG", "BUY") else mt5.ORDER_TYPE_SELL

        # ── Capture spread before execution
        tick = mt5.symbol_info_tick(symbol)
        spread_at_entry = 0.0
        if tick:
            spread_at_entry = round(tick.ask - tick.bid, 8)

        request = {
            "action":    action,
            "symbol":    symbol,
            "volume":    float(size),
            "type":      mt5_type,
            "price":     entry_price,
            "sl":        stop_loss,
            "tp":        take_profit,
            "deviation": 20,
            "magic":     19_151_500,      # unique magic number
            "comment":   f"UE-{order_type}",
            "type_time": mt5.ORDER_TIME_DAY,
        }

        # ── For STOP_LIMIT: set stoplimit price
        if order_type == "STOP_LIMIT":
            request["stoplimit"] = entry_price

        result = mt5.order_send(request)
        latency_ms = int((time.time() - t0) * 1000)

        # ── Requote / failure detection
        REQUOTE_CODES = {10004, 10016, 10021}  # REQUOTE, PRICE_OFF, PRICE_CHANGED
        is_requote = False
        retcode = 0

        if result is None:
            return ExecutionResult(
                success=False, method="MT5", message="MT5 returned None",
                retcode=0, fill_latency_ms=latency_ms,
            )

        retcode = result.retcode

        if retcode in REQUOTE_CODES:
            is_requote = True
            logger.warning(
                f"⚡ MT5 REQUOTE {symbol}: code={retcode} "
                f"({result.comment}) intended={entry_price:.5f}"
            )

        if retcode != mt5.TRADE_RETCODE_DONE:
            msg = f"MT5 error: {result.comment} (code={retcode})"
            logger.error(msg)
            return ExecutionResult(
                success=False, method="MT5", message=msg,
                retcode=retcode, is_requote=is_requote,
                fill_latency_ms=latency_ms,
            )

        # ── Success: compute slippage
        actual_fill = result.price
        slippage = abs(actual_fill - entry_price) * size
        if sizing_unit == "LOTS":
            slippage = abs(actual_fill - entry_price) * size * 100_000  # approx for forex
        elif sizing_unit == "CONTRACTS":
            slippage = abs(actual_fill - entry_price) * size * 20  # approx for NQ

        return ExecutionResult(
            success=True, order_id=str(result.order), symbol=symbol,
            side=side, size=size, sizing_unit=sizing_unit,
            entry_price=actual_fill, stop_loss=stop_loss, take_profit=take_profit,
            method="MT5", message=f"Ticket #{result.order}",
            order_type=order_type,
            intended_price=entry_price,
            actual_fill=actual_fill,
            slippage_usd=round(slippage, 4),
            spread_at_entry=spread_at_entry,
            is_requote=is_requote,
            retcode=retcode,
            fill_latency_ms=latency_ms,
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
               metadata: dict = None, order_type: str = "MARKET") -> ExecutionResult:
        import requests as req

        t0 = time.time()

        if self._alpaca_client:
            # Alpaca: ส่ง fractional ได้ → ไม่ int() ถ้า allow_fractional
            # (Config check อยู่ที่ risk_manager แล้ว → ที่นี่แค่ส่งตาม)
            alpaca_size = round(size, 4) if size != int(size) else int(size)
            return self._submit_alpaca(symbol, side, alpaca_size, entry_price,
                                       stop_loss, take_profit, order_type)

        # ── Generic REST fallback
        payload = {
            "symbol": symbol, "side": side, "qty": size,
            "price": entry_price, "stop_loss": stop_loss,
            "take_profit": take_profit,
            "order_type": order_type,
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"}
        try:
            resp = req.post(f"{self.base_url}/orders", json=payload,
                            headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            latency_ms = int((time.time() - t0) * 1000)
            return ExecutionResult(
                success=True, order_id=str(data.get("id", "")),
                symbol=symbol, side=side, size=size, sizing_unit=sizing_unit,
                entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
                method="API_REST", message="OK",
                order_type=order_type, intended_price=entry_price,
                fill_latency_ms=latency_ms,
            )
        except Exception as e:
            return ExecutionResult(success=False, method="API_REST", message=str(e))

    def _submit_alpaca(self, symbol, side, shares, entry, sl, tp,
                       order_type="MARKET") -> ExecutionResult:
        from alpaca.trading.requests import (
            LimitOrderRequest, MarketOrderRequest,
            TakeProfitRequest, StopLossRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce

        t0 = time.time()
        order_side = OrderSide.BUY if side.upper() in ("LONG", "BUY") else OrderSide.SELL

        try:
            if order_type == "MARKET":
                try:
                    alpaca_req = MarketOrderRequest(
                        symbol=symbol, qty=shares, side=order_side,
                        time_in_force=TimeInForce.DAY,
                        take_profit=TakeProfitRequest(limit_price=round(tp, 2)),
                        stop_loss=StopLossRequest(stop_price=round(sl, 2)),
                    )
                except Exception:
                    # Fallback: some Alpaca SDK versions don't support bracket on market
                    alpaca_req = LimitOrderRequest(
                        symbol=symbol, qty=shares, side=order_side,
                        limit_price=round(entry, 2),
                        time_in_force=TimeInForce.DAY,
                        take_profit=TakeProfitRequest(limit_price=round(tp, 2)),
                        stop_loss=StopLossRequest(stop_price=round(sl, 2)),
                    )
            else:
                # LIMIT / STOP / STOP_LIMIT → all use LimitOrderRequest in Alpaca
                alpaca_req = LimitOrderRequest(
                    symbol=symbol, qty=shares, side=order_side,
                    limit_price=round(entry, 2),
                    time_in_force=TimeInForce.DAY,
                    take_profit=TakeProfitRequest(limit_price=round(tp, 2)),
                    stop_loss=StopLossRequest(stop_price=round(sl, 2)),
                )

            order = self._alpaca_client.submit_order(order_data=alpaca_req)
            latency_ms = int((time.time() - t0) * 1000)

            # Detect partial fill + compute slippage
            actual_fill = float(order.filled_avg_price or entry)
            slippage = abs(actual_fill - entry) * shares

            return ExecutionResult(
                success=True, order_id=str(order.id), symbol=symbol,
                side=side, size=shares, sizing_unit="SHARES",
                entry_price=actual_fill, stop_loss=sl, take_profit=tp,
                method="API_REST",
                message=f"Alpaca {order.status} [{order_type}]",
                order_type=order_type,
                intended_price=entry,
                actual_fill=actual_fill,
                slippage_usd=round(slippage, 4),
                fill_latency_ms=latency_ms,
            )
        except Exception as e:
            return ExecutionResult(
                success=False, method="API_REST", message=str(e),
                order_type=order_type,
            )

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
      - Retry: 1 retry on failure (skip retry on requote — price moved)
      - Flatten signal: flatten_all() → adapter-specific
      - Fill Quality Tracking: FillQualityTracker integration
      - Pending Orders: MARKET / LIMIT / STOP / STOP_LIMIT support
    """

    ORDER_QUEUE_DELAY_SEC = 3.5
    MAX_RETRIES           = 1

    def __init__(self, execution_method: str = "JSON_DUMP", **kwargs):
        self.method = execution_method

        if execution_method == "JSON_DUMP":
            self.adapter = JsonDumpAdapter(
                signal_dir=kwargs.get("signal_dir", "./signals/")
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

        # Fill Quality Tracker (Enhancement)
        self.fill_tracker = None
        try:
            from trading_cost_manager import FillQualityTracker
            self.fill_tracker = FillQualityTracker()
        except ImportError:
            pass  # trading_cost_manager.py not available — tracking disabled

        # Health check adapter (Alpaca)
        self._health_client = None

        logger.info(f"✅ UniversalExecutor ready | method={self.method} | "
                     f"fill_tracking={'ON' if self.fill_tracker else 'OFF'}")

    @classmethod
    def from_config(cls, cfg) -> "UniversalOrderExecutor":
        """สร้างจาก Config"""
        kwargs = {"signal_dir": cfg.SIGNAL_DIR}
        if cfg.EXECUTION_METHOD == "API_REST":
            key, secret = cfg.get_alpaca_keys()
            kwargs["api_key"] = key
            kwargs["secret"]  = secret
        return cls(execution_method=cfg.EXECUTION_METHOD, **kwargs)

    # ------------------------------------------
    # SUBMIT ORDER (with retry)
    # ------------------------------------------

    def submit_order(
        self, symbol: str, side: str, size: float, sizing_unit: str,
        entry_price: float, stop_loss: float, take_profit: float,
        metadata: dict = None, order_type: str = "MARKET",
    ) -> ExecutionResult:
        """
        Submit order with retry on failure.
        Requotes skip retry (price already changed — need new signal).

        Args:
            order_type: "MARKET" | "LIMIT" | "STOP" | "STOP_LIMIT"
        """
        for attempt in range(1 + self.MAX_RETRIES):
            result = self.adapter.submit(
                symbol, side, size, sizing_unit,
                entry_price, stop_loss, take_profit, metadata,
                order_type=order_type,
            )

            # Record fill quality (Enhancement)
            if self.fill_tracker and result.intended_price > 0:
                self.fill_tracker.record_fill(
                    symbol         = symbol,
                    intended_price = result.intended_price or entry_price,
                    actual_price   = result.actual_fill or result.entry_price,
                    retcode        = result.retcode,
                    is_requote     = result.is_requote,
                    fill_latency_ms = result.fill_latency_ms,
                )

            if result.success:
                return result

            # Don't retry on requote — price has moved, need fresh signal
            if result.is_requote:
                logger.warning(
                    f"[Executor] {symbol} REQUOTE — no retry (price moved)"
                )
                return result

            if attempt < self.MAX_RETRIES:
                logger.warning(f"[Retry] {symbol} attempt {attempt+1} failed: {result.message}")
                time.sleep(1.0)

        logger.error(f"[Executor] {symbol} FAILED after {1+self.MAX_RETRIES} attempts")
        return result

    def get_fill_quality_report(self, symbol: str = None) -> dict:
        """Get fill quality report from tracker (Enhancement)"""
        if self.fill_tracker:
            return self.fill_tracker.get_report(symbol)
        return {"fill_tracking": "disabled"}

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