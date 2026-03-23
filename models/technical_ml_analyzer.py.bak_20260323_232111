"""
technical_ml_analyzer.py
=========================
ML/DL Technical Analysis — 15m Intraday VWAP Signal Generator

Architecture Pivot:
  เดิม : Label = "ราคาขึ้นใน 15 นาทีข้างหน้า" (scalping)
  ใหม่ : Label = "Pullback-to-VWAP → Close positive EOD" (structural)

  เดิม : Features = 1-min indicators
  ใหม่ : Features += VWAP ratio, ATR_15m, distance-from-VWAP

Classes:
  FeatureEngineer     — 47+ features + VWAP/ATR_15m
  LightGBMModel       — Daily retrain
  LSTMModel           — Weekly retrain, 30-bar sequence (15m bars)
  EnsembleAnalyzer    — รวม output
  TechnicalMLAnalyzer — Main class

Label (Y) — VWAP Pullback Signal:
  Y = 1 ถ้า price ≤ VWAP × 1.005 (pullback zone) AND Close_EOD > VWAP
  Y = 0 otherwise

Training:
  LightGBM → ทุกวัน 8:00 AM ET
  LSTM     → ทุกวันอาทิตย์

Integration: Final Score = 0.4 × regime + 0.6 × ml_score
"""

import os
import math
import time
import logging
import warnings
import json
import joblib
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import roc_auc_score
import lightgbm as lgb

# ── PyTorch (optional — graceful fallback ถ้ายังไม่ติดตั้ง)
try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    warnings.warn(
        "PyTorch ไม่พบ → LSTM disabled, ใช้แค่ LightGBM\n"
        "ติดตั้งด้วย: pip install torch --index-url https://download.pytorch.org/whl/cu121"
    )

warnings.filterwarnings("ignore", category=UserWarning)
logger = logging.getLogger("TechnicalML")

# ============================================================
# CONFIG
# ============================================================

SEQ_LEN          = 30       # bars ย้อนหลังที่ LSTM ใช้ (30 × 15m = 7.5 ชม.)
HORIZON_MIN      = 0        # ★ 0 = predict EOD close (ไม่ใช่ 15 นาทีข้างหน้า)
LGBM_TRAIN_DAYS  = 60       # ดึงข้อมูลกี่วันสำหรับ LightGBM
LSTM_TRAIN_DAYS  = 90       # ดึงข้อมูลกี่วันสำหรับ LSTM
MIN_TRAIN_BARS   = 200      # bars ขั้นต่ำก่อน train
LGBM_WEIGHT      = 0.60
LSTM_WEIGHT      = 0.40
CONFIDENCE_THRESHOLD = 0.55
MODEL_DIR        = "./models"
TIMEFRAME        = "15m"    # ★ ใหม่: ใช้ 15-minute bars
MIN_TRAIN_BARS   = 200

# ============================================================
# DATA MODEL — OUTPUT
# ============================================================

@dataclass
class MLPrediction:
    symbol:          str
    direction_prob:  float = 0.5    # 0.0–1.0 (>0.5 = bullish)
    confidence:      float = 0.0    # 0.0–1.0
    confidence_label:str   = "LOW"  # "HIGH" | "MEDIUM" | "LOW" — human-readable
    expected_move:   float = 0.0    # คาด ±%
    ml_score:        int   = 50     # 0–100 เข้า pipeline
    signal:          str   = "NEUTRAL"  # "LONG" | "SHORT" | "NEUTRAL"
    top_features:    list  = field(default_factory=list)
    lgbm_prob:       float = 0.5
    lstm_prob:       float = 0.5

    # ── Raw model scores (3-class probabilities)
    #    [P(SELL/BEAR), P(NEUTRAL), P(BUY/BULL)]
    #    ใช้สำหรับ debug, dashboard, journal analysis
    lgbm_raw_probs:  list  = field(default_factory=lambda: [0.33, 0.34, 0.33])
    lstm_raw_probs:  list  = field(default_factory=lambda: [0.33, 0.34, 0.33])
    predicted_class: int   = 0      # -1 (sell), 0 (neutral), 1 (buy)
    # ── Combined ensemble 3-class probs (weighted avg of LGBM + LSTM)
    class_probs:     dict  = field(default_factory=lambda: {"sell": 0.33, "neutral": 0.34, "buy": 0.33})

    model_versions:  dict  = field(default_factory=dict)
    timestamp:       str   = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    notes:           str   = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def score_summary(self) -> str:
        """One-line summary สำหรับ log / notification"""
        return (
            f"{self.symbol} | score={self.ml_score} signal={self.signal} "
            f"conf={self.confidence:.2f}({self.confidence_label}) | "
            f"LGBM={self.lgbm_prob:.3f} [B:{self.lgbm_raw_probs[0]:.2f} "
            f"N:{self.lgbm_raw_probs[1]:.2f} U:{self.lgbm_raw_probs[2]:.2f}] | "
            f"LSTM={self.lstm_prob:.3f} [B:{self.lstm_raw_probs[0]:.2f} "
            f"N:{self.lstm_raw_probs[1]:.2f} U:{self.lstm_raw_probs[2]:.2f}]"
        )


# ============================================================
# FEATURE ENGINEER — คำนวณ 45 institutional-grade features
# ============================================================
import pandas as pd
import numpy as np
import math
from datetime import datetime, timedelta, timezone
from sklearn.preprocessing import StandardScaler, MinMaxScaler

class FeatureEngineer:
    """
    Enhanced Feature Engineer (v2.0)
    - Fix: VWAP Intraday Reset (Daily grouping)
    - Add: Micro-Volatility & Z-Score features
    - Add: Feature Scaling for LSTM support
    - Optimization: Full Vectorized Support
    """

    def __init__(self):
        self.scaler = StandardScaler()

    # ── 1. Price Structure (Updated with Daily Reset)
    def _price_features(self, df1: pd.DataFrame, df5: pd.DataFrame) -> dict:
        c, o, h, l, v = df1["close"], df1["open"], df1["high"], df1["low"], df1["volume"]
        
        # VWAP (Reset รายวัน)
        df_tmp = df1.copy()
        df_tmp['date'] = df_tmp.index.date
        typical = (h + l + c) / 3
        cum_tpv = (typical * v).groupby(df_tmp['date']).cumsum()
        cum_vol = v.groupby(df_tmp['date']).cumsum().replace(0, np.nan)
        vwap = cum_tpv / cum_vol
        
        # New Micro-Volatility: Price distance from VWAP normalized by ATR
        atr_tmp = (h - l).rolling(14).mean()
        price_dist_atr = (c.iloc[-1] - vwap.iloc[-1]) / (atr_tmp.iloc[-1] + 1e-9)

        # VWAP Deviation
        vwap_dev = (c.iloc[-1] - vwap.iloc[-1]) / vwap.iloc[-1] * 100
        
        return {
            "vwap_dev_pct": round(float(vwap_dev), 4),
            "price_dist_atr": round(float(price_dist_atr), 4),
            "gap_pct": round(float(((o.iloc[-1] - c.iloc[-2]) / c.iloc[-2] * 100) if len(c) > 1 else 0), 4),
            "dist_from_open_pct": round(float((c.iloc[-1] - o.iloc[0]) / o.iloc[0] * 100), 4),
        }

    # ── 2. Momentum & Volatility (Combined for brevity in review)
    def _volatility_features(self, df1: pd.DataFrame) -> dict:
        c, h, l, v = df1["close"], df1["high"], df1["low"], df1["volume"]
        
        # New: Volume Z-Score (ดูว่า Volume ตอนนี้ผิดปกติแค่ไหนเมื่อเทียบกับอดีต)
        vol_rolling_mean = v.rolling(20).mean()
        vol_rolling_std = v.rolling(20).std()
        vol_zscore = (v.iloc[-1] - vol_rolling_mean.iloc[-1]) / (vol_rolling_std.iloc[-1] + 1e-9)

        # ATR & HV
        returns = c.pct_change().dropna()
        hv20 = returns.rolling(20).std().iloc[-1] * math.sqrt(252 * 390) * 100

        return {
            "vol_zscore": round(float(vol_zscore), 4),
            "hv20_annualized": round(float(np.nan_to_num(hv20)), 4),
            "atr_pct": round(float((h-l).rolling(14).mean().iloc[-1] / c.iloc[-1] * 100), 4),
        }

    # ── 3. VECTORIZED BATCH (The Core Optimization)
    def compute_vectorized(self, df1: pd.DataFrame, df5: pd.DataFrame, 
                           catalyst_type: str = "OTHER", urgency_score: int = 50) -> pd.DataFrame:
        
        # 1. Prepare Data & Strip TZ
        def _strip_tz(df):
            if df is not None and hasattr(df.index, 'tz') and df.index.tz is not None:
                df.index = df.index.tz_convert("UTC").tz_localize(None)
            return df

        df1, df5 = _strip_tz(df1.copy()), _strip_tz(df5.copy())
        c, o, h, l, v = df1["close"].astype(float), df1["open"].astype(float), \
                         df1["high"].astype(float), df1["low"].astype(float), df1["volume"].astype(float)

        # 2. Daily Reset Logic for VWAP (Fixes the "Infinite Accumulation" bug)
        dates = df1.index.date
        typical = (h + l + c) / 3
        vwap = (typical * v).groupby(dates).cumsum() / v.groupby(dates).cumsum().replace(0, np.nan)
        
        # 3. Enhanced Features Calculation
        atr14 = (h - l).rolling(14).mean()
        
        # Micro-Volatility Group
        vwap_dev = (c - vwap) / (vwap + 1e-9) * 100
        price_dist_atr = (c - vwap) / (atr14 + 1e-9)
        vol_zscore = (v - v.rolling(20).mean()) / (v.rolling(20).std() + 1e-9)
        
        # Technicals
        delta = c.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi_14 = 100 - (100 / (1 + (gain / loss.replace(0, np.nan))))
        
        # 4. Multi-Timeframe Alignment
        c5_resampled = df5["close"].reindex(df1.index, method="ffill")
        sma9_5 = df5["close"].rolling(9).mean().reindex(df1.index, method="ffill")
        mtf_align = ((c > c.rolling(9).mean()) == (c5_resampled > sma9_5)).astype(float)

        # 5. Build Result DataFrame
        feat_df = pd.DataFrame(index=df1.index)
        feat_df["vwap_dev_pct"] = vwap_dev
        feat_df["price_dist_atr"] = price_dist_atr
        feat_df["vol_zscore"] = vol_zscore
        feat_df["rsi_14"] = rsi_14.fillna(50)
        feat_df["mtf_trend_align"] = mtf_align
        feat_df["gap_pct"] = (o - c.shift(1)) / (c.shift(1) + 1e-9) * 100
        
        # Context Encoding
        CATALYST_MAP = {"EARNINGS": 5, "FDA": 5, "MA": 4, "OTHER": 0}
        feat_df["catalyst_encoded"] = float(CATALYST_MAP.get(catalyst_type, 0))
        feat_df["urgency_norm"] = float(urgency_score) / 100.0

        return feat_df.ffill().fillna(0)

    # ── 4. FEATURE SCALING (Crucial for LSTM)
    def scale_features(self, df: pd.DataFrame, method='standard') -> pd.DataFrame:
        """
        Scales features to be within a similar range.
        - 'standard': Mean=0, Std=1 (Good for most ML)
        - 'minmax': Range [0, 1] (Good for LSTM/Neural Nets)
        """
        cols = df.columns
        if method == 'standard':
            scaled_data = self.scaler.fit_transform(df)
        else:
            mms = MinMaxScaler()
            scaled_data = mms.fit_transform(df)
            
        return pd.DataFrame(scaled_data, columns=cols, index=df.index)

    # ── 5. Sequence for LSTM (Uses Scaled Data)
    def compute_sequence(self, df1: pd.DataFrame, df5: pd.DataFrame, seq_len: int = 30) -> np.ndarray:
        """
        Returns a (seq_len, n_features) array for LSTM input.
        """
        # 1. Get vectorized features
        feat_df = self.compute_vectorized(df1, df5)
        
        # 2. Scale features (Required for Deep Learning)
        scaled_df = self.scale_features(feat_df, method='minmax')
        
        # 3. Extract last N bars
        if len(scaled_df) < seq_len:
            # Padding if not enough data
            padding = np.zeros((seq_len - len(scaled_df), len(scaled_df.columns)))
            return np.vstack([padding, scaled_df.values])
        
        return scaled_df.values[-seq_len:]
    """
    รับ OHLCV DataFrame (1-min และ 5-min) → คืน feature vector 45 columns

    Features แบ่งเป็น 7 กลุ่ม:
      1. Price Structure  (8 features) — VWAP, anchored VWAP, gaps
      2. Momentum         (8 features) — RSI, MACD, ROC
      3. Volatility       (7 features) — Bollinger, ATR, range
      4. Volume / Flow    (9 features) — RVOL, CVD proxy, Volume Profile
      5. Candle Pattern   (6 features) — body ratio, wicks, engulfing
      6. Multi-Timeframe  (5 features) — 1-min vs 5-min alignment
      7. Market Context   (2 features) — time of day, session encoded
    """

    # ── 1. Price Structure
    def _price_features(self, df1: pd.DataFrame, df5: pd.DataFrame) -> dict:
        c = df1["close"]
        o = df1["open"]
        h = df1["high"]
        l = df1["low"]
        v = df1["volume"]

        # VWAP (รายวัน reset ทุกเช้า)
        typical   = (h + l + c) / 3
        cum_tpv   = (typical * v).cumsum()
        cum_vol   = v.cumsum().replace(0, np.nan)
        vwap      = cum_tpv / cum_vol

        # Anchored VWAP จาก bar แรกของวัน
        anchored_vwap = vwap.iloc[0] if not vwap.empty else c.iloc[-1]

        # VWAP Deviation (ราคาอยู่ห่าง VWAP กี่ %)
        vwap_dev       = (c.iloc[-1] - vwap.iloc[-1]) / vwap.iloc[-1] * 100
        anch_vwap_dev  = (c.iloc[-1] - anchored_vwap) / anchored_vwap * 100

        # VWAP Bands (±1 std)
        vwap_std   = c.rolling(20).std().iloc[-1]
        upper_band = vwap.iloc[-1] + vwap_std
        lower_band = vwap.iloc[-1] - vwap_std
        band_pos   = (c.iloc[-1] - lower_band) / (upper_band - lower_band + 1e-9)

        # Gap from previous close
        if len(df1) > 1:
            prev_close = df1["close"].iloc[-2]
            gap_pct    = (o.iloc[-1] - prev_close) / prev_close * 100
        else:
            gap_pct = 0.0

        # Price vs high/low of last 20 bars
        hi20       = h.rolling(20).max().iloc[-1]
        lo20       = l.rolling(20).min().iloc[-1]
        pos_in_range = (c.iloc[-1] - lo20) / (hi20 - lo20 + 1e-9)

        return {
            "vwap_dev_pct":      round(float(vwap_dev), 4),
            "anchored_vwap_dev": round(float(anch_vwap_dev), 4),
            "vwap_band_pos":     round(float(np.clip(band_pos, 0, 1)), 4),
            "gap_pct":           round(float(gap_pct), 4),
            "pos_in_20bar_range":round(float(pos_in_range), 4),
            "dist_from_open_pct":round(float((c.iloc[-1] - o.iloc[0]) / o.iloc[0] * 100), 4),
            "intraday_high_pct": round(float((h.max() - o.iloc[0]) / o.iloc[0] * 100), 4),
            "intraday_low_pct":  round(float((l.min() - o.iloc[0]) / o.iloc[0] * 100), 4),
        }

    # ── 2. Momentum
    def _momentum_features(self, df1: pd.DataFrame) -> dict:
        c = df1["close"]

        # RSI-14
        delta   = c.diff()
        gain    = delta.where(delta > 0, 0).rolling(14).mean()
        loss    = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs      = gain / loss.replace(0, np.nan)
        rsi     = (100 - 100 / (1 + rs)).iloc[-1]

        # MACD (12, 26, 9)
        ema12   = c.ewm(span=12, adjust=False).mean()
        ema26   = c.ewm(span=26, adjust=False).mean()
        macd    = ema12 - ema26
        signal  = macd.ewm(span=9, adjust=False).mean()
        macd_hist = (macd - signal).iloc[-1]
        macd_val  = macd.iloc[-1]

        # Rate of Change
        roc5    = (c.iloc[-1] / c.iloc[-6] - 1) * 100 if len(c) > 5 else 0
        roc10   = (c.iloc[-1] / c.iloc[-11] - 1) * 100 if len(c) > 10 else 0

        # SMA crossover
        sma9    = c.rolling(9).mean().iloc[-1]
        sma20   = c.rolling(20).mean().iloc[-1]
        sma_cross = (c.iloc[-1] - sma9) / (sma9 + 1e-9) * 100

        # Momentum (close vs close N bars ago)
        mom5    = c.iloc[-1] - c.iloc[-6] if len(c) > 5 else 0
        mom_norm = mom5 / (c.iloc[-6] + 1e-9) * 100 if len(c) > 5 else 0

        return {
            "rsi_14":       round(float(np.nan_to_num(rsi, nan=50)), 4),
            "macd_hist":    round(float(np.nan_to_num(macd_hist)), 6),
            "macd_val":     round(float(np.nan_to_num(macd_val)), 6),
            "roc_5":        round(float(roc5), 4),
            "roc_10":       round(float(roc10), 4),
            "sma_cross_pct":round(float(np.nan_to_num(sma_cross)), 4),
            "price_vs_sma20":round(float((c.iloc[-1] - sma20) / (sma20 + 1e-9) * 100), 4),
            "momentum_5":   round(float(mom_norm), 4),
        }

    # ── 3. Volatility
    def _volatility_features(self, df1: pd.DataFrame) -> dict:
        c = df1["close"]
        h = df1["high"]
        l = df1["low"]

        # Bollinger Bands
        sma20   = c.rolling(20).mean()
        std20   = c.rolling(20).std()
        bb_upper = (sma20 + 2 * std20).iloc[-1]
        bb_lower = (sma20 - 2 * std20).iloc[-1]
        bb_width = (bb_upper - bb_lower) / (sma20.iloc[-1] + 1e-9) * 100
        bb_pct_b = (c.iloc[-1] - bb_lower) / (bb_upper - bb_lower + 1e-9)

        # ATR-14
        hl     = h - l
        hc     = (h - c.shift()).abs()
        lc     = (l - c.shift()).abs()
        tr     = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr14  = tr.rolling(14).mean().iloc[-1]
        atr_pct= atr14 / (c.iloc[-1] + 1e-9) * 100

        # Historical Volatility (std of returns × √252×390 bars/day)
        returns    = c.pct_change().dropna()
        hv20       = returns.rolling(20).std().iloc[-1] * math.sqrt(252 * 390) * 100

        # Intraday range relative to ATR
        today_range= h.max() - l.min()
        range_vs_atr = today_range / (atr14 + 1e-9)

        return {
            "bb_pct_b":      round(float(np.nan_to_num(bb_pct_b)), 4),
            "bb_width_pct":  round(float(np.nan_to_num(bb_width)), 4),
            "atr_pct":       round(float(np.nan_to_num(atr_pct)), 4),
            "hv20_annualized":round(float(np.nan_to_num(hv20)), 4),
            "range_vs_atr":  round(float(np.nan_to_num(range_vs_atr)), 4),
            "close_vs_bb_upper": round(float((c.iloc[-1] - bb_upper) / (bb_upper + 1e-9) * 100), 4),
            "close_vs_bb_lower": round(float((c.iloc[-1] - bb_lower) / (bb_lower + 1e-9) * 100), 4),
        }

    # ── 4. Volume & Institutional Flow
    def _volume_features(self, df1: pd.DataFrame) -> dict:
        v = df1["volume"]
        c = df1["close"]
        o = df1["open"]

        # RVOL (Relative Volume vs 10-bar average)
        adv10   = v.rolling(10).mean().iloc[-1]
        rvol    = v.iloc[-1] / (adv10 + 1e-9)

        # CVD Proxy: ถ้า close > open = buy volume, < open = sell volume
        buy_vol  = np.where(c > o, v, 0)
        sell_vol = np.where(c < o, v, 0)
        cvd      = pd.Series(buy_vol - sell_vol, index=df1.index).cumsum()
        cvd_norm = cvd.iloc[-1] / (v.sum() + 1e-9)       # normalize by total volume

        # CVD trend (momentum ของ CVD เอง)
        cvd_mom  = cvd.diff(5).iloc[-1] / (v.mean() + 1e-9)

        # Volume Profile — Point of Control (ราคาที่ volume มากสุด)
        if len(df1) > 5:
            price_bins = pd.cut(c, bins=20)
            vol_profile= df1.groupby(price_bins, observed=False)["volume"].sum()
            poc_bin    = vol_profile.idxmax()
            if poc_bin is not None and hasattr(poc_bin, "mid"):
                poc_price = float(poc_bin.mid)
            else:
                poc_price = float(c.median())
            dist_from_poc = (c.iloc[-1] - poc_price) / (poc_price + 1e-9) * 100
        else:
            dist_from_poc = 0.0

        # Volume acceleration
        vol_ma5     = v.rolling(5).mean()
        vol_accel   = (v.iloc[-1] / (vol_ma5.iloc[-1] + 1e-9)) - 1

        # Up/Down volume ratio (buy pressure)
        up_vol   = v[c > c.shift()].sum()
        down_vol = v[c < c.shift()].sum()
        udv_ratio= up_vol / (up_vol + down_vol + 1e-9)

        # Cumulative volume vs average day — วันนี้เทียบกับค่าเฉลี่ย
        cum_vol_ratio = v.sum() / (adv10 * len(v) + 1e-9)

        return {
            "rvol":            round(float(np.nan_to_num(rvol)), 4),
            "cvd_norm":        round(float(np.nan_to_num(cvd_norm)), 4),
            "cvd_momentum":    round(float(np.nan_to_num(cvd_mom)), 4),
            "dist_from_poc":   round(float(dist_from_poc), 4),
            "vol_acceleration":round(float(np.nan_to_num(vol_accel)), 4),
            "up_down_vol_ratio":round(float(np.nan_to_num(udv_ratio)), 4),
            "cum_vol_ratio":   round(float(np.nan_to_num(cum_vol_ratio)), 4),
            "vol_vs_sma20":    round(float(v.iloc[-1] / (v.rolling(20).mean().iloc[-1] + 1e-9)), 4),
            "large_bar_vol":   round(float(int(v.iloc[-1] > v.rolling(20).mean().iloc[-1] * 2)), 4),
        }

    # ── 5. Candle Patterns
    def _candle_features(self, df1: pd.DataFrame) -> dict:
        c  = df1["close"]
        o  = df1["open"]
        h  = df1["high"]
        l  = df1["low"]

        body       = (c - o).abs()
        full_range = (h - l).replace(0, np.nan)

        # Body ratio
        body_ratio = (body / full_range).iloc[-1]

        # Upper / Lower wick ratio
        upper_wick = (h - c.where(c > o, o)).iloc[-1] / (full_range.iloc[-1] + 1e-9)
        lower_wick = (c.where(c < o, o) - l).iloc[-1] / (full_range.iloc[-1] + 1e-9)

        # Bullish / Bearish bar
        is_bull    = float(c.iloc[-1] > o.iloc[-1])

        # Engulfing pattern
        if len(df1) >= 2:
            prev_body  = abs(c.iloc[-2] - o.iloc[-2])
            curr_body  = abs(c.iloc[-1] - o.iloc[-1])
            bullish_engulf = float(
                c.iloc[-1] > o.iloc[-1] and       # bullish bar
                c.iloc[-2] < o.iloc[-2] and       # prev bearish
                c.iloc[-1] > o.iloc[-2] and        # current close > prev open
                o.iloc[-1] < c.iloc[-2]            # current open < prev close
            )
            bearish_engulf = float(
                c.iloc[-1] < o.iloc[-1] and
                c.iloc[-2] > o.iloc[-2] and
                c.iloc[-1] < o.iloc[-2] and
                o.iloc[-1] > c.iloc[-2]
            )
        else:
            bullish_engulf = bearish_engulf = 0.0

        # Consecutive bars direction (streak)
        directions = np.sign(c.diff().dropna())
        streak = 0
        for d in reversed(directions.values):
            if d == directions.iloc[-1]:
                streak += 1
            else:
                break

        return {
            "body_ratio":      round(float(np.nan_to_num(body_ratio)), 4),
            "upper_wick_ratio":round(float(np.nan_to_num(upper_wick)), 4),
            "lower_wick_ratio":round(float(np.nan_to_num(lower_wick)), 4),
            "is_bull_bar":     is_bull,
            "bullish_engulf":  bullish_engulf,
            "bearish_engulf":  bearish_engulf,
        }

    # ── 6. Multi-Timeframe Alignment
    def _mtf_features(self, df1: pd.DataFrame, df5: pd.DataFrame) -> dict:
        c1 = df1["close"]
        c5 = df5["close"]

        sma9_1  = c1.rolling(9).mean().iloc[-1]
        sma9_5  = c5.rolling(9).mean().iloc[-1] if len(c5) >= 9 else c5.mean()

        # Trend alignment: ทั้ง 1-min และ 5-min อยู่เหนือ SMA ไหม
        trend_align = float(
            (c1.iloc[-1] > sma9_1) == (c5.iloc[-1] > sma9_5)
        )

        # 1-min RSI vs 5-min RSI (divergence)
        def rsi_last(s):
            d = s.diff()
            g = d.where(d > 0, 0).rolling(14).mean()
            l = (-d.where(d < 0, 0)).rolling(14).mean()
            rs = g / l.replace(0, np.nan)
            return float((100 - 100 / (1 + rs)).iloc[-1])

        rsi1 = rsi_last(c1)
        rsi5 = rsi_last(c5) if len(c5) >= 20 else 50.0
        rsi_div = rsi1 - rsi5

        # Volume trend alignment
        vol1_trend = float(df1["volume"].iloc[-1] > df1["volume"].rolling(5).mean().iloc[-1])
        vol5_trend = float(df5["volume"].iloc[-1] > df5["volume"].rolling(5).mean().iloc[-1]) if len(df5) >= 5 else 0.5

        return {
            "mtf_trend_align": trend_align,
            "rsi_1m":          round(float(np.nan_to_num(rsi1, nan=50)), 4),
            "rsi_5m":          round(float(np.nan_to_num(rsi5, nan=50)), 4),
            "rsi_divergence":  round(float(np.nan_to_num(rsi_div)), 4),
            "vol_trend_align": round(float((vol1_trend + vol5_trend) / 2), 4),
        }

    # ── 7. Market Context
    def _context_features(self) -> dict:
        now_et = datetime.now(timezone.utc) - timedelta(hours=4)
        minutes_since_open = max(0, (now_et.hour * 60 + now_et.minute) - 570)  # 570 = 9:30 AM

        # ช่วงเวลาใน session (0=เปิด, 1=กลาง, 2=ปิด)
        if minutes_since_open < 60:
            session_period = 0.0   # ชั่วโมงแรก — volatile สูง
        elif minutes_since_open < 330:
            session_period = 1.0   # กลางวัน — low vol
        else:
            session_period = 2.0   # ชั่วโมงสุดท้าย — volatile อีกรอบ

        return {
            "minutes_since_open": float(min(minutes_since_open, 390)),
            "session_period":     session_period,
        }

    # ── VECTORIZED BATCH: คำนวณทุก bar พร้อมกัน (แก้ TD #1)
    def compute_vectorized(
        self,
        df1: "pd.DataFrame",
        df5: "pd.DataFrame",
        catalyst_type: str = "OTHER",
        urgency_score: int = 50,
    ) -> "pd.DataFrame":
        """
        คืน DataFrame shape (n_bars, n_features) คำนวณทุก bar พร้อมกัน

        แทน for loop ใน precompute_features_daily:
          for i in range(30, len(df1)):           <- O(n) Python iterations
              fe.compute(df1.iloc[:i], ...)        <- O(n) rolling ซ้ำทุกรอบ
          รวม: O(n²) overhead

        ด้วย vectorized rolling ครั้งเดียว:
          feat_df = fe.compute_vectorized(df1, df5)  <- O(n) C extension ครั้งเดียว

        Performance (300 symbols, 1,000 bars/symbol):
          เดิม loop:       ~300 x 1.0s = ~5 นาที
          vectorized:      ~300 x 0.05s = ~15 วินาที  (20x faster)
        """
        import pandas as pd
        import numpy as np

        # ══════════════════════════════════════════════════════
        # CRITICAL FIX: Normalize timezone ก่อนทำอะไร
        # ป้องกัน "Cannot compare dtypes datetime64[ms] and datetime64[ms, UTC]"
        #
        # ปัญหา: yfinance 15m → tz-aware, daily → tz-naive
        #         Parquet cache → preserves original tz
        #         reindex() ระหว่าง tz-aware/naive → TypeError
        #
        # วิธีแก้: ลบ tz metadata ออกจากทั้ง df1 และ df5
        # ══════════════════════════════════════════════════════
        def _strip_tz(df):
            if df is not None and hasattr(df.index, 'tz') and df.index.tz is not None:
                try:
                    df.index = df.index.tz_localize(None)
                except TypeError:
                    # pandas บาง version ต้องใช้ tz_convert ก่อน
                    df.index = df.index.tz_convert("UTC").tz_localize(None)
            return df

        df1 = _strip_tz(df1)
        df5 = _strip_tz(df5)

        c = df1["close"].astype(float)
        o = df1["open"].astype(float)
        h = df1["high"].astype(float)
        l = df1["low"].astype(float)
        v = df1["volume"].astype(float)

        # 1. Price Structure
        typical   = (h + l + c) / 3
        vwap      = (typical * v).cumsum() / v.cumsum().replace(0, np.nan)
        vwap_std  = c.rolling(20).std()
        vwap_dev  = (c - vwap) / (vwap + 1e-9) * 100
        anchored  = float(vwap.iloc[0]) if not vwap.empty else float(c.iloc[0])
        anch_dev  = (c - anchored) / (anchored + 1e-9) * 100
        ub        = vwap + vwap_std
        lb        = vwap - vwap_std
        vwap_band = (c - lb) / (ub - lb + 1e-9)
        gap_pct   = (o - c.shift(1)) / (c.shift(1) + 1e-9) * 100
        hi20      = h.rolling(20).max()
        lo20      = l.rolling(20).min()
        pos_range = (c - lo20) / (hi20 - lo20 + 1e-9)
        open0     = float(o.iloc[0])
        dist_open = (c - open0) / (open0 + 1e-9) * 100
        intra_hi  = (h.expanding().max() - open0) / (open0 + 1e-9) * 100
        intra_lo  = (l.expanding().min() - open0) / (open0 + 1e-9) * 100

        # 2. Momentum
        delta     = c.diff()
        gain      = delta.where(delta > 0, 0).rolling(14).mean()
        loss_     = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs        = gain / loss_.replace(0, np.nan)
        rsi_14    = 100 - 100 / (1 + rs)
        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd      = ema12 - ema26
        sig       = macd.ewm(span=9, adjust=False).mean()
        macd_hist = macd - sig
        roc_5     = c.pct_change(5) * 100
        roc_10    = c.pct_change(10) * 100
        sma9      = c.rolling(9).mean()
        sma20     = c.rolling(20).mean()
        sma_cross = (c - sma9) / (sma9 + 1e-9) * 100
        vs_sma20  = (c - sma20) / (sma20 + 1e-9) * 100
        mom5      = c.pct_change(5) * 100

        # 3. Volatility
        std20    = c.rolling(20).std()
        bb_up    = sma20 + 2 * std20
        bb_lo    = sma20 - 2 * std20
        bb_pct_b = (c - bb_lo) / (bb_up - bb_lo + 1e-9)
        bb_width = (bb_up - bb_lo) / (sma20 + 1e-9) * 100
        hl_s     = h - l
        hc_s     = (h - c.shift(1)).abs()
        lc_s     = (l - c.shift(1)).abs()
        tr       = pd.concat([hl_s, hc_s, lc_s], axis=1).max(axis=1)
        atr14    = tr.rolling(14).mean()
        atr_pct  = atr14 / (c + 1e-9) * 100
        hv20     = c.pct_change().rolling(20).std() * math.sqrt(252 * 390) * 100
        t_range  = h.expanding().max() - l.expanding().min()
        range_atr  = t_range / (atr14 + 1e-9)
        cvbb_up    = (c - bb_up) / (bb_up + 1e-9) * 100
        cvbb_lo    = (c - bb_lo) / (bb_lo + 1e-9) * 100

        # 4. Volume / Flow
        adv10     = v.rolling(10).mean()
        rvol      = v / (adv10 + 1e-9)
        buy_v     = pd.Series(np.where(c > o, v, 0), index=df1.index)
        sell_v    = pd.Series(np.where(c < o, v, 0), index=df1.index)
        cvd       = (buy_v - sell_v).cumsum()
        cvd_norm  = cvd / (v.expanding().sum() + 1e-9)
        cvd_mom   = cvd.diff(5) / (v.expanding().mean() + 1e-9)
        dist_poc  = (c - c.expanding().median()) / (c.expanding().median() + 1e-9) * 100
        vol_ma5   = v.rolling(5).mean()
        vol_accel = v / (vol_ma5 + 1e-9) - 1
        up_v_cum  = v.where(c > c.shift(1), 0.0).expanding().sum()
        dn_v_cum  = v.where(c < c.shift(1), 0.0).expanding().sum()
        udv       = up_v_cum / (up_v_cum + dn_v_cum + 1e-9)
        cum_vr    = v.expanding().sum() / (adv10 * v.expanding().count() + 1e-9)
        vol_sma20 = v.rolling(20).mean()
        vol_vs20  = v / (vol_sma20 + 1e-9)
        large_bar = (v > vol_sma20 * 2).astype(float)

        # 5. Candle Patterns
        body       = (c - o).abs()
        full_rng   = (h - l).replace(0, np.nan)
        body_ratio = body / full_rng
        c_bull     = c.where(c > o, o)
        c_bear     = c.where(c < o, o)
        uw_ratio   = (h - c_bull) / (full_rng + 1e-9)
        lw_ratio   = (c_bear - l) / (full_rng + 1e-9)
        is_bull    = (c > o).astype(float)
        pc, po     = c.shift(1), o.shift(1)
        bull_eng   = ((c > o) & (pc < po) & (c > po) & (o < pc)).astype(float)
        bear_eng   = ((c < o) & (pc > po) & (c < po) & (o > pc)).astype(float)

        # 6. Multi-Timeframe
        c5          = df5["close"].astype(float)
        v5          = df5["volume"].astype(float)
        sma9_5a     = c5.rolling(9).mean().reindex(df1.index, method="ffill")
        c5a         = c5.reindex(df1.index, method="ffill")
        v5a         = v5.reindex(df1.index, method="ffill")
        mtf_align   = ((c > sma9) == (c5a > sma9_5a)).astype(float)
        d5          = c5.diff()
        g5          = d5.where(d5 > 0, 0).rolling(14).mean()
        l5          = (-d5.where(d5 < 0, 0)).rolling(14).mean()
        rsi_5m      = (100 - 100/(1 + g5/l5.replace(0,np.nan))).reindex(df1.index, method="ffill").fillna(50)
        rsi_div     = rsi_14 - rsi_5m
        v5_ma5a     = v5.rolling(5).mean().reindex(df1.index, method="ffill")
        vol_align   = ((v > vol_ma5).astype(float) + (v5a > v5_ma5a).astype(float).fillna(0.5)) / 2

        # 7. Context + Catalyst (constants during training)
        CATALYST_MAP = {
            "EARNINGS": 5, "FDA": 5, "MA": 4,
            "GUIDANCE_UP": 3, "GUIDANCE_DOWN": 3,
            "ANALYST_UP": 2, "ANALYST_DOWN": 2,
            "DEAL": 1, "SHAREHOLDER": 1, "OTHER": 0,
        }
        idx = df1.index
        feat_df = pd.DataFrame({
            "vwap_dev_pct": vwap_dev,       "anchored_vwap_dev": anch_dev,
            "vwap_band_pos": vwap_band.clip(0,1), "gap_pct": gap_pct,
            "pos_in_20bar_range": pos_range, "dist_from_open_pct": dist_open,
            "intraday_high_pct": intra_hi,  "intraday_low_pct": intra_lo,
            "rsi_14": rsi_14,               "macd_hist": macd_hist,
            "macd_val": macd,               "roc_5": roc_5,
            "roc_10": roc_10,               "sma_cross_pct": sma_cross,
            "price_vs_sma20": vs_sma20,     "momentum_5": mom5,
            "bb_pct_b": bb_pct_b,           "bb_width_pct": bb_width,
            "atr_pct": atr_pct,             "hv20_annualized": hv20,
            "range_vs_atr": range_atr,      "close_vs_bb_upper": cvbb_up,
            "close_vs_bb_lower": cvbb_lo,   "rvol": rvol,
            "cvd_norm": cvd_norm,           "cvd_momentum": cvd_mom,
            "dist_from_poc": dist_poc,      "vol_acceleration": vol_accel,
            "up_down_vol_ratio": udv,       "cum_vol_ratio": cum_vr,
            "vol_vs_sma20": vol_vs20,       "large_bar_vol": large_bar,
            "body_ratio": body_ratio,       "upper_wick_ratio": uw_ratio,
            "lower_wick_ratio": lw_ratio,   "is_bull_bar": is_bull,
            "bullish_engulf": bull_eng,     "bearish_engulf": bear_eng,
            "mtf_trend_align": mtf_align,   "rsi_1m": rsi_14,
            "rsi_5m": rsi_5m,              "rsi_divergence": rsi_div,
            "vol_trend_align": vol_align,
            "minutes_since_open": pd.Series(0.0, index=idx),
            "session_period": pd.Series(0.0, index=idx),
            "catalyst_encoded": float(CATALYST_MAP.get(catalyst_type, 0)),
            "urgency_norm": float(urgency_score) / 100.0,
        }, index=idx)

        return feat_df.ffill().fillna(0)

    # ── MAIN: รวมทุก feature groups
    def compute(self, df1: pd.DataFrame, df5: pd.DataFrame,
                catalyst_type: str = "OTHER", urgency_score: int = 50) -> pd.Series:
        """
        คืน pd.Series ของ features ทั้งหมด (1 row สำหรับ predict)
        """
        feats = {}
        feats.update(self._price_features(df1, df5))
        feats.update(self._momentum_features(df1))
        feats.update(self._volatility_features(df1))
        feats.update(self._volume_features(df1))
        feats.update(self._candle_features(df1))
        feats.update(self._mtf_features(df1, df5))
        feats.update(self._context_features())

        # Catalyst encoding
        CATALYST_MAP = {
            "EARNINGS": 5, "FDA": 5, "MA": 4,
            "GUIDANCE_UP": 3, "GUIDANCE_DOWN": 3,
            "ANALYST_UP": 2, "ANALYST_DOWN": 2,
            "DEAL": 1, "SHAREHOLDER": 1, "OTHER": 0,
        }
        feats["catalyst_encoded"] = float(CATALYST_MAP.get(catalyst_type, 0))
        feats["urgency_norm"]     = float(urgency_score) / 100.0

        return pd.Series(feats, dtype=float)

    def compute_sequence(self, df1: pd.DataFrame, df5: pd.DataFrame,
                         catalyst_type: str = "OTHER", urgency_score: int = 50,
                         seq_len: int = SEQ_LEN) -> np.ndarray:
        """
        คืน array shape (seq_len, n_features) สำหรับ LSTM
        วิ่งผ่าน rolling window ของ seq_len bars ล่าสุด
        """
        sequences = []
        n = len(df1)

        for i in range(max(1, n - seq_len), n + 1):
            slice1 = df1.iloc[max(0, i - 30):i]
            slice5 = df5.iloc[max(0, len(df5) - 30):]
            if len(slice1) < 5:
                continue
            row = self.compute(slice1, slice5, catalyst_type, urgency_score)
            sequences.append(row.values)

        if not sequences:
            return np.zeros((seq_len, 47))

        arr = np.array(sequences)
        # pad ถ้าสั้นกว่า seq_len
        if len(arr) < seq_len:
            pad = np.zeros((seq_len - len(arr), arr.shape[1]))
            arr = np.vstack([pad, arr])
        return arr[-seq_len:]

class LabelGenerator:
    """
    สร้าง ternary label (-1, 0, 1) โดยคำนวณจาก Window (High/Low) ในอนาคต
    เพื่อให้แม่นยำกว่าการดูราคา ณ จุดเดียว (Point-in-time)
    """

    def generate(self, df: pd.DataFrame,
                 horizon: int = 2, # สมมติ 2 bars (ถ้าแท่งละ 5m คือ 10 นาที)
                 up_threshold_pct: float = 0.3,
                 down_threshold_pct: float = 0.3) -> pd.Series:
        
        # 1. หาจุดสูงสุด และ ต่ำสุด ในช่วง N bars ข้างหน้า (Window calculation)
        # เราใช้ .shift(-horizon) เพื่อดึงข้อมูล "อนาคต" กลับมาที่บรรทัดปัจจุบัน
        future_max = df["high"].rolling(window=horizon).max().shift(-horizon)
        future_min = df["low"].rolling(window=horizon).min().shift(-horizon)

        # 2. คำนวณ Max Potential Return และ Min Potential Return
        max_return = (future_max - df["close"]) / df["close"] * 100
        min_return = (future_min - df["close"]) / df["close"] * 100

        # 3. สร้าง Label
        label = pd.Series(0, index=df.index, dtype=int)
        
        # กฎ: ถ้ามีโอกาสแตะเป้าบนก่อน (หรือแรงกว่า) ให้เป็น 1, ถ้าแตะเป้าล่างให้เป็น -1
        # กรณีถึงทั้งคู่ (Volatile) เราจะให้ความสำคัญกับทิศทางที่ "แรงกว่า"
        label[max_return >= up_threshold_pct] = 1
        label[min_return <= -down_threshold_pct] = -1
        
        # ----------------------------------------------------
        # 🐛 FIXED DEBUGGER: เช็คความสมดุลของ Class
        # ----------------------------------------------------
        count_pos = (label == 1).sum()
        count_neg = (label == -1).sum()
        count_neu = (label == 0).sum()
        
        print(f"[DEBUG] Window Horizon: {horizon} bars | Total rows: {len(df)}")
        print(f"[DEBUG] Class Distribution -> SELL(-1): {count_neg} | NEU(0): {count_neu} | BUY(1): {count_pos}")
        
        # เช็ค Imbalance: ถ้า Class ใดน้อยกว่า 10% ของทั้งหมด โมเดลอาจจะ Train ยาก
        total = len(label)
        if count_pos / total < 0.1 or count_neg / total < 0.1:
            print("⚠️ WARNING: Class Imbalance detected! อาจต้องปรับ threshold ลง")
        # ----------------------------------------------------
        
        return label


# ============================================================
# LIGHTGBM MODEL — Daily retrain, tabular features
# ============================================================

class LightGBMModel:
    """
    LightGBM Multi-class Classifier for Directional Trading
    - Optimized for GPU (RTX 3090)
    - Ternary Labels: -1 (Bear), 0 (Neutral), 1 (Bull)
    """

    def __init__(self):
        self.model = None
        self.feature_names = None
        self.trained_at = None
        self.auc_score = 0.0
        self.version = f"lgbm-{datetime.now().strftime('%Y%m%d')}"

        # --- Hyperparameters Optimized for Quant & GPU ---
        self.PARAMS = {
            "objective": "multiclass",
            "num_class": 3,
            "metric": "multi_logloss",
            "learning_rate": 0.03,      # ลดลงเพื่อให้โมเดลเรียนรู้ได้ละเอียดขึ้น
            "num_leaves": 15,           # คุมไม่ให้ซับซ้อนเกินไป (กัน Overfit)
            "max_depth": 4,             # ตื้นหน่อยเพื่อให้ Model Generalize ดีขึ้น
            "feature_fraction": 0.7,    # สุ่ม Feature 70% มาเทรนในแต่ละรอบ
            "lambda_l1": 0.5,           # Regularization กันจำคำตอบ
            "lambda_l2": 0.5,
            "is_unbalance": True,       # ช่วยจัดการกรณีสัญญาณ Buy/Sell มีน้อยกว่า Neutral
            "verbose": -1,
            "n_jobs": -1,
            # --- GPU CONFIG (RTX 3090) ---
            "device": "gpu",
            "gpu_platform_id": 0,
            "gpu_device_id": 0,
        }

    def train(self, X: pd.DataFrame, y: pd.Series) -> float:
        """เทรน Model ด้วย TimeSeriesSplit และวัดผลด้วย Average AUC (Bull & Bear)"""
        
        if len(X) < MIN_TRAIN_BARS:
            logger.warning(f"[LGBM] ข้อมูลน้อยเกินไป ({len(X)} bars)")
            return 0.0

        # 1. Map labels: {-1:0, 0:1, 1:2}
        y_mapped = y.replace({-1: 0, 0: 1, 1: 2})
        self.feature_names = X.columns.tolist()

        # 2. Setup TimeSeriesSplit (3-5 splits กำลังดี)
        tscv = TimeSeriesSplit(n_splits=3)
        aucs = []
        best_model = None
        max_auc = 0.0

        for train_idx, val_idx in tscv.split(X):
            X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_tr, y_val = y_mapped.iloc[train_idx], y_mapped.iloc[val_idx]

            # เช็คว่ามี Class ครบไหมก่อนเทรน
            if y_tr.nunique() < 3 or y_val.nunique() < 3:
                continue

            dtrain = lgb.Dataset(X_tr, label=y_tr)
            dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)

            # 3. Train with Early Stopping
            tmp_model = lgb.train(
                self.PARAMS,
                dtrain,
                num_boost_round=500,
                valid_sets=[dval],
                callbacks=[
                    lgb.early_stopping(stopping_rounds=30, verbose=False),
                    lgb.log_evaluation(-1)
                ]
            )

            # 4. วัดผลแบบ Dual-AUC (เฉลี่ยความเก่งทั้งขาขึ้นและขาลง)
            preds_prob = tmp_model.predict(X_val) # shape (n, 3)
            
            auc_bull = roc_auc_score(y_val == 2, preds_prob[:, 2])
            auc_bear = roc_auc_score(y_val == 0, preds_prob[:, 0])
            avg_auc = (auc_bull + auc_bear) / 2
            
            aucs.append(avg_auc)
            
            # เก็บโมเดลรอบที่เก่งที่สุดไว้
            if avg_auc > max_auc:
                max_auc = avg_auc
                best_model = tmp_model

        if not best_model:
            return 0.0

        self.model = best_model
        self.auc_score = round(float(np.mean(aucs)), 4)
        self.trained_at = datetime.now(timezone.utc).isoformat()
        
        logger.info(f"[LGBM] Train Success | Avg AUC={self.auc_score:.4f} | Final Fold AUC={max_auc:.4f}")
        return self.auc_score

    def predict(self, X: pd.DataFrame) -> float:
        """คืนค่า Bullishness Probability (0-1) โดย 0.5 คือ Neutral"""
        if self.model is None:
            return 0.5
        
        # รองรับทั้ง Series (1 row) และ DataFrame
        if isinstance(X, pd.Series):
            X = X.values.reshape(1, -1)
            
        preds = self.model.predict(X) 
        prob_bear = preds[0, 0] # Class 0
        prob_bull = preds[0, 2] # Class 2

        # Scaled Probability: (Bull - Bear + 1) / 2
        direction_score = (prob_bull - prob_bear + 1) / 2
        return float(np.clip(direction_score, 0.0, 1.0))

    def predict_raw_probs(self, X: pd.DataFrame) -> list:
        """
        คืน raw 3-class probabilities: [P(BEAR), P(NEUTRAL), P(BULL)]
        สำหรับ dashboard / journal / debug

        Returns:
          list[float] — [p_bear, p_neutral, p_bull] (sum ≈ 1.0)
        """
        if self.model is None:
            return [0.33, 0.34, 0.33]

        if isinstance(X, pd.Series):
            X = X.values.reshape(1, -1)

        preds = self.model.predict(X)  # shape (1, 3)
        return [round(float(preds[0, i]), 4) for i in range(3)]

    def feature_importance(self, top_n: int = 10):
        if not self.model: return []
        imp = self.model.feature_importance(importance_type='gain')
        ranked = sorted(zip(self.feature_names, imp), key=lambda x: x[1], reverse=True)
        return ranked[:top_n]


# ============================================================
# LSTM MODEL — Weekly retrain, sequence model, GPU-ready
# ============================================================

class LSTMModel:
    """
    LSTM 3-Class Classifier (ตรงกับ LightGBM multiclass)

    Classes:  0 = sell (label -1)
              1 = neutral (label 0)
              2 = buy (label +1)

    Architecture:
      LSTM(n_layers) → LayerNorm → GELU → Linear(32) → Linear(3)
      Loss:     CrossEntropyLoss (รับ raw logits + int64 labels 0/1/2)
      Predict:  softmax → P(buy) = class 2

    ทำไมเปลี่ยนจาก Binary:
      - LabelGenerator สร้าง -1/0/1 (3 class)
      - LightGBM ใช้ multiclass num_class=3 แล้ว
      - BCELoss รับค่า 0-1 เท่านั้น → ค่า 2 ทำให้ CUDA assert!
      - 3-class ให้ข้อมูลมากกว่า: แยก sell/neutral/buy ชัดเจน
    """

    N_CLASSES = 3

    def __init__(self, input_size: int = 47, hidden_size: int = 64,
                 num_layers: int = 2, dropout: float = 0.3):
        self.input_size  = input_size
        self.hidden_size = hidden_size
        self.num_layers  = num_layers
        self.dropout     = dropout
        self.model       = None
        self.scaler      = RobustScaler()
        self.trained_at  = None
        self.version     = ""
        self.device      = None

        if TORCH_AVAILABLE:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            logger.info(f"[LSTM] Device: {self.device}")

    def _build_model(self):
        """สร้าง LSTM architecture — 3-class output"""
        if not TORCH_AVAILABLE:
            return None

        class LSTMNet(nn.Module):
            def __init__(self, input_size, hidden_size, num_layers, dropout, n_classes):
                super().__init__()
                self.lstm = nn.LSTM(
                    input_size  = input_size,
                    hidden_size = hidden_size,
                    num_layers  = num_layers,
                    dropout     = dropout if num_layers > 1 else 0.0,
                    batch_first = True,
                )
                self.norm    = nn.LayerNorm(hidden_size)
                self.dropout = nn.Dropout(dropout)
                self.fc1     = nn.Linear(hidden_size, 32)
                self.fc2     = nn.Linear(32, n_classes)   # 3 outputs (sell/neutral/buy)
                self.act     = nn.GELU()

            def forward(self, x):
                out, _ = self.lstm(x)
                out    = self.norm(out[:, -1, :])
                out    = self.dropout(out)
                out    = self.act(self.fc1(out))
                return self.fc2(out)    # raw logits — ไม่มี sigmoid/softmax
                # CrossEntropyLoss ทำ log_softmax ในตัว
                # ตอน predict ค่อย softmax เอง

        return LSTMNet(self.input_size, self.hidden_size,
                       self.num_layers, self.dropout,
                       self.N_CLASSES).to(self.device)

    def train(self, X_seq: np.ndarray, y: np.ndarray,
              epochs: int = 30, batch_size: int = 64,
              lr: float = 1e-3) -> float:
        """
        เทรน LSTM 3-class

        Args:
          X_seq: shape (n_samples, seq_len, n_features)
          y:     shape (n_samples,) int64 labels {0, 1, 2}
                 0=sell(-1), 1=neutral(0), 2=buy(+1)

        Returns:
          AUC (macro OVR) หรือ 0.0 ถ้า fail
        """
        if not TORCH_AVAILABLE:
            logger.warning("[LSTM] PyTorch ไม่พบ → ข้ามการเทรน LSTM")
            return 0.0

        if len(X_seq) < MIN_TRAIN_BARS:
            logger.warning(f"[LSTM] ข้อมูลน้อยเกินไป ({len(X_seq)} samples)")
            return 0.0

        # ── Validate labels
        y = np.asarray(y).astype(np.int64)
        unique = np.unique(y)
        counts = np.bincount(y, minlength=3)
        logger.info(f"[LSTM] Labels: unique={unique} | sell={counts[0]} neutral={counts[1]} buy={counts[2]}")

        if not all(l in [0, 1, 2] for l in unique):
            logger.error(f"[LSTM] Invalid labels: {unique} — expected {{0,1,2}}")
            return 0.0

        # ── Normalize features
        n, s, f = X_seq.shape
        X_flat   = X_seq.reshape(-1, f)
        X_scaled = self.scaler.fit_transform(X_flat).reshape(n, s, f)
        X_scaled = np.nan_to_num(X_scaled, nan=0.0, posinf=1.0, neginf=-1.0)

        # ── Train/Val split (80/20, time-ordered)
        split = int(n * 0.8)
        X_tr, X_val = X_scaled[:split], X_scaled[split:]
        y_tr, y_val = y[:split],        y[split:]

        # ── Tensors: X=Float, y=Long (CrossEntropyLoss ต้องการ int64)
        X_tr_t  = torch.FloatTensor(X_tr).to(self.device)
        y_tr_t  = torch.LongTensor(y_tr).to(self.device)
        X_val_t = torch.FloatTensor(X_val).to(self.device)
        y_val_t = torch.LongTensor(y_val).to(self.device)

        dataset = TensorDataset(X_tr_t, y_tr_t)
        loader  = DataLoader(dataset, batch_size=batch_size, shuffle=False)

        self.model = self._build_model()
        optimizer  = torch.optim.AdamW(self.model.parameters(), lr=lr, weight_decay=1e-4)
        scheduler  = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
        criterion  = nn.CrossEntropyLoss()   # รับ raw logits + int64 labels

        best_val_loss = float("inf")
        best_state    = None

        for epoch in range(epochs):
            self.model.train()
            for X_b, y_b in loader:
                optimizer.zero_grad()
                logits = self.model(X_b)          # shape: (batch, 3)
                loss   = criterion(logits, y_b)   # y_b: int64 {0,1,2}
                loss.backward()
                nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                optimizer.step()
            scheduler.step()

            # ── Validation
            self.model.eval()
            with torch.no_grad():
                val_logits = self.model(X_val_t)
                val_loss   = criterion(val_logits, y_val_t).item()

            if val_loss < best_val_loss:
                best_val_loss = val_loss
                best_state = {k: v.clone() for k, v in self.model.state_dict().items()}

        if best_state:
            self.model.load_state_dict(best_state)

        # ── AUC (multi-class One-vs-Rest)
        self.model.eval()
        with torch.no_grad():
            val_logits = self.model(X_val_t)
            val_probs  = torch.softmax(val_logits, dim=1).cpu().numpy()

        try:
            if len(np.unique(y_val)) >= 2:
                auc = roc_auc_score(y_val, val_probs, multi_class='ovr', average='macro')
            else:
                auc = 0.5
        except Exception:
            auc = 0.5

        self.trained_at = datetime.now(timezone.utc).isoformat()
        self.version    = f"lstm-3c-{datetime.now().strftime('%Y%m%d')}"
        logger.info(f"[LSTM] เทรนเสร็จ | AUC={auc:.4f} | 3-class | device={self.device}")
        return float(auc)

    def predict(self, X_seq: np.ndarray) -> float:
        """
        คืน P(buy) — probability ของ class 2

        X_seq: shape (seq_len, n_features) — 1 sample
        Returns: 0.0–1.0 (ยิ่งสูง = ยิ่งมั่นใจว่า buy)
        """
        if not TORCH_AVAILABLE or self.model is None:
            return 0.5

        try:
            n, f     = X_seq.shape
            X_flat   = X_seq.reshape(-1, f)
            X_scaled = self.scaler.transform(X_flat).reshape(1, n, f)
            X_scaled = np.nan_to_num(X_scaled, nan=0.0, posinf=1.0, neginf=-1.0)
            X_t      = torch.FloatTensor(X_scaled).to(self.device)

            self.model.eval()
            with torch.no_grad():
                logits = self.model(X_t)                      # (1, 3)
                probs  = torch.softmax(logits, dim=1)         # (1, 3)
                p_buy  = probs[0, 2].cpu().item()             # P(buy)
            return float(np.clip(p_buy, 0.0, 1.0))
        except Exception as e:
            logger.warning(f"[LSTM] predict error: {e}")
            return 0.5

    def predict_proba(self, X_seq: np.ndarray) -> np.ndarray:
        """
        คืน probabilities ทั้ง 3 class: [P(sell), P(neutral), P(buy)]

        X_seq: shape (seq_len, n_features) — 1 sample
        """
        if not TORCH_AVAILABLE or self.model is None:
            return np.array([0.33, 0.34, 0.33])

        try:
            n, f     = X_seq.shape
            X_flat   = X_seq.reshape(-1, f)
            X_scaled = self.scaler.transform(X_flat).reshape(1, n, f)
            X_scaled = np.nan_to_num(X_scaled, nan=0.0, posinf=1.0, neginf=-1.0)
            X_t      = torch.FloatTensor(X_scaled).to(self.device)

            self.model.eval()
            with torch.no_grad():
                logits = self.model(X_t)
                probs  = torch.softmax(logits, dim=1).cpu().numpy()
            return probs[0]   # [P(sell), P(neutral), P(buy)]
        except Exception:
            return np.array([0.33, 0.34, 0.33])

# ============================================================
# MODEL REGISTRY — save/load model รายหุ้น
# ============================================================

class ModelRegistry:
    """
    บันทึกและโหลด model (LightGBM + LSTM) รายหุ้น
    Directory structure:
      ./models/
        NVDA/
          lgbm_20260317.pkl
          lstm_20260317.pt
          meta.json
        TSLA/
          ...
    """

    def __init__(self, model_dir: str = MODEL_DIR):
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)

    def save_lgbm(self, symbol: str, model: LightGBMModel):
        sym_dir = self.model_dir / symbol
        sym_dir.mkdir(exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        path     = sym_dir / f"lgbm_{date_str}.pkl"
        joblib.dump(model, path)

        # เก็บ meta
        meta = self._load_meta(symbol)
        meta["lgbm_version"]    = model.version
        meta["lgbm_auc"]        = model.auc_score
        meta["lgbm_trained_at"] = model.trained_at
        meta["lgbm_path"]       = str(path)
        self._save_meta(symbol, meta)
        logger.info(f"[Registry] Saved LGBM {symbol} → {path.name}")

    def load_lgbm(self, symbol: str) -> Optional[LightGBMModel]:
        meta = self._load_meta(symbol)
        path = meta.get("lgbm_path")
        if path and Path(path).exists():
            model = joblib.load(path)
            logger.info(f"[Registry] Loaded LGBM {symbol} | AUC={model.auc_score}")
            return model
        return None

    def save_lstm(self, symbol: str, model: LSTMModel):
        if not TORCH_AVAILABLE or model.model is None:
            return
        sym_dir  = self.model_dir / symbol
        sym_dir.mkdir(exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        pt_path  = sym_dir / f"lstm_{date_str}.pt"
        sc_path  = sym_dir / f"lstm_scaler_{date_str}.pkl"

        torch.save(model.model.state_dict(), pt_path)
        joblib.dump(model.scaler, sc_path)

        meta = self._load_meta(symbol)
        meta["lstm_version"]    = model.version
        meta["lstm_trained_at"] = model.trained_at
        meta["lstm_pt_path"]    = str(pt_path)
        meta["lstm_sc_path"]    = str(sc_path)
        self._save_meta(symbol, meta)
        logger.info(f"[Registry] Saved LSTM {symbol} → {pt_path.name}")

    def load_lstm(self, symbol: str, input_size: int = 47) -> Optional[LSTMModel]:
        if not TORCH_AVAILABLE:
            return None
        meta    = self._load_meta(symbol)
        pt_path = meta.get("lstm_pt_path")
        sc_path = meta.get("lstm_sc_path")
        if pt_path and Path(pt_path).exists():
            wrapper = LSTMModel(input_size=input_size)
            wrapper.model = wrapper._build_model()
            try:
                wrapper.model.load_state_dict(
                    torch.load(pt_path, map_location=wrapper.device))
            except RuntimeError as e:
                # Old binary model (fc2=Linear(32,1)) → incompatible กับ 3-class ใหม่
                logger.warning(
                    f"[Registry] LSTM {symbol}: incompatible model "
                    f"(old binary?) → need retrain. {e}"
                )
                Path(pt_path).unlink(missing_ok=True)
                return None
            if sc_path and Path(sc_path).exists():
                wrapper.scaler = joblib.load(sc_path)
            wrapper.version    = meta.get("lstm_version", "")
            wrapper.trained_at = meta.get("lstm_trained_at", "")
            logger.info(f"[Registry] Loaded LSTM 3-class {symbol}")
            return wrapper
        return None

    def needs_daily_retrain(self, symbol: str) -> bool:
        """เช็คว่า LGBM ของวันนี้มีแล้วไหม"""
        meta = self._load_meta(symbol)
        trained = meta.get("lgbm_trained_at", "")
        if not trained:
            return True
        try:
            last = datetime.fromisoformat(trained)
            return last.date() < datetime.now(timezone.utc).date()
        except Exception:
            return True

    def needs_weekly_retrain(self, symbol: str) -> bool:
        """เช็คว่า LSTM ของสัปดาห์นี้มีแล้วไหม"""
        meta = self._load_meta(symbol)
        trained = meta.get("lstm_trained_at", "")
        if not trained:
            return True
        try:
            last = datetime.fromisoformat(trained)
            return (datetime.now(timezone.utc) - last).days >= 7
        except Exception:
            return True

    def _load_meta(self, symbol: str) -> dict:
        path = self.model_dir / symbol / "meta.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_meta(self, symbol: str, meta: dict):
        sym_dir = self.model_dir / symbol
        sym_dir.mkdir(exist_ok=True)
        with open(sym_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

# ============================================================
# ENSEMBLE ANALYZER — รวม LGBM + LSTM
# ============================================================

class EnsembleAnalyzer:
    """
    รวม output ของ LightGBM และ LSTM แบบ weighted average
    ปรับ weight ตาม market session และ model AUC
    """

    def combine(self, lgbm_prob: float, lstm_prob: float,
                lgbm_auc: float = 0.0,
                session: str = "MARKET") -> tuple[float, float]:
        """
        คืน (combined_prob, confidence)

        น้ำหนัก dynamic:
          - ถ้า LSTM ไม่พร้อม (prob = 0.5) → ใช้ LGBM 100%
          - ถ้า session = PRE_MARKET → LGBM น้ำหนักมากกว่า
          - ถ้า LGBM AUC ต่ำ → ลด weight
        """
        w_lgbm = LGBM_WEIGHT
        w_lstm  = LSTM_WEIGHT

        # Pre-market: LSTM ไม่มีข้อมูลเพียงพอ ให้ LGBM เป็นหลัก
        if session == "PRE_MARKET":
            w_lgbm, w_lstm = 0.80, 0.20

        # ถ้า LSTM ยังไม่เทรน (prob = 0.5 ± 0.02)
        if abs(lstm_prob - 0.5) < 0.02:
            w_lgbm, w_lstm = 1.0, 0.0

        # ปรับตาม LGBM AUC quality
        if lgbm_auc > 0 and lgbm_auc < 0.55:
            w_lgbm *= 0.7    # model ไม่ดี ลด trust

        total   = w_lgbm + w_lstm
        w_lgbm /= total
        w_lstm  /= total

        combined = w_lgbm * lgbm_prob + w_lstm * lstm_prob

        # Confidence = ระยะห่างจาก 0.5 (ยิ่งไกล ยิ่งมั่นใจ)
        confidence = min(1.0, abs(combined - 0.5) * 3.5)

        return round(float(combined), 4), round(float(confidence), 4)

    def to_ml_score(self, direction_prob: float, confidence: float,
                    urgency: int = 50) -> int:
        """
        แปลง probability + confidence → ml_score (0–100)
        สำหรับเป็น gate ใน pipeline

        สูตร:
          base   = |prob - 0.5| × 2 × 100   (0–100)
          weight = confidence × urgency_factor
          final  = base × weight
        """
        urgency_factor = 0.5 + (urgency / 100) * 0.5   # 0.5–1.0
        base   = abs(direction_prob - 0.5) * 200        # 0–100
        score  = base * confidence * urgency_factor

        # ถ้า confidence ต่ำ ลด score
        if confidence < CONFIDENCE_THRESHOLD:
            score *= 0.7

        return int(np.clip(score, 0, 100))

    def to_expected_move(self, direction_prob: float, atr_pct: float) -> float:
        """คาด % move ใน horizon โดยใช้ probability × ATR"""
        direction = 1.0 if direction_prob > 0.5 else -1.0
        magnitude = abs(direction_prob - 0.5) * 2    # 0–1 scaling
        return round(direction * magnitude * atr_pct * 3, 2)
    def combine_3class(
        self,
        lgbm_probs: np.ndarray,   # [P(sell), P(neutral), P(buy)]
        lstm_probs: np.ndarray,   # [P(sell), P(neutral), P(buy)]
        lgbm_auc: float = 0.0,
        session: str = "MARKET",
    ) -> tuple[np.ndarray, int, float]:
        """
        รวม 3-class probabilities จาก LGBM + LSTM

        Returns:
            combined_probs: [P(sell), P(neutral), P(buy)]
            predicted_class: -1 / 0 / 1
            confidence: 0.0–1.0
        """
        w_lgbm = LGBM_WEIGHT
        w_lstm = LSTM_WEIGHT

        if session == "PRE_MARKET":
            w_lgbm, w_lstm = 0.80, 0.20
						

        # ถ้า LSTM ยังไม่เทรน (uniform distribution ± 0.05)
        if np.max(lstm_probs) - np.min(lstm_probs) < 0.05:
            w_lgbm, w_lstm = 1.0, 0.0

        if lgbm_auc > 0 and lgbm_auc < 0.55:
            w_lgbm *= 0.7

        total = w_lgbm + w_lstm
        w_lgbm /= total
        w_lstm /= total

        combined = w_lgbm * lgbm_probs + w_lstm * lstm_probs
        # normalize to sum=1
        combined = combined / (combined.sum() + 1e-9)

        # predicted class: argmax → map {0:-1, 1:0, 2:1}
        class_map = {0: -1, 1: 0, 2: 1}
        predicted_class = class_map[int(np.argmax(combined))]

        # confidence: ยิ่ง dominant class สูง ยิ่งมั่นใจ
        # max_prob=0.34 → conf=0.0 (ไม่มั่นใจ), max_prob=0.80 → conf=0.92
        max_prob = float(np.max(combined))
        confidence = min(1.0, max(0.0, (max_prob - 0.34) / 0.66))  # normalize 0.34→1.0 to 0→1

        return combined, predicted_class, round(confidence, 4)

    @staticmethod
    def to_confidence_label(confidence: float) -> str:
        """
        แปลง confidence (0.0–1.0) → human-readable label

        Thresholds:
          >= 0.70 → HIGH    (strong conviction, full size)
          >= 0.45 → MEDIUM  (moderate conviction, consider reducing)
          <  0.45 → LOW     (weak signal, likely NEUTRAL or skip)
        """
        if confidence >= 0.70:
            return "HIGH"
        elif confidence >= 0.45:
            return "MEDIUM"
        else:
            return "LOW"

# ============================================================
# LRU MODEL CACHE — ป้องกัน OOM บน VPS
# ============================================================

class LRUModelCache:
    """
    LRU (Least Recently Used) cache สำหรับ ML models

    ปัญหาเดิม: plain dict ไม่มี size limit
      ข่าวออก 200 symbols คืนเดียว → โหลด 200 models ค้างใน RAM
      LightGBM 1.77MB + LSTM 0.34MB = ~2.1MB/symbol
      200 symbols × 2.1MB = 420MB (ยังโอเค แต่ unbounded = risk)
      spike ถึง 500 symbols → ~1GB → OOM บน VPS 4GB ได้

    วิธีแก้: OrderedDict + max_size eviction
      - get(key)  → hit: move to end (recently used), คืน model
                  → miss: คืน None
      - put(key, model) → insert at end
                        → ถ้า len > max_size → pop oldest (leftmost = LRU)
      - O(1) สำหรับทั้ง get และ put (OrderedDict ใช้ doubly-linked list)

    RAM estimates:
      max_size=50:  ~106 MB  ← แนะนำสำหรับ VPS 4GB
      max_size=100: ~213 MB  ← ใช้ได้ถ้า VPS ≥ 8GB
      max_size=200: ~425 MB  ← production ขนาดใหญ่

    Usage:
      cache = LRUModelCache(max_size=50)
      cache.put("NVDA", lgbm_model)
      model = cache.get("NVDA")   # None ถ้า evicted
      cache.stats()               # {"size": 49, "max": 50, "hits": 120, "misses": 30}
    """

    def __init__(self, max_size: int = 50):
        from collections import OrderedDict
        self._cache: "OrderedDict" = OrderedDict()
        self.max_size  = max_size
        self._hits     = 0
        self._misses   = 0
        self._evictions= 0

    def get(self, key: str):
        """
        คืน model ถ้ามีใน cache และ mark as recently used
        คืน None ถ้าไม่มี (cache miss → caller ต้อง load จาก disk)
        """
        if key in self._cache:
            self._cache.move_to_end(key)   # mark recently used
            self._hits += 1
            return self._cache[key]
        self._misses += 1
        return None

    def put(self, key: str, model) -> bool:
        """
        เพิ่ม/อัปเดต model ใน cache
        ถ้าเต็ม (len > max_size) → evict LRU entry (oldest)

        Returns True ถ้ามีการ evict
        """
        evicted = False
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self.max_size:
                lru_key, _ = self._cache.popitem(last=False)  # pop oldest
                self._evictions += 1
                evicted = True
                logger.debug(
                    f"[LRUCache] evict '{lru_key}' "
                    f"(cache={len(self._cache)+1}/{self.max_size})"
                )
        self._cache[key] = model
        return evicted

    def invalidate(self, key: str) -> bool:
        """ลบ model ออกจาก cache (เรียกหลัง retrain)"""
        if key in self._cache:
            del self._cache[key]
            logger.debug(f"[LRUCache] invalidated '{key}'")
            return True
        return False

    def clear(self):
        """ล้าง cache ทั้งหมด (เรียกตอน shutdown หรือ memory pressure)"""
        n = len(self._cache)
        self._cache.clear()
        logger.info(f"[LRUCache] cleared {n} models")

    def __contains__(self, key: str) -> bool:
        return key in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def stats(self) -> dict:
        """คืน cache statistics สำหรับ monitoring"""
        total = self._hits + self._misses
        hit_rate = self._hits / total * 100 if total > 0 else 0
        return {
            "size":       len(self._cache),
            "max_size":   self.max_size,
            "hits":       self._hits,
            "misses":     self._misses,
            "evictions":  self._evictions,
            "hit_rate_pct": round(hit_rate, 1),
        }


# ============================================================
# MAIN CLASS — TechnicalMLAnalyzer
# ============================================================

class TechnicalMLAnalyzer:
    """
    Gate ใหม่ใน pipeline: ระหว่าง RegimeWeightedScorer และ TTPRiskManager

    วิธีใช้งานใน main.py:
        analyzer = TechnicalMLAnalyzer()

        # ตอนเช้าก่อนตลาดเปิด (8:00 AM ET)
        analyzer.daily_retrain_all(watchlist=["NVDA","TSLA","META"])

        # ทุกวันอาทิตย์
        analyzer.weekly_retrain_all(watchlist=["NVDA","TSLA","META"])

        # ตอน news เข้า (ใน process_news)
        prediction = analyzer.analyze(
            symbol        = "NVDA",
            df1           = df_1min,   # DataFrame 1-min OHLCV
            df5           = df_5min,   # DataFrame 5-min OHLCV
            catalyst_type = "EARNINGS",
            urgency_score = 80,
            session       = "PRE_MARKET",
        )

        if prediction.ml_score < 50:
            return  # ข้ามตัวนี้

        # บันทึกลง journal
        journal.open_trade(..., ml_score=prediction.ml_score,
                               ml_direction_prob=prediction.direction_prob,
                               ml_confidence=prediction.confidence,
                               ml_top_features=prediction.top_features)
    """

    def __init__(self, model_dir: str = MODEL_DIR):
        self.registry  = ModelRegistry(model_dir)
        self.feat_eng  = FeatureEngineer()
        self.label_gen = LabelGenerator()
        self.ensemble  = EnsembleAnalyzer()

        # ── LRU Model Cache (จำกัด RAM บน VPS)
        # ปัญหาเดิม: plain dict ไม่มี limit → โหลดข่าว 200 symbols คืนเดียว
        #             dict ค้างโมเดลทั้งหมดใน RAM → OOM บน VPS 4GB
        #
        # แก้: LRUModelCache ใช้ OrderedDict
        #   - hit  → move_to_end() (mark as recently used)
        #   - miss → load + insert → ถ้าเกิน max → pop oldest (LRU eviction)
        #   - max_size=50: ~106 MB (LGBM 1.77 + LSTM 0.34 MB/symbol)
        #     เพิ่มเป็น 100 ได้ถ้า VPS RAM ≥ 8GB
        self._lgbm_cache = LRUModelCache(max_size=50)
        self._lstm_cache = LRUModelCache(max_size=50)
        self._lock = threading.Lock()

    # ------------------------------------------
    # PUBLIC: analyze — เรียกใน process_news()
    # ------------------------------------------

    def analyze(self, symbol: str,
                df1: pd.DataFrame, df5: pd.DataFrame,
                catalyst_type: str = "OTHER",
                urgency_score: int = 50,
                session: str = "MARKET") -> MLPrediction:
        """
        รับ OHLCV → คืน MLPrediction พร้อมทุก field

        ใหม่: เพิ่ม raw scores + confidence label
          - lgbm_raw_probs: [P(BEAR), P(NEUTRAL), P(BULL)] จาก LightGBM
          - lstm_raw_probs:  [P(BEAR), P(NEUTRAL), P(BULL)] จาก LSTM
          - confidence_label: "HIGH" / "MEDIUM" / "LOW"
        """
        start = time.time()

        # ── Feature vector (current bar)
        feats = self.feat_eng.compute(df1, df5, catalyst_type, urgency_score)
        atr_pct = float(feats.get("atr_pct", 1.0))

        # ── LGBM predict
        lgbm_model = self._get_lgbm(symbol)
        lgbm_prob  = lgbm_model.predict(feats) if lgbm_model else 0.5
        lgbm_auc   = lgbm_model.auc_score if lgbm_model else 0.0
        top_feats  = lgbm_model.feature_importance(top_n=5) if lgbm_model else []

        # ── LGBM raw 3-class probabilities
        lgbm_raw = (lgbm_model.predict_raw_probs(feats)
                    if lgbm_model else [0.33, 0.34, 0.33])

        # ── LSTM predict
        lstm_model = self._get_lstm(symbol)
        lstm_raw   = [0.33, 0.34, 0.33]   # default
        if lstm_model and lstm_model.model is not None:
            seq       = self.feat_eng.compute_sequence(df1, df5, catalyst_type, urgency_score)
            lstm_prob = lstm_model.predict(seq)
            # LSTM raw 3-class probabilities
            lstm_raw  = lstm_model.predict_proba(seq).tolist()
            lstm_raw  = [round(float(p), 4) for p in lstm_raw]
        else:
            lstm_prob = 0.5

        # ── Ensemble (scalar — backward compat)
        direction_prob, confidence_scalar = self.ensemble.combine(
            lgbm_prob, lstm_prob, lgbm_auc, session
        )

        # ── Ensemble 3-class — ใช้ full probability distribution
        combined_3c, predicted_class, confidence_3c = self.ensemble.combine_3class(
            np.array(lgbm_raw), np.array(lstm_raw), lgbm_auc, session
        )
        class_probs = {
            "sell":    round(float(combined_3c[0]), 4),
            "neutral": round(float(combined_3c[1]), 4),
            "buy":     round(float(combined_3c[2]), 4),
        }

        # ── ใช้ confidence จาก 3-class (แม่นกว่า scalar)
        confidence       = confidence_3c
        ml_score         = self.ensemble.to_ml_score(direction_prob, confidence, urgency_score)
        expected_move    = self.ensemble.to_expected_move(direction_prob, atr_pct)
        confidence_label = self.ensemble.to_confidence_label(confidence)

        # ── Signal จาก 3-class classification
        CLASS_TO_SIGNAL = {-1: "SHORT", 0: "NEUTRAL", 1: "LONG"}
        signal = CLASS_TO_SIGNAL[predicted_class]

        # ── Safety: ถ้า confidence ต่ำเกินไป → force NEUTRAL
        if confidence < CONFIDENCE_THRESHOLD:
            signal = "NEUTRAL"
            predicted_class = 0

        elapsed = round(time.time() - start, 3)
        logger.info(
            f"[ML] {symbol} | class={predicted_class:+d} "
            f"P(sell={class_probs['sell']:.2f} neu={class_probs['neutral']:.2f} buy={class_probs['buy']:.2f}) "
            f"conf={confidence:.3f}({confidence_label}) score={ml_score} signal={signal} | "
            f"LGBM={lgbm_prob:.3f} raw={lgbm_raw} | "
            f"LSTM={lstm_prob:.3f} raw={lstm_raw} | {elapsed}s"
        )

        return MLPrediction(
            symbol          = symbol,
            direction_prob  = direction_prob,
            confidence      = confidence,
            confidence_label= confidence_label,
            expected_move   = expected_move,
            ml_score        = ml_score,
            signal          = signal,
            top_features    = top_feats,
            lgbm_prob       = round(lgbm_prob, 4),
            lstm_prob       = round(lstm_prob, 4),
            lgbm_raw_probs  = lgbm_raw,
            lstm_raw_probs  = lstm_raw,
            predicted_class = predicted_class,
            class_probs     = class_probs,
            model_versions  = {
                "lgbm": lgbm_model.version if lgbm_model else "not_trained",
                "lstm": lstm_model.version if lstm_model else "not_trained",
            },
            notes = f"inference={elapsed}s | session={session} | 3class={predicted_class:+d}",
        )

    # ------------------------------------------
    # PUBLIC: retrain
    # ------------------------------------------

    def daily_retrain(self, symbol: str, df1: pd.DataFrame, df5: pd.DataFrame,
                      force: bool = False) -> float:
        """
        เทรน LightGBM ใหม่สำหรับ symbol นี้
        เรียกทุกเช้า 8:00 AM ET ก่อนตลาดเปิด
        คืน AUC score
        """
        if not force and not self.registry.needs_daily_retrain(symbol):
            logger.info(f"[LGBM] {symbol} เทรนแล้ววันนี้ → ข้าม")
            cached = self._lgbm_cache.get(symbol)
            return cached.auc_score if cached else 0.0

        logger.info(f"[LGBM] เริ่ม daily retrain {symbol} | bars={len(df1)}")

        # ── ใช้ compute_vectorized แทน for loop (แก้ TD#1 ใน retrain ด้วย)
        X = self.feat_eng.compute_vectorized(df1, df5).iloc[30:].ffill().fillna(0)
        y = self.label_gen.generate(df1.iloc[30:]).reset_index(drop=True)
        X = X.iloc[:len(y)]
        y = y.iloc[:len(X)]

        if X.empty:
            return 0.0

        model = LightGBMModel()
        auc   = model.train(X, y)

        if auc > 0:
            with self._lock:
                # invalidate ก่อน put เพื่อให้ inference ใช้ model ใหม่ทันที
                self._lgbm_cache.invalidate(symbol)
                self._lgbm_cache.put(symbol, model)
            self.registry.save_lgbm(symbol, model)

        return auc

    def weekly_retrain(self, symbol: str, df1: pd.DataFrame, df5: pd.DataFrame,
                       force: bool = False) -> float:
        """
        เทรน LSTM ใหม่สำหรับ symbol นี้
        เรียกทุกวันอาทิตย์ ใช้ข้อมูลย้อนหลัง LSTM_TRAIN_DAYS วัน
        คืน AUC score
        """
        if not TORCH_AVAILABLE:
            return 0.0

        if not force and not self.registry.needs_weekly_retrain(symbol):
            logger.info(f"[LSTM] {symbol} เทรนแล้วสัปดาห์นี้ → ข้าม")
            return 0.0

        logger.info(f"[LSTM] เริ่ม weekly retrain {symbol} | bars={len(df1)}")

        # สร้าง sequences
        seqs, labels = [], []
        label_series = self.label_gen.generate(df1)

        for i in range(SEQ_LEN + 30, len(df1) - HORIZON_MIN):
            slice1 = df1.iloc[i - SEQ_LEN - 30: i]
            slice5 = df5.iloc[max(0, i // 5 - 30): i // 5]
            seq    = self.feat_eng.compute_sequence(slice1, slice5)
            lbl    = int(label_series.iloc[i])
            seqs.append(seq)
            labels.append(lbl)

        if len(seqs) < MIN_TRAIN_BARS:
            logger.warning(f"[LSTM] {symbol} ข้อมูลไม่พอ ({len(seqs)} sequences)")
            return 0.0

        X_seq = np.array(seqs)
        y_arr = np.array(labels, dtype=np.float32)

        model = LSTMModel(input_size=X_seq.shape[-1])
        auc   = model.train(X_seq, y_arr)

        if auc > 0:
            with self._lock:
                # invalidate ก่อน put — ทำให้ inference thread โหลด model ใหม่ทันที
                self._lstm_cache.invalidate(symbol)
                self._lstm_cache.put(symbol, model)
            self.registry.save_lstm(symbol, model)

        return auc

    def daily_retrain_all(self, watchlist: list[str],
                          fetch_fn=None, days: int = LGBM_TRAIN_DAYS):
        """
        เทรน LightGBM ทุกตัวใน watchlist
        fetch_fn: callable(symbol, days) → (df1, df5) ถ้าไม่ใส่จะใช้ yfinance
        """
        logger.info(f"[Daily Retrain] เริ่ม | {len(watchlist)} symbols")
        for symbol in watchlist:
            try:
                df1, df5 = self._fetch_data(symbol, days, fetch_fn)
                if df1 is None or len(df1) < MIN_TRAIN_BARS:
                    continue
                auc = self.daily_retrain(symbol, df1, df5)
                logger.info(f"  {symbol}: LGBM AUC={auc:.4f}")
            except Exception as e:
                logger.error(f"  {symbol}: retrain error — {e}")

    def weekly_retrain_all(self, watchlist: list[str],
                           fetch_fn=None, days: int = LSTM_TRAIN_DAYS):
        """เทรน LSTM ทุกตัวใน watchlist"""
        if not TORCH_AVAILABLE:
            logger.warning("[Weekly Retrain] ข้าม LSTM — PyTorch ไม่พร้อม")
            return
        logger.info(f"[Weekly Retrain] เริ่ม | {len(watchlist)} symbols")
        for symbol in watchlist:
            try:
                df1, df5 = self._fetch_data(symbol, days, fetch_fn)
                if df1 is None or len(df1) < MIN_TRAIN_BARS:
                    continue
                auc = self.weekly_retrain(symbol, df1, df5)
                logger.info(f"  {symbol}: LSTM AUC={auc:.4f}")
            except Exception as e:
                logger.error(f"  {symbol}: retrain error — {e}")

    # ------------------------------------------
    # INTERNAL helpers
    # ------------------------------------------

    def _get_lgbm(self, symbol: str) -> Optional[LightGBMModel]:
        model = self._lgbm_cache.get(symbol)   # LRU hit → O(1)
        if model is not None:
            return model
        # Cache miss → load from disk
        model = self.registry.load_lgbm(symbol)
        if model:
            evicted = self._lgbm_cache.put(symbol, model)
            if evicted:
                # log cache stats เมื่อมี eviction เพื่อ monitor RAM pressure
                s = self._lgbm_cache.stats()
                logger.debug(
                    f"[LRU LGBM] evicted oldest model | "
                    f"size={s['size']}/{s['max_size']} "
                    f"hit_rate={s['hit_rate_pct']:.0f}%"
                )
        return model

    def _get_lstm(self, symbol: str) -> Optional[LSTMModel]:
        model = self._lstm_cache.get(symbol)   # LRU hit → O(1)
        if model is not None:
            return model
        # Cache miss → load from disk
        model = self.registry.load_lstm(symbol)
        if model:
            evicted = self._lstm_cache.put(symbol, model)
            if evicted:
                s = self._lstm_cache.stats()
                logger.debug(
                    f"[LRU LSTM] evicted oldest model | "
                    f"size={s['size']}/{s['max_size']} "
                    f"hit_rate={s['hit_rate_pct']:.0f}%"
                )
        return model

    def get_cache_stats(self) -> dict:
        """คืน cache statistics ทั้งคู่ — ใช้ monitor RAM pressure ใน production"""
        lgbm = self._lgbm_cache.stats()
        lstm = self._lstm_cache.stats()
        return {
            "lgbm": lgbm,
            "lstm": lstm,
            "total_models_in_ram": lgbm["size"] + lstm["size"],
            "estimated_ram_mb": round((lgbm["size"] + lstm["size"]) * 2.13, 1),
        }

    def _fetch_data(self, symbol: str, days: int, fetch_fn=None):
        """
        ดึง 15m + daily data ผ่าน safe_yf_download (rate-limited + tz-safe)
        """
        if fetch_fn:
            return fetch_fn(symbol, days)
        try:
            from data_pipeline_manager import safe_yf_download
            period = f"{min(days, 59)}d"
            df1 = safe_yf_download(symbol, period=period, interval=TIMEFRAME)
            df5 = safe_yf_download(symbol, period=period, interval="1d")
            return df1, df5
        except Exception as e:
            logger.error(f"_fetch_data {symbol}: {e}")
            return None, None

# ============================================================
# MAIN.PY INTEGRATION SNIPPET
# ============================================================

def build_main_integration_example():
    """
    แสดง code ที่ต้องเพิ่มใน main.py เพื่อ integrate TechnicalMLAnalyzer
    """
    snippet = '''
# ── ใน TradingPipeline.__init__() เพิ่ม:
from technical_ml_analyzer import TechnicalMLAnalyzer
self.ml_analyzer = TechnicalMLAnalyzer(model_dir="./models")

# ── ใน run_live() ก่อน scanner.start() เพิ่ม:
WATCHLIST = ["NVDA", "TSLA", "META", "AAPL", "MRNA", "AMZN"]
pipeline.ml_analyzer.daily_retrain_all(watchlist=WATCHLIST)

# ── ใน process_news() ระหว่าง Gate 6 และ Gate 7 เพิ่ม:
# GATE ML: Technical ML Analysis
df1, df5 = pipeline.ml_analyzer._fetch_data(sym, days=5)
if df1 is not None and len(df1) >= 30:
    prediction = pipeline.ml_analyzer.analyze(
        symbol        = sym,
        df1           = df1,
        df5           = df5,
        catalyst_type = candidate.catalyst_type,
        urgency_score = candidate.urgency_score,
        session       = session,
    )
    # Combine scores: 40% regime + 60% ML
    combined_score = 0.4 * final_score + 0.6 * prediction.ml_score
    if combined_score < 50 or prediction.signal == "NEUTRAL":
        logger.info(f"⛔ ML Gate: {sym} combined={combined_score:.1f} signal={prediction.signal}")
        return
else:
    prediction = None
    combined_score = final_score

# ── ใน journal.open_trade() เพิ่ม fields:
# (ต้อง extend TradeRecord ด้วย fields เหล่านี้)
# ml_score=prediction.ml_score if prediction else 0,
# ml_direction_prob=prediction.direction_prob if prediction else 0.5,
# ml_confidence=prediction.confidence if prediction else 0.0,
# ml_signal=prediction.signal if prediction else "NEUTRAL",
# ml_top_features=str(prediction.top_features) if prediction else "",
# ml_expected_move=prediction.expected_move if prediction else 0.0,
'''
    return snippet


# ============================================================
# MAIN — standalone test
# ============================================================

if __name__ == "__main__":
    import yfinance as yf

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    )

    print("=" * 60)
    print("  TECHNICAL ML ANALYZER — Test")
    print("=" * 60)

    SYMBOL = "NVDA"

    # ── ดึงข้อมูล
    print(f"\n[1] ดึงข้อมูล {SYMBOL}...")
    from data_pipeline_manager import safe_yf_download
    df1 = safe_yf_download(SYMBOL, period="5d", interval="15m")
    df5 = safe_yf_download(SYMBOL, period="5d", interval="1d")

    print(f"  15m bars: {len(df1)} | daily bars: {len(df5)}")

    # ── Feature Engineering
    print(f"\n[2] คำนวณ Features...")
    fe    = FeatureEngineer()
    feats = fe.compute(df1, df5, catalyst_type="EARNINGS", urgency_score=80)
    print(f"  Features: {len(feats)} columns")
    print(f"  Sample: vwap_dev={feats['vwap_dev_pct']:.3f}% | rsi={feats['rsi_14']:.1f} | rvol={feats['rvol']:.2f}x")

    # ── LightGBM Training
    print(f"\n[3] เทรน LightGBM (daily retrain)...")
    analyzer = TechnicalMLAnalyzer(model_dir="./models_test")
    auc = analyzer.daily_retrain(SYMBOL, df1, df5, force=True)
    print(f"  LightGBM AUC: {auc:.4f}")

    # ── LSTM Training (ถ้ามี PyTorch)
    if TORCH_AVAILABLE:
        print(f"\n[4] เทรน LSTM...")
        lstm_auc = analyzer.weekly_retrain(SYMBOL, df1, df5, force=True)
        print(f"  LSTM AUC: {lstm_auc:.4f}")
    else:
        print(f"\n[4] ข้าม LSTM (PyTorch ไม่พบ)")

    # ── Prediction
    print(f"\n[5] Predict (simulate news event)...")
    pred = analyzer.analyze(
        symbol        = SYMBOL,
        df1           = df1,
        df5           = df5,
        catalyst_type = "EARNINGS",
        urgency_score = 80,
        session       = "PRE_MARKET",
    )
    print(f"\n  ┌─ ML Prediction for {SYMBOL} ─────────────────")
    print(f"  │ direction_prob : {pred.direction_prob:.4f} ({'BULLISH' if pred.direction_prob>0.5 else 'BEARISH'})")
    print(f"  │ confidence     : {pred.confidence:.4f} ({pred.confidence_label})")
    print(f"  │ expected_move  : {pred.expected_move:+.2f}%")
    print(f"  │ ml_score       : {pred.ml_score}/100")
    print(f"  │ signal         : {pred.signal}")
    print(f"  │ ─── Raw Scores ───────────────────────────")
    print(f"  │ lgbm_prob      : {pred.lgbm_prob:.4f}")
    print(f"  │ lgbm_raw_probs : BEAR={pred.lgbm_raw_probs[0]:.3f} "
          f"NEUT={pred.lgbm_raw_probs[1]:.3f} BULL={pred.lgbm_raw_probs[2]:.3f}")
    print(f"  │ lstm_prob      : {pred.lstm_prob:.4f}")
    print(f"  │ lstm_raw_probs : BEAR={pred.lstm_raw_probs[0]:.3f} "
          f"NEUT={pred.lstm_raw_probs[1]:.3f} BULL={pred.lstm_raw_probs[2]:.3f}")
    print(f"  │ ─── Meta ──────────────────────────────────")
    print(f"  │ model_versions : {pred.model_versions}")
    print(f"  │ score_summary  : {pred.score_summary()}")
    print(f"  │ top features   :")
    for f in pred.top_features:
        print(f"  │   {f['feature']:30s} importance={f['importance']:.1f}")
    print(f"  └─────────────────────────────────────────────")

    # ── Integration snippet
    print(f"\n[6] Integration กับ main.py:")
    print(build_main_integration_example())

    print("\n✅ ทดสอบเสร็จสิ้น")