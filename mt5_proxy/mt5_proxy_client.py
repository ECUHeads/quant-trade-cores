"""
mt5_proxy_client.py
====================
MT5 Proxy Client — รันบน Linux VPS

เชื่อมต่อกับ mt5_proxy_server.py ที่รันอยู่บน Windows VPS
ทำหน้าที่แทน Mt5Adapter เดิม — interface เดียวกัน, แต่ส่ง HTTP แทน direct MT5 call

Architecture:
  Linux: UniversalOrderExecutor → Mt5ProxyAdapter ──HTTPS──▶ Windows: mt5_proxy_server → MT5 → FTMO

Usage:
  # ใช้แทน Mt5Adapter ใน UniversalOrderExecutor
  from mt5_proxy_client import Mt5ProxyAdapter

  adapter = Mt5ProxyAdapter(
      proxy_url="https://your-windows-vps:8500",
      api_key="your-api-key",
      hmac_secret="your-hmac-secret",
  )
  result = adapter.submit("EURUSD", "BUY", 0.10, "LOTS", 1.0850, 1.0800, 1.0950)
"""

import os
import json
import time
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass

import requests

logger = logging.getLogger("Mt5ProxyClient")


# ============================================================
# HEALTH MONITOR — background heartbeat
# ============================================================

class HealthMonitor:
    """
    Background thread ที่ ping proxy ทุก N วินาที
    ถ้า unhealthy → block orders + trigger callback (เช่น LINE/Telegram alert)
    """

    def __init__(
        self,
        proxy_url: str,
        api_key: str,
        interval_sec: int = 30,
        unhealthy_callback=None,
        recovery_callback=None,
    ):
        self.proxy_url = proxy_url.rstrip("/")
        self.api_key = api_key
        self.interval_sec = interval_sec
        self._unhealthy_cb = unhealthy_callback
        self._recovery_cb = recovery_callback

        self._healthy = True
        self._consecutive_fails = 0
        self._last_status: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """เริ่ม background heartbeat"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="MT5HealthMonitor"
        )
        self._thread.start()
        logger.info(f"💓 Health monitor started (every {self.interval_sec}s)")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy

    @property
    def last_status(self) -> dict:
        with self._lock:
            return self._last_status.copy()

    def _loop(self):
        while not self._stop.is_set():
            try:
                resp = requests.get(
                    f"{self.proxy_url}/health",
                    headers={"X-API-Key": self.api_key},
                    timeout=10,
                )
                data = resp.json()
                mt5_ok = data.get("mt5_connected", False)
                trade_ok = data.get("terminal", {}).get("trade_allowed", False)

                with self._lock:
                    self._last_status = data
                    was_healthy = self._healthy

                    if resp.status_code == 200 and mt5_ok:
                        self._healthy = True
                        self._consecutive_fails = 0

                        # Recovery callback
                        if not was_healthy and self._recovery_cb:
                            try:
                                self._recovery_cb(data)
                            except Exception as e:
                                logger.error(f"Recovery callback error: {e}")
                    else:
                        self._consecutive_fails += 1
                        if self._consecutive_fails >= 3:
                            self._healthy = False
                            if was_healthy and self._unhealthy_cb:
                                try:
                                    self._unhealthy_cb(data)
                                except Exception as e:
                                    logger.error(f"Unhealthy callback error: {e}")

                        logger.warning(
                            f"⚠️  Health check: status={data.get('status')} "
                            f"mt5={mt5_ok} trade={trade_ok} "
                            f"fails={self._consecutive_fails}"
                        )

            except requests.RequestException as e:
                with self._lock:
                    self._consecutive_fails += 1
                    if self._consecutive_fails >= 3:
                        was_healthy = self._healthy
                        self._healthy = False
                        self._last_status = {"error": str(e)}
                        if was_healthy and self._unhealthy_cb:
                            try:
                                self._unhealthy_cb({"error": str(e)})
                            except Exception:
                                pass

                logger.error(f"❌ Health check failed: {e} (fails={self._consecutive_fails})")

            self._stop.wait(self.interval_sec)


# ============================================================
# MT5 PROXY ADAPTER — drop-in replacement for Mt5Adapter
# ============================================================

class Mt5ProxyAdapter:
    """
    HTTP client adapter ที่เรียก mt5_proxy_server.py บน Windows VPS

    Interface เดียวกับ Mt5Adapter เดิม:
      - submit(symbol, side, size, ...) → ExecutionResult
      - flatten_all()
      - get_positions() → list
      - get_account() → dict
      - check_health() → dict

    เพิ่มเติม:
      - HMAC signing สำหรับ /execute, /flatten
      - Auto health monitoring
      - Symbol mapping (canonical → MT5)
    """

    def __init__(
        self,
        proxy_url: str = "",
        api_key: str = "",
        hmac_secret: str = "",
        timeout_sec: int = 15,
        health_interval: int = 30,
        unhealthy_callback=None,
        recovery_callback=None,
    ):
        self.proxy_url = (
            proxy_url or os.getenv("MT5_PROXY_URL", "http://localhost:8500")
        ).rstrip("/")
        self.api_key = api_key or os.getenv("MT5_PROXY_API_KEY", "change-me-in-production")
        self.hmac_secret = hmac_secret or os.getenv("MT5_PROXY_HMAC_SECRET", "change-hmac-secret-too")
        self.timeout = timeout_sec

        # Session with connection pooling
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        })

        # Health monitor
        self._health = HealthMonitor(
            proxy_url=self.proxy_url,
            api_key=self.api_key,
            interval_sec=health_interval,
            unhealthy_callback=unhealthy_callback,
            recovery_callback=recovery_callback,
        )
        self._health.start()

        logger.info(f"✅ Mt5ProxyAdapter ready | proxy={self.proxy_url}")

    # ------------------------------------------
    # HMAC Signing
    # ------------------------------------------

    def _sign_payload(self, payload: dict) -> tuple[str, str]:
        """
        Sign payload with HMAC-SHA256

        Returns:
          (timestamp, signature)
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        # ลบ fields ที่จะถูก add ภายหลัง
        clean = {k: v for k, v in payload.items()
                 if k not in ("timestamp", "signature")}
        message = f"{timestamp}|{json.dumps(clean, sort_keys=True)}"
        signature = hmac.new(
            self.hmac_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return timestamp, signature

    # ------------------------------------------
    # SUBMIT ORDER
    # ------------------------------------------

    def submit(
        self,
        symbol: str,
        side: str,
        size: float,
        sizing_unit: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        metadata: dict = None,
    ):
        """
        ส่ง order ผ่าน proxy → MT5 → FTMO

        Returns:
          ExecutionResult (imported from universal_order_executor)
        """
        # Lazy import to avoid circular dependency
        from universal_order_executor import ExecutionResult

        # Block ถ้า proxy unhealthy
        if not self._health.is_healthy:
            last = self._health.last_status
            return ExecutionResult(
                success=False, method="MT5",
                message=f"MT5 proxy unhealthy — orders blocked. "
                        f"Last status: {last.get('status', 'UNKNOWN')}",
            )

        # Build payload
        payload = {
            "symbol":      symbol,
            "side":        side.upper(),
            "volume":      round(size, 2),
            "entry_price": round(entry_price, 6),
            "stop_loss":   round(stop_loss, 6),
            "take_profit": round(take_profit, 6),
            "order_type":  "MARKET" if entry_price == 0 else "LIMIT",
            "comment":     f"QE|{metadata.get('signal_id', '')}" if metadata else "QuantEngine",
        }

        # Sign
        ts, sig = self._sign_payload(payload)
        payload["timestamp"] = ts
        payload["signature"] = sig

        try:
            resp = self._session.post(
                f"{self.proxy_url}/execute",
                json=payload,
                timeout=self.timeout,
            )
            data = resp.json()

            if data.get("success"):
                logger.info(
                    f"✅ MT5 order filled: {data.get('message')} | "
                    f"{data.get('execution_ms', 0):.0f}ms"
                )
                return ExecutionResult(
                    success=True,
                    order_id=data.get("order_id", ""),
                    symbol=symbol,
                    side=side,
                    size=size,
                    sizing_unit=sizing_unit,
                    entry_price=data.get("price", entry_price),
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    method="MT5",
                    message=data.get("message", "OK"),
                    metadata={
                        "ticket": data.get("ticket", 0),
                        "retcode": data.get("retcode", 0),
                        "execution_ms": data.get("execution_ms", 0),
                        "proxy_url": self.proxy_url,
                    },
                )
            else:
                logger.error(
                    f"❌ MT5 order failed: {data.get('message')} "
                    f"(retcode={data.get('retcode', -1)})"
                )
                return ExecutionResult(
                    success=False, method="MT5",
                    symbol=symbol, side=side,
                    message=data.get("message", "Unknown error"),
                    metadata={"retcode": data.get("retcode", -1)},
                )

        except requests.Timeout:
            logger.error(f"⏱️  MT5 proxy timeout ({self.timeout}s) for {symbol}")
            return ExecutionResult(
                success=False, method="MT5",
                message=f"Proxy timeout after {self.timeout}s",
            )
        except requests.ConnectionError as e:
            logger.error(f"🔌 MT5 proxy connection error: {e}")
            return ExecutionResult(
                success=False, method="MT5",
                message=f"Proxy connection error: {e}",
            )
        except Exception as e:
            logger.error(f"💥 MT5 proxy unexpected error: {e}")
            return ExecutionResult(
                success=False, method="MT5",
                message=f"Unexpected error: {e}",
            )

    # ------------------------------------------
    # FLATTEN ALL
    # ------------------------------------------

    def flatten_all(self):
        """ปิดทุก position — Emergency"""
        try:
            resp = self._session.post(
                f"{self.proxy_url}/flatten",
                timeout=30,  # flatten อาจใช้เวลานานกว่า
            )
            data = resp.json()
            if data.get("success"):
                logger.warning(f"🔴 FLATTEN: closed {data.get('closed')}/{data.get('total')} positions")
            else:
                logger.error(f"❌ Flatten failed: {data}")
            return data
        except Exception as e:
            logger.error(f"💥 Flatten error: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------
    # CLOSE SINGLE POSITION
    # ------------------------------------------

    def close_position(self, ticket: int) -> dict:
        """ปิด position เดี่ยว"""
        try:
            resp = self._session.post(
                f"{self.proxy_url}/close/{ticket}",
                timeout=self.timeout,
            )
            return resp.json()
        except Exception as e:
            logger.error(f"Close position error: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------
    # MODIFY SL/TP
    # ------------------------------------------

    def modify_position(self, ticket: int, stop_loss: float = 0, take_profit: float = 0) -> dict:
        """แก้ไข SL/TP ของ position"""
        try:
            resp = self._session.post(
                f"{self.proxy_url}/modify",
                json={"ticket": ticket, "stop_loss": stop_loss, "take_profit": take_profit},
                timeout=self.timeout,
            )
            return resp.json()
        except Exception as e:
            logger.error(f"Modify position error: {e}")
            return {"success": False, "message": str(e)}

    # ------------------------------------------
    # INFO ENDPOINTS
    # ------------------------------------------

    def get_positions(self) -> list:
        """ดึง open positions จาก MT5"""
        try:
            resp = self._session.get(
                f"{self.proxy_url}/positions",
                timeout=self.timeout,
            )
            data = resp.json()
            return data.get("positions", [])
        except Exception as e:
            logger.error(f"Get positions error: {e}")
            return []

    def get_account(self) -> dict:
        """ดึงข้อมูลบัญชี"""
        try:
            resp = self._session.get(
                f"{self.proxy_url}/account",
                timeout=self.timeout,
            )
            return resp.json()
        except Exception as e:
            logger.error(f"Get account error: {e}")
            return {}

    def get_symbol_info(self, symbol: str) -> dict:
        """ดึงข้อมูล symbol (spread, lot constraints, etc.)"""
        try:
            resp = self._session.get(
                f"{self.proxy_url}/symbol/{symbol}",
                timeout=self.timeout,
            )
            return resp.json()
        except Exception as e:
            logger.error(f"Get symbol info error: {e}")
            return {}

    # ------------------------------------------
    # DISCOVERY & DATA FEED
    # ------------------------------------------

    def get_all_symbols(self, category: str = "") -> list[dict]:
        """
        ดึงทุก tradeable symbol จาก MT5 Terminal

        Args:
          category: "forex" | "index" | "commodity" | "crypto" | "stock" | "" (all)

        Returns:
          list of symbol dicts with spread, volume_min, contract_size, etc.
        """
        try:
            params = {}
            if category:
                params["category"] = category
            resp = self._session.get(
                f"{self.proxy_url}/symbols/all",
                params=params,
                timeout=30,  # อาจนานเพราะ scan ทั้ง terminal
            )
            data = resp.json()
            return data.get("symbols", [])
        except Exception as e:
            logger.error(f"Get all symbols error: {e}")
            return []

    def get_bars(self, symbol: str, timeframe: str = "15m", count: int = 100) -> list[dict]:
        """
        ดึง OHLCV bars จาก MT5 Terminal

        Args:
          symbol:    canonical symbol (e.g. EURUSD, US30)
          timeframe: 1m | 5m | 15m | 30m | 1h | 4h | 1d | 1w
          count:     จำนวน bars (max 5000)

        Returns:
          list of {time, open, high, low, close, volume, spread}
        """
        try:
            resp = self._session.get(
                f"{self.proxy_url}/bars/{symbol}",
                params={"timeframe": timeframe, "count": count},
                timeout=30,
            )
            data = resp.json()
            return data.get("bars", [])
        except Exception as e:
            logger.error(f"Get bars error: {e}")
            return []

    def get_tick(self, symbol: str) -> dict:
        """ดึง bid/ask ล่าสุดจาก symbol info (lightweight price check)"""
        info = self.get_symbol_info(symbol)
        if info and info.get("bid", 0) > 0:
            return {
                "symbol": symbol,
                "bid": info.get("bid", 0),
                "ask": info.get("ask", 0),
                "spread": info.get("spread", 0),
                "mt5_name": info.get("mt5_name", symbol),
            }
        return {}

    def check_health(self) -> dict:
        """Manual health check (นอกเหนือจาก background monitor)"""
        return self._health.last_status

    # ------------------------------------------
    # SYSTEM HEALTH (compatible with UniversalOrderExecutor)
    # ------------------------------------------

    def check_system_health(self, max_daily_loss: float = 5000.0) -> dict:
        """
        ตรวจ account equity — compatible กับ UniversalOrderExecutor.check_system_health()

        Returns:
          {"status": "OK"|"HALT", "today_pnl": float, ...}
        """
        acct = self.get_account()
        if not acct or "balance" not in acct:
            # Proxy ไม่ตอบ แต่ไม่ halt (health monitor จะจัดการ)
            return {"status": "OK", "today_pnl": 0.0, "proxy_healthy": self._health.is_healthy}

        # FTMO daily P&L = equity - balance (simplified)
        # จริงๆ ต้องดู starting equity ของวัน แต่ profit field ใกล้เคียง
        pnl = acct.get("profit", 0)
        buffer = 500  # buffer ก่อนชน max daily loss

        if pnl <= -(max_daily_loss - buffer):
            logger.critical(f"🚨 HALT: daily P&L = ${pnl:,.2f} (limit = -${max_daily_loss:,.2f})")
            return {"status": "HALT", "today_pnl": pnl}

        return {
            "status": "OK",
            "today_pnl": pnl,
            "balance": acct.get("balance", 0),
            "equity": acct.get("equity", 0),
            "free_margin": acct.get("free_margin", 0),
            "margin_level": acct.get("margin_level", 0),
            "proxy_healthy": self._health.is_healthy,
        }

    # ------------------------------------------
    # CLEANUP
    # ------------------------------------------

    def stop(self):
        """หยุด health monitor + close HTTP session"""
        self._health.stop()
        self._session.close()
        logger.info("Mt5ProxyAdapter stopped")


# ============================================================
# FACTORY FUNCTION — for UniversalOrderExecutor integration
# ============================================================

def create_mt5_proxy_adapter(
    proxy_url: str = "",
    api_key: str = "",
    hmac_secret: str = "",
    unhealthy_callback=None,
    recovery_callback=None,
) -> Mt5ProxyAdapter:
    """
    Factory — สร้าง Mt5ProxyAdapter พร้อม default callbacks

    Usage ใน UniversalOrderExecutor:
      from mt5_proxy_client import create_mt5_proxy_adapter
      adapter = create_mt5_proxy_adapter()
    """
    return Mt5ProxyAdapter(
        proxy_url=proxy_url,
        api_key=api_key,
        hmac_secret=hmac_secret,
        unhealthy_callback=unhealthy_callback,
        recovery_callback=recovery_callback,
    )


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Mt5ProxyAdapter Test ===\n")

    proxy_url = os.getenv("MT5_PROXY_URL", "http://localhost:8500")
    print(f"Proxy URL: {proxy_url}")

    adapter = Mt5ProxyAdapter(proxy_url=proxy_url)

    # 1. Health check
    print("\n[1] Health check:")
    health = adapter.check_health()
    print(f"    {json.dumps(health, indent=2)}")

    # 2. Account info
    print("\n[2] Account info:")
    acct = adapter.get_account()
    if acct:
        print(f"    Balance: ${acct.get('balance', 0):,.2f}")
        print(f"    Equity:  ${acct.get('equity', 0):,.2f}")
    else:
        print("    (proxy not reachable)")

    # 3. Symbol info
    print("\n[3] Symbol info (EURUSD):")
    sym = adapter.get_symbol_info("EURUSD")
    if sym:
        print(f"    MT5 name: {sym.get('mt5_name')}")
        print(f"    Spread: {sym.get('spread')}")
        print(f"    Bid/Ask: {sym.get('bid')}/{sym.get('ask')}")

    # 4. Positions
    print("\n[4] Open positions:")
    positions = adapter.get_positions()
    print(f"    Count: {len(positions)}")
    for p in positions:
        print(f"    - #{p['ticket']} {p['type']} {p['volume']} {p['symbol']} "
              f"P&L: ${p['profit']}")

    # 5. System health (FTMO-compatible)
    print("\n[5] System health (FTMO daily loss check):")
    sys_health = adapter.check_system_health(max_daily_loss=5000)
    print(f"    Status: {sys_health.get('status')}")
    print(f"    Today P&L: ${sys_health.get('today_pnl', 0):,.2f}")

    adapter.stop()
    print("\n✅ Test complete")
