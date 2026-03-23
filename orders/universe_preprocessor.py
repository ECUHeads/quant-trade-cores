"""
universe_preprocessor.py
========================
Universal Asset Filter — Level 1 Pre-processing

Pivot จาก ttp_preprocessor.py:
  - ลบ ETB (Easy to Borrow) เป็น optional
  - เพิ่ม Gap% filter (≥2%) และ RVOL filter (≥1.5x) สำหรับ In-Play
  - Futures bypass: ถ้า asset_class=FUTURES → skip ทุก filter (เทรดแค่ NQ)

Usage:
  from config import Config
  Config.load_profile("TTP_5K_FLEX")

  pp = UniversePreprocessor.from_config(Config)
  if pp.check_tradeable("NVDA", price=182, prev_close=175, morning_vol=5e6, avg_vol=2e6):
      # proceed
"""

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger("UniverseFilter")


class UniversePreprocessor:
    """
    Level 1: Universe Filter (Universal)

    For EQUITIES:
      1. Price ≥ min_price ($5)
      2. ADV ≥ min_adv (1M shares)
      3. Gap% ≥ min_gap_pct (2%)
      4. Morning RVOL ≥ min_rvol (1.5x)

    For CFD/FUTURES:
      → Bypass all filters (single instrument from Config)
    """

    def __init__(
        self,
        asset_class:    str   = "EQUITIES",
        min_price:      float = 5.0,
        min_adv:        int   = 1_000_000,
        min_gap_pct:    float = 2.0,
        min_morning_rvol: float = 1.5,
        min_dollar_vol: float = 10_000_000.0,
    ):
        self.asset_class     = asset_class
        self.min_price       = min_price
        self.min_adv         = min_adv
        self.min_gap_pct     = min_gap_pct
        self.min_morning_rvol = min_morning_rvol
        self.min_dollar_vol  = min_dollar_vol

    @classmethod
    def from_config(cls, cfg) -> "UniversePreprocessor":
        return cls(
            asset_class    = cfg.ASSET_CLASS,
            min_price      = cfg.MIN_PRICE,
            min_adv        = cfg.MIN_ADV,
            min_gap_pct    = getattr(cfg, "MIN_GAP_PCT", 2.0),
            min_morning_rvol = getattr(cfg, "MIN_MORNING_RVOL", 1.5),
        )

    # ------------------------------------------
    # MAIN: Check if asset is tradeable today
    # ------------------------------------------

    def check_tradeable(
        self,
        symbol:         str,
        price:          float = 0.0,
        prev_close:     float = 0.0,
        morning_vol:    float = 0.0,
        avg_vol:        float = 0.0,
    ) -> bool:
        """
        ตรวจสอบว่า asset นี้ผ่านเกณฑ์วันนี้หรือไม่

        สำหรับ FUTURES/CFD → bypass ทุก filter (return True เสมอ)
        สำหรับ EQUITIES → ตรวจทุกเกณฑ์
        """
        # ── Futures/CFD bypass
        if self.asset_class in ("FUTURES", "CFD"):
            logger.debug(f"[Filter] {symbol} bypass — {self.asset_class}")
            return True

        # ── Price filter
        if price > 0 and price < self.min_price:
            logger.info(f"[Filter] {symbol} price=${price:.2f} < ${self.min_price} → SKIP")
            return False

        # ── Volume filter
        if avg_vol > 0 and avg_vol < self.min_adv:
            logger.info(f"[Filter] {symbol} ADV={avg_vol:,.0f} < {self.min_adv:,} → SKIP")
            return False

        # ── Gap% filter (In-Play)
        if prev_close > 0 and price > 0:
            gap_pct = abs(price - prev_close) / prev_close * 100
            if gap_pct < self.min_gap_pct:
                logger.info(f"[Filter] {symbol} gap={gap_pct:.1f}% < {self.min_gap_pct}% → SKIP")
                return False

        # ── Morning RVOL filter
        if morning_vol > 0 and avg_vol > 0:
            rvol = morning_vol / avg_vol
            if rvol < self.min_morning_rvol:
                logger.info(f"[Filter] {symbol} RVOL={rvol:.2f}x < {self.min_morning_rvol}x → SKIP")
                return False

        return True

    # ------------------------------------------
    # BATCH: Filter DataFrame (pre-market scan)
    # ------------------------------------------

    def batch_filter(
        self,
        df: pd.DataFrame,
        earnings_symbols: list = None,
    ) -> pd.DataFrame:
        """
        Batch filter สำหรับ pre-market universe scan

        Input columns: [symbol, close, high, low, volume]
        Optional: [prev_close, morning_volume, avg_volume]
        """
        if self.asset_class in ("FUTURES", "CFD"):
            return df

        earnings_symbols = earnings_symbols or []
        result = df.copy()

        # Price filter
        if "close" in result.columns:
            result = result[result["close"] >= self.min_price]

        # ADV filter
        if "ADV_10" in result.columns:
            result = result[result["ADV_10"] >= self.min_adv]
        elif "volume" in result.columns:
            result["ADV_10"] = result.groupby("symbol")["volume"].transform(
                lambda x: x.rolling(10, min_periods=1).mean()
            )
            result = result[result["ADV_10"] >= self.min_adv]

        # Earnings filter
        if earnings_symbols and "symbol" in result.columns:
            before = len(result)
            result = result[~result["symbol"].isin(earnings_symbols)]
            logger.info(f"Earnings filter: removed {before - len(result)} symbols")

        return result

    # ------------------------------------------
    # TECHNICAL FEATURES (for batch mode)
    # ------------------------------------------

    @staticmethod
    def add_technical_features(df: pd.DataFrame) -> pd.DataFrame:
        """Add ADV_10, Dollar_Volume, ATR_14"""
        df = df.copy()
        if "volume" in df.columns and "symbol" in df.columns:
            df["ADV_10"] = df.groupby("symbol")["volume"].transform(
                lambda x: x.rolling(10, min_periods=1).mean()
            )
        if "close" in df.columns and "volume" in df.columns:
            df["Dollar_Volume"] = df["close"] * df["volume"]
        return df


# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== UniversePreprocessor Test ===")

    pp = UniversePreprocessor(asset_class="EQUITIES")
    tests = [
        ("NVDA", 182.0, 175.0, 5e6, 2e6, True),   # gap=4%, rvol=2.5
        ("PENNY", 2.0, 1.9, 1e6, 1e6, False),       # price < $5
        ("FLAT", 100.0, 99.5, 1e6, 1e6, False),      # gap=0.5% < 2%
    ]
    for sym, price, prev, mvol, avol, expected in tests:
        ok = pp.check_tradeable(sym, price, prev, mvol, avol)
        status = "✅" if ok == expected else "❌"
        print(f"  {status} {sym}: tradeable={ok} (expected={expected})")

    # Futures bypass
    pp2 = UniversePreprocessor(asset_class="FUTURES")
    assert pp2.check_tradeable("NQ", price=0) is True
    print("  ✅ FUTURES bypass works")
    print("Done")
