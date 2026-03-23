#!/bin/bash
# ============================================================
# test_step0.sh — Quick Test (Dry-Run)
# ============================================================
# ทดสอบว่า pipeline โหลดได้ครบ + watchlist ถูกต้อง
# ไม่ download ข้อมูล / ไม่ train จริง
# ============================================================

set -e

echo "═══════════════════════════════════════════"
echo "  Step 0: Quick Smoke Test (Dry-Run)"
echo "═══════════════════════════════════════════"

# ── Test 1: Daily dry-run (universe → watchlist)
echo ""
echo "── Test 1: Daily Pipeline (dry-run) ──"
python3 data_pipeline_manager.py --daily --auto-universe --dry-run

# ── Test 2: Weekly dry-run (watchlist → LSTM plan)
echo ""
echo "── Test 2: Weekly Pipeline (dry-run) ──"
python3 data_pipeline_manager.py --weekly --dry-run

# ── Test 3: Specific symbols dry-run
echo ""
echo "── Test 3: Specific Symbols (dry-run) ──"
python3 data_pipeline_manager.py --daily --weekly --dry-run --watchlist NVDA,TSLA

echo ""
echo "✅ All smoke tests passed!"
