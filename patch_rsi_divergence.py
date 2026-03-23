"""
patch_rsi_divergence.py
========================
Patch สำหรับ mode/technical_scanner.py

เพิ่ม RSI Divergence detection เป็น scanner rule ที่ 4:
  DIVERGENCE_SIGNAL — จับ Hidden Divergence (Pullback opportunity)

ตามคำแนะนำจากเอกสาร Day Trading มืออาชีพ:
  "มือโปรมักไม่ได้ใช้ RSI แบบ Overbought (70) แล้วเซล
   หรือ Oversold (30) แล้วบาย เพราะในเทรนด์ที่แข็งแกร่ง
   RSI สามารถค้างอยู่ในโซนนั้นได้นาน
   สิ่งที่พวกเขาหาคือ Divergence (ความขัดแย้งระหว่างราคากับโมเมนตัม)
   โดยเฉพาะ Hidden Divergence เพื่อหาจังหวะเข้าทำกำไรในจังหวะย่อตัว (Pullback)"

Types of RSI Divergence:
  1. Regular Bullish Divergence: Price lower low + RSI higher low
     → เทรนด์ขาลงอ่อนแรง อาจกลับตัว
  2. Regular Bearish Divergence: Price higher high + RSI lower high
     → เทรนด์ขาขึ้นอ่อนแรง อาจกลับตัว
  3. Hidden Bullish Divergence: Price higher low + RSI lower low
     → Pullback ในเทรนด์ขาขึ้น → โอกาสเข้า LONG ★
  4. Hidden Bearish Divergence: Price lower high + RSI higher high
     → Pullback ในเทรนด์ขาลง → โอกาสเข้า SHORT ★

★ Hidden Divergence เป็น focus หลัก (ตามคำแนะนำ)

Integration:
  1. เพิ่ม divergence params ใน TechScanConfig
  2. เพิ่ม _check_rsi_divergence() ใน TechnicalScanner
  3. เพิ่ม "rsi_divergence" ใน active_rules default set
  4. เรียกใน _scan_symbol()

Apply:
  python patch_rsi_divergence.py mode/technical_scanner.py
"""

import numpy as np
import pandas as pd
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("RSIDivergence")


# ============================================================
# RSI DIVERGENCE DETECTOR
# ============================================================

def find_swing_points(
    series: pd.Series,
    order: int = 5,
    max_points: int = 10,
) -> list:
    """
    หา swing highs/lows ใน time series

    Args:
        series: ข้อมูล price หรือ RSI
        order: จำนวน bars ซ้าย-ขวาที่ต้อง lower/higher กว่า swing point
        max_points: จำนวน swing points สูงสุดที่จะ return

    Returns:
        list of (index_position, value, type) where type = "high" | "low"
    """
    values = series.values
    n = len(values)
    swings = []

    for i in range(order, n - order):
        # Check swing high
        is_high = True
        for j in range(1, order + 1):
            if values[i] <= values[i - j] or values[i] <= values[i + j]:
                is_high = False
                break

        # Check swing low
        is_low = True
        for j in range(1, order + 1):
            if values[i] >= values[i - j] or values[i] >= values[i + j]:
                is_low = False
                break

        if is_high:
            swings.append((i, float(values[i]), "high"))
        elif is_low:
            swings.append((i, float(values[i]), "low"))

    # Return most recent swings
    return swings[-max_points:]


def detect_rsi_divergence(
    price_series: pd.Series,
    rsi_series: pd.Series,
    swing_order: int = 5,
    lookback_bars: int = 30,
    min_swing_gap: int = 5,
) -> dict:
    """
    ตรวจจับ RSI Divergence ทั้ง 4 แบบ

    Args:
        price_series: ราคา close
        rsi_series: RSI values
        swing_order: bars ซ้าย-ขวาสำหรับ swing detection
        lookback_bars: ดูย้อนหลังกี่ bars
        min_swing_gap: swing 2 จุดต้องห่างกันอย่างน้อยกี่ bars

    Returns:
        dict: {
            "type": "hidden_bullish" | "hidden_bearish" | "regular_bullish" | "regular_bearish" | None,
            "confidence": 0.0-1.0,
            "price_swing1": (idx, price),
            "price_swing2": (idx, price),
            "rsi_swing1": (idx, rsi),
            "rsi_swing2": (idx, rsi),
            "description": "..."
        }
    """
    # ใช้เฉพาะ lookback window
    price = price_series.iloc[-lookback_bars:].reset_index(drop=True)
    rsi = rsi_series.iloc[-lookback_bars:].reset_index(drop=True)

    if len(price) < lookback_bars or len(rsi) < lookback_bars:
        return {"type": None, "confidence": 0.0}

    # หา swing points
    price_swings = find_swing_points(price, order=swing_order)
    rsi_swings = find_swing_points(rsi, order=swing_order)

    if len(price_swings) < 2 or len(rsi_swings) < 2:
        return {"type": None, "confidence": 0.0}

    # แยก swing highs และ lows
    price_lows = [(i, v) for i, v, t in price_swings if t == "low"]
    price_highs = [(i, v) for i, v, t in price_swings if t == "high"]
    rsi_lows = [(i, v) for i, v, t in rsi_swings if t == "low"]
    rsi_highs = [(i, v) for i, v, t in rsi_swings if t == "high"]

    result = {"type": None, "confidence": 0.0}
    best_confidence = 0.0

    # ── Hidden Bullish Divergence ★ (Priority — ตามคำแนะนำ)
    # Price: higher low (HL)  +  RSI: lower low (LL)
    # → Pullback ในเทรนด์ขาขึ้น → เข้า LONG
    if len(price_lows) >= 2 and len(rsi_lows) >= 2:
        pl1, pl2 = price_lows[-2], price_lows[-1]
        rl1, rl2 = rsi_lows[-2], rsi_lows[-1]

        if (pl2[0] - pl1[0] >= min_swing_gap  # ห่างกันพอ
            and pl2[1] > pl1[1]                # Price: higher low
            and rl2[1] < rl1[1]):              # RSI: lower low

            # Confidence based on divergence magnitude
            price_diff = (pl2[1] - pl1[1]) / (pl1[1] + 1e-9) * 100
            rsi_diff = abs(rl2[1] - rl1[1])
            conf = min(1.0, (price_diff * 0.3 + rsi_diff * 0.02))

            if conf > best_confidence:
                best_confidence = conf
                result = {
                    "type": "hidden_bullish",
                    "confidence": round(conf, 3),
                    "price_swing1": pl1, "price_swing2": pl2,
                    "rsi_swing1": rl1, "rsi_swing2": rl2,
                    "description": (
                        f"Hidden Bullish: Price HL ({pl1[1]:.2f}→{pl2[1]:.2f}) "
                        f"+ RSI LL ({rl1[1]:.1f}→{rl2[1]:.1f}) → Pullback in uptrend"
                    ),
                }

    # ── Hidden Bearish Divergence ★
    # Price: lower high (LH)  +  RSI: higher high (HH)
    # → Pullback ในเทรนด์ขาลง → เข้า SHORT
    if len(price_highs) >= 2 and len(rsi_highs) >= 2:
        ph1, ph2 = price_highs[-2], price_highs[-1]
        rh1, rh2 = rsi_highs[-2], rsi_highs[-1]

        if (ph2[0] - ph1[0] >= min_swing_gap
            and ph2[1] < ph1[1]              # Price: lower high
            and rh2[1] > rh1[1]):            # RSI: higher high

            price_diff = abs((ph2[1] - ph1[1]) / (ph1[1] + 1e-9) * 100)
            rsi_diff = abs(rh2[1] - rh1[1])
            conf = min(1.0, (price_diff * 0.3 + rsi_diff * 0.02))

            if conf > best_confidence:
                best_confidence = conf
                result = {
                    "type": "hidden_bearish",
                    "confidence": round(conf, 3),
                    "price_swing1": ph1, "price_swing2": ph2,
                    "rsi_swing1": rh1, "rsi_swing2": rh2,
                    "description": (
                        f"Hidden Bearish: Price LH ({ph1[1]:.2f}→{ph2[1]:.2f}) "
                        f"+ RSI HH ({rh1[1]:.1f}→{rh2[1]:.1f}) → Pullback in downtrend"
                    ),
                }

    # ── Regular Bullish Divergence (secondary)
    # Price: lower low (LL)  +  RSI: higher low (HL)
    # → เทรนด์ขาลงอ่อนแรง อาจกลับตัว
    if len(price_lows) >= 2 and len(rsi_lows) >= 2 and best_confidence < 0.3:
        pl1, pl2 = price_lows[-2], price_lows[-1]
        rl1, rl2 = rsi_lows[-2], rsi_lows[-1]

        if (pl2[0] - pl1[0] >= min_swing_gap
            and pl2[1] < pl1[1]              # Price: lower low
            and rl2[1] > rl1[1]):            # RSI: higher low

            price_diff = abs((pl2[1] - pl1[1]) / (pl1[1] + 1e-9) * 100)
            rsi_diff = abs(rl2[1] - rl1[1])
            conf = min(0.8, (price_diff * 0.2 + rsi_diff * 0.015))

            if conf > best_confidence:
                best_confidence = conf
                result = {
                    "type": "regular_bullish",
                    "confidence": round(conf, 3),
                    "price_swing1": pl1, "price_swing2": pl2,
                    "rsi_swing1": rl1, "rsi_swing2": rl2,
                    "description": (
                        f"Regular Bullish: Price LL ({pl1[1]:.2f}→{pl2[1]:.2f}) "
                        f"+ RSI HL ({rl1[1]:.1f}→{rl2[1]:.1f}) → Weakening downtrend"
                    ),
                }

    # ── Regular Bearish Divergence (secondary)
    # Price: higher high (HH)  +  RSI: lower high (LH)
    if len(price_highs) >= 2 and len(rsi_highs) >= 2 and best_confidence < 0.3:
        ph1, ph2 = price_highs[-2], price_highs[-1]
        rh1, rh2 = rsi_highs[-2], rsi_highs[-1]

        if (ph2[0] - ph1[0] >= min_swing_gap
            and ph2[1] > ph1[1]              # Price: higher high
            and rh2[1] < rh1[1]):            # RSI: lower high

            price_diff = abs((ph2[1] - ph1[1]) / (ph1[1] + 1e-9) * 100)
            rsi_diff = abs(rh2[1] - rh1[1])
            conf = min(0.8, (price_diff * 0.2 + rsi_diff * 0.015))

            if conf > best_confidence:
                result = {
                    "type": "regular_bearish",
                    "confidence": round(conf, 3),
                    "price_swing1": ph1, "price_swing2": ph2,
                    "rsi_swing1": rh1, "rsi_swing2": rh2,
                    "description": (
                        f"Regular Bearish: Price HH ({ph1[1]:.2f}→{ph2[1]:.2f}) "
                        f"+ RSI LH ({rh1[1]:.1f}→{rh2[1]:.1f}) → Weakening uptrend"
                    ),
                }

    return result


# ============================================================
# TECH SCANNER INTEGRATION — เพิ่มใน TechnicalScanner class
# ============================================================

# เพิ่มใน TechScanConfig:
#   # ── RSI Divergence params
#   div_swing_order:     int   = 5      # bars ซ้าย-ขวาสำหรับ swing detection
#   div_lookback_bars:   int   = 30     # ดูย้อนหลังกี่ bars
#   div_min_confidence:  float = 0.3    # confidence ขั้นต่ำ
#   div_rvol_min:        float = 1.0    # RVOL ขั้นต่ำ (ลดลงจาก rules อื่น)

def check_rsi_divergence(symbol, df, price, ind, config) -> Optional[dict]:
    """
    RSI Divergence Scanner Rule

    ตรวจจับ divergence ระหว่าง Price กับ RSI
    เน้น Hidden Divergence (Pullback opportunity)

    Args:
        symbol: ticker
        df: OHLCV DataFrame (15m bars)
        price: current price
        ind: indicator dict จาก _compute_indicators()
        config: TechScanConfig instance

    Returns:
        TechSignal dict หรือ None

    เรียกใช้ใน TechnicalScanner._scan_symbol():
        sig = check_rsi_divergence(symbol, df, price, ind, self.config)
        if sig:
            signals.append(sig)
    """
    if len(df) < 30:
        return None

    # ดึง RSI ขั้นต่ำ
    rvol_min = getattr(config, "div_rvol_min", 1.0)
    min_conf = getattr(config, "div_min_confidence", 0.3)
    swing_order = getattr(config, "div_swing_order", 5)
    lookback = getattr(config, "div_lookback_bars", 30)

    # RVOL check (ลดลงเพราะ divergence เป็นสัญญาณ leading)
    if ind.get("rvol", 0) < rvol_min:
        return None

    # คำนวณ RSI series
    c = df["close"].astype(float)
    delta = c.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + (gain / (loss.replace(0, np.nan)))))
    rsi = rsi.fillna(50)

    # Detect divergence
    div = detect_rsi_divergence(
        price_series=c,
        rsi_series=rsi,
        swing_order=swing_order,
        lookback_bars=lookback,
    )

    if div["type"] is None or div["confidence"] < min_conf:
        return None

    # Determine side
    if div["type"] in ("hidden_bullish", "regular_bullish"):
        side = "buy"
    else:
        side = "sell"

    # Urgency based on divergence type
    base_urgency = 65 if "hidden" in div["type"] else 55
    urgency = min(90, base_urgency + int(div["confidence"] * 25))

    # Build TechSignal-compatible dict
    # (ต้อง import TechSignal จาก technical_scanner.py เมื่อ integrate)
    return {
        "symbol": symbol,
        "rule_name": "RSI_DIVERGENCE",
        "side": side,
        "urgency": urgency,
        "detail": (
            f"{div['description']} | "
            f"RVOL={ind.get('rvol', 0):.1f}x conf={div['confidence']:.2f}"
        ),
        "metrics": {
            "divergence_type": div["type"],
            "confidence": div["confidence"],
            "rsi_current": float(rsi.iloc[-1]),
            "rvol": ind.get("rvol", 0),
            "price_swing": div.get("price_swing2", (0, 0)),
            "rsi_swing": div.get("rsi_swing2", (0, 0)),
        },
    }


# ============================================================
# INTEGRATION INSTRUCTIONS
# ============================================================

INTEGRATION_GUIDE = """
═══════════════════════════════════════════════
  RSI Divergence — Integration Guide
═══════════════════════════════════════════════

1. เพิ่ม params ใน TechScanConfig (technical_scanner.py):

   # ── RSI Divergence params
   div_swing_order:     int   = 5
   div_lookback_bars:   int   = 30
   div_min_confidence:  float = 0.3
   div_rvol_min:        float = 1.0

2. เพิ่ม "rsi_divergence" ใน active_rules default:

   active_rules: set = field(default_factory=lambda: {
       "vwap_pullback", "ml_breakout", "volume_spike",
       "rsi_divergence",   # ← เพิ่ม
   })

3. เพิ่ม env var ใน TechScanConfig.from_env():

   TECH_SCAN_DIV_CONFIDENCE=0.3
   TECH_SCAN_DIV_SWING_ORDER=5
   TECH_SCAN_DIV_LOOKBACK=30
   TECH_SCAN_DIV_RVOL_MIN=1.0

4. เพิ่ม method ใน TechnicalScanner:

   def _check_rsi_divergence(self, symbol, df, price, ind) -> Optional[TechSignal]:
       from patch_rsi_divergence import check_rsi_divergence
       result = check_rsi_divergence(symbol, df, price, ind, self.config)
       if result is None:
           return None
       return TechSignal(**result)

5. เรียกใน _scan_symbol():

   if "rsi_divergence" in self.config.active_rules:
       sig = self._check_rsi_divergence(symbol, df, price, ind)
       if sig:
           signals.append(sig)

6. เพิ่มใน env var documentation:

   TECH_SCAN_RULES=vwap_pullback,ml_breakout,volume_spike,rsi_divergence
"""


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("  RSI Divergence — Standalone Test")
    print("=" * 60)

    # ── Mock data: สร้าง Hidden Bullish Divergence
    print("\n[Test 1] Hidden Bullish Divergence (synthetic)")
    np.random.seed(42)
    n = 60

    # Price: downtrend → higher low (HL)
    price_base = np.concatenate([
        np.linspace(100, 95, 20),    # ลง
        np.linspace(95, 97, 10),     # ขึ้น
        np.linspace(97, 96, 15),     # ลง (higher low = 96 > 95)
        np.linspace(96, 99, 15),     # ขึ้น
    ])
    price = price_base + np.random.randn(n) * 0.3

    # RSI: will be calculated from price — needs to show lower low
    dates = pd.date_range("2026-03-20 09:30", periods=n, freq="15min")
    df_mock = pd.DataFrame({
        "open": price + 0.1,
        "high": price + abs(np.random.randn(n)) * 0.5,
        "low": price - abs(np.random.randn(n)) * 0.5,
        "close": price,
        "volume": np.random.randint(10000, 80000, n).astype(float),
    }, index=dates)

    # RSI จริง
    c = df_mock["close"]
    delta = c.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + (gain / (loss.replace(0, np.nan)))))
    rsi = rsi.fillna(50)

    div = detect_rsi_divergence(c, rsi, swing_order=3, lookback_bars=50)
    print(f"  Type:       {div['type']}")
    print(f"  Confidence: {div['confidence']}")
    if div.get("description"):
        print(f"  Detail:     {div['description']}")

    # ── Test 2: No divergence
    print("\n[Test 2] No divergence (flat)")
    flat_price = pd.Series(np.full(60, 100.0) + np.random.randn(60) * 0.1)
    flat_rsi = pd.Series(np.full(60, 50.0) + np.random.randn(60) * 2)
    div2 = detect_rsi_divergence(flat_price, flat_rsi)
    print(f"  Type:       {div2['type']}")
    print(f"  Confidence: {div2['confidence']}")

    # ── Test 3: Scanner integration mock
    print("\n[Test 3] Scanner integration mock")
    mock_ind = {
        "rvol": 1.5,
        "rsi": float(rsi.iloc[-1]),
        "price": float(c.iloc[-1]),
        "vwap": float(c.mean()),
    }

    class MockConfig:
        div_swing_order = 3
        div_lookback_bars = 50
        div_min_confidence = 0.1
        div_rvol_min = 1.0

    sig = check_rsi_divergence("TEST", df_mock, c.iloc[-1], mock_ind, MockConfig())
    if sig:
        print(f"  Signal: {sig['rule_name']} {sig['side'].upper()}")
        print(f"  Urgency: {sig['urgency']}")
        print(f"  Detail: {sig['detail']}")
    else:
        print("  No signal (expected for synthetic data)")

    # ── Test 4: Swing point detection
    print("\n[Test 4] Swing point detection")
    swings = find_swing_points(c, order=3, max_points=6)
    for idx, val, stype in swings:
        print(f"  [{stype:4s}] bar={idx:3d}  price=${val:.2f}")

    print(f"\n{INTEGRATION_GUIDE}")
    print("✅ Test complete")
