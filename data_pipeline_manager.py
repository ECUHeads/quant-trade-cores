"""
data_pipeline_manager.py
========================
Data Pipeline — 15m VWAP Signal Generator

Architecture Pivot:
  เดิม : ดึง 1m/5m bars สำหรับ scalping
  ใหม่ : ดึง 15m/1d bars สำหรับ VWAP Intraday Trading

  เพิ่ม VWAP + ATR_15m Feature Engineering:
    - VWAP = cumsum(Price × Volume) / cumsum(Volume) ภายในวัน
    - ATR_15m = ATR ของ 15-minute bars
    - Price/VWAP Ratio = ราคาปัจจุบัน / VWAP (>1 = above, <1 = below)

Responsibilities:
  LOCAL  → Feature pre-computation + Model training → Parquet + .pkl/.pt
  GDRIVE → Retention storage
  VPS    → Download latest model/features → Inference only
"""

import os
import re
import shutil
import logging
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from config.config import Config
warnings.filterwarnings("ignore", category=UserWarning)
logger = logging.getLogger("DataPipelineManager")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)


# ============================================================
# RETENTION HELPERS
# ============================================================

def daily_tag(dt: Optional[datetime] = None) -> str:
    """DD-MM-YYYY — ใช้กับ features รายวัน และ LightGBM model"""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%d-%m-%Y")


def weekly_tag(dt: Optional[datetime] = None) -> str:
    """WK{week}-{YYYY} — ใช้กับ LSTM sequences และ LSTM model"""
    dt = dt or datetime.now(timezone.utc)
    week = dt.isocalendar()[1]
    return f"WK{week:02d}-{dt.year}"


def parse_tag_date(tag: str) -> Optional[datetime]:
    """แปลง tag กลับเป็น datetime (สำหรับ retention cleanup)"""
    try:
        if re.match(r"^\d{2}-\d{2}-\d{4}$", tag):
            return datetime.strptime(tag, "%d-%m-%Y").replace(tzinfo=timezone.utc)
        if re.match(r"^WK\d{2}-\d{4}$", tag):
            week = int(tag[2:4])
            year = int(tag[5:])
            return datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None


# ============================================================
# TIMEZONE + COLUMN NORMALIZER (ใช้ทุกที่ที่โหลด DataFrame)
# ============================================================

def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize DataFrame จาก yfinance / Parquet ให้พร้อมใช้งาน

    แก้ปัญหา:
      1. MultiIndex columns (yfinance v0.2+) → flatten
         yfinance คืน columns เป็น (Price, Ticker) เช่น ("Close", "NVDA")
         ถ้า get_level_values(0) จะได้ ["Close","Close","High","High",...] ซ้ำ!
         ต้อง droplevel ชั้น Ticker ออกแทน
      2. Column names → lowercase
      3. tz-aware index (15m data) → tz-naive (ลบ timezone metadata)
      4. Duplicate columns → dedup

    เรียกทุกครั้งหลัง yf.download() หรือ pd.read_parquet()
    """
    if df is None or df.empty:
        return df

    # ── Flatten MultiIndex columns
    # yfinance v0.2+ single ticker: columns = [("Close","NVDA"), ("High","NVDA"), ...]
    # ต้อง drop ชั้น Ticker (level 1) ออก ไม่ใช่ get_level_values(0)
    if isinstance(df.columns, pd.MultiIndex):
        # เช็คว่า level 1 มีแค่ 1 ticker (single-ticker download)
        if df.columns.nlevels == 2:
            unique_tickers = df.columns.get_level_values(1).unique()
            if len(unique_tickers) == 1:
                # Single ticker: drop ticker level → เหลือแค่ Price level
                df = df.droplevel(1, axis=1)
            else:
                # Multi-ticker (group_by=ticker): ไม่ flatten
                pass
        else:
            df.columns = df.columns.get_level_values(0)

    # ── Deduplicate columns (safety net)
    if not isinstance(df.columns, pd.MultiIndex) and df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated()]

    # ── Lowercase column names (skip if still MultiIndex)
    if not isinstance(df.columns, pd.MultiIndex):
        df.columns = [c.lower() for c in df.columns]

    # ── Remove timezone
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        try:
            df.index = df.index.tz_localize(None)
        except TypeError:
            df.index = df.index.tz_convert("UTC").tz_localize(None)

    return df


# ============================================================
# UNIFIED DATA DOWNLOAD LAYER
# ============================================================
#
# Priority: Alpaca (primary) → yfinance (fallback)
#
# ทำไม Alpaca ดีกว่า yfinance:
#   ✅ ไม่มี rate limit (200 req/min free tier)
#   ✅ columns ปกติ (open, high, low, close, volume) — ไม่มี MultiIndex
#   ✅ 15m data ย้อนหลังได้ 5+ ปี (yfinance จำกัด 60 วัน)
#   ✅ มี VWAP column ในตัว
#   ✅ reliable uptime (yfinance scrape Yahoo ซึ่งอาจเปลี่ยน format)
#
# เมื่อไหร่ fallback ไป yfinance:
#   - Alpaca API key ไม่ได้ตั้ง
#   - Alpaca error (network, server down)
#   - Symbol ที่ Alpaca ไม่รองรับ (เช่น ^VIX, crypto)

import threading as _threading
import time as _time
from datetime import datetime, timezone, timedelta

# ── Config
DOWNLOAD_MAX_WORKERS = 5      # Alpaca รองรับ concurrent ได้ดี (ไม่ต้องลดเหมือน yfinance)
_DOWNLOAD_MAX_RETRIES = 3

# ── Alpaca rate limiter
# Free tier: 200 req/min → ปลอดภัยที่ ~3 req/sec (semaphore + pace)
_ALPACA_SEM       = _threading.Semaphore(3)     # max 3 concurrent Alpaca calls
_ALPACA_PACE_SEC  = 0.4                         # 0.4s ระหว่าง call = ~150 req/min (safety margin)
_alpaca_last_call = 0.0
_alpaca_pace_lock = _threading.Lock()

# ── Alpaca client singleton
_alpaca_client = None
_alpaca_lock   = _threading.Lock()


def _get_alpaca_client():
    """Lazy-init Alpaca StockHistoricalDataClient (singleton)"""
    global _alpaca_client
    if _alpaca_client is not None:
        return _alpaca_client

    with _alpaca_lock:
        if _alpaca_client is not None:
            return _alpaca_client

        from config.config import Config
        api_key, api_secret = Config.get_alpaca_keys()

        if not api_key or not api_secret:
            logger.warning("[Alpaca] API keys not set in Config → fallback to yfinance")
            return None

        try:
            from alpaca.data.historical import StockHistoricalDataClient
            _alpaca_client = StockHistoricalDataClient(api_key, api_secret)
            logger.info("[Alpaca] Data client initialized ✅")
            return _alpaca_client
        except ImportError:
            logger.warning("[Alpaca] alpaca-py not installed → fallback to yfinance")
            return None
        except Exception as e:
            logger.error(f"[Alpaca] Init error: {e} → fallback to yfinance")
            return None


def _parse_interval_to_timeframe(interval: str):
    """แปลง interval string เป็น Alpaca TimeFrame object"""
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    interval = interval.lower().strip()
    mapping = {
        "1m":  TimeFrame(1, TimeFrameUnit.Minute),
        "5m":  TimeFrame(5, TimeFrameUnit.Minute),
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "30m": TimeFrame(30, TimeFrameUnit.Minute),
        "1h":  TimeFrame(1, TimeFrameUnit.Hour),
        "1d":  TimeFrame.Day,
        "1w":  TimeFrame.Week,
        "1mo": TimeFrame.Month,
    }
    if interval in mapping:
        return mapping[interval]
    raise ValueError(f"Unknown interval: {interval}")


def _period_to_dates(period: str):
    """แปลง period string (เช่น '59d', '2mo') เป็น (start, end) datetime"""
    now = datetime.now(timezone.utc)
    period = period.lower().strip()

    if period.endswith("d"):
        days = int(period[:-1])
        return now - timedelta(days=days), now
    elif period.endswith("mo"):
        months = int(period[:-2])
        return now - timedelta(days=months * 30), now
    elif period.endswith("y"):
        years = int(period[:-1])
        return now - timedelta(days=years * 365), now
    else:
        return now - timedelta(days=60), now


def _alpaca_pace():
    """Enforce minimum interval between Alpaca API calls (thread-safe)"""
    global _alpaca_last_call
    with _alpaca_pace_lock:
        now = _time.monotonic()
        wait = _ALPACA_PACE_SEC - (now - _alpaca_last_call)
        # จอง timestamp ก่อน release lock → thread อื่นจะเห็น gap ที่ถูกต้อง
        _alpaca_last_call = now + max(wait, 0)
    # sleep นอก lock → threads อื่นคำนวณ wait ของตัวเองได้เลย
    if wait > 0:
        _time.sleep(wait)


def _alpaca_download(
    symbol:   str,
    period:   str = "59d",
    interval: str = "15m",
) -> pd.DataFrame:
    """
    ดึงข้อมูลจาก Alpaca Data API (พร้อม rate limiter + retry)

    Rate Limit Strategy:
      - Semaphore: max 3 concurrent calls
      - Pace: 0.4s ระหว่าง call (~150 req/min, ภายใน limit 200 req/min)
      - Retry: exponential backoff เมื่อโดน 429 (too many requests)

    Returns:
      DataFrame with columns: [open, high, low, close, volume, vwap, ...]
      Index: DatetimeIndex (tz-naive, UTC)
      Empty DataFrame ถ้า error
    """
    client = _get_alpaca_client()
    if client is None:
        return pd.DataFrame()

    last_err = None

    for attempt in range(1, _DOWNLOAD_MAX_RETRIES + 1):
        _ALPACA_SEM.acquire()
        try:
            _alpaca_pace()  # enforce minimum interval

            from alpaca.data.requests import StockBarsRequest
            from config.config import Config

            timeframe = _parse_interval_to_timeframe(interval)
            #start, end = _period_to_dates(period)
            start, end = _period_to_dates("1y")
            feed = Config.ALPACA_FEED   # default "iex"

            request = StockBarsRequest(
                symbol_or_symbols=symbol.upper(),
                timeframe=timeframe,
                start=start,
                adjustment='split',
                end=end,
                feed=feed,
                limit=None
            )

            bars = client.get_stock_bars(request)
            df = bars.df

            if df.empty:
                return pd.DataFrame()

            # ── Alpaca returns MultiIndex (symbol, timestamp) for index
            if isinstance(df.index, pd.MultiIndex):
                df = df.droplevel("symbol")

            # ── Columns already lowercase: open, high, low, close, volume, trade_count, vwap
            keep_cols = ["open", "high", "low", "close", "volume"]
            extra = [c for c in ["vwap", "trade_count"] if c in df.columns]
            df = df[keep_cols + extra]

            # ── Strip timezone → tz-naive
            if hasattr(df.index, 'tz') and df.index.tz is not None:
                try:
                    df.index = df.index.tz_localize(None)
                except TypeError:
                    df.index = df.index.tz_convert("UTC").tz_localize(None)
            df.insert(0, 'datetime', df.index)
            logger.debug(f"[Alpaca] {symbol} {interval} → {len(df)} bars")
            return df

        except Exception as e:
            last_err = e
            err_str = str(e).lower()

            # ── Rate limited → exponential backoff
            if "too many" in err_str or "rate" in err_str or "429" in err_str:
                wait = 5 * (2 ** (attempt - 1))   # 5s, 10s, 20s
                logger.warning(
                    f"[Alpaca] {symbol} {interval} rate limited "
                    f"(attempt {attempt}/{_DOWNLOAD_MAX_RETRIES}) → backoff {wait}s"
                )
                _time.sleep(wait)
            else:
                logger.warning(f"[Alpaca] {symbol} {interval} error (attempt {attempt}): {e}")
                _time.sleep(1)
        finally:
            _ALPACA_SEM.release()

    logger.error(f"[Alpaca] {symbol} {interval} FAILED after {_DOWNLOAD_MAX_RETRIES} retries: {last_err}")
    return pd.DataFrame()


def _yfinance_download(
    tickers:  str,
    period:   str = "59d",
    interval: str = "15m",
    **kwargs,
) -> pd.DataFrame:
    """
    Fallback: ดึงข้อมูลจาก yfinance (rate-limited)

    มี Semaphore + pace + retry เหมือนเดิม
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("[YF] yfinance not installed")
        return pd.DataFrame()

    kwargs.setdefault("progress", False)
    kwargs.setdefault("auto_adjust", True)

    _sem  = _threading.Semaphore(2)
    last_err = None

    for attempt in range(1, _DOWNLOAD_MAX_RETRIES + 1):
        _sem.acquire()
        try:
            df = yf.download(tickers, period=period, interval=interval, **kwargs)
            _time.sleep(1.5)    # pace

            is_multi = " " in tickers.strip() or "group_by" in kwargs
            if not is_multi:
                df = normalize_ohlcv(df)

            return df

        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            if "rate" in err_str or "too many" in err_str:
                wait = 10 * (2 ** (attempt - 1))
                logger.warning(f"[YF] Rate limited (attempt {attempt}) → backoff {wait}s")
                _time.sleep(wait)
            else:
                logger.warning(f"[YF] Error (attempt {attempt}): {e}")
                _time.sleep(2)
        finally:
            _sem.release()

    logger.error(f"[YF] FAILED after {_DOWNLOAD_MAX_RETRIES} retries: {last_err}")
    return pd.DataFrame()


# ============================================================
# safe_download() — UNIFIED ENTRY POINT
# ============================================================
#
# ทุกไฟล์ในโปรเจกต์เรียก safe_download() ตัวเดียว
# ภายในเลือก Alpaca → yfinance อัตโนมัติ

def safe_download(
    symbol:   str,
    period:   str = "119d",
    interval: str = "15m",
    **kwargs,
) -> pd.DataFrame:
    """
    Unified data download: Alpaca (primary) → yfinance (fallback)

    Usage:
      df = safe_download("NVDA", period="59d", interval="15m")
      df = safe_download("NVDA", period="2y", interval="1d")

    Returns:
      DataFrame with columns: [open, high, low, close, volume]
      Index: DatetimeIndex (tz-naive)
      Empty DataFrame ถ้าทุก source ล้มเหลว

    Note:
      Multi-ticker (เช่น "SPY ^VIX") จะ fallback ไป yfinance โดยตรง
      เพราะ Alpaca ไม่รองรับ index symbols (^VIX)
    """
    # ── Multi-ticker → yfinance only (Alpaca ไม่รองรับ ^VIX etc.)
    is_multi = " " in symbol.strip()
    if is_multi:
        return _yfinance_download(symbol, period=period, interval=interval, **kwargs)

    # ── Single ticker: Alpaca first
    df = _alpaca_download(symbol, period=period, interval=interval)
    if not df.empty and len(df) >= 5:
        return df

    # ── Fallback: yfinance
    logger.info(f"[Download] {symbol} Alpaca empty → fallback yfinance")
    return _yfinance_download(symbol, period=period, interval=interval, **kwargs)


# Backward compatibility alias
safe_yf_download = safe_download


# ============================================================
# TIERED WATCHLIST — Dynamic Pre-Market Classification
# ============================================================
#
# แบ่ง universe 300 ตัว เป็น 3 ชั้นตามความ "ร้อนแรง" ของแต่ละวัน
#
# Tier 1 HOT  (5–15 ตัว):  Gap ≥ 2% + RVOL ≥ 2x หรือมี earnings/FDA วันนี้
#   → Train ทุกเช้า + Engine จับตาทุก 15m bar
#
# Tier 2 WARM (30–50 ตัว): RVOL ≥ 1.5x หรือ analyst upgrade
#   → Train ทุกเช้า + Engine ตรวจเมื่อมี news trigger
#
# Tier 3 COLD (ที่เหลือ):  ไม่ train วันนี้ ใช้ model เก่า
#   → Promote ขึ้น Tier 1 ถ้ามี breaking news (Just-in-Time Train)
#
# Data Source: Alpaca Snapshot API (1 call ได้ 300 ตัวพร้อมกัน)

from dataclasses import dataclass, field as dc_field


@dataclass
class TieredWatchlist:
    """ผลลัพธ์จาก scan_pre_market()"""
    tier1_hot:   list = dc_field(default_factory=list)    # 5–15 ตัว
    tier2_warm:  list = dc_field(default_factory=list)    # 30–50 ตัว
    tier3_cold:  list = dc_field(default_factory=list)    # ที่เหลือ
    scan_time:   str  = ""
    snapshot:    dict = dc_field(default_factory=dict)    # raw snapshot data

    @property
    def trainable(self) -> list:
        """Tier 1 + 2 → ตัวที่ต้อง train วันนี้"""
        return self.tier1_hot + self.tier2_warm

    @property
    def all_symbols(self) -> list:
        return self.tier1_hot + self.tier2_warm + self.tier3_cold

    def get_tier(self, symbol: str) -> int:
        """คืน tier ของ symbol (1/2/3) หรือ 0 ถ้าไม่อยู่ใน universe"""
        if symbol in self.tier1_hot:  return 1
        if symbol in self.tier2_warm: return 2
        if symbol in self.tier3_cold: return 3
        return 0

    def promote(self, symbol: str, to_tier: int = 1) -> bool:
        """ย้าย symbol ขึ้น tier ที่สูงกว่า (ใช้ตอน breaking news)"""
        # ลบจาก tier เดิม
        for tier_list in [self.tier1_hot, self.tier2_warm, self.tier3_cold]:
            if symbol in tier_list:
                tier_list.remove(symbol)
                break

        # เพิ่มเข้า tier ใหม่
        if to_tier == 1:
            self.tier1_hot.append(symbol)
        elif to_tier == 2:
            self.tier2_warm.append(symbol)
        else:
            self.tier3_cold.append(symbol)
        return True

    def summary(self) -> str:
        return (
            f"Tier1={len(self.tier1_hot)} | "
            f"Tier2={len(self.tier2_warm)} | "
            f"Tier3={len(self.tier3_cold)} | "
            f"Train={len(self.trainable)}"
        )


# ── Tier Thresholds (ปรับได้ผ่าน Config หรือ env)
TIER1_GAP_PCT     = float(os.getenv("TIER1_GAP_PCT",    "2.0"))   # Gap ≥ 2%
TIER1_RVOL        = float(os.getenv("TIER1_RVOL",       "2.0"))   # RVOL ≥ 2x
TIER2_RVOL        = float(os.getenv("TIER2_RVOL",       "1.5"))   # RVOL ≥ 1.5x
TIER1_MAX         = int(os.getenv("TIER1_MAX",          "15"))     # จำกัด Tier 1
TIER2_MAX         = int(os.getenv("TIER2_MAX",          "50"))     # จำกัด Tier 2

# Catalyst types ที่ auto-promote เข้า Tier 1
TIER1_CATALYSTS   = {"EARNINGS", "FDA", "MA", "GUIDANCE_UP", "GUIDANCE_DOWN"}


def get_alpaca_snapshots(symbols: list) -> dict:
    """
    ดึง snapshot ทุก symbol ใน 1 API call ผ่าน Alpaca

    Returns:
      dict: { "NVDA": { "price": 182.5, "prev_close": 178.0,
                         "gap_pct": 2.53, "volume": 15_000_000,
                         "adv_10d": 50_000_000, "rvol": 0.30 }, ... }

    Alpaca Snapshot API (free tier, IEX feed):
      client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=universe))
      → latest_trade, daily_bar, prev_daily_bar, minute_bar ของทุกตัว
    """
    client = _get_alpaca_client()
    if client is None:
        logger.warning("[Snapshot] No Alpaca client → empty")
        return {}

    try:
        from alpaca.data.requests import StockSnapshotRequest
        from config.config import Config

        # ── Alpaca snapshot: 1 call ได้ทุก symbol
        request = StockSnapshotRequest(
            symbol_or_symbols=symbols,
            feed=Config.ALPACA_FEED,
        )
        raw_snapshots = client.get_stock_snapshot(request)

        result = {}
        for sym, snap in raw_snapshots.items():
            try:
                # ── Current price
                price = float(snap.latest_trade.price) if snap.latest_trade else 0

                # ── Previous close + Gap%
                prev_close = float(snap.previous_daily_bar.close) if snap.previous_daily_bar else 0
                gap_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 else 0

                # ── Today's volume
                today_vol = float(snap.daily_bar.volume) if snap.daily_bar else 0

                # ── RVOL (today's volume so far / historical average)
                # Snapshot ไม่มี ADV → ประมาณจาก prev day volume
                prev_vol = float(snap.previous_daily_bar.volume) if snap.previous_daily_bar else 1
                rvol = today_vol / prev_vol if prev_vol > 0 else 0

                result[sym] = {
                    "price":      round(price, 2),
                    "prev_close": round(prev_close, 2),
                    "gap_pct":    round(gap_pct, 2),
                    "volume":     int(today_vol),
                    "prev_vol":   int(prev_vol),
                    "rvol":       round(rvol, 2),
                }
            except Exception:
                continue

        logger.info(f"[Snapshot] Got {len(result)}/{len(symbols)} snapshots")
        return result

    except Exception as e:
        logger.error(f"[Snapshot] Error: {e}")
        return {}


def classify_tiers(
    universe:      list,
    snapshots:     dict,
    catalyst_map:  dict = None,
) -> TieredWatchlist:
    """
    จัด symbols เข้า Tier 1/2/3 จาก snapshot data

    Args:
      universe:     list of symbols
      snapshots:    dict from get_alpaca_snapshots()
      catalyst_map: dict { "NVDA": "EARNINGS", "TSLA": "ANALYST_UP", ... }
                    (จาก NewsScanner pre-market scan)

    Returns:
      TieredWatchlist
    """
    catalyst_map = catalyst_map or {}

    tier1_candidates = []   # (symbol, score) — sort แล้วตัด top N
    tier2_candidates = []
    tier3 = []

    for sym in universe:
        snap = snapshots.get(sym)
        if not snap:
            tier3.append(sym)
            continue

        gap   = abs(snap["gap_pct"])
        rvol  = snap["rvol"]
        cat   = catalyst_map.get(sym, "")

        # ── Tier 1: HOT
        is_hot = (
            (gap >= TIER1_GAP_PCT and rvol >= TIER1_RVOL) or
            cat in TIER1_CATALYSTS
        )
        if is_hot:
            # score = gap × rvol (ยิ่งสูงยิ่ง hot)
            score = gap * max(rvol, 1.0) + (20 if cat in TIER1_CATALYSTS else 0)
            tier1_candidates.append((sym, score))
            continue

        # ── Tier 2: WARM
        is_warm = (
            rvol >= TIER2_RVOL or
            gap >= 1.0 or
            cat != ""
        )
        if is_warm:
            score = gap * max(rvol, 1.0)
            tier2_candidates.append((sym, score))
            continue

        # ── Tier 3: COLD
        tier3.append(sym)

    # ── Rank + Trim
    tier1_candidates.sort(key=lambda x: x[1], reverse=True)
    tier2_candidates.sort(key=lambda x: x[1], reverse=True)

    tier1 = [s for s, _ in tier1_candidates[:TIER1_MAX]]
    tier2 = [s for s, _ in tier2_candidates[:TIER2_MAX]]

    # ── Overflow จาก tier1/tier2 → push ลง tier ถัดไป
    tier2 += [s for s, _ in tier1_candidates[TIER1_MAX:]]
    tier3 += [s for s, _ in tier2_candidates[TIER2_MAX:]]

    result = TieredWatchlist(
        tier1_hot=tier1,
        tier2_warm=tier2,
        tier3_cold=tier3,
        scan_time=datetime.now(timezone.utc).isoformat(),
        snapshot=snapshots,
    )

    logger.info(f"[Tiers] {result.summary()}")
    if tier1:
        logger.info(f"[Tier1 HOT] {tier1[:10]}{'...' if len(tier1) > 10 else ''}")
    return result


# ============================================================
# VWAP + ATR_15m FEATURE HELPERS
# ============================================================

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    คำนวณ VWAP (Volume Weighted Average Price) ภายในวัน

    สมการ:  VWAP = cumsum(Typical_Price × Volume) / cumsum(Volume)
            Typical_Price = (High + Low + Close) / 3

    IMPORTANT: VWAP ต้อง reset ทุกวัน (Daily Anchor)
               ห้ามเอา Volume เมื่อวานมาปน

    Input: DataFrame ที่มี columns [high, low, close, volume]
    Output: pd.Series ของ VWAP
    """
    # ── Normalize column names
    cols = {c.lower(): c for c in df.columns}
    h = df[cols.get("high", "High")]
    l = df[cols.get("low", "Low")]
    c = df[cols.get("close", "Close")]
    v = df[cols.get("volume", "Volume")]

    typical_price = (h + l + c) / 3.0

    # ── Group by date (reset cumsum ทุกวัน)
    # FIX: tz-aware index → tz_localize(None) ก่อนดึง .date
    idx = df.index
    if hasattr(idx, 'tz') and idx.tz is not None:
        groups = idx.tz_localize(None).date
    elif hasattr(idx, 'date'):
        groups = idx.date
    else:
        groups = pd.Series(0, index=idx)

    cumul_tp_vol = (typical_price * v).groupby(groups).cumsum()
    cumul_vol    = v.groupby(groups).cumsum()

    vwap = cumul_tp_vol / cumul_vol.replace(0, np.nan)
    return vwap


def compute_atr_15m(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    คำนวณ ATR ของ 15-minute bars

    Input: DataFrame ที่มี columns [high, low, close]
    Output: pd.Series ของ ATR
    """
    cols = {c.lower(): c for c in df.columns}
    h = df[cols.get("high", "High")]
    l = df[cols.get("low", "Low")]
    c = df[cols.get("close", "Close")]

    high_low   = h - l
    high_close = np.abs(h - c.shift(1))
    low_close  = np.abs(l - c.shift(1))

    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr        = true_range.rolling(window=period).mean()
    return atr


def compute_vwap_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    เพิ่ม VWAP-related features ลง DataFrame

    Columns ที่เพิ่ม:
      - vwap:           VWAP ภายในวัน
      - atr_15m:        ATR ของ 15m bars
      - price_vwap_ratio: Close / VWAP (>1 = above VWAP)
      - dist_from_vwap:  (Close - VWAP) / ATR_15m (normalized distance)
    """
    df = df.copy()

    df["vwap"]     = compute_vwap(df)
    df["atr_15m"]  = compute_atr_15m(df)

    cols = {c.lower(): c for c in df.columns}
    close_col = cols.get("close", "Close")

    df["price_vwap_ratio"] = df[close_col] / df["vwap"].replace(0, np.nan)
    df["dist_from_vwap"]   = (df[close_col] - df["vwap"]) / df["atr_15m"].replace(0, np.nan)

    return df


# ============================================================
# GOOGLE DRIVE DIRECTORY STRUCTURE
# ============================================================

class GDriveLayout:
    """
    กำหนด directory structure ใน Google Drive

    gdrive_root/
      models/
        {SYMBOL}/
          lgbm_{DD-MM-YYYY}.pkl
          lstm_{DD-MM-YYYY}.pt
          lstm_scaler_{DD-MM-YYYY}.pkl
          meta.json
      features/
        daily/
          {SYMBOL}/
            features_{SYMBOL}_{DD-MM-YYYY}.parquet
        weekly/
          {SYMBOL}/
            lstm_seq_{SYMBOL}_{WK-YYYY}.parquet
      training_data/
        {SYMBOL}/
          ohlcv_1m_{SYMBOL}_{DD-MM-YYYY}.parquet
          ohlcv_5m_{SYMBOL}_{DD-MM-YYYY}.parquet
      journal/
        trades_{DD-MM-YYYY}.csv
        performance_{DD-MM-YYYY}.json
    """

    def __init__(self, root: str):
        self.root = Path(root)

    def models_dir(self, symbol: str) -> Path:
        return self.root / "models" / symbol

    def features_daily_dir(self, symbol: str) -> Path:
        return self.root / "features" / "daily" / symbol

    def features_weekly_dir(self, symbol: str) -> Path:
        return self.root / "features" / "weekly" / symbol

    def training_data_dir(self, symbol: str) -> Path:
        return self.root / "training_data" / symbol

    def journal_dir(self) -> Path:
        return self.root / "journal"

    def ensure_all(self, symbol: str):
        """สร้าง directories ทั้งหมดถ้ายังไม่มี"""
        for d in [
            self.models_dir(symbol),
            self.features_daily_dir(symbol),
            self.features_weekly_dir(symbol),
            self.training_data_dir(symbol),
            self.journal_dir(),
        ]:
            d.mkdir(parents=True, exist_ok=True)

    # ── Filename helpers
    def lgbm_path(self, symbol: str, tag: Optional[str] = None) -> Path:
        tag = tag or daily_tag()
        return self.models_dir(symbol) / f"lgbm_{tag}.pkl"

    def lstm_pt_path(self, symbol: str, tag: Optional[str] = None) -> Path:
        tag = tag or daily_tag()
        return self.models_dir(symbol) / f"lstm_{tag}.pt"

    def lstm_scaler_path(self, symbol: str, tag: Optional[str] = None) -> Path:
        tag = tag or daily_tag()
        return self.models_dir(symbol) / f"lstm_scaler_{tag}.pkl"

    def features_daily_path(self, symbol: str, tag: Optional[str] = None) -> Path:
        tag = tag or daily_tag()
        return self.features_daily_dir(symbol) / f"features_{symbol}_{tag}.parquet"

    def features_weekly_path(self, symbol: str, tag: Optional[str] = None) -> Path:
        tag = tag or weekly_tag()
        return self.features_weekly_dir(symbol) / f"lstm_seq_{symbol}_{tag}.parquet"

    def ohlcv_1m_path(self, symbol: str, tag: Optional[str] = None) -> Path:
        tag = tag or daily_tag()
        return self.training_data_dir(symbol) / f"ohlcv_1m_{symbol}_{tag}.parquet"

    def ohlcv_5m_path(self, symbol: str, tag: Optional[str] = None) -> Path:
        tag = tag or daily_tag()
        return self.training_data_dir(symbol) / f"ohlcv_5m_{symbol}_{tag}.parquet"


# ============================================================
# DATA PIPELINE MANAGER
# ============================================================

class DataPipelineManager:
    """
    จัดการ pipeline ทั้งหมดตาม mode:

    mode="local" → ทำงานบนเครื่อง Local (RTX 3090)
      - ดึง OHLCV → save Parquet
      - คำนวณ features → save Parquet
      - เทรน LightGBM (daily) + LSTM (weekly)
      - sync ทั้งหมดไป Google Drive

    mode="vps" → ทำงานบน VPS
      - sync model + features ล่าสุดจาก Google Drive
      - โหลด features จาก Parquet (ไม่ต้อง re-compute)
      - Inference เท่านั้น
    """

    def __init__(
        self,
        mode:         str = "vps",          # "local" หรือ "vps"
        gdrive_root:  str = "./gdrive",     # path ที่ mount Google Drive
        local_cache:  str = "./cache",      # local cache ก่อน sync
        retain_daily: int = 30,             # เก็บ daily files กี่วัน
        retain_weekly: int = 12,            # เก็บ weekly files กี่สัปดาห์
    ):
        self.mode          = mode
        self.retain_daily  = retain_daily
        self.retain_weekly = retain_weekly

        self.gdrive = GDriveLayout(gdrive_root)
        self.cache  = GDriveLayout(local_cache)

        # โหลด ML modules
        self._load_ml_modules()

        logger.info(f"DataPipelineManager | mode={mode} | gdrive={gdrive_root}")

    def _load_ml_modules(self):
        """โหลด modules แบบ lazy เพื่อไม่ให้ import หนักตั้งแต่ต้น"""
        try:
            from models.technical_ml_analyzer import (
                FeatureEngineer, LabelGenerator,
                LightGBMModel, LSTMModel, ModelRegistry
            )
            self.FeatureEngineer  = FeatureEngineer
            self.LabelGenerator   = LabelGenerator
            self.LightGBMModel    = LightGBMModel
            self.LSTMModel        = LSTMModel
            self.ModelRegistry    = ModelRegistry
            logger.info("ML modules โหลดสำเร็จ")
        except ImportError as e:
            logger.warning(f"ML modules ไม่พร้อม: {e}")
            self.FeatureEngineer = None

    # ------------------------------------------
    # STEP 1: ดึงและ cache OHLCV
    # ------------------------------------------

    def fetch_and_cache_ohlcv(
        self,
        symbol: str,
        days_1m: int = 120,   # yfinance จำกัด 60 วันสำหรับ 15m → ใช้ 59 (safe margin)
        days_5m: int = 120,
    ) -> tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
        """
        ดึง OHLCV 1-min + 5-min แล้ว save เป็น Parquet รายวัน

        Parquet path:
          training_data/{SYMBOL}/ohlcv_1m_{SYMBOL}_{DD-MM-YYYY}.parquet
          training_data/{SYMBOL}/ohlcv_5m_{SYMBOL}_{DD-MM-YYYY}.parquet
        """

        tag = daily_tag()
        self.gdrive.ensure_all(symbol)

        df1_path = self.gdrive.ohlcv_1m_path(symbol, tag)
        df5_path = self.gdrive.ohlcv_5m_path(symbol, tag)

        # ถ้ามี cache แล้ว โหลดจาก Parquet เลย
        if df1_path.exists() and df5_path.exists():
            df1 = normalize_ohlcv(pd.read_parquet(df1_path))
            df5 = normalize_ohlcv(pd.read_parquet(df5_path))

            # ── Stale cache check: ถ้า bars น้อยเกินไป → ลบ cache แล้ว download ใหม่
            # เกิดจาก cache เก่าที่ save ตอน days_1m=5 (ได้แค่ 80 bars)
            MIN_OHLCV_BARS = 200
            if len(df1) < MIN_OHLCV_BARS:
                logger.warning(
                    f"[{symbol}] OHLCV cache stale: {len(df1)} bars < {MIN_OHLCV_BARS} "
                    f"→ delete & re-download"
                )
                df1_path.unlink()
                df5_path.unlink()
                # fall through ไป download ใหม่
            else:
                logger.info(f"[{symbol}] OHLCV cache hit: {tag} ({len(df1)} bars)")
                return df1, df5

        logger.info(f"[{symbol}] ดึง OHLCV 15m={days_1m}d daily={days_5m}d...")
        try:
            df1 = safe_download(symbol, period=f"{min(days_1m,59)}d",
                                   interval="15m")
            df5 = safe_download(symbol, period=f"{min(days_5m,59)}d",
                                   interval="1d")

            if df1.empty or df5.empty:
                logger.warning(f"[{symbol}] ไม่มีข้อมูล OHLCV")
                return None, None

            # Save Parquet
            df1.to_parquet(df1_path, compression="snappy", index=True)
            df5.to_parquet(df5_path, compression="snappy", index=True)
            logger.info(f"[{symbol}] Saved OHLCV → {df1_path.name}, {df5_path.name}")

            return df1, df5

        except Exception as e:
            logger.error(f"[{symbol}] fetch_ohlcv error: {e}")
            return None, None

    # ------------------------------------------
    # STEP 2: Pre-compute features → Parquet
    # ------------------------------------------

    def precompute_features_daily(
        self,
        symbol: str,
        df1: pd.DataFrame,
        df5: pd.DataFrame,
        catalyst_type: str = "OTHER",
        urgency_score: int = 50,
    ) -> Optional[pd.DataFrame]:
        """
        คำนวณ feature DataFrame (45 columns × N bars) แล้ว save Parquet

        Retention: DD-MM-YYYY (เก็บ 30 วัน)
        Path: features/daily/{SYMBOL}/features_{SYMBOL}_{DD-MM-YYYY}.parquet

        Return: DataFrame ที่พร้อมส่งเข้า LightGBM.train()
        """
        DAILY_HORIZEN_BAR_MIN = Config.DAILY_HORIZEN_BAR_MIN #2 # คาดการที่ time * 2 : เช่น 1 ข้อมูล 1 นาที ก็คือ ภายใน 2 นาทีถัดไป
        if self.FeatureEngineer is None:
            logger.warning("FeatureEngineer ไม่พร้อม")
            return None

        tag = daily_tag()
        path = self.gdrive.features_daily_path(symbol, tag)
        self.gdrive.ensure_all(symbol)

        if path.exists():
            cached_df = pd.read_parquet(path)
            # ── Stale cache check: ถ้า rows น้อยเกิน → ลบ cache แล้วคำนวณใหม่
            MIN_FEATURE_ROWS = 150   # ต้องมีอย่างน้อย 150 rows ถึงจะ train ได้
            if len(cached_df) < MIN_FEATURE_ROWS:
                logger.warning(
                    f"[{symbol}] Feature cache stale: {len(cached_df)} rows < {MIN_FEATURE_ROWS} "
                    f"→ delete & recompute"
                )
                path.unlink()
                # fall through ไปคำนวณใหม่
            else:
                logger.info(f"[{symbol}] Feature cache hit (daily): {tag} ({len(cached_df)} rows)")
                return cached_df

        logger.info(f"[{symbol}] คำนวณ daily features | bars={len(df1)}")
        fe   = self.FeatureEngineer()

        # ── TD #1 FIX: Vectorized batch computation
        # แทน for loop (O(n²)) ด้วย compute_vectorized (O(n) rolling ครั้งเดียว)
        feat_df = fe.compute_vectorized(df1, df5, catalyst_type, urgency_score)
        feat_df = feat_df.iloc[30:]   # ตัด warmup period 30 bars (rolling ต้องการ)

        if feat_df.empty:
            return None

        # เพิ่ม label
        lg        = self.LabelGenerator()
        #labels    = lg.generate(df1.iloc[30:],horizon=Config.DAILY_HORIZEN_BAR_MIN ).reset_index(drop=True)
        labels    = lg.generate(df1.iloc[30:], horizon=Config.DAILY_HORIZEN_BAR_MIN , up_threshold_pct=0.3, down_threshold_pct=0.3).reset_index(drop=True)

        feat_df   = feat_df.iloc[:len(labels)].copy()
        feat_df["label"] = labels.values[:len(feat_df)]

        feat_df.to_parquet(path, compression="snappy", index=False)
        logger.info(f"[{symbol}] Saved daily features → {path.name} | shape={feat_df.shape}")
        return feat_df

    def precompute_features_weekly(
        self,
        symbol: str,
        df1: pd.DataFrame,
        df5: pd.DataFrame,
        catalyst_type: str = "OTHER",
        urgency_score: int = 50,
        seq_len: int = 30,
    ) -> Optional[pd.DataFrame]:
        """
        คำนวณ LSTM sequence arrays แล้ว save Parquet

        Retention: WK-YYYY (เก็บ 12 สัปดาห์)
        Path: features/weekly/{SYMBOL}/lstm_seq_{SYMBOL}_{WK-YYYY}.parquet

        Return: DataFrame ที่ unpack เป็น numpy arrays ได้
        """
        WEEKLY_HORIZEN_BAR_MIN = 1
        if self.FeatureEngineer is None:
            return None

        tag  = weekly_tag()
        path = self.gdrive.features_weekly_path(symbol, tag)
        self.gdrive.ensure_all(symbol)

        if path.exists():
            logger.info(f"[{symbol}] Feature cache hit (weekly): {tag}")
            return pd.read_parquet(path)

        logger.info(f"[{symbol}] คำนวณ LSTM sequences | bars={len(df1)}")
        from models.technical_ml_analyzer import LabelGenerator, SEQ_LEN, HORIZON_MIN
        fe  = self.FeatureEngineer()
        lg  = LabelGenerator()
        label_series = lg.generate(df1)

        # ════════════════════════════════════════
        # Fix 3 (CPU): sliding_window_view แทน for loop
        # ════════════════════════════════════════
        # เดิม: for i in range(seq_len+30, len(df1)-HORIZON_MIN):
        #           slice1 = df1.iloc[i-seq_len-30:i]   ← สร้าง DataFrame ใหม่ทุก iteration
        #           seq    = fe.compute_sequence(slice1, ...) ← Python overhead ทุก row
        #       O(n) iterations × O(n) rolling per slice = O(n²)
        #
        # ใหม่: compute_vectorized บน full df1 ครั้งเดียว → sliding_window_view
        #       numpy สร้าง memory view (ไม่ copy) → reshape เป็น (n_windows, seq_len, n_feat)
        #       O(n) total, ไม่มี Python loop เลย

        # Step 1: Vectorize features บน full df1 ทั้งก้อน
        feat_matrix = fe.compute_vectorized(df1, df5).values.astype(np.float32)
        # feat_matrix shape: (n_bars, n_features)

        # Step 2: sliding_window_view — สร้าง windows ทั้งหมดพร้อมกัน (zero-copy view)
        windows = np.lib.stride_tricks.sliding_window_view(
            feat_matrix, window_shape=seq_len, axis=0
        )
        # windows shape: (n_bars - seq_len + 1, n_features, seq_len)
        # transpose → (n_windows, seq_len, n_features) เพื่อให้ตรงกับ LSTM input
        windows = windows.transpose(0, 2, 1)

        # Step 3: align labels กับ windows
        # window i ครอบคลุม bar [i, i+seq_len) → label คือ bar i+seq_len+HORIZON_MIN
        warmup   = 30   # ตัด warmup period ที่ rolling ยังไม่ stable
        start    = seq_len + warmup
        end      = len(df1) - HORIZON_MIN
        if start >= end or len(windows) < (end - seq_len - warmup):
            logger.warning(f"[{symbol}] ข้อมูลน้อยเกินไปสำหรับ LSTM sequences")
            return None

        # ตัด windows ให้ตรงกับช่วงที่มี label
        w_start  = warmup                # index ใน windows array
        w_end    = end - seq_len         # windows ที่ label ยังอยู่ใน range
        seqs_arr = windows[w_start:w_end]
        labels   = label_series.iloc[start:start + len(seqs_arr)].values.astype(np.float32)
        timestamps = (df1.index[start:start + len(seqs_arr)].tolist()
                      if hasattr(df1.index, '__getitem__') else list(range(len(seqs_arr))))

        if len(seqs_arr) == 0 or len(seqs_arr) != len(labels):
            logger.warning(f"[{symbol}] seqs/labels mismatch: {len(seqs_arr)} vs {len(labels)}")
            return None

        n_features = seqs_arr.shape[2]
        logger.info(f"[{symbol}] sliding_window_view: {len(seqs_arr)} sequences "
                    f"| shape={seqs_arr.shape}")

        # ── Save: pyarrow List<float32> (TD #2 fix)
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq

            # seqs_arr shape: (n_windows, seq_len, n_features) → flatten per window
            flat_lists = [row.flatten().tolist() for row in seqs_arr]
            pa_table = pa.table({
                "timestamp":  pa.array([str(t) for t in timestamps]),
                "label":      pa.array(labels, type=pa.float32()),
                "seq_flat":   pa.array(flat_lists, type=pa.list_(pa.float32())),
                "seq_len":    pa.array([seq_len] * len(seqs_arr), type=pa.int32()),
                "n_features": pa.array([n_features] * len(seqs_arr), type=pa.int32()),
                "fmt":        pa.array(["pa_list_f32"] * len(seqs_arr)),
            })
            pq.write_table(pa_table, path, compression="snappy")
            logger.info(f"[{symbol}] Saved weekly sequences → {path.name} "
                        f"| samples={len(seqs_arr)} | fmt=pa_list_f32")

            # อ่านกลับเป็น DataFrame เพื่อ return ให้ caller ใช้ train ต่อ
            seq_df = pd.read_parquet(path)

        except ImportError:
            logger.warning(f"[{symbol}] pyarrow ไม่พบ → fallback string serialization")
            seq_df = pd.DataFrame({
                "timestamp":  timestamps,
                "label":      labels,
                "seq_flat":   [str(row.flatten().tolist()) for row in seqs_arr],
                "seq_len":    seq_len,
                "n_features": n_features,
                "fmt":        "str",
            })
            seq_df.to_parquet(path, compression="snappy", index=False)
        return seq_df

    # ------------------------------------------
    # STEP 3: Train + Save model
    # ------------------------------------------

    def train_and_save_lgbm(self, symbol: str, feat_df: Optional[pd.DataFrame] = None) -> float:
        """
        Pipeline สำหรับการ Train และบันทึก Model ลง Google Drive
        """
        tag = daily_tag() # ฟังก์ชันสร้างวันที่ DD-MM-YYYY
        save_path = self.gdrive.lgbm_path(symbol, tag)

        # 1. เช็คว่ามีโมเดลของวันนี้หรือยัง
        if save_path.exists():
            logger.info(f"[{symbol}] Model today already exists: {tag}")
            return 0.0

        # 2. จัดเตรียมข้อมูล (ถ้าไม่ได้ส่ง feat_df มา ให้โหลดจาก Parquet)
        if feat_df is None:
            feat_path = self.gdrive.features_daily_path(symbol, tag)
            if not feat_path.exists():
                logger.error(f"[{symbol}] No feature file found at {feat_path}")
                return 0.0
            feat_df = pd.read_parquet(feat_path)

        if "label" not in feat_df.columns:
            logger.error(f"[{symbol}] Missing 'label' column in dataframe")
            return 0.0

        X = feat_df.drop(columns=["label"])
        y = feat_df["label"]

        # 3. สั่งเทรนผ่าน Class LightGBMModel
        lgbm_engine = self.LightGBMModel()
        auc = lgbm_engine.train(X, y)

        # 4. บันทึกถ้า AUC ผ่านเกณฑ์ (เช่น > 0.52 เพื่อความมี Edge)
        if auc > 0.52:
            import joblib
            self.gdrive.ensure_all(symbol) # สร้าง folder ถ้ายังไม่มี
            joblib.dump(lgbm_engine, save_path)
            logger.info(f"[{symbol}] ✅ Saved Model | AUC: {auc:.4f} | Path: {save_path.name}")
        else:
            logger.warning(f"[{symbol}] ❌ Model weak (AUC: {auc:.4f}) | Not saving.")

        return auc

    def train_and_save_lstm(
        self,
        symbol: str,
        seq_df: Optional[pd.DataFrame] = None,
    ) -> float:
        """
        เทรน LSTM จาก sequence Parquet แล้ว save model
        ใช้ GPU ถ้ามี (RTX 3090 → CUDA อัตโนมัติ)

        Save paths:
          models/{SYMBOL}/lstm_{DD-MM-YYYY}.pt
          models/{SYMBOL}/lstm_scaler_{DD-MM-YYYY}.pkl
        """
        try:
            import torch
        except ImportError:
            logger.warning("[LSTM] PyTorch ไม่พบ → ข้าม LSTM training")
            return 0.0

        if self.LSTMModel is None:
            return 0.0

        tag      = daily_tag()
        pt_path  = self.gdrive.lstm_pt_path(symbol, tag)
        sc_path  = self.gdrive.lstm_scaler_path(symbol, tag)

        if pt_path.exists():
            logger.info(f"[{symbol}] LSTM model มีแล้ว: {tag}")
            return 0.0

        # โหลด sequences
        seq_path = self.gdrive.features_weekly_path(symbol, weekly_tag())
        if seq_df is None:
            
            if not seq_path.exists():
                logger.warning(f"[{symbol}] ไม่พบ sequence Parquet → ข้าม LSTM")
                return 0.0
            seq_df = pd.read_parquet(seq_path)

        # Reconstruct numpy arrays จาก Parquet
        # ตรวจ format tag เพื่อเลือก fast path (pyarrow zero-copy) หรือ string fallback
        seq_len    = int(seq_df["seq_len"].iloc[0])
        n_features = int(seq_df["n_features"].iloc[0])
        # 🚩 1. ดึง Label ดิบออกมาก่อน (อาจจะมี -1, 0, 1)
        raw_labels = seq_df["label"].values

        # 🚩 2. Map Labels ให้เป็น 0, 1, 2 เหมือนที่เราทำใน LightGBM
        mapped_labels = np.zeros_like(raw_labels)
        mapped_labels[raw_labels == -1] = 0
        mapped_labels[raw_labels == 0]  = 1
        mapped_labels[raw_labels == 1]  = 2
        
        #labels     = seq_df["label"].values.astype(np.float32)
        # 🚩 3. เปลี่ยนชนิดตัวแปรเป็น int64 (สำคัญมากสำหรับ PyTorch CrossEntropy)
        labels = mapped_labels.astype(np.int64)
        
        fmt        = str(seq_df["fmt"].iloc[0]) if "fmt" in seq_df.columns else "str"

        if fmt == "pa_list_f32":
            # ── Fast path: pyarrow zero-copy flatten (34x faster than string parse)
            # ไม่ผ่าน Python list → ไม่มี Python overhead ทั้งหมด
            import pyarrow.parquet as _pq
            tbl   = _pq.read_table(seq_path)
            col   = tbl.column("seq_flat")
            flat  = col.combine_chunks().flatten()           # nested list → flat float32
            X_seq = flat.to_numpy(zero_copy_only=False).reshape(-1, seq_len, n_features)
        else:
            # ── Fallback: string parse (ไฟล์ที่ generate ด้วย format เก่า)
            seqs = []
            for s in seq_df["seq_flat"]:
                arr = np.fromstring(str(s).strip("[]"), sep=",", dtype=np.float32)
                seqs.append(arr.reshape(seq_len, n_features))
            X_seq = np.array(seqs)

        model = self.LSTMModel(input_size=n_features)
        auc   = model.train(X_seq, labels)

        if auc > 0 and model.model is not None:
            import joblib
            self.gdrive.ensure_all(symbol)
            torch.save(model.model.state_dict(), pt_path)
            joblib.dump(model.scaler, sc_path)
            logger.info(f"[{symbol}] Saved LSTM → {pt_path.name} | AUC={auc:.4f}")

        return auc

    # ------------------------------------------
    # TIERED WATCHLIST — Pre-Market Scan + Train
    # ------------------------------------------

    def scan_pre_market(
        self,
        universe:     list = None,
        catalyst_map: dict = None,
    ) -> TieredWatchlist:
        """
        Pre-Market Scan: จัด universe เข้า Tier 1/2/3

        เรียกทุกเช้า 08:00 ET (ก่อนตลาดเปิด 1.5 ชม.)

        Args:
          universe:     list of symbols (ถ้า None → โหลดจาก universe.json)
          catalyst_map: dict { "NVDA": "EARNINGS", ... } จาก NewsScanner

        Returns:
          TieredWatchlist

        Flow:
          1. โหลด universe (300 ตัว)
          2. Alpaca snapshot API → gap%, volume ทุกตัวใน 1 call
          3. classify_tiers() → Tier 1/2/3
        """
        if not universe:
            universe = self.load_universe(prefer_gdrive=(self.mode == "vps"))
        if not universe:
            logger.error("[PreMarket] ไม่มี universe → return empty tiers")
            return TieredWatchlist()

        logger.info(f"[PreMarket] Scanning {len(universe)} symbols...")

        # ── Alpaca Snapshot (1 API call ได้ทุกตัว)
        # แบ่ง batch ละ 1000 ตัว (Alpaca limit)
        all_snapshots = {}
        for i in range(0, len(universe), 1000):
            batch = universe[i:i+1000]
            snaps = get_alpaca_snapshots(batch)
            all_snapshots.update(snaps)

        # ── Classify
        tiers = classify_tiers(universe, all_snapshots, catalyst_map)

        logger.info(f"[PreMarket] Done: {tiers.summary()}")
        return tiers

    def precompute_features_daily(
        self,
        symbol: str,
        df1: pd.DataFrame,
        df5: pd.DataFrame,
        catalyst_type: str = "OTHER",
        urgency_score: int = 50,
    ) -> Optional[pd.DataFrame]:

        if self.FeatureEngineer is None:
            logger.warning("FeatureEngineer ไม่พร้อม")
            return None

        tag = daily_tag()
        path = self.gdrive.features_daily_path(symbol, tag)
        self.gdrive.ensure_all(symbol)

        if path.exists():
            cached_df = pd.read_parquet(path)
            # ── Stale cache check: ถ้า rows น้อยเกิน → ลบ cache แล้วคำนวณใหม่
            MIN_FEATURE_ROWS = 150   # ต้องมีอย่างน้อย 150 rows ถึงจะ train ได้
            if len(cached_df) < MIN_FEATURE_ROWS:
                logger.warning(
                    f"[{symbol}] Feature cache stale: {len(cached_df)} rows < {MIN_FEATURE_ROWS} "
                    f"→ delete & recompute"
                )
                path.unlink()
                # fall through ไปคำนวณใหม่
            else:
                logger.info(f"[{symbol}] Feature cache hit (daily): {tag} ({len(cached_df)} rows)")
                return cached_df

        logger.info(f"[{symbol}] คำนวณ daily features | bars={len(df1)}")
        logger.info(f"[{symbol}] คำนวณ daily features | bars={len(df1)}")
        fe = self.FeatureEngineer() # ใช้ Class ที่เราปรับปรุงกันเมื่อครู่

        # 1. คำนวณ Features (Vectorized)
        feat_df = fe.compute_vectorized(df1, df5, catalyst_type, urgency_score)
        
        # 2. สร้าง Labels
        lg = self.LabelGenerator()
        # หมายเหตุ: ถ้า df1 เป็น 5m, horizon=2 คือทาย 10 นาทีข้างหน้า
        # threshold 0.3% สำหรับ 5-10 นาที ถือว่าท้าทายมาก (Aggressive) สำหรับหุ้นใหญ่
        labels = lg.generate(
            df1, 
            horizon=Config.DAILY_HORIZEN_BAR_MIN , 
            up_threshold_pct=0.3, 
            down_threshold_pct=0.3
        )

        # 3. CRITICAL FIX: การรวม Feature และ Label ด้วย Index (กันข้อมูลเยื้องกัน)
        # เราจะเอาเฉพาะแถวที่มีทั้ง Features และ Labels
        labels.name = "label"
        final_df = feat_df.join(labels, how='inner').dropna()

        # 4. ตัด Warmup Period (30 bars แรกที่ Indicators ยังคำนวณไม่นิ่ง)
        if len(final_df) > 30:
            final_df = final_df.iloc[30:]
        else:
            logger.warning(f"[{symbol}] Data too short after joining labels")
            return None

        # 5. Save และ Return
        final_df.to_parquet(path, compression="snappy", index=True) # เก็บ Index ไว้เช็คเวลาด้วย
        logger.info(f"[{symbol}] Saved daily features → {path.name} | shape={final_df.shape}")
        
        return final_df

    def just_in_time_train(
        self,
        symbol:  str,
        days_1m: int = 59,
        days_5m: int = 60,
    ) -> float:
        """
        Just-in-Time Training — เทรน LightGBM ทันทีเมื่อ symbol ถูก promote

        ใช้เมื่อ:
          - Breaking news สำหรับ Tier 3 symbol → promote เป็น Tier 1
          - Symbol ไม่มี model เลย → ต้อง train ก่อน trade

        Speed: ~10–15 วินาทีต่อ symbol (LightGBM only, ไม่ train LSTM)

        Returns:
          AUC score (0.0 ถ้า fail)
        """
        logger.info(f"[JIT] Just-in-Time train: {symbol}")
        start = _time.time()

        try:
            # ── Download
            df1 = safe_download(symbol, period=f"{days_1m}d", interval="15m")
            df5 = safe_download(symbol, period=f"{days_5m}d", interval="1d")

            if df1.empty or df5.empty:
                logger.warning(f"[JIT] {symbol}: no data")
                return 0.0

            # ── Feature Engineering
            feat_df = self.precompute_features_daily(symbol, df1, df5)
            if feat_df is None:
                return 0.0

            # ── Train LightGBM (fast, ~5–10s)
            auc = self.train_and_save_lgbm(symbol, feat_df)

            elapsed = _time.time() - start
            logger.info(f"[JIT] {symbol} done | AUC={auc:.4f} | {elapsed:.1f}s")
            return auc

        except Exception as e:
            logger.error(f"[JIT] {symbol} error: {e}")
            return 0.0

    def _find_recent_model(self, symbol: str, max_age_days: int = 3) -> str:
        """หา model ที่เทรนภายใน N วัน สำหรับ Tier 2 reuse"""
        model_dir = self.gdrive.models_dir(symbol)
        if not model_dir.exists():
            return ""

        now = datetime.now(timezone.utc)
        for i in range(max_age_days):
            check_date = now - timedelta(days=i)
            tag = daily_tag(check_date)
            path = model_dir / f"lgbm_{tag}.pkl"
            if path.exists():
                return tag
        return ""

    # ------------------------------------------
    # FULL PIPELINES (สำหรับรัน scheduler)
    # ------------------------------------------

    def run_daily_pipeline(
        self,
        watchlist:       list  = None,
        days_1m:         int   = 120,   # yfinance 15m: max 60 วัน → ใช้ 59
        days_5m:         int   = 120,
        auto_universe:   bool  = False,
        universe_kwargs: dict  = None,
    ):
        """
        รัน pipeline รายวัน (LOCAL เท่านั้น):
          0. [Optional] generate/load Universe
          1. ดึง OHLCV → Parquet
          2. คำนวณ features → Parquet
          3. เทรน LightGBM → .pkl
          4. Cleanup files เก่า

        Args:
          watchlist:       รายชื่อ symbols — ถ้า None จะโหลดจาก universe.json
          auto_universe:   True = รัน generate_ttp_universe() ก่อน (refresh สัปดาห์ละครั้ง)
          universe_kwargs: kwargs ส่งต่อให้ generate_ttp_universe()

        เรียกใน Cron Job: 08:00 AM ET ทุกวัน (20:00 น. ไทย)
        """
        if self.mode != "local":
            logger.warning("run_daily_pipeline ใช้ได้เฉพาะ mode='local'")
            return

        # ── Step 0: Universe
        if auto_universe:
            logger.info("[Daily Pipeline] generate_ttp_universe()...")
            watchlist = self.generate_ttp_universe(**(universe_kwargs or {}))
        elif not watchlist:
            watchlist = self.load_universe(prefer_gdrive=False)

        if not watchlist:
            logger.error("[Daily Pipeline] ไม่มี watchlist — abort")
            return

        # ── Step 0.5: Save Watchlist จาก Universe
        #    สร้าง watchlist.json เพื่อให้ run_weekly_pipeline() อ่านได้
        source = "universe_auto" if auto_universe else "universe_loaded"
        self.save_watchlist(watchlist, source=source)
        logger.info(f"[Daily Pipeline] Watchlist saved: {len(watchlist)} symbols (source={source})")

        tag = daily_tag()
        logger.info(f"[Daily Pipeline] {tag} | {len(watchlist)} symbols")

        # ════════════════════════════════════════
        # Phase 1 (I/O): Parallel download — ThreadPoolExecutor
        # ════════════════════════════════════════
        # Alpaca รองรับ concurrent ได้ดี → workers มากขึ้นได้
        # (yfinance fallback ยังมี semaphore ป้องกัน rate limit ในตัว)
        import concurrent.futures

        MAX_DOWNLOAD_WORKERS = min(DOWNLOAD_MAX_WORKERS, len(watchlist))

        ohlcv_cache: dict = {}   # sym → (df1, df5) หรือ None

        def _fetch(sym):
            try:
                return sym, self.fetch_and_cache_ohlcv(sym, days_1m, days_5m)
            except Exception as e:
                logger.error(f"[{sym}] fetch error: {e}")
                return sym, (None, None)

        logger.info(f"  Phase 1: Parallel download ({MAX_DOWNLOAD_WORKERS} workers)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
            futures = {pool.submit(_fetch, sym): sym for sym in watchlist}
            for fut in concurrent.futures.as_completed(futures):
                sym, result = fut.result()
                ohlcv_cache[sym] = result

        logger.info(f"  Phase 1 done: {sum(1 for v in ohlcv_cache.values() if v[0] is not None)}"
                    f"/{len(watchlist)} symbols downloaded")

        # ════════════════════════════════════════
        # Phase 2 (CPU): Feature + Train — Sequential (GPU ใช้ได้ทีละ 1)
        # ════════════════════════════════════════
        for sym in watchlist:
            logger.info(f"\n── {sym} ──")
            try:
                df1, df5 = ohlcv_cache.get(sym, (None, None))
                if df1 is None:
                    continue

                feat_df = self.precompute_features_daily(sym, df1, df5)
                if feat_df is None:
                    continue

                auc = self.train_and_save_lgbm(sym, feat_df)
                logger.info(f"[{sym}] Daily done | AUC={auc:.4f}")

            except Exception as e:
                logger.error(f"[{sym}] daily pipeline error: {e}", exc_info=True)

        self._cleanup_old_files("daily")
        logger.info(f"[Daily Pipeline] เสร็จสิ้น")

    def run_weekly_pipeline(
        self,
        watchlist: list[str] = None,
        days_5m: int = 90,
    ):
        """
        รัน pipeline รายสัปดาห์ (LOCAL + RTX 3090 เท่านั้น):
          0. โหลด watchlist จาก watchlist.json (ถ้าไม่ส่ง watchlist มา)
          1. ดึง OHLCV 5-min ย้อนหลัง 90 วัน → Parquet
          2. คำนวณ LSTM sequences → Parquet
          3. เทรน LSTM บน GPU → .pt
          4. Cleanup files เก่า

        Args:
          watchlist: รายชื่อ symbols — ถ้า None จะโหลดจาก watchlist.json
                     (สร้างโดย run_daily_pipeline → save_watchlist)
          days_5m:   จำนวนวันย้อนหลังสำหรับดึงข้อมูล

        เรียกใน Cron Job: 07:00 AM ET วันอาทิตย์ (19:00 น. ไทย)
        """
        if self.mode != "local":
            logger.warning("run_weekly_pipeline ใช้ได้เฉพาะ mode='local'")
            return

        # ── Step 0: โหลด Watchlist จากไฟล์ (ถ้าไม่ได้ส่งมา)
        if not watchlist:
            logger.info("[Weekly Pipeline] โหลด watchlist จาก watchlist.json...")
            watchlist = self.load_watchlist(prefer_gdrive=False)

        if not watchlist:
            logger.error("[Weekly Pipeline] ไม่มี watchlist — abort "
                         "(ต้องรัน run_daily_pipeline ก่อนเพื่อสร้าง watchlist.json)")
            return

        tag = weekly_tag()
        logger.info(f"[Weekly Pipeline] {tag} | {len(watchlist)} symbols")

        # ════════════════════════════════════════
        # Phase 1 (I/O): Parallel download
        # ════════════════════════════════════════
        # Weekly: 2 calls/symbol (15m + 1d) → ลด workers เหลือ 3
        # เพื่อไม่ให้ชน Alpaca rate limit (200 req/min)
        # _alpaca_download() มี semaphore + pace + retry อยู่แล้ว
        import concurrent.futures

        def _fetch_weekly(sym):
            try:
                df1 = safe_download(sym, period="120d", interval="15m")
                df5 = safe_download(sym, period="120d", interval="1d")
                if df1.empty or df5.empty:
                    return sym, (None, None)
                return sym, (df1, df5)
            except Exception as e:
                logger.error(f"[{sym}] weekly fetch error: {e}")
                return sym, (None, None)

        MAX_WORKERS = min(3, len(watchlist))   # ลดจาก 5 → 3 (2 calls/sym)
        ohlcv_cache: dict = {}
        _done_count = 0

        logger.info(f"  Phase 1: Parallel download ({MAX_WORKERS} workers, {len(watchlist)} symbols)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_fetch_weekly, sym): sym for sym in watchlist}
            for fut in concurrent.futures.as_completed(futures):
                sym, result = fut.result()
                ohlcv_cache[sym] = result
                _done_count += 1
                if _done_count % 10 == 0 or _done_count == len(watchlist):
                    n_ok = sum(1 for v in ohlcv_cache.values() if v[0] is not None)
                    logger.info(
                        f"  Phase 1 progress: {_done_count}/{len(watchlist)} "
                        f"(success={n_ok})"
                    )

        n_ok = sum(1 for v in ohlcv_cache.values() if v[0] is not None)
        logger.info(f"  Phase 1 done: {n_ok}/{len(watchlist)} symbols downloaded")

        # ════════════════════════════════════════
        # Phase 2 (CPU+GPU): Sequence + Train — Sequential (GPU ทีละ 1)
        # ════════════════════════════════════════
        for sym in watchlist:
            logger.info(f"\n── {sym} ──")
            try:
                df1, df5 = ohlcv_cache.get(sym, (None, None))
                if df1 is None:
                    continue

                seq_df = self.precompute_features_weekly(sym, df1, df5)
                if seq_df is None:
                    continue

                auc = self.train_and_save_lstm(sym, seq_df)
                logger.info(f"[{sym}] Weekly done | AUC={auc:.4f}")

            except Exception as e:
                logger.error(f"[{sym}] weekly pipeline error: {e}", exc_info=True)

        self._cleanup_old_files("weekly")
        logger.info(f"[Weekly Pipeline] เสร็จสิ้น")

    # ------------------------------------------
    # VPS: โหลด model ล่าสุดจาก Google Drive
    # ------------------------------------------

    def sync_latest_models(self, watchlist: list[str]):
        """
        VPS: sync model + features ล่าสุดจาก Google Drive
        เรียกตอน startup และหลัง daily retrain เสร็จ
        """
        tag_daily  = daily_tag()
        tag_weekly = weekly_tag()

        for sym in watchlist:
            # LightGBM model
            src = self.gdrive.lgbm_path(sym, tag_daily)
            dst = self.cache.lgbm_path(sym, tag_daily)
            self._sync_file(src, dst, label=f"{sym} LGBM")

            # LSTM model
            src = self.gdrive.lstm_pt_path(sym, tag_daily)
            dst = self.cache.lstm_pt_path(sym, tag_daily)
            self._sync_file(src, dst, label=f"{sym} LSTM.pt")

            src = self.gdrive.lstm_scaler_path(sym, tag_daily)
            dst = self.cache.lstm_scaler_path(sym, tag_daily)
            self._sync_file(src, dst, label=f"{sym} LSTM scaler")

    def load_features(self, symbol: str, freq: str = "daily") -> Optional[pd.DataFrame]:
        """
        VPS: โหลด feature Parquet ล่าสุดสำหรับ inference
        ไม่ต้อง re-compute ทำให้ inference เร็วขึ้น

        freq: "daily" หรือ "weekly"
        """
        tag  = daily_tag() if freq == "daily" else weekly_tag()
        path = (self.gdrive.features_daily_path(symbol, tag)
                if freq == "daily"
                else self.gdrive.features_weekly_path(symbol, tag))

        if path.exists():
            df = pd.read_parquet(path)
            logger.debug(f"[{symbol}] Loaded {freq} features: {df.shape}")
            return df

        logger.info(f"[{symbol}] ไม่พบ {freq} features ({tag}) → จะ compute ใหม่")
        return None

    # ------------------------------------------
    # BATCH DOWNLOAD (Alpaca multi-symbol)
    # ------------------------------------------

    def _batch_download_alpaca(
        self,
        symbols: list,
        period:  str = "15d",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        Alpaca batch download — ดึงหลาย symbols พร้อมกัน

        Alpaca รองรับ symbol_or_symbols=["NVDA","TSLA",...] ในตัว
        → return DataFrame MultiIndex (symbol, timestamp)

        Returns:
          DataFrame MultiIndex columns (field, symbol)
          Empty DataFrame ถ้า error
        """
        client = _get_alpaca_client()
        if client is None:
            return pd.DataFrame()

        try:
            from alpaca.data.requests import StockBarsRequest
            from config.config import Config

            timeframe = _parse_interval_to_timeframe(interval)
            start, end = _period_to_dates(period)

            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=timeframe,
                start=start,
                end=end,
                feed=Config.ALPACA_FEED,
            )

            bars = client.get_stock_bars(request)
            df = bars.df

            if df.empty:
                return pd.DataFrame()

            # ── Alpaca returns: MultiIndex index (symbol, timestamp)
            # Pivot เป็น MultiIndex columns (field, symbol) ให้ caller parse ง่าย
            if isinstance(df.index, pd.MultiIndex) and "symbol" in df.index.names:
                # Unstack symbol level → columns become (field, symbol)
                result = df.unstack(level="symbol")
                # Strip timezone
                if hasattr(result.index, 'tz') and result.index.tz is not None:
                    try:
                        result.index = result.index.tz_localize(None)
                    except TypeError:
                        result.index = result.index.tz_convert("UTC").tz_localize(None)
                return result

            return df

        except Exception as e:
            logger.warning(f"[Alpaca] Batch download error: {e}")
            return pd.DataFrame()

    def generate_ttp_universe(
        self,
        paper:             bool  = True,
        min_price:         float = 5.0,
        min_adv:           int   = 1_000_000,
        min_dollar_vol:    float = 5_000_000.0,
        target_size:       int   = 300,
        save_to_gdrive:    bool  = True,
        batch_size:        int   = 200,
    ) -> list:
        """
        สร้าง Dynamic Universe — ร่อนหุ้น ~8,000-10,000 ตัว
        เหลือ "ช้างเผือก" 100-300 ตัว สำหรับส่งเข้า pipeline

        API Keys อ่านจาก Config.get_alpaca_keys() (กำหนดที่เดียวใน config.py)

        เกณฑ์กรอง (TTP Level 1):
          1. Status: Active + Tradable + Marginable
          2. Easy-to-Borrow
          3. Price ≥ $5
          4. ADV 10d ≥ 1,000,000 shares
          5. Dollar Volume ≥ $5M/วัน
          6. Rank by Dollar Volume → top target_size

        Returns:
          list[str] — symbols ที่ผ่านเกณฑ์ เรียงตาม Dollar Volume มากไปน้อย
        """
        import json

        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass, AssetStatus
        except ImportError:
            logger.error("alpaca-py ไม่ได้ติดตั้ง: pip install alpaca-py")
            return []

        # ── อ่าน API key จาก Config (ที่เดียว)
        from config.config import Config
        mode = "paper" if paper else "live"
        key, secret = Config.get_alpaca_keys(mode)

        if not key or not secret:
            raise EnvironmentError(
                "ต้องการ Alpaca API key\n"
                "  ตั้งค่าใน .env: ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET\n"
                "  (Config จะอ่านจาก env อัตโนมัติ)"
            )

        # ════════════════════════════════════════
        # Step 1: Alpaca Metadata Filter
        # ════════════════════════════════════════
        logger.info("=" * 50)
        logger.info("Universe Generator — Step 1: Alpaca Metadata")
        logger.info("=" * 50)

        client = TradingClient(key, secret, paper=paper)
        req    = GetAssetsRequest(
            asset_class = AssetClass.US_EQUITY,
            status      = AssetStatus.ACTIVE,
        )
        assets = client.get_all_assets(req)
        logger.info(f"  ดึง US Equity ทั้งหมด: {len(assets):,} ตัว")

        candidates = []
        skip = {"not_tradable": 0, "not_marginable": 0, "hard_to_borrow": 0}

        for a in assets:
            if not a.tradable:
                skip["not_tradable"] += 1; continue
            if not a.marginable:
                skip["not_marginable"] += 1; continue
            if not a.easy_to_borrow:
                skip["hard_to_borrow"] += 1; continue
            # กรอง symbol แปลก: ตัวพิมพ์เล็ก, มี / หรือ . (มักเป็น warrant/right)
            sym = a.symbol
            if not sym.isupper() or "/" in sym or len(sym) > 5:
                continue
            candidates.append(sym)

        logger.info(f"  ผ่าน Metadata filter: {len(candidates):,} ตัว | ตัดออก: {skip}")

        if not candidates:
            logger.error("ไม่มี candidate — ตรวจสอบ API key")
            return []

        # ════════════════════════════════════════
        # Step 2: Alpaca Batch ADV + Price (primary) → yfinance fallback
        # ════════════════════════════════════════
        logger.info(f"Step 2: Batch ADV 10d (batch={batch_size})")
        logger.info(f"  จำนวน batch: {(len(candidates) + batch_size - 1) // batch_size}")

        symbol_data = {}   # sym → {price, adv_10d, dollar_vol}
        total_batches = (len(candidates) + batch_size - 1) // batch_size

        for b_idx in range(0, len(candidates), batch_size):
            batch = candidates[b_idx: b_idx + batch_size]
            b_num = b_idx // batch_size + 1

            if b_num % 5 == 1:
                logger.info(f"  Batch {b_num}/{total_batches} ({len(batch)} symbols)...")

            try:
                # ── Alpaca batch download (primary)
                raw = self._batch_download_alpaca(batch, period="15d", interval="1d")

                # ── Fallback: yfinance ถ้า Alpaca ไม่ได้
                if raw is None or raw.empty:
                    raw = _yfinance_download(
                        tickers=" ".join(batch), period="15d", interval="1d",
                        group_by="ticker",
                    )

                if raw.empty:
                    continue

                for sym in batch:
                    try:
                        # ── Extract single symbol from batch result
                        if isinstance(raw.columns, pd.MultiIndex):
                            # MultiIndex: (field, symbol) or (symbol, field)
                            level_values = [
                                raw.columns.get_level_values(i).unique().tolist()
                                for i in range(raw.columns.nlevels)
                            ]
                            # หา level ที่มี sym อยู่
                            for lvl in range(raw.columns.nlevels):
                                if sym in level_values[lvl]:
                                    df_sym = raw.xs(sym, axis=1, level=lvl).dropna(how="all")
                                    break
                            else:
                                continue
                        elif len(batch) == 1:
                            df_sym = raw
                        else:
                            continue

                        if df_sym.empty or len(df_sym) < 5:
                            continue

                        # ── Normalize column names
                        col_map = {c: c.lower() for c in df_sym.columns}
                        df_sym = df_sym.rename(columns=col_map)

                        close  = float(df_sym["close"].iloc[-1])
                        vol_10 = float(df_sym["volume"].tail(10).mean())
                        dv     = close * vol_10

                        if close < min_price or vol_10 < min_adv or dv < min_dollar_vol:
                            continue

                        symbol_data[sym] = {
                            "price":      round(close, 2),
                            "adv_10d":    int(vol_10),
                            "dollar_vol": round(dv, 0),
                        }
                    except Exception:
                        continue

            except Exception as e:
                logger.warning(f"  Batch {b_num} error: {e} → ข้าม")
                continue

        logger.info(f"  ผ่าน ADV + Price filter: {len(symbol_data):,} ตัว")

        # ════════════════════════════════════════
        # Step 3: Rank + Trim
        # ════════════════════════════════════════
        logger.info(f"Step 3: Rank by Dollar Volume → top {target_size}")

        ranked = sorted(
            symbol_data.items(),
            key     = lambda x: x[1]["dollar_vol"],
            reverse = True,
        )
        final = [sym for sym, _ in ranked[:target_size]]

        # ════════════════════════════════════════
        # Step 4: Save universe.json
        # ════════════════════════════════════════
        payload = {
            "generated_at":     datetime.now(timezone.utc).isoformat(),
            "tag":              daily_tag(),
            "stats": {
                "total_us_equity":  len(assets),
                "after_metadata":   len(candidates),
                "after_adv_price":  len(symbol_data),
                "final":            len(final),
            },
            "criteria": {
                "min_price":      min_price,
                "min_adv_10d":    min_adv,
                "min_dollar_vol": min_dollar_vol,
                "target_size":    target_size,
            },
            "symbols":     final,
            "symbol_data": {sym: symbol_data[sym] for sym in final},
        }

        # Local cache
        local_path = Path(self.cache.root) / "universe.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info(f"  Saved local: {local_path}")

        # Google Drive
        if save_to_gdrive:
            try:
                gdrive_path = self.gdrive.root / "universe.json"
                self.gdrive.root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, gdrive_path)
                logger.info(f"  Synced GDrive: {gdrive_path}")
            except Exception as e:
                logger.warning(f"  GDrive sync failed: {e}")

        # ── Summary
        logger.info("=" * 50)
        logger.info(f"Universe Generator เสร็จสิ้น")
        logger.info(
            f"  {len(assets):,} → metadata {len(candidates):,} "
            f"→ ADV {len(symbol_data):,} → final {len(final)}"
        )
        logger.info("  Top 10 by Dollar Volume:")
        for sym, d in ranked[:10]:
            logger.info(
                f"    {sym:6s}  ${d['price']:>8.2f}  "
                f"ADV {d['adv_10d']:>12,}  "
                f"DV ${d['dollar_vol']/1e6:>7.1f}M"
            )
        logger.info("=" * 50)
        return final

    def load_universe(self, prefer_gdrive: bool = False) -> list:
        """
        โหลด universe จาก cache ที่ generate ไว้แล้ว

        Args:
          prefer_gdrive: True = โหลดจาก GDrive ก่อน (VPS ใช้)
                         False = โหลด local ก่อน (Local machine ใช้)

        Returns:
          list[str] — symbols หรือ [] ถ้าไม่มี cache

        Usage (VPS):
          watchlist = mgr.load_universe(prefer_gdrive=True)
          mgr.sync_latest_models(watchlist=watchlist)
        """
        import json

        local_path  = Path(self.cache.root) / "universe.json"
        gdrive_path = self.gdrive.root / "universe.json"

        paths = ([gdrive_path, local_path] if prefer_gdrive
                 else [local_path, gdrive_path])

        for p in paths:
            try:
                if p.exists():
                    with open(p) as f:
                        data = json.load(f)
                    symbols = data.get("symbols", [])
                    tag     = data.get("tag", "?")
                    stats   = data.get("stats", {})
                    logger.info(
                        f"โหลด Universe: {len(symbols)} symbols "
                        f"(tag={tag}, source={p.name})"
                    )
                    logger.info(f"  Stats: {stats}")
                    return symbols
            except Exception as e:
                logger.warning(f"load_universe {p}: {e}")
                continue

        logger.warning("ไม่พบ universe.json — ใช้ watchlist เริ่มต้น")
        return []

    # ------------------------------------------
    # WATCHLIST — สร้างจาก Universe สำหรับ Weekly LSTM Training
    # ------------------------------------------

    def save_watchlist(
        self,
        symbols:        list,
        source:         str  = "universe",
        save_to_gdrive: bool = True,
    ) -> Path:
        """
        บันทึก watchlist.json จาก Universe ที่ generate แล้ว

        File จะถูกใช้โดย run_weekly_pipeline() เพื่อรู้ว่าต้อง train LSTM symbols ไหนบ้าง

        Args:
          symbols:        list[str] — รายชื่อ symbols
          source:         แหล่งที่มา (เช่น "universe", "tiered_scan")
          save_to_gdrive: True = sync ไป Google Drive ด้วย

        Returns:
          Path — path ของ watchlist.json ที่บันทึกแล้ว

        Structure:
          {
            "generated_at": "2026-03-22T...",
            "tag": "22-03-2026",
            "source": "universe",
            "count": 82,
            "symbols": ["SPY", "NVDA", ...]
          }
        """
        import json

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tag":          daily_tag(),
            "source":       source,
            "count":        len(symbols),
            "symbols":      symbols,
        }

        # ── Local cache
        local_path = Path(self.cache.root) / "watchlist.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info(f"[Watchlist] Saved: {local_path} ({len(symbols)} symbols)")

        # ── Google Drive
        if save_to_gdrive:
            try:
                gdrive_path = self.gdrive.root / "watchlist.json"
                self.gdrive.root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, gdrive_path)
                logger.info(f"[Watchlist] Synced GDrive: {gdrive_path}")
            except Exception as e:
                logger.warning(f"[Watchlist] GDrive sync failed: {e}")

        return local_path

    def load_watchlist(self, prefer_gdrive: bool = False) -> list:
        """
        โหลด watchlist จาก watchlist.json ที่ save_watchlist() สร้างไว้

        Args:
          prefer_gdrive: True = โหลดจาก GDrive ก่อน (VPS ใช้)
                         False = โหลด local ก่อน (Local machine ใช้)

        Returns:
          list[str] — symbols หรือ [] ถ้าไม่มี watchlist.json

        Fallback: ถ้าไม่มี watchlist.json → ลองโหลด universe.json แทน
        """
        import json

        local_path  = Path(self.cache.root) / "watchlist.json"
        gdrive_path = self.gdrive.root / "watchlist.json"

        paths = ([gdrive_path, local_path] if prefer_gdrive
                 else [local_path, gdrive_path])

        for p in paths:
            try:
                if p.exists():
                    with open(p) as f:
                        data = json.load(f)
                    symbols = data.get("symbols", [])
                    tag     = data.get("tag", "?")
                    source  = data.get("source", "?")
                    logger.info(
                        f"[Watchlist] โหลด: {len(symbols)} symbols "
                        f"(tag={tag}, source={source}, file={p.name})"
                    )
                    return symbols
            except Exception as e:
                logger.warning(f"[Watchlist] load error {p}: {e}")
                continue

        # Fallback: ลองโหลดจาก universe.json
        logger.info("[Watchlist] ไม่พบ watchlist.json → fallback to universe.json")
        return self.load_universe(prefer_gdrive=prefer_gdrive)

    def backup_journal(self, journal_dir: str = "./journal"):
        """
        copy trade journal ล่าสุดไป Google Drive
        เรียกปลายวันหลัง flatten
        """
        tag       = daily_tag()
        src_dir   = Path(journal_dir)
        dst_dir   = self.gdrive.journal_dir()
        dst_dir.mkdir(parents=True, exist_ok=True)

        for fname in ["trades.csv", "daily_summary.csv", "performance.json"]:
            src = src_dir / fname
            if src.exists():
                stem, ext = fname.rsplit(".", 1)
                dst = dst_dir / f"{stem}_{tag}.{ext}"
                shutil.copy2(src, dst)
                logger.info(f"Journal backup: {fname} → {dst.name}")

    # ------------------------------------------
    # RETENTION CLEANUP
    # ------------------------------------------

    def _cleanup_old_files(self, freq: str):
        """
        ลบไฟล์เก่าตาม retention policy
        daily  → เก็บ retain_daily วัน (default 30)
        weekly → เก็บ retain_weekly สัปดาห์ (default 12)
        """
        now     = datetime.now(timezone.utc)
        cutoff  = (now - timedelta(days=self.retain_daily)
                   if freq == "daily"
                   else now - timedelta(weeks=self.retain_weekly))
        pattern = "*.parquet" if freq in ("daily", "weekly") else "*.pkl"
        base    = (self.gdrive.root / "features" / freq
                   if freq in ("daily", "weekly")
                   else self.gdrive.root / "models")

        count = 0
        for f in base.rglob(pattern):
            tag_match = re.search(r"(\d{2}-\d{2}-\d{4}|WK\d{2}-\d{4})", f.stem)
            if not tag_match:
                continue
            tag_date = parse_tag_date(tag_match.group(1))
            if tag_date and tag_date < cutoff:
                f.unlink()
                count += 1

        if count:
            logger.info(f"Cleanup ({freq}): ลบ {count} files เก่ากว่า {cutoff.date()}")

    def _sync_file(self, src: Path, dst: Path, label: str = ""):
        """copy file จาก src → dst ถ้า src ใหม่กว่า"""
        if not src.exists():
            logger.debug(f"[Sync] {label}: src ไม่มี ({src.name})")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
            shutil.copy2(src, dst)
            logger.info(f"[Sync] {label}: {src.name} → {dst}")
        else:
            logger.debug(f"[Sync] {label}: ทันสมัยแล้ว")


# ============================================================
# INTEGRATION — เพิ่มใน main.py
# ============================================================

MAIN_PY_PATCH = '''
# ── ใน Config class เพิ่ม:
GDRIVE_ROOT  = os.getenv("GDRIVE_ROOT", "/mnt/gdrive/ttp-trading")
LOCAL_CACHE  = "./cache"
DEPLOY_MODE  = os.getenv("DEPLOY_MODE", "vps")   # "local" หรือ "vps"

# ── ใน TradingPipeline.__init__() เพิ่มหลัง ml_analyzer:
from data_pipeline_manager import DataPipelineManager
self.data_mgr = DataPipelineManager(
    mode        = self.cfg.DEPLOY_MODE,
    gdrive_root = self.cfg.GDRIVE_ROOT,
    local_cache = self.cfg.LOCAL_CACHE,
)
# VPS: sync models ตอน startup
if self.cfg.DEPLOY_MODE == "vps":
    self.data_mgr.sync_latest_models(watchlist=self.cfg.ML_WATCHLIST)

# ── ใน flatten_all() เพิ่มบรรทัดสุดท้าย:
self.data_mgr.backup_journal(self.cfg.JOURNAL_DIR)

# ── ใน _daily_retrain_scheduler() แทนที่ daily_retrain_all:
if self.cfg.DEPLOY_MODE == "local":
    # รัน full pipeline บน local
    self.data_mgr.run_daily_pipeline(watchlist=self.cfg.ML_WATCHLIST)
else:
    # VPS: sync model ใหม่จาก Google Drive
    self.data_mgr.sync_latest_models(watchlist=self.cfg.ML_WATCHLIST)
    # เทรน LightGBM fallback บน VPS ถ้าไม่มี model
    self.ml_analyzer.daily_retrain_all(watchlist=self.cfg.ML_WATCHLIST)
'''


# ============================================================
# STANDALONE — รันบน Local เพื่อทดสอบ
# ============================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")

    mode = sys.argv[1] if len(sys.argv) > 1 else "local"
    print(f"{'='*55}")
    print(f"  DATA PIPELINE MANAGER — mode={mode}")
    print(f"{'='*55}")

    WATCHLIST  = ["NVDA", "TSLA"]
    GDRIVE_DIR = "./gdrive_test"   # เปลี่ยนเป็น path Google Drive จริง

    mgr = DataPipelineManager(
        mode        = mode,
        gdrive_root = GDRIVE_DIR,
        local_cache = "./cache_test",
        retain_daily  = 30,
        retain_weekly = 12,
    )

    if mode == "local":
        print("\n[1] รัน daily pipeline (NVDA)...")
        #mgr.run_daily_pipeline(watchlist=["NVDA"])  # uses default days_1m=59
        mgr.run_weekly_pipeline(watchlist=["NVDA"]) 
        print("\n[2] ทดสอบ retention naming:")
        print(f"  daily_tag()  = {daily_tag()}")
        print(f"  weekly_tag() = {weekly_tag()}")

        print("\n[3] ตรวจสอบ files ที่สร้าง:")
        for p in Path(GDRIVE_DIR).rglob("*.*"):
            print(f"  {p.relative_to(GDRIVE_DIR)}")

    elif mode == "vps":
        print("\n[1] Sync models จาก Google Drive...")
        mgr.sync_latest_models(watchlist=WATCHLIST)

        print("\n[2] โหลด features จาก Parquet:")
        for sym in WATCHLIST:
            df = mgr.load_features(sym, freq="daily")
            print(f"  {sym}: {'shape=' + str(df.shape) if df is not None else 'ไม่พบ → จะ compute ใหม่'}")

    #print("\n[Integration patch สำหรับ main.py]")
    #print(MAIN_PY_PATCH)
    #print("\n✅ เสร็จสิ้น")