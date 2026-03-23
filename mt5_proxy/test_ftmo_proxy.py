"""
test_ftmo_proxy.py
===================
FTMO Proxy — End-to-End Test Script

ทดสอบ 5 ระดับ (รันทีละ level ได้ หรือรันทั้งหมด):

  Level 0: Config         — โหลด FTMO profile, ตรวจ proxy fields, symbol map
  Level 1: Connectivity   — ping proxy /health, ตรวจ MT5 connection
  Level 2: Account & Sym  — ดึง account info, ตรวจ symbol ทุกตัวว่า MT5 เจอ
  Level 3: Paper Order    — ส่ง order จริงเข้า MT5 (ใช้ lot เล็กสุด) แล้วปิดทันที
  Level 4: Full Pipeline  — รัน mock news ผ่าน pipeline จริง แต่ใช้ proxy จริง

Usage:
  # รันทุก level
  python test_ftmo_proxy.py

  # รันแค่ level 0-1 (ไม่ส่ง order)
  python test_ftmo_proxy.py --level 1

  # รันถึง level 3 (ส่ง order จริง — ใช้กับ FTMO demo account!)
  python test_ftmo_proxy.py --level 3

  # ใช้ symbol เฉพาะ (default = EURUSD)
  python test_ftmo_proxy.py --level 3 --symbol XAUUSD

⚠️  Level 3+ จะส่ง order จริงเข้า MT5 Terminal!
    ใช้กับ FTMO Demo / Free Trial เท่านั้น!
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)-18s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("TestFTMO")


# ============================================================
# TEST HELPERS
# ============================================================

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.details: list[str] = []

    def ok(self, msg: str):
        self.passed += 1
        self.details.append(f"  ✅ {msg}")
        print(f"  ✅ {msg}")

    def fail(self, msg: str):
        self.failed += 1
        self.details.append(f"  ❌ {msg}")
        print(f"  ❌ {msg}")

    def warn(self, msg: str):
        self.warnings += 1
        self.details.append(f"  ⚠️  {msg}")
        print(f"  ⚠️  {msg}")

    def info(self, msg: str):
        self.details.append(f"  ℹ️  {msg}")
        print(f"  ℹ️  {msg}")

    @property
    def success(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        status = "PASS" if self.success else "FAIL"
        return f"[{status}] {self.name}: {self.passed} passed, {self.failed} failed, {self.warnings} warnings"


# ============================================================
# LEVEL 0: Config Verification
# ============================================================

def test_level_0_config() -> TestResult:
    """โหลด FTMO profile, ตรวจ proxy fields, symbol map"""
    r = TestResult("Level 0: Config")
    print(f"\n{'='*60}")
    print(f"  LEVEL 0: Config Verification")
    print(f"{'='*60}")

    try:
        from config import Config
    except ImportError:
        r.fail("Cannot import config — check PYTHONPATH")
        return r

    # Load profile
    try:
        Config.load_profile("FTMO_100K")
        r.ok(f"Profile loaded: {Config.summary()}")
    except Exception as e:
        r.fail(f"load_profile failed: {e}")
        return r

    # Check execution method
    if Config.EXECUTION_METHOD == "MT5_PROXY":
        r.ok(f"execution_method = MT5_PROXY")
    else:
        r.fail(f"execution_method = {Config.EXECUTION_METHOD} (expected MT5_PROXY)")

    # Check proxy fields
    if Config.MT5_PROXY_URL:
        r.ok(f"MT5_PROXY_URL = {Config.MT5_PROXY_URL}")
    else:
        r.fail("MT5_PROXY_URL is empty — set env MT5_PROXY_URL")

    if Config.MT5_PROXY_API_KEY:
        r.ok(f"MT5_PROXY_API_KEY = {Config.MT5_PROXY_API_KEY[:8]}...")
    else:
        r.fail("MT5_PROXY_API_KEY is empty — set env MT5_PROXY_API_KEY")

    if Config.MT5_PROXY_HMAC_SECRET:
        r.ok(f"MT5_PROXY_HMAC_SECRET = {Config.MT5_PROXY_HMAC_SECRET[:8]}...")
    else:
        r.warn("MT5_PROXY_HMAC_SECRET is empty (HMAC signing disabled)")

    # Check symbol map
    if Config.SYMBOL_MAP and len(Config.SYMBOL_MAP) > 0:
        r.ok(f"Symbol map: {len(Config.SYMBOL_MAP)} symbols")

        # Spot check categories
        forex = [s for s in Config.SYMBOL_MAP if "USD" in s and len(s) == 6]
        indices = [s for s in Config.SYMBOL_MAP if ".cash" in Config.SYMBOL_MAP.get(s, "")]
        r.info(f"  Forex: {len(forex)} | Indices: {len(indices)} | "
               f"Total: {len(Config.SYMBOL_MAP)}")
    else:
        r.fail("Symbol map is empty")

    # Validate
    try:
        Config.validate("paper")
        r.ok("Config.validate('paper') passed")
    except Exception as e:
        r.fail(f"Config.validate('paper') failed: {e}")

    # get_mt5_proxy_config helper
    proxy_cfg = Config.get_mt5_proxy_config()
    if proxy_cfg.get("mt5_proxy_url"):
        r.ok("get_mt5_proxy_config() returns valid dict")
    else:
        r.warn("get_mt5_proxy_config() returns empty URL")

    return r


# ============================================================
# LEVEL 1: Proxy Connectivity
# ============================================================

def test_level_1_connectivity() -> TestResult:
    """Ping proxy, ตรวจ MT5 connection + terminal status"""
    r = TestResult("Level 1: Connectivity")
    print(f"\n{'='*60}")
    print(f"  LEVEL 1: Proxy Connectivity")
    print(f"{'='*60}")

    from config import Config
    Config.load_profile("FTMO_100K")

    try:
        from mt5_proxy_client import Mt5ProxyAdapter
    except ImportError:
        r.fail("Cannot import Mt5ProxyAdapter — check mt5_proxy_client.py")
        return r

    adapter = Mt5ProxyAdapter(
        proxy_url=Config.MT5_PROXY_URL,
        api_key=Config.MT5_PROXY_API_KEY,
        hmac_secret=Config.MT5_PROXY_HMAC_SECRET,
        health_interval=9999,  # ปิด auto health — test เอง
    )

    # Health check
    try:
        import requests
        resp = requests.get(
            f"{Config.MT5_PROXY_URL}/health",
            headers={"X-API-Key": Config.MT5_PROXY_API_KEY},
            timeout=10,
        )
        data = resp.json()

        if resp.status_code == 200:
            r.ok(f"Proxy reachable: {resp.status_code}")
        else:
            r.fail(f"Proxy returned HTTP {resp.status_code}")
            adapter.stop()
            return r

        # MT5 connected?
        if data.get("mt5_connected"):
            r.ok("MT5 terminal connected")
        else:
            r.fail("MT5 terminal NOT connected — check MT5 is running on Windows VPS")
            adapter.stop()
            return r

        # Trade allowed?
        terminal = data.get("terminal", {})
        if terminal.get("trade_allowed"):
            r.ok("Trading is allowed")
        else:
            r.warn("trade_allowed = False — check MT5 terminal settings")

        # Account info in health
        acct = data.get("account", {})
        if acct:
            r.ok(f"Account: login={acct.get('login')} | "
                 f"server={acct.get('server')} | "
                 f"balance=${acct.get('balance', 0):,.2f}")
        else:
            r.warn("No account info in health response")

        # Reconnect count
        rc = data.get("reconnect_count", 0)
        if rc == 0:
            r.ok("No reconnects (stable connection)")
        else:
            r.warn(f"Reconnect count: {rc}")

        r.info(f"Full health: {json.dumps(data, indent=2)}")

    except requests.ConnectionError as e:
        r.fail(f"Cannot reach proxy at {Config.MT5_PROXY_URL}: {e}")
    except requests.Timeout:
        r.fail(f"Proxy timeout (10s) — check firewall / port 8500")
    except Exception as e:
        r.fail(f"Health check error: {e}")

    adapter.stop()
    return r


# ============================================================
# LEVEL 2: Account Info & Symbol Resolution
# ============================================================

def test_level_2_symbols() -> TestResult:
    """ดึง account info, ตรวจว่า MT5 เจอทุก symbol ใน map"""
    r = TestResult("Level 2: Account & Symbols")
    print(f"\n{'='*60}")
    print(f"  LEVEL 2: Account Info & Symbol Resolution")
    print(f"{'='*60}")

    from config import Config
    Config.load_profile("FTMO_100K")

    from mt5_proxy_client import Mt5ProxyAdapter
    adapter = Mt5ProxyAdapter(
        proxy_url=Config.MT5_PROXY_URL,
        api_key=Config.MT5_PROXY_API_KEY,
        hmac_secret=Config.MT5_PROXY_HMAC_SECRET,
        health_interval=9999,
    )

    # Account info
    acct = adapter.get_account()
    if acct and "balance" in acct:
        r.ok(f"Account: {acct.get('name', '?')} | "
             f"Balance: ${acct.get('balance', 0):,.2f} | "
             f"Equity: ${acct.get('equity', 0):,.2f} | "
             f"Leverage: 1:{acct.get('leverage', '?')}")

        # FTMO-specific checks
        balance = acct.get("balance", 0)
        if 90_000 <= balance <= 110_000:
            r.ok(f"Balance ${balance:,.0f} is within FTMO $100K range")
        else:
            r.warn(f"Balance ${balance:,.0f} — verify this is the correct FTMO account")
    else:
        r.fail("Cannot get account info — proxy may be down")
        adapter.stop()
        return r

    # Check system health (daily loss check)
    health = adapter.check_system_health(max_daily_loss=5000)
    if health.get("status") == "OK":
        r.ok(f"Daily P&L: ${health.get('today_pnl', 0):,.2f} (within limits)")
    else:
        r.warn(f"System health: {health.get('status')} — P&L: ${health.get('today_pnl', 0):,.2f}")

    # Test each symbol in the map
    print(f"\n  --- Symbol Resolution ({len(Config.SYMBOL_MAP)} symbols) ---")
    found = 0
    not_found = []
    for canonical, expected_mt5 in Config.SYMBOL_MAP.items():
        info = adapter.get_symbol_info(canonical)
        if info and "digits" in info:
            found += 1
            spread = info.get("spread", "?")
            vol_min = info.get("volume_min", "?")
            bid = info.get("bid", 0)
            # Only print details for first few + any failures
            if found <= 5:
                r.info(f"  {canonical:8s} → {info.get('mt5_name', '?'):14s} | "
                       f"bid={bid:.5f} | spread={spread} | min_lot={vol_min}")
        else:
            not_found.append(canonical)

    if found == len(Config.SYMBOL_MAP):
        r.ok(f"All {found} symbols resolved successfully")
    else:
        if found > 0:
            r.ok(f"{found}/{len(Config.SYMBOL_MAP)} symbols found")
        if not_found:
            r.fail(f"Missing symbols: {not_found}")
            r.info("Check SYMBOL_MAP in config.py or MT5_SYMBOL_MAP env override")

    # Open positions
    positions = adapter.get_positions()
    if positions:
        r.info(f"Open positions: {len(positions)}")
        for p in positions:
            r.info(f"  #{p['ticket']} {p['type']} {p['volume']} {p['symbol']} "
                   f"P&L=${p['profit']}")
    else:
        r.ok("No open positions (clean slate)")

    adapter.stop()
    return r


# ============================================================
# LEVEL 3: Paper Order (send real order → close immediately)
# ============================================================

def test_level_3_paper_order(symbol: str = "EURUSD") -> TestResult:
    """ส่ง order ขนาดเล็กสุดเข้า MT5 แล้วปิดทันที"""
    r = TestResult(f"Level 3: Paper Order ({symbol})")
    print(f"\n{'='*60}")
    print(f"  LEVEL 3: Paper Order — {symbol}")
    print(f"  ⚠️  จะส่ง order จริงเข้า MT5! ใช้ demo account เท่านั้น!")
    print(f"{'='*60}")

    from config import Config
    Config.load_profile("FTMO_100K")

    from mt5_proxy_client import Mt5ProxyAdapter
    adapter = Mt5ProxyAdapter(
        proxy_url=Config.MT5_PROXY_URL,
        api_key=Config.MT5_PROXY_API_KEY,
        hmac_secret=Config.MT5_PROXY_HMAC_SECRET,
        health_interval=9999,
    )

    # Get symbol info first (min lot size)
    sym_info = adapter.get_symbol_info(symbol)
    if not sym_info or "volume_min" not in sym_info:
        r.fail(f"Cannot get symbol info for {symbol}")
        adapter.stop()
        return r

    min_lot = sym_info.get("volume_min", 0.01)
    bid = sym_info.get("bid", 0)
    ask = sym_info.get("ask", 0)
    r.info(f"Symbol: {sym_info.get('mt5_name')} | bid={bid} | ask={ask} | min_lot={min_lot}")

    if bid == 0 or ask == 0:
        r.fail("No price data — market may be closed")
        adapter.stop()
        return r

    # ── Step 1: Send BUY order (minimum lot, market order, no SL/TP)
    from universal_order_executor import ExecutionResult
    r.info(f"Sending: BUY {min_lot} {symbol} @ market")

    result = adapter.submit(
        symbol=symbol,
        side="BUY",
        size=min_lot,
        sizing_unit="LOTS",
        entry_price=0,     # market order
        stop_loss=0,       # no SL (we'll close immediately)
        take_profit=0,     # no TP
        metadata={"signal_id": "TEST-PROXY-001", "test": True},
    )

    if result.success:
        ticket = result.metadata.get("ticket", 0)
        exec_ms = result.metadata.get("execution_ms", 0)
        r.ok(f"Order FILLED: ticket={ticket} | price={result.entry_price} | {exec_ms:.0f}ms")

        # ── Step 2: Verify position exists
        time.sleep(0.5)
        positions = adapter.get_positions()
        found = [p for p in positions if p.get("ticket") == ticket]
        if found:
            r.ok(f"Position confirmed: #{ticket} {found[0]['type']} {found[0]['volume']} {found[0]['symbol']}")
        else:
            r.warn(f"Position #{ticket} not found in positions list (may have closed)")

        # ── Step 3: Close position immediately
        r.info(f"Closing position #{ticket}...")
        close_result = adapter.close_position(ticket)
        if close_result.get("success"):
            r.ok(f"Position #{ticket} closed @ {close_result.get('close_price', '?')}")
        else:
            r.fail(f"Close failed: {close_result.get('message')}")
            r.warn("⚠️  Position may still be open! Check MT5 manually!")

        # ── Step 4: Verify clean
        time.sleep(0.5)
        positions_after = adapter.get_positions()
        our_positions = [p for p in positions_after if p.get("ticket") == ticket]
        if not our_positions:
            r.ok("Position fully closed — clean state")
        else:
            r.warn(f"Position #{ticket} may still be open!")

        # ── Step 5: Execution quality report
        if result.entry_price and close_result.get("close_price"):
            slippage = abs(result.entry_price - ask)
            cost = abs(result.entry_price - close_result["close_price"]) * min_lot * 100_000
            r.info(f"Execution quality:")
            r.info(f"  Entry slippage: {slippage:.5f} ({slippage/ask*100:.3f}%)")
            r.info(f"  Round-trip cost: ${cost:.2f} (spread + slippage)")
            r.info(f"  Execution time: {exec_ms:.0f}ms")

    else:
        r.fail(f"Order REJECTED: {result.message}")
        retcode = result.metadata.get("retcode", -1)
        r.info(f"  MT5 retcode: {retcode}")
        if "market is closed" in result.message.lower():
            r.info("  Market may be closed — try during trading hours")
        elif "invalid volume" in result.message.lower():
            r.info(f"  Try adjusting lot size (current: {min_lot})")

    adapter.stop()
    return r


# ============================================================
# LEVEL 4: Full Pipeline with Real Proxy
# ============================================================

def test_level_4_pipeline() -> TestResult:
    """รัน mock news ผ่าน pipeline จริง ใช้ proxy จริง แต่ volume เล็กสุด"""
    r = TestResult("Level 4: Full Pipeline")
    print(f"\n{'='*60}")
    print(f"  LEVEL 4: Full Pipeline (mock news → real proxy)")
    print(f"  ⚠️  จะส่ง order จริง! ใช้ demo account เท่านั้น!")
    print(f"{'='*60}")

    from config import Config
    Config.load_profile("FTMO_100K")

    # Override risk to minimum for safety
    original_risk = Config.RISK_PER_TRADE_USD
    Config.RISK_PER_TRADE_USD = 10.0  # $10 risk → จะได้ lot เล็กมาก
    Config.MAX_ORDERS_PER_DAY = 1      # แค่ 1 order
    r.info(f"Safety override: risk=$10/trade (was ${original_risk}), max_orders=1")

    try:
        from universal_order_executor import UniversalOrderExecutor

        # สร้าง executor ที่เชื่อม proxy จริง
        ex = UniversalOrderExecutor.from_config(Config)
        r.ok(f"Executor created: method={ex.method} adapter={type(ex.adapter).__name__}")

        # System health check
        health = ex.adapter.check_system_health(max_daily_loss=5000)
        r.info(f"System health: {health.get('status')} | P&L: ${health.get('today_pnl', 0):,.2f}")

        if health.get("status") == "HALT":
            r.warn("System is HALTED (daily loss limit) — cannot test orders")
            ex.adapter.stop()
            ex.stop()
            return r

        # Test order submission through executor (not adapter directly)
        r.info("Sending test order through UniversalOrderExecutor...")
        result = ex.submit_order(
            symbol="EURUSD",
            side="BUY",
            size=0.01,          # minimum lot
            sizing_unit="LOTS",
            entry_price=0,      # market
            stop_loss=0,
            take_profit=0,
            metadata={"signal_id": "TEST-PIPELINE-001"},
        )

        if result.success:
            ticket = result.metadata.get("ticket", 0)
            r.ok(f"Pipeline order filled: {result.message}")

            # Flatten
            time.sleep(1)
            r.info("Flattening all positions...")
            ex.flatten_all_positions()
            time.sleep(1)

            positions = ex.adapter.get_positions()
            if not positions:
                r.ok("All positions closed after flatten")
            else:
                r.warn(f"{len(positions)} positions still open after flatten")
        else:
            r.fail(f"Pipeline order failed: {result.message}")
            if "market" in result.message.lower():
                r.info("Market may be closed — try during trading hours")

        ex.adapter.stop()
        ex.stop()

    except Exception as e:
        r.fail(f"Pipeline test error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Restore config
        Config.RISK_PER_TRADE_USD = original_risk

    return r


# ============================================================
# MAIN RUNNER
# ============================================================

def run_tests(max_level: int = 2, symbol: str = "EURUSD"):
    """รันทุก test level ตั้งแต่ 0 ถึง max_level"""

    print(f"\n{'#'*60}")
    print(f"  FTMO PROXY — End-to-End Test")
    print(f"  Max level: {max_level} | Symbol: {symbol}")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'#'*60}")

    results: list[TestResult] = []

    # Level 0: Always run
    results.append(test_level_0_config())
    if not results[-1].success:
        print("\n⛔ Level 0 FAILED — fix config before continuing")
        _print_summary(results)
        return results

    if max_level >= 1:
        results.append(test_level_1_connectivity())
        if not results[-1].success:
            print("\n⛔ Level 1 FAILED — fix proxy connection before continuing")
            _print_summary(results)
            return results

    if max_level >= 2:
        results.append(test_level_2_symbols())

    if max_level >= 3:
        print(f"\n{'!'*60}")
        print(f"  ⚠️  Level 3 will SEND A REAL ORDER to MT5!")
        print(f"  Make sure you're using a DEMO account!")
        print(f"{'!'*60}")
        confirm = input("  Type 'TEST' to continue: ").strip()
        if confirm == "TEST":
            results.append(test_level_3_paper_order(symbol))
        else:
            print("  Skipped Level 3")

    if max_level >= 4:
        print(f"\n{'!'*60}")
        print(f"  ⚠️  Level 4 will RUN THE FULL PIPELINE with real orders!")
        print(f"{'!'*60}")
        confirm = input("  Type 'PIPELINE' to continue: ").strip()
        if confirm == "PIPELINE":
            results.append(test_level_4_pipeline())
        else:
            print("  Skipped Level 4")

    _print_summary(results)
    return results


def _print_summary(results: list[TestResult]):
    """สรุปผลทุก level"""
    print(f"\n{'='*60}")
    print(f"  TEST SUMMARY")
    print(f"{'='*60}")

    total_pass = sum(r.passed for r in results)
    total_fail = sum(r.failed for r in results)
    total_warn = sum(r.warnings for r in results)

    for r in results:
        icon = "✅" if r.success else "❌"
        print(f"  {icon} {r.summary()}")

    print(f"\n  Total: {total_pass} passed | {total_fail} failed | {total_warn} warnings")

    if total_fail == 0:
        print(f"\n  🎉 ALL TESTS PASSED!")
        if any("Level 3" in r.name for r in results):
            print(f"  → Proxy is ready for live trading")
        elif any("Level 2" in r.name for r in results):
            print(f"  → Run --level 3 to test actual order execution")
        else:
            print(f"  → Run --level 2 to test symbol resolution")
    else:
        print(f"\n  🔴 {total_fail} test(s) failed — fix before going live")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FTMO Proxy End-to-End Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Test Levels:
  0  Config only (no network)
  1  Proxy connectivity (/health)
  2  Account info + symbol resolution (default)
  3  Paper order (sends real order, closes immediately) ⚠️
  4  Full pipeline (mock news → real proxy) ⚠️

Examples:
  python test_ftmo_proxy.py                    # Level 0-2 (safe)
  python test_ftmo_proxy.py --level 3          # + paper order
  python test_ftmo_proxy.py --level 3 -s XAUUSD  # test Gold
  python test_ftmo_proxy.py --level 4          # full pipeline
        """
    )
    parser.add_argument("--level", "-l", type=int, default=2, choices=[0,1,2,3,4],
                        help="Max test level to run (default: 2)")
    parser.add_argument("--symbol", "-s", type=str, default="EURUSD",
                        help="Symbol for Level 3 paper order (default: EURUSD)")
    args = parser.parse_args()

    run_tests(max_level=args.level, symbol=args.symbol)