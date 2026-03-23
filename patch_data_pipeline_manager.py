"""
patch_data_pipeline_manager.py
==============================
แก้ไข data_pipeline_manager.py ให้รองรับ dry-run และทำงานตาม requirement:

Requirement:
  1. run_daily_pipeline: สร้าง Universe → สร้าง watchlist (watchlist.json)
  2. run_weekly_pipeline: อ่าน watchlist.json → เข้ากระบวนการ training LSTM
  3. สามารถ dry-run ได้ (ทุก function)

Changes needed (3 patches):
  PATCH 1: เพิ่ม dry_run parameter ใน run_daily_pipeline()
  PATCH 2: เพิ่ม dry_run parameter ใน run_weekly_pipeline()
  PATCH 3: แก้ __main__ block ให้รองรับ CLI arguments (--daily, --weekly, --dry-run)

วิธีใช้:
  python patch_data_pipeline_manager.py [path_to_data_pipeline_manager.py]

  ถ้าไม่ใส่ path จะสร้างไฟล์ใหม่ในชื่อ data_pipeline_manager_patched.py
"""

import re
import sys
from pathlib import Path


# ============================================================
# PATCH 1: run_daily_pipeline — เพิ่ม dry_run parameter
# ============================================================

PATCH_1_DESCRIPTION = """
PATCH 1: run_daily_pipeline() — เพิ่ม dry_run=False parameter

เปลี่ยนจาก:
    def run_daily_pipeline(
        self,
        watchlist: list[str] = None,
        auto_universe: bool = False,
        universe_kwargs: dict = None,
        days_1m: int = 59,
        days_5m: int = 90,
    ):

เป็น:
    def run_daily_pipeline(
        self,
        watchlist: list[str] = None,
        auto_universe: bool = False,
        universe_kwargs: dict = None,
        days_1m: int = 59,
        days_5m: int = 90,
        dry_run: bool = False,
    ):

และเพิ่ม dry_run logic ใน function body
"""

# Old function signature pattern
PATCH_1_OLD_SIG = """    def run_daily_pipeline(
        self,
        watchlist: list[str] = None,
        auto_universe: bool = False,
        universe_kwargs: dict = None,
        days_1m: int = 59,
        days_5m: int = 90,
    ):"""

PATCH_1_NEW_SIG = """    def run_daily_pipeline(
        self,
        watchlist: list[str] = None,
        auto_universe: bool = False,
        universe_kwargs: dict = None,
        days_1m: int = 59,
        days_5m: int = 90,
        dry_run: bool = False,
    ):"""

# ── เพิ่ม dry_run guard หลัง Step 0.5: Save Watchlist
# หา pattern ที่อยู่หลัง save_watchlist แล้วก่อน Phase 1
PATCH_1_OLD_PHASE1_MARKER = '''        tag = daily_tag()
        logger.info(f"[Daily Pipeline] {tag} | {len(watchlist)} symbols")

        # ════════════════════════════════════════
        # Phase 1 (I/O): Parallel download — ThreadPoolExecutor
        # ════════════════════════════════════════'''

PATCH_1_NEW_PHASE1_MARKER = '''        tag = daily_tag()
        logger.info(f"[Daily Pipeline] {tag} | {len(watchlist)} symbols")

        # ── Dry-run: แสดงแผนงานแล้วหยุด (ไม่ download / ไม่ train)
        if dry_run:
            logger.info(f"[DRY-RUN] Daily Pipeline Plan:")
            logger.info(f"  Tag:     {tag}")
            logger.info(f"  Symbols: {len(watchlist)} symbols")
            logger.info(f"  Top 10:  {watchlist[:10]}")
            logger.info(f"  Phase 1: จะดึง OHLCV 15m ({days_1m}d) + 1d ({days_5m}d)")
            logger.info(f"  Phase 2: จะคำนวณ features + เทรน LightGBM")
            logger.info(f"  Watchlist saved: watchlist.json (เพื่อให้ weekly pipeline อ่านได้)")
            logger.info(f"[DRY-RUN] จบ — ไม่มี download / training จริง")
            return

        # ════════════════════════════════════════
        # Phase 1 (I/O): Parallel download — ThreadPoolExecutor
        # ════════════════════════════════════════'''


# ============================================================
# PATCH 2: run_weekly_pipeline — เพิ่ม dry_run parameter
# ============================================================

PATCH_2_DESCRIPTION = """
PATCH 2: run_weekly_pipeline() — เพิ่ม dry_run=False parameter

เปลี่ยนจาก:
    def run_weekly_pipeline(
        self,
        watchlist: list[str] = None,
        days_5m: int = 90,
    ):

เป็น:
    def run_weekly_pipeline(
        self,
        watchlist: list[str] = None,
        days_5m: int = 90,
        dry_run: bool = False,
    ):

และเพิ่ม dry_run logic หลัง Step 0
"""

PATCH_2_OLD_SIG = """    def run_weekly_pipeline(
        self,
        watchlist: list[str] = None,
        days_5m: int = 90,
    ):"""

PATCH_2_NEW_SIG = """    def run_weekly_pipeline(
        self,
        watchlist: list[str] = None,
        days_5m: int = 90,
        dry_run: bool = False,
    ):"""

# ── เพิ่ม dry_run guard หลัง Step 0 (load watchlist) แล้วก่อน Phase 1
PATCH_2_OLD_PHASE1_MARKER = '''        tag = weekly_tag()
        logger.info(f"[Weekly Pipeline] {tag} | {len(watchlist)} symbols")

        # ════════════════════════════════════════
        # Phase 1 (I/O): Parallel download
        # ════════════════════════════════════════'''

PATCH_2_NEW_PHASE1_MARKER = '''        tag = weekly_tag()
        logger.info(f"[Weekly Pipeline] {tag} | {len(watchlist)} symbols")

        # ── Dry-run: แสดงแผนงานแล้วหยุด (ไม่ download / ไม่ train LSTM)
        if dry_run:
            logger.info(f"[DRY-RUN] Weekly Pipeline Plan:")
            logger.info(f"  Tag:     {tag}")
            logger.info(f"  Symbols: {len(watchlist)} symbols")
            logger.info(f"  Top 10:  {watchlist[:10]}")
            logger.info(f"  Source:  watchlist.json (สร้างโดย run_daily_pipeline)")
            logger.info(f"  Phase 1: จะดึง OHLCV 15m (120d) + 1d (120d)")
            logger.info(f"  Phase 2: จะคำนวณ LSTM sequences + เทรน LSTM บน GPU")
            logger.info(f"  Output:  models/{{SYMBOL}}/lstm_{{tag}}.pt + lstm_scaler_{{tag}}.pkl")
            logger.info(f"[DRY-RUN] จบ — ไม่มี download / LSTM training จริง")
            return

        # ════════════════════════════════════════
        # Phase 1 (I/O): Parallel download
        # ════════════════════════════════════════'''


# ============================================================
# PATCH 3: __main__ block — CLI arguments
# ============================================================

PATCH_3_DESCRIPTION = """
PATCH 3: แก้ __main__ block ให้รองรับ CLI arguments

เดิม: hardcoded mode, watchlist, manual comment/uncomment
ใหม่: argparse รองรับ --daily, --weekly, --dry-run, --watchlist
"""

# Pattern: ตั้งแต่ if __name__ == "__main__": จนจบไฟล์
PATCH_3_OLD_MAIN = r'if __name__ == "__main__":'  # ใช้เป็น marker

PATCH_3_NEW_MAIN = '''if __name__ == "__main__":
    import sys
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(
        description="Data Pipeline Manager — Universe → Features → Models",
        epilog="""
Examples:
  # Dry-run daily pipeline (สร้าง universe + watchlist แต่ไม่ download/train)
  python data_pipeline_manager.py --daily --dry-run

  # Dry-run weekly pipeline (อ่าน watchlist + แสดงแผน LSTM training)
  python data_pipeline_manager.py --weekly --dry-run

  # รัน daily pipeline จริง (auto universe + save watchlist + LightGBM)
  python data_pipeline_manager.py --daily --auto-universe

  # รัน weekly pipeline จริง (อ่าน watchlist.json → train LSTM)
  python data_pipeline_manager.py --weekly

  # รันเฉพาะบาง symbols
  python data_pipeline_manager.py --daily --watchlist NVDA,TSLA,META
  python data_pipeline_manager.py --weekly --watchlist NVDA,TSLA

  # Dry-run ทั้ง daily + weekly ต่อกัน
  python data_pipeline_manager.py --daily --weekly --dry-run
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--daily",  action="store_true",
                        help="รัน Daily Pipeline (LightGBM training)")
    parser.add_argument("--weekly", action="store_true",
                        help="รัน Weekly Pipeline (LSTM training)")
    parser.add_argument("--dry-run", action="store_true",
                        help="แสดงแผนงานโดยไม่รันจริง (ไม่ download / ไม่ train)")
    parser.add_argument("--auto-universe", action="store_true",
                        help="สร้าง Universe ใหม่ก่อนรัน daily (default: โหลดจาก cache)")
    parser.add_argument("--watchlist", type=str, default=None,
                        help="ระบุ symbols เอง (comma-separated, เช่น NVDA,TSLA,META)")
    parser.add_argument("--mode", choices=["local", "vps"], default="local",
                        help="โหมด: local (train) หรือ vps (inference)")
    parser.add_argument("--gdrive", type=str, default="./gdrive",
                        help="Path ไปยัง Google Drive mount (default: ./gdrive)")
    parser.add_argument("--cache", type=str, default="./cache",
                        help="Path สำหรับ local cache (default: ./cache)")

    args = parser.parse_args()

    # ── ถ้าไม่ระบุ --daily หรือ --weekly ให้แสดง help
    if not args.daily and not args.weekly:
        parser.print_help()
        print()
        print("❌ ต้องระบุอย่างน้อย --daily หรือ --weekly")
        sys.exit(1)

    # ── Parse watchlist
    watchlist = None
    if args.watchlist:
        watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]

    # ── สร้าง Manager
    print(f"{'='*60}")
    print(f"  DATA PIPELINE MANAGER")
    print(f"  mode={args.mode} | gdrive={args.gdrive}")
    print(f"  dry_run={args.dry_run}")
    if watchlist:
        print(f"  watchlist={watchlist}")
    print(f"{'='*60}")

    mgr = DataPipelineManager(
        mode        = args.mode,
        gdrive_root = args.gdrive,
        local_cache = args.cache,
    )

    # ── Daily Pipeline
    if args.daily:
        print(f"\\n{'─'*40}")
        print(f"  DAILY PIPELINE {'(DRY-RUN)' if args.dry_run else ''}")
        print(f"{'─'*40}")
        mgr.run_daily_pipeline(
            watchlist       = watchlist,
            auto_universe   = args.auto_universe,
            dry_run         = args.dry_run,
        )

    # ── Weekly Pipeline
    if args.weekly:
        print(f"\\n{'─'*40}")
        print(f"  WEEKLY PIPELINE {'(DRY-RUN)' if args.dry_run else ''}")
        print(f"{'─'*40}")
        mgr.run_weekly_pipeline(
            watchlist = watchlist,
            dry_run   = args.dry_run,
        )

    # ── Summary
    print(f"\\n{'='*60}")
    print(f"  Tags: daily={daily_tag()} | weekly={weekly_tag()}")
    print(f"  Done!")
    print(f"{'='*60}")
'''


# ============================================================
# APPLY PATCHES
# ============================================================

def apply_patches(source_path: str, output_path: str = None):
    """
    อ่าน data_pipeline_manager.py ต้นฉบับแล้ว apply 3 patches

    Args:
        source_path: path ไปยังไฟล์ต้นฉบับ
        output_path: path สำหรับ save ผลลัพธ์ (None = overwrite)
    """
    src = Path(source_path)
    if not src.exists():
        print(f"❌ ไม่พบไฟล์: {source_path}")
        return False

    content = src.read_text(encoding="utf-8")
    original_len = len(content)
    patches_applied = 0

    # ── PATCH 1: run_daily_pipeline signature + dry_run guard
    if PATCH_1_OLD_SIG in content:
        content = content.replace(PATCH_1_OLD_SIG, PATCH_1_NEW_SIG, 1)
        print("  ✅ PATCH 1a: run_daily_pipeline() — เพิ่ม dry_run parameter")
        patches_applied += 1
    else:
        # อาจเป็น format ต่างกันเล็กน้อย
        print("  ⚠️  PATCH 1a: ไม่พบ exact match — ต้อง apply manual")

    if PATCH_1_OLD_PHASE1_MARKER in content:
        content = content.replace(PATCH_1_OLD_PHASE1_MARKER, PATCH_1_NEW_PHASE1_MARKER, 1)
        print("  ✅ PATCH 1b: run_daily_pipeline() — เพิ่ม dry_run guard ก่อน Phase 1")
        patches_applied += 1
    else:
        print("  ⚠️  PATCH 1b: ไม่พบ Phase 1 marker — ต้อง apply manual")

    # ── PATCH 2: run_weekly_pipeline signature + dry_run guard
    if PATCH_2_OLD_SIG in content:
        content = content.replace(PATCH_2_OLD_SIG, PATCH_2_NEW_SIG, 1)
        print("  ✅ PATCH 2a: run_weekly_pipeline() — เพิ่ม dry_run parameter")
        patches_applied += 1
    else:
        print("  ⚠️  PATCH 2a: ไม่พบ exact match — ต้อง apply manual")

    if PATCH_2_OLD_PHASE1_MARKER in content:
        content = content.replace(PATCH_2_OLD_PHASE1_MARKER, PATCH_2_NEW_PHASE1_MARKER, 1)
        print("  ✅ PATCH 2b: run_weekly_pipeline() — เพิ่ม dry_run guard ก่อน Phase 1")
        patches_applied += 1
    else:
        print("  ⚠️  PATCH 2b: ไม่พบ Phase 1 marker — ต้อง apply manual")

    # ── PATCH 3: __main__ block
    # ตัดตั้งแต่ if __name__ == "__main__": จนจบ แล้วแทนที่ใหม่ทั้งหมด
    main_marker = 'if __name__ == "__main__":'
    if main_marker in content:
        idx = content.index(main_marker)
        content = content[:idx] + PATCH_3_NEW_MAIN
        print("  ✅ PATCH 3:  __main__ block — CLI arguments (argparse)")
        patches_applied += 1
    else:
        print("  ⚠️  PATCH 3: ไม่พบ __main__ block")

    # ── Save
    out = Path(output_path or source_path)
    out.write_text(content, encoding="utf-8")

    print(f"\n  Patches applied: {patches_applied}/5")
    print(f"  Original: {original_len:,} chars")
    print(f"  Patched:  {len(content):,} chars")
    print(f"  Saved to: {out}")
    return patches_applied > 0


# ============================================================
# MANUAL PATCH INSTRUCTIONS (ถ้า auto-patch ไม่ match)
# ============================================================

MANUAL_INSTRUCTIONS = """
════════════════════════════════════════════════════════════
MANUAL PATCH INSTRUCTIONS
════════════════════════════════════════════════════════════

ถ้า auto-patch ไม่สามารถ match exact string ได้ ให้ทำ manual ดังนี้:

━━━ PATCH 1: run_daily_pipeline ━━━

1a) เพิ่ม parameter ใน function signature:
    หาบรรทัด: def run_daily_pipeline(
    เพิ่ม:     dry_run: bool = False,
    (ใส่หลัง days_5m: int = 90, ก่อนปิดวงเล็บ)

1b) เพิ่ม dry_run guard:
    หาบรรทัด:   tag = daily_tag()
    เพิ่มก่อน Phase 1 comment:
        if dry_run:
            logger.info(f"[DRY-RUN] Daily Pipeline Plan:")
            logger.info(f"  Tag: {tag} | Symbols: {len(watchlist)}")
            logger.info(f"  Top 10: {watchlist[:10]}")
            logger.info(f"[DRY-RUN] จบ — ไม่มี download/training จริง")
            return

━━━ PATCH 2: run_weekly_pipeline ━━━

2a) เพิ่ม parameter ใน function signature:
    หาบรรทัด: def run_weekly_pipeline(
    เพิ่ม:     dry_run: bool = False,
    (ใส่หลัง days_5m: int = 90, ก่อนปิดวงเล็บ)

2b) เพิ่ม dry_run guard:
    หาบรรทัด:   tag = weekly_tag()
    เพิ่มก่อน Phase 1 comment:
        if dry_run:
            logger.info(f"[DRY-RUN] Weekly Pipeline Plan:")
            logger.info(f"  Tag: {tag} | Symbols: {len(watchlist)}")
            logger.info(f"  Source: watchlist.json")
            logger.info(f"[DRY-RUN] จบ — ไม่มี LSTM training จริง")
            return

━━━ PATCH 3: __main__ block ━━━

    แทนที่ทั้ง if __name__ == "__main__": block ด้วย version ใหม่
    (ดู PATCH_3_NEW_MAIN ในไฟล์นี้)

════════════════════════════════════════════════════════════
"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python patch_data_pipeline_manager.py <path_to_source>")
        print()
        print("จะ apply 3 patches:")
        print("  1. run_daily_pipeline + dry_run")
        print("  2. run_weekly_pipeline + dry_run")
        print("  3. __main__ CLI arguments")
        print()
        print(MANUAL_INSTRUCTIONS)
        sys.exit(0)

    source = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"{'='*60}")
    print(f"  Patching: {source}")
    print(f"{'='*60}")

    success = apply_patches(source, output)

    if success:
        print("\n✅ Patching สำเร็จ!")
    else:
        print("\n❌ Patching ไม่สำเร็จ")
        print(MANUAL_INSTRUCTIONS)
