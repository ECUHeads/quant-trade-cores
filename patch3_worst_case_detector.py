"""
PATCH 3: worst_case_detector.py — Robustness Fixes
=====================================================

โค้ดหลักทำงานถูกต้องแล้ว แต่มี edge cases ที่ต้องแก้:

  Patch 3A: WorstCaseFeatures.compute() — DatetimeIndex safety
  Patch 3B: WorstCaseGate.evaluate() — df_15m column validation
  Patch 3C: เพิ่ม __main__ block สำหรับ standalone test
"""


# ============================================================
# PATCH 3A: DatetimeIndex safety
# ============================================================

PATCH_3A_DESCRIPTION = """
WorstCaseFeatures.compute() ใช้ df.index.date สำหรับ VWAP calculation
และ df.index.hour สำหรับ minutes_since_930

ปัญหา: ถ้า DataFrame มี RangeIndex (ไม่ใช่ DatetimeIndex) จะ crash

แก้ไข: เพิ่ม guard check ก่อน groupby
"""

PATCH_3A_ANCHOR = """        # 12. Price vs VWAP distance (normalized)
        typical = (h + l + c) / 3
        dates   = df.index.date"""

PATCH_3A_REPLACEMENT = """        # 12. Price vs VWAP distance (normalized)
        typical = (h + l + c) / 3
        # Guard: ต้องมี DatetimeIndex ถึงจะ groupby date ได้
        if hasattr(df.index, 'date'):
            dates = df.index.date
        else:
            # Fallback: ใช้ทั้ง DataFrame เป็น 1 group
            dates = pd.Series(0, index=df.index)"""


# ============================================================
# PATCH 3B: Column validation ใน evaluate()
# ============================================================

PATCH_3B_DESCRIPTION = """
WorstCaseGate.evaluate() รับ df_15m แต่ไม่ validate ว่ามี columns
ที่ต้องใช้ (open, high, low, close, volume) หรือไม่

ถ้า DataFrame มาจาก API ที่ใช้ column names ต่างกัน
(เช่น 'Open' vs 'open', 'Close' vs 'close') จะ KeyError

แก้ไข: เพิ่ม column normalization ใน evaluate()
"""

PATCH_3B_ANCHOR = """        # ── Compute features
        try:
            feat_df = self.fe.compute(df_15m)"""

PATCH_3B_REPLACEMENT = """        # ── Normalize column names (handle yfinance / MT5 / Alpaca variants)
        col_map = {}
        for col in df_15m.columns:
            lower = col.lower()
            if lower in ("open", "high", "low", "close", "volume"):
                col_map[col] = lower
        if col_map:
            df_15m = df_15m.rename(columns=col_map)

        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df_15m.columns)
        if missing:
            logger.warning(f"[WC-Gate] Missing columns {missing} for {symbol}")
            return WorstCaseVerdict(is_danger=False, danger_score=0.0,
                                    conditions={"note": f"missing columns: {missing}"})

        # ── Compute features
        try:
            feat_df = self.fe.compute(df_15m)"""


# ============================================================
# PATCH 3C: __main__ block สำหรับ standalone test
# ============================================================

STANDALONE_TEST = '''

# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    """
    Standalone test: ทดสอบ worst_case_detector แบบไม่ต้อง main.py

    Usage:
      python models/worst_case_detector.py                 # test ด้วย NVDA
      python models/worst_case_detector.py --symbol TSLA   # test ด้วย symbol อื่น
      python models/worst_case_detector.py --train          # train + predict
      python models/worst_case_detector.py --dry-run        # label only, no train
    """
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    )

    parser = argparse.ArgumentParser(description="Worst Case Detector — Standalone Test")
    parser.add_argument("--symbol", default="NVDA", help="Symbol to test")
    parser.add_argument("--train", action="store_true", help="Train model + predict")
    parser.add_argument("--dry-run", action="store_true", help="Label only, no train")
    args = parser.parse_args()

    SYMBOL = args.symbol.upper()

    print(f"{'='*60}")
    print(f"  WORST CASE DETECTOR — Standalone Test")
    print(f"  Symbol: {SYMBOL}")
    print(f"{'='*60}")

    # ── Step 1: Download data
    print(f"\\n[1] Downloading {SYMBOL} 15m data...")
    try:
        import yfinance as yf
        df = yf.download(SYMBOL, period="60d", interval="15m", progress=False)
        if df.empty:
            print(f"  ❌ No data for {SYMBOL}")
            sys.exit(1)

        # Flatten MultiIndex columns if present
        if hasattr(df.columns, 'levels') and len(df.columns.levels) > 1:
            df.columns = [col[0].lower() for col in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]

        print(f"  ✅ {len(df)} bars | {df.index[0]} → {df.index[-1]}")
    except Exception as e:
        print(f"  ❌ Download error: {e}")
        sys.exit(1)

    # ── Step 2: Generate labels
    print(f"\\n[2] Generating Worst Case labels (look-ahead {WC_LOOKFORWARD_BARS} bars)...")
    labeler = WorstCaseLabeler()
    labeled = labeler.generate(df)

    y = labeled["wc_target"]
    valid = y.notna()
    total = valid.sum()
    n_wc = y[valid].sum() if total > 0 else 0
    pct = n_wc / total * 100 if total > 0 else 0

    print(f"  Total valid bars: {int(total)}")
    print(f"  Worst Case bars:  {int(n_wc)} ({pct:.1f}%)")
    print(f"    Condition 1 (Whipsaw): {int(labeled['wc_cond1'].sum())}")
    print(f"    Condition 2 (Chop):    {int(labeled['wc_cond2'].sum())}")
    print(f"    Condition 3 (MAE):     {int(labeled['wc_cond3'].sum())}")

    if args.dry_run:
        print(f"\\n  [dry-run] Label only — no train")
        sys.exit(0)

    # ── Step 3: Compute features
    print(f"\\n[3] Computing WC-specific features...")
    fe = WorstCaseFeatures()
    feat_df = fe.compute(df)
    print(f"  ✅ {feat_df.shape[1]} features × {feat_df.shape[0]} bars")

    # ── Step 4: Train
    if args.train or True:  # default: always train in test
        print(f"\\n[4] Training WC model...")
        gate = WorstCaseGate(model_dir="./models")
        auc = gate.train_symbol(SYMBOL, df)

        if auc > 0:
            print(f"  ✅ AUC = {auc:.4f}")

            # ── Step 5: Predict current bar
            print(f"\\n[5] Predicting current bar...")
            verdict = gate.evaluate(symbol=SYMBOL, df_15m=df)
            print(f"  Danger Score: {verdict.danger_score:.4f}")
            print(f"  Is Danger:    {verdict.is_danger}")
            print(f"  Top Features: {verdict.top_features}")
            print(f"  Latency:      {verdict.latency_ms:.1f}ms")

            # ── Stats
            print(f"\\n[Stats]")
            stats = gate.get_stats()
            for k, v in stats.items():
                print(f"  {k}: {v}")
        else:
            print(f"  ❌ Training failed (AUC = 0)")

    print(f"\\n{'='*60}")
    print(f"  Test complete!")
    print(f"{'='*60}")
'''

PATCH_3C_DESCRIPTION = f"""
เพิ่ม block นี้ที่ท้ายไฟล์ worst_case_detector.py:

{STANDALONE_TEST}
"""


# ============================================================
# SUMMARY
# ============================================================

def print_patches():
    print("=" * 60)
    print("  PATCH 3: worst_case_detector.py — Robustness Fixes")
    print("=" * 60)

    print("""
  Patch 3A: DatetimeIndex safety
    → ป้องกัน crash เมื่อ df ไม่มี DatetimeIndex
    → เกิดได้กับ data จาก MT5 Proxy หรือ custom CSV

  Patch 3B: Column name normalization
    → รองรับ yfinance ('Open') / MT5 ('open') / Alpaca ('open')
    → ป้องกัน KeyError จาก column mismatch

  Patch 3C: Standalone test (__main__ block)
    → ทดสอบ WC detector แยกจาก pipeline
    → รองรับ --symbol, --train, --dry-run flags
    → ใช้ validate ว่า model ทำงานก่อน integrate
    """)


if __name__ == "__main__":
    print_patches()
