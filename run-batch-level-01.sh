#!/bin/bash
# ============================================================
# run-batch-level-01.sh
# ============================================================
# รัน Daily Pipeline จริง — สร้าง Universe + Watchlist + LightGBM
#
# Flow:
#   1. generate_ttp_universe() → universe.json
#   2. save_watchlist()        → watchlist.json (สำหรับ weekly)
#   3. ดึง OHLCV 15m + 1d     → Parquet
#   4. คำนวณ features          → Parquet
#   5. เทรน LightGBM           → .pkl
#
# Cron: 08:00 AM ET ทุกวัน (20:00 น. ไทย)
# ============================================================

set -e

echo "══════════════════════════════════════"
echo "  Daily Pipeline — Level 01"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "══════════════════════════════════════"

python3 data_pipeline_manager.py \
    --daily \
    --auto-universe \
    --gdrive ./gdrive \
    --cache ./cache

echo ""
echo "✅ Daily Pipeline เสร็จสิ้น"
echo "   watchlist.json พร้อมให้ Weekly Pipeline อ่านแล้ว"
