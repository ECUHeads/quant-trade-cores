"""
patch_volume_profile.py
========================
Patch สำหรับ models/technical_ml_analyzer.py

เพิ่ม Volume Profile features ใน FeatureEngineer:
  - POC (Point of Control) — ราคาที่มี Volume สูงสุด
  - VAH (Value Area High) — ขอบบนของ Value Area (70% ของ Volume)
  - VAL (Value Area Low) — ขอบล่างของ Value Area
  - dist_from_poc — ราคาปัจจุบันห่างจาก POC กี่ %
  - price_in_value_area — ราคาอยู่ใน Value Area หรือไม่ (0/1)
  - va_width_pct — ความกว้างของ Value Area เป็น % ของราคา

ตามคำแนะนำจากเอกสาร Day Trading มืออาชีพ:
  "Volume Profile จะพล็อตอยู่แกน Y (ราคา) เพื่อบอกว่า
   ช่วงราคาไหนมีการซื้อขายหนาแน่นที่สุด (POC)
   มือโปรใช้หาแนวรับ-แนวต้านที่มีนัยสำคัญจริงๆ
   เพราะมันคือโซนที่สถาบันหรือรายใหญ่สะสมของ"

Integration:
  1. เพิ่ม method _volume_profile_features() ใน class FeatureEngineer
  2. เรียกใน compute() method
  3. เพิ่ม columns ใน compute_vectorized()

Apply:
  python patch_volume_profile.py models/technical_ml_analyzer.py
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger("VolumeProfile")


# ============================================================
# VOLUME PROFILE CALCULATOR — เพิ่มใน FeatureEngineer class
# ============================================================

def compute_volume_profile(
    df: pd.DataFrame,
    n_bins: int = 50,
    value_area_pct: float = 0.70,
) -> dict:
    """
    คำนวณ Volume Profile จาก OHLCV DataFrame

    Volume Profile คือ histogram ของ volume ตามช่วงราคา:
      - แบ่งช่วงราคา (price_low → price_high) เป็น n_bins ช่อง
      - นับ volume ที่เกิดในแต่ละช่อง
      - POC = ช่องที่มี volume สูงสุด
      - Value Area = ช่วงราคาที่มี 70% ของ total volume

    Args:
        df: OHLCV DataFrame (ต้องมี high, low, close, volume)
        n_bins: จำนวนช่องราคา (50 bins default)
        value_area_pct: % ของ total volume ที่นับเป็น Value Area (0.70 = 70%)

    Returns:
        dict: {
            "poc": ราคา POC,
            "vah": Value Area High,
            "val": Value Area Low,
            "va_width": ความกว้าง Value Area,
            "profile": array ของ (price_center, volume) ทุก bin
        }
    """
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    v = df["volume"].astype(float)

    price_low = l.min()
    price_high = h.max()

    if price_high <= price_low or price_high == 0:
        mid = c.iloc[-1] if len(c) > 0 else 0
        return {
            "poc": mid, "vah": mid, "val": mid,
            "va_width": 0.0, "profile": [],
        }

    # สร้าง price bins
    bin_edges = np.linspace(price_low, price_high, n_bins + 1)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    bin_volumes = np.zeros(n_bins)

    # กระจาย volume ของแต่ละ bar ไปยัง bins ที่ราคาครอบคลุม
    # แต่ละ bar มี range [low, high] → volume กระจายเท่าๆ กันในช่วงนั้น
    for i in range(len(df)):
        bar_low = l.iloc[i]
        bar_high = h.iloc[i]
        bar_vol = v.iloc[i]

        if bar_vol <= 0 or bar_high <= bar_low:
            continue

        # หา bins ที่ bar นี้ครอบคลุม
        low_idx = np.searchsorted(bin_edges, bar_low, side='right') - 1
        high_idx = np.searchsorted(bin_edges, bar_high, side='left')

        low_idx = max(0, low_idx)
        high_idx = min(n_bins, high_idx)

        if high_idx <= low_idx:
            high_idx = low_idx + 1

        n_covered = high_idx - low_idx
        if n_covered > 0:
            vol_per_bin = bar_vol / n_covered
            bin_volumes[low_idx:high_idx] += vol_per_bin

    # POC = bin ที่มี volume สูงสุด
    poc_idx = np.argmax(bin_volumes)
    poc = float(bin_centers[poc_idx])

    # Value Area: เริ่มจาก POC แล้วขยายออกทั้ง 2 ข้าง
    # จนกว่า volume รวมจะ ≥ value_area_pct ของ total
    total_vol = bin_volumes.sum()
    if total_vol <= 0:
        return {
            "poc": poc, "vah": poc, "val": poc,
            "va_width": 0.0, "profile": list(zip(bin_centers.tolist(), bin_volumes.tolist())),
        }

    target_vol = total_vol * value_area_pct
    accumulated = bin_volumes[poc_idx]
    va_low_idx = poc_idx
    va_high_idx = poc_idx

    while accumulated < target_vol:
        # ขยายทีละข้าง: เลือกข้างที่มี volume มากกว่า
        expand_low = bin_volumes[va_low_idx - 1] if va_low_idx > 0 else 0
        expand_high = bin_volumes[va_high_idx + 1] if va_high_idx < n_bins - 1 else 0

        if expand_low == 0 and expand_high == 0:
            break

        if expand_low >= expand_high and va_low_idx > 0:
            va_low_idx -= 1
            accumulated += bin_volumes[va_low_idx]
        elif va_high_idx < n_bins - 1:
            va_high_idx += 1
            accumulated += bin_volumes[va_high_idx]
        elif va_low_idx > 0:
            va_low_idx -= 1
            accumulated += bin_volumes[va_low_idx]
        else:
            break

    val = float(bin_edges[va_low_idx])       # Value Area Low
    vah = float(bin_edges[va_high_idx + 1])  # Value Area High

    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "va_width": vah - val,
        "profile": list(zip(bin_centers.tolist(), bin_volumes.tolist())),
    }


def volume_profile_features(df1: pd.DataFrame, lookback_bars: int = 60) -> dict:
    """
    คำนวณ Volume Profile features สำหรับ bar ล่าสุด

    ใช้ข้อมูลย้อนหลัง lookback_bars bars เพื่อสร้าง profile
    แล้วคำนวณ features เทียบกับราคาปัจจุบัน

    Args:
        df1: OHLCV DataFrame (15m bars)
        lookback_bars: จำนวน bars ย้อนหลังสำหรับสร้าง profile

    Returns:
        dict ของ features:
          - dist_from_poc: ราคาห่างจาก POC กี่ % (+ = above, - = below)
          - price_in_value_area: 1.0 ถ้าราคาอยู่ใน VA, 0.0 ถ้าไม่
          - va_width_pct: ความกว้าง VA เป็น % ของ POC
          - dist_from_vah: ราคาห่างจาก VAH กี่ %
          - dist_from_val: ราคาห่างจาก VAL กี่ %
          - poc_price: ราคา POC จริง (สำหรับ log/debug)
    """
    c = df1["close"].astype(float)
    current_price = c.iloc[-1]

    # ใช้ข้อมูลย้อนหลัง (ไม่รวม bar สุดท้าย เพื่อไม่ให้ look-ahead)
    start = max(0, len(df1) - lookback_bars - 1)
    end = len(df1) - 1  # ไม่รวม bar สุดท้าย
    profile_df = df1.iloc[start:end]

    if len(profile_df) < 10:
        return {
            "dist_from_poc": 0.0,
            "price_in_value_area": 0.5,
            "va_width_pct": 0.0,
            "dist_from_vah": 0.0,
            "dist_from_val": 0.0,
            "poc_price": float(current_price),
        }

    vp = compute_volume_profile(profile_df, n_bins=40)

    poc = vp["poc"]
    vah = vp["vah"]
    val = vp["val"]

    # Distance from POC (% of price)
    dist_poc = (current_price - poc) / (poc + 1e-9) * 100

    # Is price inside Value Area?
    in_va = 1.0 if val <= current_price <= vah else 0.0

    # VA width as % of POC
    va_width_pct = vp["va_width"] / (poc + 1e-9) * 100

    # Distance from boundaries
    dist_vah = (current_price - vah) / (vah + 1e-9) * 100
    dist_val = (current_price - val) / (val + 1e-9) * 100

    return {
        "dist_from_poc": round(float(np.nan_to_num(dist_poc)), 4),
        "price_in_value_area": float(in_va),
        "va_width_pct": round(float(np.nan_to_num(va_width_pct)), 4),
        "dist_from_vah": round(float(np.nan_to_num(dist_vah)), 4),
        "dist_from_val": round(float(np.nan_to_num(dist_val)), 4),
        "poc_price": round(float(poc), 2),
    }


def volume_profile_vectorized(df1: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    """
    Vectorized Volume Profile features สำหรับทุก bar

    คำนวณ rolling Volume Profile ทุก bar (ใช้ lookback bars ก่อนหน้า)
    สำหรับใช้ใน compute_vectorized()

    Performance: O(n × lookback × n_bins) — ช้ากว่า features อื่น
    แต่ยอมได้เพราะ n_bins=30 และ lookback=60

    Returns: DataFrame ที่มี columns:
      dist_from_poc, price_in_value_area, va_width_pct,
      dist_from_vah, dist_from_val
    """
    c = df1["close"].astype(float)
    n = len(df1)

    # Pre-allocate
    dist_poc = np.zeros(n)
    in_va = np.full(n, 0.5)
    va_width = np.zeros(n)
    dist_vah = np.zeros(n)
    dist_val = np.zeros(n)

    # Skip first `lookback` bars (warmup period)
    for i in range(lookback, n):
        window = df1.iloc[max(0, i - lookback):i]

        if len(window) < 10:
            continue

        try:
            vp = compute_volume_profile(window, n_bins=30)
            poc = vp["poc"]
            _vah = vp["vah"]
            _val = vp["val"]
            price = c.iloc[i]

            dist_poc[i] = (price - poc) / (poc + 1e-9) * 100
            in_va[i] = 1.0 if _val <= price <= _vah else 0.0
            va_width[i] = vp["va_width"] / (poc + 1e-9) * 100
            dist_vah[i] = (price - _vah) / (_vah + 1e-9) * 100
            dist_val[i] = (price - _val) / (_val + 1e-9) * 100
        except Exception:
            continue

    return pd.DataFrame({
        "dist_from_poc": dist_poc,
        "price_in_value_area": in_va,
        "va_width_pct": va_width,
        "dist_from_vah": dist_vah,
        "dist_from_val": dist_val,
    }, index=df1.index)


# ============================================================
# INTEGRATION INSTRUCTIONS
# ============================================================

INTEGRATION_GUIDE = """
═══════════════════════════════════════════════
  Volume Profile — Integration Guide
═══════════════════════════════════════════════

1. เพิ่มใน FeatureEngineer._price_features() (หรือสร้าง method ใหม่):

   def _volume_profile_features(self, df1: pd.DataFrame) -> dict:
       from patch_volume_profile import volume_profile_features
       return volume_profile_features(df1, lookback_bars=60)

2. เรียกใน compute():

   def compute(self, df1, df5, catalyst_type="OTHER", urgency_score=50):
       feats = {}
       feats.update(self._price_features(df1, df5))
       feats.update(self._momentum_features(df1))
       feats.update(self._volatility_features(df1))
       feats.update(self._volume_profile_features(df1))  # ← เพิ่มบรรทัดนี้
       ...

3. เพิ่มใน compute_vectorized():

   def compute_vectorized(self, df1, df5, ...):
       ...
       # Volume Profile (rolling)
       from patch_volume_profile import volume_profile_vectorized
       vp_df = volume_profile_vectorized(df1, lookback=60)
       for col in vp_df.columns:
           feat_df[col] = vp_df[col]
       ...

4. Features ใหม่ที่ LightGBM จะเรียนรู้:

   - dist_from_poc:        ราคาห่าง POC กี่ % → ยิ่งไกล ยิ่งมีโอกาส mean revert
   - price_in_value_area:  ราคาอยู่ใน VA ไหม → ใน VA = consolidation zone
   - va_width_pct:         VA กว้างไหม → กว้าง = ตลาด choppy
   - dist_from_vah:        ราคาห่าง VAH → ใกล้ = resistance zone
   - dist_from_val:        ราคาห่าง VAL → ใกล้ = support zone
"""


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("  Volume Profile — Standalone Test")
    print("=" * 60)

    # สร้าง mock data
    np.random.seed(42)
    n = 200
    dates = pd.date_range("2026-03-20 09:30", periods=n, freq="15min")
    price = 180 + np.cumsum(np.random.randn(n) * 0.5)
    volume = np.random.randint(10000, 100000, n).astype(float)

    df_mock = pd.DataFrame({
        "open": price + np.random.randn(n) * 0.2,
        "high": price + abs(np.random.randn(n)) * 0.5,
        "low": price - abs(np.random.randn(n)) * 0.5,
        "close": price,
        "volume": volume,
    }, index=dates)

    # Test 1: Single-point Volume Profile
    print("\n[Test 1] Single-point features")
    feats = volume_profile_features(df_mock, lookback_bars=60)
    print(f"  POC:              ${feats['poc_price']:.2f}")
    print(f"  dist_from_poc:    {feats['dist_from_poc']:.2f}%")
    print(f"  in_value_area:    {feats['price_in_value_area']}")
    print(f"  va_width_pct:     {feats['va_width_pct']:.2f}%")
    print(f"  dist_from_vah:    {feats['dist_from_vah']:.2f}%")
    print(f"  dist_from_val:    {feats['dist_from_val']:.2f}%")

    # Test 2: Vectorized
    print("\n[Test 2] Vectorized (200 bars)")
    import time
    t0 = time.time()
    vp_df = volume_profile_vectorized(df_mock, lookback=60)
    elapsed = time.time() - t0
    print(f"  Shape: {vp_df.shape}")
    print(f"  Time:  {elapsed:.3f}s")
    print(f"  Last row:")
    for col in vp_df.columns:
        print(f"    {col:25s} = {vp_df[col].iloc[-1]:.4f}")

    # Test 3: Raw profile
    print("\n[Test 3] Raw Volume Profile")
    vp = compute_volume_profile(df_mock.iloc[-60:], n_bins=10)
    print(f"  POC:  ${vp['poc']:.2f}")
    print(f"  VAH:  ${vp['vah']:.2f}")
    print(f"  VAL:  ${vp['val']:.2f}")
    print(f"  Width: ${vp['va_width']:.2f}")
    print(f"  Bins:  {len(vp['profile'])}")

    print(f"\n{INTEGRATION_GUIDE}")
    print("✅ Test complete")
