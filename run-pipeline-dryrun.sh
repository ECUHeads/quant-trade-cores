#!/bin/bash
# ============================================================
# run-pipeline-dryrun.sh
# ============================================================
# Dry-run ทั้ง Daily + Weekly pipeline
# แสดงแผนงานโดยไม่ download ข้อมูล / ไม่ train model จริง
#
# Usage:
#   chmod +x run-pipeline-dryrun.sh
#   ./run-pipeline-dryrun.sh              # dry-run ทั้งคู่
#   ./run-pipeline-dryrun.sh daily        # dry-run เฉพาะ daily
#   ./run-pipeline-dryrun.sh weekly       # dry-run เฉพาะ weekly
#   ./run-pipeline-dryrun.sh daily NVDA,TSLA   # dry-run daily เฉพาะ 2 symbols
# ============================================================

set -e

PIPELINE="${1:-both}"
WATCHLIST="${2:-}"

echo "══════════════════════════════════════"
echo "  Pipeline Dry-Run"
echo "  Mode: ${PIPELINE}"
if [ -n "$WATCHLIST" ]; then
    echo "  Watchlist: ${WATCHLIST}"
fi
echo "══════════════════════════════════════"

WATCHLIST_ARG=""
if [ -n "$WATCHLIST" ]; then
    WATCHLIST_ARG="--watchlist ${WATCHLIST}"
fi

case "$PIPELINE" in
    daily)
        python3 data_pipeline_manager.py --daily --auto-universe --dry-run ${WATCHLIST_ARG}
        ;;
    weekly)
        python3 data_pipeline_manager.py --weekly --dry-run ${WATCHLIST_ARG}
        ;;
    both)
        python3 data_pipeline_manager.py --daily --weekly --auto-universe --dry-run ${WATCHLIST_ARG}
        ;;
    *)
        echo "Usage: $0 [daily|weekly|both] [SYMBOLS]"
        echo "  $0                    # dry-run ทั้ง daily + weekly"
        echo "  $0 daily              # dry-run เฉพาะ daily"
        echo "  $0 weekly             # dry-run เฉพาะ weekly"
        echo "  $0 daily NVDA,TSLA    # dry-run daily เฉพาะ 2 symbols"
        exit 1
        ;;
esac

echo ""
echo "✅ Dry-run เสร็จสิ้น — ไม่มีข้อมูลจริงถูกใช้"
