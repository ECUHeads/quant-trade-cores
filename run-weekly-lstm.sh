#!/bin/bash
# ============================================================
# run-weekly-lstm.sh
# ============================================================
# รัน Weekly Pipeline — อ่าน watchlist.json → เทรน LSTM
#
# Flow:
#   1. load_watchlist()                → อ่านจาก watchlist.json
#      (สร้างโดย run_daily_pipeline → save_watchlist)
#   2. ดึง OHLCV 15m + 1d (120 วัน)   → Parquet
#   3. คำนวณ LSTM sequences            → Parquet
#   4. เทรน LSTM บน GPU               → .pt + scaler .pkl
#
# Cron: 07:00 AM ET วันอาทิตย์ (19:00 น. ไทย)
# ============================================================

set -e

echo "══════════════════════════════════════"
echo "  Weekly Pipeline — LSTM Training"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "══════════════════════════════════════"

# ── ตรวจว่ามี watchlist.json หรือยัง
if [ ! -f ./cache/watchlist.json ] && [ ! -f ./gdrive/watchlist.json ]; then
    echo "⚠️  ไม่พบ watchlist.json"
    echo "   ต้องรัน Daily Pipeline (run-batch-level-01.sh) ก่อน"
    echo "   เพื่อสร้าง Universe → Watchlist"
    exit 1
fi

python3 data_pipeline_manager.py \
    --weekly \
    --gdrive ./gdrive \
    --cache ./cache

echo ""
echo "✅ Weekly Pipeline เสร็จสิ้น"
echo "   LSTM models (.pt) พร้อมใช้งาน"
