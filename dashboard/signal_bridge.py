"""
platform/signal_bridge.py
=========================
Signal Bridge — เชื่อม Python Core Engine ↔ API Gateway

Data Flow:
  Core Engine (Gate 19) → JSON files ใน ./signals/
  Signal Bridge         → อ่าน JSON → POST /api/signals → ลบไฟล์
                        → หรือรับ CIOVerdict ตรง → POST /api/signals

วิธีใช้:

  # Mode 1: File Watcher (daemon — อ่านไฟล์ JSON จาก ./signals/)
  python -m platform.signal_bridge --watch ./signals/

  # Mode 2: Direct call จาก main.py (ไม่ต้องอ่านไฟล์)
  from platform.signal_bridge import SignalBridge
  bridge = SignalBridge(api_url="http://localhost:8000")
  bridge.publish_signal(signal_data)
"""

import os
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("SignalBridge")


class SignalBridge:
    """
    เชื่อม Core Engine → API Gateway

    Methods:
      publish_signal(data)       — POST signal ไป API
      publish_from_json(path)    — อ่าน JSON file แล้ว POST
      update_signal(signal_id, ...) — PUT อัปเดต status
      watch_directory(dir)       — daemon mode อ่านไฟล์ JSON ใหม่
    """

    def __init__(
        self,
        api_url:    str = "http://localhost:8000",
        api_secret: str = "",
    ):
        self.api_url = api_url.rstrip("/")
        self.api_secret = api_secret or os.getenv("SIGNAL_API_SECRET", "dev-secret-change-me")
        self._headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_secret,
        }

    # ------------------------------------------
    # PUBLISH SIGNAL
    # ------------------------------------------

    def publish_signal(self, data: dict) -> bool:
        """
        POST signal ไป API Gateway

        Args:
          data: dict ที่มี fields ตาม SignalCreate schema
                (signal_id, asset, action, entry, tp, sl, ...)

        Returns:
          True ถ้าสำเร็จ
        """
        # ── Map from Core Engine JSON format → API format
        payload = self._map_engine_to_api(data)

        try:
            resp = requests.post(
                f"{self.api_url}/api/signals",
                json=payload,
                headers=self._headers,
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()
                logger.info(f"[Bridge] Published: {payload.get('signal_id')} → API OK")
                return True
            else:
                logger.error(f"[Bridge] API error {resp.status_code}: {resp.text[:200]}")
                return False
        except requests.RequestException as e:
            logger.error(f"[Bridge] Connection error: {e}")
            return False

    def publish_from_json(self, filepath: str) -> bool:
        """อ่าน JSON file แล้ว POST ไป API"""
        path = Path(filepath)
        if not path.exists():
            logger.error(f"[Bridge] File not found: {filepath}")
            return False

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"[Bridge] Invalid JSON {path.name}: {e}")
            return False

        # ── Skip flatten signals (จัดการแยก)
        if data.get("action") == "FLATTEN_ALL":
            logger.info(f"[Bridge] Flatten signal — skipping publish")
            return True

        return self.publish_signal(data)

    # ------------------------------------------
    # UPDATE SIGNAL STATUS
    # ------------------------------------------

    def update_signal(
        self,
        signal_id: str,
        status:    str = None,
        exit_price: float = None,
        pnl_usd:   float = None,
        pnl_pct:   float = None,
    ) -> bool:
        """PUT อัปเดต signal status (WON/LOST/CANCELLED)"""
        payload = {}
        if status:
            payload["status"] = status
        if exit_price is not None:
            payload["exit_price"] = exit_price
        if pnl_usd is not None:
            payload["pnl_usd"] = pnl_usd
        if pnl_pct is not None:
            payload["pnl_pct"] = pnl_pct

        try:
            resp = requests.put(
                f"{self.api_url}/api/signals/{signal_id}",
                json=payload,
                headers=self._headers,
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"[Bridge] Update error: {e}")
            return False

    # ------------------------------------------
    # FIELD MAPPING (Core Engine → API)
    # ------------------------------------------

    def _map_engine_to_api(self, data: dict) -> dict:
        """
        Map from Core Engine JSON signal format → API SignalCreate schema

        Core Engine format (from ttp_order_executor / universal_order_executor):
          {"signal_id", "ticker", "side", "entry_price", "stop_loss",
           "take_profit", "size", "sizing_unit", "metadata": {...}}

        API format:
          {"signal_id", "asset", "action", "entry_low", "entry_high",
           "take_profit", "stop_loss", "strategy_type", ...}
        """
        meta = data.get("metadata", {})
        entry = data.get("entry_price", 0)
        side  = data.get("side", "LONG").upper()

        # ── Action mapping
        action = "BUY" if side in ("LONG", "BUY") else "SELL"
        if data.get("action") == "AVOID":
            action = "AVOID"

        # ── Entry range (±0.25%)
        spread = entry * 0.0025
        entry_low  = round(entry - spread, 2)
        entry_high = round(entry + spread, 2)

        # ── R:R calculation
        tp = data.get("take_profit", 0)
        sl = data.get("stop_loss", 0)
        risk   = abs(entry - sl) if sl else 0
        reward = abs(tp - entry) if tp else 0
        rr_str = f"1:{reward/risk:.1f}" if risk > 0 else "1:2"

        # ── Strategy type from metadata
        catalyst = meta.get("catalyst", meta.get("catalyst_type", ""))
        strategy = "Trend Following"
        if "reversion" in catalyst.lower() or "mean" in catalyst.lower():
            strategy = "Mean Reversion"

        # ── Technical summary
        atr = meta.get("atr_15m", 0)
        tech_summary = f"ATR(15m): {atr:.2f}" if atr else ""

        return {
            "signal_id":         data.get("signal_id", ""),
            "asset":             data.get("ticker", data.get("asset", "")).upper(),
            "timeframe":         data.get("timeframe", "15m"),
            "action":            action,
            "side":              side,
            "entry_low":         entry_low,
            "entry_high":        entry_high,
            "take_profit":       tp,
            "stop_loss":         sl,
            "risk_reward":       rr_str,
            "technical_summary": tech_summary,
            "news_catalyst":     meta.get("news_catalyst", ""),
            "strategy_type":     strategy,
            "llm_cio_comment":   meta.get("llm_comment", meta.get("cio_reasoning", "")),
            "size":              data.get("size", data.get("shares", 0)),
            "sizing_unit":       data.get("sizing_unit", "SHARES"),
            "sizing_multiplier": meta.get("cio_mult", 1.0),
            "ml_score":          meta.get("ml_score", 0),
            "regime_score":      meta.get("regime_score", 0),
            "confidence":        meta.get("confidence", 0),
            "gate19_action":     meta.get("cio_action", ""),
            "profile_name":      meta.get("profile_name", ""),
            "metadata":          meta,
        }

    # ------------------------------------------
    # FILE WATCHER (daemon mode) + RETRY FAILED
    # ------------------------------------------

    MAX_RETRIES = 3
    FAILED_MAX_AGE_HOURS = 6    # ไฟล์ failed เก่ากว่านี้ ไม่ retry

    def watch_directory(self, signal_dir: str, poll_sec: float = 2.0):
        """
        Daemon: เฝ้าดู directory → อ่าน JSON ใหม่ → POST → ย้ายไฟล์

        On startup:
          1. ตรวจ failed/ → retry ไฟล์ที่ยังไม่เก่าเกิน FAILED_MAX_AGE_HOURS
          2. จากนั้นเฝ้าดู signals/ ตามปกติ

        On failure:
          - retry 3 ครั้ง → ยังไม่สำเร็จ → ย้ายไป failed/
          - เมื่อ bridge restart ใหม่ → กวาด failed/ อีกรอบ
        """
        watch_path = Path(signal_dir)
        watch_path.mkdir(parents=True, exist_ok=True)

        done_dir   = watch_path / "processed"
        failed_dir = watch_path / "failed"
        done_dir.mkdir(exist_ok=True)
        failed_dir.mkdir(exist_ok=True)

        # ── Step 1: Retry failed/ on startup
        self._retry_failed(failed_dir, done_dir)

        logger.info(f"[Bridge] Watching: {watch_path.resolve()} (every {poll_sec}s)")
        processed = set()

        # ── Step 2: Main watch loop
        while True:
            try:
                json_files = sorted(watch_path.glob("*.json"))
                for f in json_files:
                    if f.name in processed:
                        continue

                    logger.info(f"[Bridge] Found: {f.name}")
                    success = self._publish_with_retry(f, done_dir, failed_dir)

                    if not success:
                        processed.add(f.name)  # ข้าม poll ถัดไป (ไฟล์อยู่ใน failed/ แล้ว)

            except Exception as e:
                logger.error(f"[Bridge] Watch error: {e}")

            time.sleep(poll_sec)

    def _publish_with_retry(self, filepath: Path, done_dir: Path, failed_dir: Path) -> bool:
        """
        ลอง POST ไฟล์ — retry MAX_RETRIES ครั้ง
        สำเร็จ → ย้ายไป processed/
        ล้มเหลว → ย้ายไป failed/
        """
        for attempt in range(1, self.MAX_RETRIES + 1):
            success = self.publish_from_json(str(filepath))
            if success:
                dest = done_dir / filepath.name
                filepath.rename(dest)
                logger.info(f"[Bridge] ✅ Processed → {dest.name} (attempt {attempt})")
                return True
            else:
                if attempt < self.MAX_RETRIES:
                    logger.warning(
                        f"[Bridge] Attempt {attempt}/{self.MAX_RETRIES} failed "
                        f"for {filepath.name} — retrying in 2s..."
                    )
                    time.sleep(2)

        # ── Max retries exhausted → move to failed/
        dest = failed_dir / filepath.name
        try:
            filepath.rename(dest)
        except Exception:
            pass   # ไฟล์อาจถูกย้ายไปแล้วจาก poll อื่น
        logger.error(
            f"[Bridge] ❌ {filepath.name} failed after {self.MAX_RETRIES} attempts "
            f"→ moved to failed/"
        )
        return False

    def _retry_failed(self, failed_dir: Path, done_dir: Path):
        """
        เมื่อ Bridge startup → กวาดไฟล์ใน failed/ ที่ยังไม่เก่าเกิน
        แล้ว retry ส่ง API อีกรอบ

        ไฟล์ที่เก่ากว่า FAILED_MAX_AGE_HOURS → ลบทิ้ง (signal หมดอายุแล้ว)
        """
        failed_files = sorted(failed_dir.glob("*.json"))
        if not failed_files:
            return

        logger.info(f"[Bridge] Startup: found {len(failed_files)} files in failed/ → retrying...")
        cutoff = time.time() - (self.FAILED_MAX_AGE_HOURS * 3600)

        for f in failed_files:
            # ── เช็คอายุ
            if f.stat().st_mtime < cutoff:
                logger.info(f"[Bridge] {f.name} too old (>{self.FAILED_MAX_AGE_HOURS}h) → deleting")
                f.unlink()
                continue

            # ── Retry
            logger.info(f"[Bridge] Retrying failed: {f.name}")
            success = self.publish_from_json(str(f))
            if success:
                dest = done_dir / f.name
                f.rename(dest)
                logger.info(f"[Bridge] ✅ Recovered → {dest.name}")
            else:
                logger.warning(f"[Bridge] {f.name} still failing — kept in failed/")

        logger.info("[Bridge] Startup retry complete")


# ============================================================
# INTEGRATION HOOK — เรียกจาก main.py
# ============================================================

def publish_trade_to_platform(
    signal_id:   str,
    symbol:      str,
    side:        str,
    entry_price: float,
    stop_loss:   float,
    take_profit: float,
    shares:      float,
    sizing_unit: str,
    metadata:    dict,
    api_url:     str = "http://localhost:8000",
):
    """
    Helper function เรียกจาก main.py หลัง Gate 19 อนุมัติ

    เพิ่มบรรทัดนี้ใน main.py หลัง execute order:
        from platform.signal_bridge import publish_trade_to_platform
        publish_trade_to_platform(...)
    """
    bridge = SignalBridge(api_url=api_url)
    return bridge.publish_signal({
        "signal_id":   signal_id,
        "ticker":      symbol,
        "side":        side,
        "entry_price": entry_price,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "size":        shares,
        "sizing_unit": sizing_unit,
        "metadata":    metadata,
    })


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")

    parser = argparse.ArgumentParser(description="Signal Bridge")
    parser.add_argument("--watch", type=str, help="Watch directory for JSON signals")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--poll", type=float, default=2.0)
    args = parser.parse_args()

    bridge = SignalBridge(api_url=args.api_url)

    if args.watch:
        bridge.watch_directory(args.watch, poll_sec=args.poll)
    else:
        print("Usage: python -m platform.signal_bridge --watch ./signals/")
