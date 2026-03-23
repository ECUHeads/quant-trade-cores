"""
technical_scanner.py
====================
Technical Signal Scanner — 15m Candle-Driven Trigger

Purpose:
  สแกน indicator ทุก 15 นาทีสำหรับ watchlist symbols
  เมื่อเจอสัญญาณ → สร้าง NewsCandidate(source="TECH_SCAN")
  → ส่งเข้า pipeline เหมือน news แต่ Gate 19 LLM รู้ว่าเป็น "tech-only"

Signal Types (configurable, เปิด/ปิดแต่ละตัวได้):
  1. VWAP_PULLBACK  — ราคาย่อแตะ VWAP + RVOL สูง
  2. ML_BREAKOUT    — ML score > threshold โดยไม่ต้องรอข่าว
  3. VOLUME_SPIKE   — RVOL > 2x + price move แรง

Integration:
  main.py 15m tick loop → TechnicalScanner.scan_tick()
                            → list[NewsCandidate]
                              → pipeline.process_news(candidate)

Usage:
  python main.py --mode paper --enable-tech-scan
  python main.py --mode shadow --enable-tech-scan --skip-gates gate19
  python main.py --mode live --enable-tech-scan

Config Overrides (env vars):
  TECH_SCAN_ENABLED=true
  TECH_SCAN_RULES=vwap_pullback,ml_breakout,volume_spike
  TECH_SCAN_VWAP_THRESHOLD=0.5      # % within VWAP
  TECH_SCAN_RVOL_MIN=1.5            # relative volume minimum
  TECH_SCAN_ML_MIN=65               # ML score threshold
  TECH_SCAN_VOL_SPIKE_MULT=2.0      # RVOL multiplier for volume spike
  TECH_SCAN_PRICE_MOVE_PCT=1.5      # min % price move for volume spike
"""

import os
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("TechScan")

_TZ_ET = ZoneInfo("America/New_York")


# ============================================================
# SIGNAL RULES — Configurable
# ============================================================

@dataclass
class TechScanConfig:
    """
    Configuration for Technical Scanner rules.

    ทุกค่าอ่านจาก env var ได้ (override ผ่าน .env)
    หรือเซ็ตตรงใน code ก็ได้
    """
    # ── Master switch
    enabled:  bool = False

    # ── Active rules (comma-separated string or set)
    active_rules: set = field(default_factory=lambda: {
        "vwap_pullback", "ml_breakout", "volume_spike", "rsi_divergence",
    })

    # ── VWAP Pullback params
    vwap_threshold_pct:  float = 0.5    # ราคาอยู่ภายใน ±0.5% ของ VWAP
    vwap_rvol_min:       float = 1.5    # RVOL ขั้นต่ำ
    vwap_rsi_max:        float = 45.0   # RSI ต้องไม่สูงเกิน (pullback zone)

    # ── ML Score Breakout params
    ml_score_min:        int   = 65     # ML score threshold
    ml_confidence_min:   float = 0.55   # minimum confidence

    # ── Volume Spike params
    vol_spike_rvol:      float = 2.0    # RVOL multiplier
    vol_spike_price_pct: float = 1.5    # min % price move in last 2 bars

    # ── RSI Divergence params
    div_swing_order:     int   = 5      # bars ซ้าย-ขวาสำหรับ swing detection
    div_lookback_bars:   int   = 30     # ดูย้อนหลังกี่ bars
    div_min_confidence:  float = 0.3    # confidence ขั้นต่ำ
    div_rvol_min:        float = 1.0    # RVOL ขั้นต่ำ

    # ── Scan scope
    scan_tier1_only:     bool  = False  # True = scan เฉพาะ Tier 1 (faster)
    max_signals_per_tick: int  = 3      # จำกัด signals ต่อ 15m tick
    cooldown_sec:        float = 900.0  # 15 นาที cooldown per symbol

    @classmethod
    def from_env(cls) -> "TechScanConfig":
        """โหลดค่าจาก environment variables"""
        cfg = cls()
        cfg.enabled = os.getenv("TECH_SCAN_ENABLED", "false").lower() == "true"

        rules_str = os.getenv("TECH_SCAN_RULES", "")
        if rules_str:
            cfg.active_rules = set(r.strip() for r in rules_str.split(",") if r.strip())

        cfg.vwap_threshold_pct  = float(os.getenv("TECH_SCAN_VWAP_THRESHOLD", cfg.vwap_threshold_pct))
        cfg.vwap_rvol_min       = float(os.getenv("TECH_SCAN_RVOL_MIN", cfg.vwap_rvol_min))
        cfg.ml_score_min        = int(os.getenv("TECH_SCAN_ML_MIN", cfg.ml_score_min))
        cfg.vol_spike_rvol      = float(os.getenv("TECH_SCAN_VOL_SPIKE_MULT", cfg.vol_spike_rvol))
        cfg.vol_spike_price_pct = float(os.getenv("TECH_SCAN_PRICE_MOVE_PCT", cfg.vol_spike_price_pct))

        return cfg


# ============================================================
# SIGNAL RESULT
# ============================================================

@dataclass
class TechSignal:
    """ผลลัพธ์จากการสแกน 1 signal"""
    symbol:       str
    rule_name:    str            # "VWAP_PULLBACK" | "ML_BREAKOUT" | "VOLUME_SPIKE"
    side:         str            # "buy" | "sell"
    urgency:      int            # 1-100
    detail:       str            # human-readable description
    metrics:      dict = field(default_factory=dict)  # raw numbers

    @property
    def catalyst_type(self) -> str:
        return f"TECH_{self.rule_name}"

    @property
    def headline(self) -> str:
        return f"[TECH] {self.rule_name}: {self.detail}"


# ============================================================
# TECHNICAL SCANNER
# ============================================================

class TechnicalScanner:
    """
    สแกน technical signals ทุก 15m candle close.

    Usage:
        scanner = TechnicalScanner(config=TechScanConfig.from_env())
        signals = scanner.scan_tick(symbols, pipeline)
        for sig in signals:
            pipeline.process_news(sig.to_news_candidate())
    """

    ALL_RULES = {"vwap_pullback", "ml_breakout", "volume_spike", "rsi_divergence"}

    def __init__(self, config: TechScanConfig = None):
        self.config = config or TechScanConfig()
        self._cooldowns: dict = {}   # symbol → last_signal_time
        self._scan_count: int = 0

        # Validate rules
        invalid = self.config.active_rules - self.ALL_RULES
        if invalid:
            logger.warning(f"Unknown tech scan rules: {invalid}. Valid: {self.ALL_RULES}")
            self.config.active_rules -= invalid

        logger.info(
            f"TechnicalScanner initialized | "
            f"rules={sorted(self.config.active_rules)} | "
            f"enabled={self.config.enabled}"
        )

    # ------------------------------------------
    # MAIN: Scan one 15m tick
    # ------------------------------------------

    def scan_tick(self, symbols: list, pipeline) -> list:
        """
        สแกนทุก symbol ใน watchlist สำหรับ technical signals.

        Called by main.py ทุก 15 นาที.

        Args:
            symbols: list of symbols to scan
            pipeline: TradingPipeline instance (for data access)

        Returns:
            list[NewsCandidate] — signals ที่พร้อมส่งเข้า pipeline
        """
        if not self.config.enabled:
            return []

        self._scan_count += 1
        t_start = time.time()
        all_signals = []

        for sym in symbols:
            # ── Cooldown check
            if self._is_cooled_down(sym):
                continue

            # ── Skip if already have open position
            if sym in pipeline._open_trades:
                continue

            try:
                signals = self._scan_symbol(sym, pipeline)
                for sig in signals:
                    self._cooldowns[sym] = time.time()
                    all_signals.append(sig)

                    if len(all_signals) >= self.config.max_signals_per_tick:
                        break

            except Exception as e:
                logger.debug(f"[TechScan] {sym} error: {e}")

            if len(all_signals) >= self.config.max_signals_per_tick:
                break

        elapsed = time.time() - t_start

        if all_signals:
            logger.info(
                f"🔬 TechScan tick #{self._scan_count}: "
                f"{len(all_signals)} signal(s) from {len(symbols)} symbols "
                f"({elapsed:.1f}s)"
            )
            for sig in all_signals:
                logger.info(f"  📡 {sig.symbol} | {sig.rule_name} | {sig.side} | {sig.detail}")

        # ── Convert to NewsCandidate
        return [self._to_news_candidate(sig) for sig in all_signals]

    # ------------------------------------------
    # SCAN ONE SYMBOL
    # ------------------------------------------

    def _scan_symbol(self, symbol: str, pipeline) -> list:
        """สแกน symbol เดียว ทุก active rule → return signals"""
        signals = []

        # ── Fetch 15m data (reuse pipeline helpers)
        try:
            from data_pipeline_manager import safe_download, compute_vwap, compute_atr_15m
            df = safe_download(symbol, period="5d", interval="15m")
            if df.empty or len(df) < 30:
                return signals
        except Exception:
            return signals

        price = float(df["close"].iloc[-1])
        if price <= 0:
            return signals

        # ── Compute shared indicators
        indicators = self._compute_indicators(df)

        # ── Rule 1: VWAP Pullback
        if "vwap_pullback" in self.config.active_rules:
            sig = self._check_vwap_pullback(symbol, df, price, indicators)
            if sig:
                signals.append(sig)

        # ── Rule 2: ML Score Breakout
        if "ml_breakout" in self.config.active_rules:
            sig = self._check_ml_breakout(symbol, pipeline, indicators)
            if sig:
                signals.append(sig)

        # ── Rule 3: Volume Spike
        if "rsi_divergence" in self.config.active_rules:
            sig = self._check_rsi_divergence(symbol, df, price, ind)
            if sig:
                signals.append(sig)

        if "volume_spike" in self.config.active_rules:
            sig = self._check_volume_spike(symbol, df, price, indicators)
            if sig:
                signals.append(sig)

        return signals

    # ------------------------------------------
    # SHARED INDICATOR COMPUTATION
    # ------------------------------------------

    def _compute_indicators(self, df) -> dict:
        """คำนวณ indicators ที่ใช้ร่วมกันทุก rule"""
        import numpy as np

        c = df["close"]
        h = df["high"]
        l = df["low"]
        o = df["open"]
        v = df["volume"]

        # ── VWAP (intraday reset)
        typical = (h + l + c) / 3
        if hasattr(df.index, 'date'):
            try:
                idx = df.index
                if hasattr(idx, 'tz') and idx.tz is not None:
                    groups = idx.tz_localize(None).date
                else:
                    groups = idx.date
                cum_tpv = (typical * v).groupby(groups).cumsum()
                cum_vol = v.groupby(groups).cumsum().replace(0, float('nan'))
                vwap = cum_tpv / cum_vol
            except Exception:
                cum_tpv = (typical * v).cumsum()
                cum_vol = v.cumsum().replace(0, float('nan'))
                vwap = cum_tpv / cum_vol
        else:
            cum_tpv = (typical * v).cumsum()
            cum_vol = v.cumsum().replace(0, float('nan'))
            vwap = cum_tpv / cum_vol

        vwap_val = float(vwap.iloc[-1]) if not vwap.empty else float(c.iloc[-1])
        price    = float(c.iloc[-1])

        # ── VWAP deviation %
        vwap_dev_pct = (price - vwap_val) / (vwap_val + 1e-9) * 100

        # ── RVOL (relative volume vs 20-bar avg)
        adv20 = float(v.rolling(20).mean().iloc[-1])
        rvol  = float(v.iloc[-1]) / (adv20 + 1e-9)

        # ── ATR (14-period)
        hl = h - l
        hc = (h - c.shift(1)).abs()
        lc = (l - c.shift(1)).abs()
        import pandas as pd
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        # ── RSI-14
        delta = c.diff()
        gain  = delta.where(delta > 0, 0).rolling(14).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float('nan'))
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])

        # ── Price change last 2 bars (%)
        if len(c) >= 3:
            price_change_2bar = (price - float(c.iloc[-3])) / float(c.iloc[-3]) * 100
        else:
            price_change_2bar = 0.0

        # ── Close vs Open of latest bar (buy/sell pressure)
        latest_body_pct = (price - float(o.iloc[-1])) / (float(o.iloc[-1]) + 1e-9) * 100

        return {
            "price":             price,
            "vwap":              vwap_val,
            "vwap_dev_pct":      vwap_dev_pct,
            "rvol":              rvol,
            "atr":               atr,
            "rsi":               rsi,
            "price_change_2bar": price_change_2bar,
            "latest_body_pct":   latest_body_pct,
            "volume_latest":     float(v.iloc[-1]),
            "adv20":             adv20,
        }

    # ------------------------------------------
    # RULE 1: VWAP PULLBACK
    # ------------------------------------------

    def _check_vwap_pullback(self, symbol, df, price, ind) -> Optional[TechSignal]:
        """
        VWAP Pullback Signal:
          ราคาย่อลงมาแตะโซน VWAP (±threshold%)
          + RVOL สูงพอ (สถาบันอาจเข้าซื้อ)
          + RSI ไม่ overbought (ยังมี upside)

        Logic:
          LONG:  price within [-threshold, +threshold] of VWAP
                 AND RVOL >= min AND RSI < max AND price > VWAP
          SHORT: price within [-threshold, +threshold] of VWAP
                 AND RVOL >= min AND RSI > (100-max) AND price < VWAP
        """
        cfg = self.config
        dev = abs(ind["vwap_dev_pct"])

        if dev > cfg.vwap_threshold_pct:
            return None  # ราคาไกล VWAP เกินไป

        if ind["rvol"] < cfg.vwap_rvol_min:
            return None  # volume ไม่พอ

        # ── Determine side from price structure
        if ind["price"] >= ind["vwap"] and ind["rsi"] < cfg.vwap_rsi_max:
            # Bullish: ราคาเพิ่ง bounce จาก VWAP ขึ้นมา + RSI ยังต่ำ
            if ind["latest_body_pct"] < 0:
                return None  # latest bar เป็นแท่งแดง → ยังไม่ bounce
            side = "buy"
            urgency = min(85, 60 + int(ind["rvol"] * 10))
        elif ind["price"] < ind["vwap"] and ind["rsi"] > (100 - cfg.vwap_rsi_max):
            # Bearish: ราคาหลุด VWAP ลงมา + RSI สูง
            if ind["latest_body_pct"] > 0:
                return None
            side = "sell"
            urgency = min(85, 60 + int(ind["rvol"] * 10))
        else:
            return None

        return TechSignal(
            symbol=symbol,
            rule_name="VWAP_PULLBACK",
            side=side,
            urgency=urgency,
            detail=(
                f"Price ${ind['price']:.2f} within {dev:.2f}% of VWAP ${ind['vwap']:.2f} | "
                f"RVOL={ind['rvol']:.1f}x RSI={ind['rsi']:.0f}"
            ),
            metrics={
                "vwap": ind["vwap"],
                "vwap_dev_pct": ind["vwap_dev_pct"],
                "rvol": ind["rvol"],
                "rsi": ind["rsi"],
            },
        )

    # ------------------------------------------
    # RULE 2: ML SCORE BREAKOUT
    # ------------------------------------------

    def _check_ml_breakout(self, symbol, pipeline, ind) -> Optional[TechSignal]:
        """
        ML Score Breakout:
          ML model ให้ score สูง (> threshold) โดยไม่ต้องมีข่าว
          = model เห็น pattern ในราคาที่บ่งบอกว่าวันนี้จะปิดเหนือ VWAP

        Logic:
          ml_score >= threshold AND confidence >= min
        """
        if not pipeline.ml_analyzer:
            return None

        cfg = self.config

        try:
            session = "MARKET"
            try:
                session = pipeline.session_filter.current_session()
            except Exception:
                pass

            prediction = pipeline._run_ml_gate(
                symbol, "OTHER", 50, session,
            )
            if prediction is None:
                return None

            if prediction.ml_score < cfg.ml_score_min:
                return None
            if prediction.confidence < cfg.ml_confidence_min:
                return None

            # ── Side from ML direction
            side = "buy" if prediction.direction_prob > 0.5 else "sell"
            urgency = min(90, 50 + int(prediction.ml_score * 0.4))

            return TechSignal(
                symbol=symbol,
                rule_name="ML_BREAKOUT",
                side=side,
                urgency=urgency,
                detail=(
                    f"ML score={prediction.ml_score} conf={prediction.confidence:.2f} "
                    f"dir={prediction.direction_prob:.2f} | "
                    f"No news catalyst — pure technical signal"
                ),
                metrics={
                    "ml_score": prediction.ml_score,
                    "confidence": prediction.confidence,
                    "direction_prob": prediction.direction_prob,
                },
            )
        except Exception as e:
            logger.debug(f"[ML_BREAKOUT] {symbol} error: {e}")
            return None

    # ------------------------------------------
    # RULE 3: VOLUME SPIKE
    # ------------------------------------------

    def _check_volume_spike(self, symbol, df, price, ind) -> Optional[TechSignal]:
        """
        Volume Spike:
          Volume ผิดปกติ (RVOL > 2x) + ราคาเคลื่อนไหวแรง (> 1.5%)
          = อาจมี institutional activity หรือข่าวที่ scanner ยังไม่จับ

        Logic:
          RVOL >= spike_mult AND |price_change_2bar| >= price_move_pct
          Side = direction of price move
        """
        cfg = self.config

        if ind["rvol"] < cfg.vol_spike_rvol:
            return None

        price_move = abs(ind["price_change_2bar"])
        if price_move < cfg.vol_spike_price_pct:
            return None

        # ── Side from price direction
        side = "buy" if ind["price_change_2bar"] > 0 else "sell"
        urgency = min(95, 70 + int(ind["rvol"] * 5) + int(price_move * 3))

        return TechSignal(
            symbol=symbol,
            rule_name="VOLUME_SPIKE",
            side=side,
            urgency=urgency,
            detail=(
                f"RVOL={ind['rvol']:.1f}x ({ind['volume_latest']:,.0f} vs avg {ind['adv20']:,.0f}) | "
                f"Price move {ind['price_change_2bar']:+.2f}% in 2 bars | "
                f"Possible unreported catalyst"
            ),
            metrics={
                "rvol": ind["rvol"],
                "price_change_2bar": ind["price_change_2bar"],
                "volume_latest": ind["volume_latest"],
                "adv20": ind["adv20"],
            },
        )

    # ------------------------------------------
    # HELPERS
    # ------------------------------------------

    def _is_cooled_down(self, symbol: str) -> bool:
        last = self._cooldowns.get(symbol, 0.0)
        return (time.time() - last) < self.config.cooldown_sec

    def _to_news_candidate(self, sig: TechSignal):
        """แปลง TechSignal → NewsCandidate ให้เข้า pipeline เดิมได้"""
        from ext_data.news_scanner import NewsCandidate

        return NewsCandidate(
            symbol=sig.symbol,
            headline=sig.headline,
            catalyst_type=sig.catalyst_type,
            urgency_score=sig.urgency,
            source="TECH_SCAN",
        )

    def get_stats(self) -> dict:
        return {
            "scan_count":       self._scan_count,
            "active_rules":     sorted(self.config.active_rules),
            "active_cooldowns": sum(1 for s, t in self._cooldowns.items()
                                    if time.time() - t < self.config.cooldown_sec),
        }

    # ------------------------------------------
    # RULE 4: RSI DIVERGENCE
    # ------------------------------------------

    def _check_rsi_divergence(self, symbol, df, price, ind) -> Optional[TechSignal]:
        """
        RSI Divergence: จับ Hidden + Regular Divergence

        ตามคำแนะนำมืออาชีพ:
          "สิ่งที่พวกเขาหาคือ Divergence โดยเฉพาะ Hidden Divergence
           เพื่อหาจังหวะเข้าทำกำไรในจังหวะย่อตัว (Pullback)"

        Hidden Bullish:  Price HL + RSI LL → Pullback in uptrend → LONG
        Hidden Bearish:  Price LH + RSI HH → Pullback in downtrend → SHORT
        Regular Bullish: Price LL + RSI HL → Weakening downtrend
        Regular Bearish: Price HH + RSI LH → Weakening uptrend
        """
        if len(df) < 30:
            return None

        cfg = self.config
        if ind.get("rvol", 0) < getattr(cfg, "div_rvol_min", 1.0):
            return None

        c = df["close"].astype(float)
        lookback = getattr(cfg, "div_lookback_bars", 30)
        swing_order = getattr(cfg, "div_swing_order", 5)
        min_conf = getattr(cfg, "div_min_confidence", 0.3)

        # Calculate RSI
        delta = c.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + (gain / (loss.replace(0, np.nan)))))
        rsi = rsi.fillna(50)

        # Find swing points
        price_window = c.iloc[-lookback:].reset_index(drop=True)
        rsi_window = rsi.iloc[-lookback:].reset_index(drop=True)

        def _find_swings(series, order=5):
            vals = series.values
            n = len(vals)
            swings = []
            for i in range(order, n - order):
                is_hi = all(vals[i] > vals[i-j] and vals[i] > vals[i+j] for j in range(1, order+1))
                is_lo = all(vals[i] < vals[i-j] and vals[i] < vals[i+j] for j in range(1, order+1))
                if is_hi: swings.append((i, float(vals[i]), "high"))
                elif is_lo: swings.append((i, float(vals[i]), "low"))
            return swings[-10:]

        price_swings = _find_swings(price_window, swing_order)
        rsi_swings = _find_swings(rsi_window, swing_order)

        p_lows = [(i,v) for i,v,t in price_swings if t == "low"]
        p_highs = [(i,v) for i,v,t in price_swings if t == "high"]
        r_lows = [(i,v) for i,v,t in rsi_swings if t == "low"]
        r_highs = [(i,v) for i,v,t in rsi_swings if t == "high"]

        best = {"type": None, "conf": 0.0, "desc": ""}

        # Hidden Bullish: Price HL + RSI LL
        if len(p_lows) >= 2 and len(r_lows) >= 2:
            pl1, pl2 = p_lows[-2], p_lows[-1]
            rl1, rl2 = r_lows[-2], r_lows[-1]
            if pl2[0]-pl1[0] >= 5 and pl2[1] > pl1[1] and rl2[1] < rl1[1]:
                conf = min(1.0, abs(pl2[1]-pl1[1])/(pl1[1]+1e-9)*100*0.3 + abs(rl2[1]-rl1[1])*0.02)
                if conf > best["conf"]:
                    best = {"type": "hidden_bullish", "conf": conf,
                            "desc": f"Hidden Bull: Price HL→{pl2[1]:.0f} + RSI LL→{rl2[1]:.0f}"}

        # Hidden Bearish: Price LH + RSI HH
        if len(p_highs) >= 2 and len(r_highs) >= 2:
            ph1, ph2 = p_highs[-2], p_highs[-1]
            rh1, rh2 = r_highs[-2], r_highs[-1]
            if ph2[0]-ph1[0] >= 5 and ph2[1] < ph1[1] and rh2[1] > rh1[1]:
                conf = min(1.0, abs(ph2[1]-ph1[1])/(ph1[1]+1e-9)*100*0.3 + abs(rh2[1]-rh1[1])*0.02)
                if conf > best["conf"]:
                    best = {"type": "hidden_bearish", "conf": conf,
                            "desc": f"Hidden Bear: Price LH→{ph2[1]:.0f} + RSI HH→{rh2[1]:.0f}"}

        # Regular Bullish: Price LL + RSI HL
        if len(p_lows) >= 2 and len(r_lows) >= 2 and best["conf"] < 0.3:
            pl1, pl2 = p_lows[-2], p_lows[-1]
            rl1, rl2 = r_lows[-2], r_lows[-1]
            if pl2[0]-pl1[0] >= 5 and pl2[1] < pl1[1] and rl2[1] > rl1[1]:
                conf = min(0.8, abs(pl2[1]-pl1[1])/(pl1[1]+1e-9)*100*0.2 + abs(rl2[1]-rl1[1])*0.015)
                if conf > best["conf"]:
                    best = {"type": "regular_bullish", "conf": conf,
                            "desc": f"Reg Bull: Price LL→{pl2[1]:.0f} + RSI HL→{rl2[1]:.0f}"}

        # Regular Bearish: Price HH + RSI LH
        if len(p_highs) >= 2 and len(r_highs) >= 2 and best["conf"] < 0.3:
            ph1, ph2 = p_highs[-2], p_highs[-1]
            rh1, rh2 = r_highs[-2], r_highs[-1]
            if ph2[0]-ph1[0] >= 5 and ph2[1] > ph1[1] and rh2[1] < rh1[1]:
                conf = min(0.8, abs(ph2[1]-ph1[1])/(ph1[1]+1e-9)*100*0.2 + abs(rh2[1]-rh1[1])*0.015)
                if conf > best["conf"]:
                    best = {"type": "regular_bearish", "conf": conf,
                            "desc": f"Reg Bear: Price HH→{ph2[1]:.0f} + RSI LH→{rh2[1]:.0f}"}

        if best["type"] is None or best["conf"] < min_conf:
            return None

        side = "buy" if "bullish" in best["type"] else "sell"
        base_urg = 65 if "hidden" in best["type"] else 55
        urgency = min(90, base_urg + int(best["conf"] * 25))

        return TechSignal(
            symbol=symbol,
            rule_name="RSI_DIVERGENCE",
            side=side,
            urgency=urgency,
            detail=f"{best['desc']} | RVOL={ind.get('rvol',0):.1f}x conf={best['conf']:.2f}",
            metrics={"divergence_type": best["type"], "confidence": best["conf"],
                     "rsi_current": float(rsi.iloc[-1]), "rvol": ind.get("rvol", 0)},
        )
