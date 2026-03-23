# Pipeline Patch — Deployment Guide
## data_pipeline_manager.py + Dry-Run Support

---

## สรุปสิ่งที่แก้ไข

### Patch 1: `run_daily_pipeline()` — เพิ่ม `dry_run` parameter

```python
# เดิม:
def run_daily_pipeline(self, watchlist=None, auto_universe=False,
                       universe_kwargs=None, days_1m=59, days_5m=90):

# ใหม่:
def run_daily_pipeline(self, watchlist=None, auto_universe=False,
                       universe_kwargs=None, days_1m=59, days_5m=90,
                       dry_run=False):
```

เพิ่ม dry_run guard **หลัง** Step 0.5 (save_watchlist) แต่ **ก่อน** Phase 1 (download):

```python
# ── Dry-run: แสดงแผนงานแล้วหยุด
if dry_run:
    logger.info(f"[DRY-RUN] Daily Pipeline Plan:")
    logger.info(f"  Tag:     {tag}")
    logger.info(f"  Symbols: {len(watchlist)} symbols")
    logger.info(f"  Top 10:  {watchlist[:10]}")
    logger.info(f"  Phase 1: จะดึง OHLCV 15m ({days_1m}d) + 1d ({days_5m}d)")
    logger.info(f"  Phase 2: จะคำนวณ features + เทรน LightGBM")
    logger.info(f"  Watchlist saved: watchlist.json")
    logger.info(f"[DRY-RUN] จบ — ไม่มี download / training จริง")
    return
```

> **หมายเหตุ**: dry_run จะยัง save_watchlist() จริง เพราะ watchlist.json
> ไม่ต้องใช้ network เลย — มันแค่ save list ของ symbols ลง JSON
> ซึ่งจะทำให้ weekly pipeline อ่านได้ (ตาม requirement)

---

### Patch 2: `run_weekly_pipeline()` — เพิ่ม `dry_run` parameter

```python
# เดิม:
def run_weekly_pipeline(self, watchlist=None, days_5m=90):

# ใหม่:
def run_weekly_pipeline(self, watchlist=None, days_5m=90, dry_run=False):
```

เพิ่ม dry_run guard **หลัง** Step 0 (load_watchlist) แต่ **ก่อน** Phase 1:

```python
if dry_run:
    logger.info(f"[DRY-RUN] Weekly Pipeline Plan:")
    logger.info(f"  Tag:     {tag}")
    logger.info(f"  Symbols: {len(watchlist)} symbols")
    logger.info(f"  Source:  watchlist.json (สร้างโดย run_daily_pipeline)")
    logger.info(f"  Phase 1: จะดึง OHLCV 15m (120d) + 1d (120d)")
    logger.info(f"  Phase 2: จะคำนวณ LSTM sequences + เทรน LSTM บน GPU")
    logger.info(f"  Output:  models/{SYMBOL}/lstm_{tag}.pt")
    logger.info(f"[DRY-RUN] จบ — ไม่มี LSTM training จริง")
    return
```

---

### Patch 3: `__main__` block — CLI arguments (argparse)

แทนที่ hardcoded block ด้วย argparse รองรับ:

| Flag | ความหมาย |
|---|---|
| `--daily` | รัน Daily Pipeline (LightGBM) |
| `--weekly` | รัน Weekly Pipeline (LSTM) |
| `--dry-run` | แสดงแผนงาน ไม่รันจริง |
| `--auto-universe` | สร้าง Universe ใหม่ (สำหรับ daily) |
| `--watchlist NVDA,TSLA` | ระบุ symbols เอง |
| `--mode local\|vps` | โหมดทำงาน |
| `--gdrive ./gdrive` | Google Drive path |
| `--cache ./cache` | Local cache path |

---

## วิธี Apply

### วิธีที่ 1: Auto-Patch (แนะนำ)

```bash
# อยู่ใน project directory
cd /path/to/ttp-trading

# รัน patch script
python patch_data_pipeline_manager.py data_pipeline_manager.py

# output:
#   ✅ PATCH 1a: run_daily_pipeline() — เพิ่ม dry_run parameter
#   ✅ PATCH 1b: run_daily_pipeline() — เพิ่ม dry_run guard ก่อน Phase 1
#   ✅ PATCH 2a: run_weekly_pipeline() — เพิ่ม dry_run parameter
#   ✅ PATCH 2b: run_weekly_pipeline() — เพิ่ม dry_run guard ก่อน Phase 1
#   ✅ PATCH 3:  __main__ block — CLI arguments (argparse)
#   Patches applied: 5/5
```

### วิธีที่ 2: Manual (ถ้า auto-patch ไม่ match)

ดู MANUAL INSTRUCTIONS ใน `patch_data_pipeline_manager.py` — มีคำอธิบายทุกจุดที่ต้องแก้

---

## ทดสอบหลัง Patch

### Test 1: Dry-Run Daily

```bash
python data_pipeline_manager.py --daily --auto-universe --dry-run
```

Expected output:
```
══════════════════════════════════════════════════════
  DATA PIPELINE MANAGER
  mode=local | gdrive=./gdrive
  dry_run=True
══════════════════════════════════════════════════════
[Daily Pipeline] generate_ttp_universe()...
[Universe] ... symbols generated
[Watchlist] Saved: ./cache/watchlist.json (82 symbols)
[DRY-RUN] Daily Pipeline Plan:
  Tag:     23-03-2026
  Symbols: 82 symbols
  Top 10:  ['SPY', 'NVDA', 'QQQ', 'LQD', 'IWM', ...]
  Phase 1: จะดึง OHLCV 15m (59d) + 1d (90d)
  Phase 2: จะคำนวณ features + เทรน LightGBM
  Watchlist saved: watchlist.json
[DRY-RUN] จบ — ไม่มี download / training จริง
```

### Test 2: Dry-Run Weekly

```bash
python data_pipeline_manager.py --weekly --dry-run
```

Expected output:
```
[Weekly Pipeline] โหลด watchlist จาก watchlist.json...
[Watchlist] โหลด: 82 symbols (tag=23-03-2026, source=universe_auto)
[DRY-RUN] Weekly Pipeline Plan:
  Tag:     WK12-2026
  Symbols: 82 symbols
  Source:  watchlist.json (สร้างโดย run_daily_pipeline)
  Phase 1: จะดึง OHLCV 15m (120d) + 1d (120d)
  Phase 2: จะคำนวณ LSTM sequences + เทรน LSTM บน GPU
[DRY-RUN] จบ — ไม่มี LSTM training จริง
```

### Test 3: Specific Symbols

```bash
python data_pipeline_manager.py --daily --weekly --dry-run --watchlist NVDA,TSLA
```

### Test 4: Full Smoke Test

```bash
chmod +x test_step0.sh
./test_step0.sh
```

---

## Batch Scripts

| Script | วัตถุประสงค์ | Cron |
|---|---|---|
| `run-batch-level-01.sh` | Daily: Universe → Watchlist → LightGBM | 08:00 AM ET ทุกวัน |
| `run-weekly-lstm.sh` | Weekly: Watchlist → LSTM | 07:00 AM ET อาทิตย์ |
| `run-pipeline-dryrun.sh` | Dry-run ทดสอบ | manual |
| `test_step0.sh` | Smoke test 3 scenarios | manual |

### Crontab Setup (Production)

```cron
# Daily Pipeline — 08:00 AM ET (20:00 ไทย)
0 20 * * 1-5 cd /opt/ttp-trading && ./run-batch-level-01.sh >> logs/daily-pipeline.log 2>&1

# Weekly Pipeline — 07:00 AM ET วันอาทิตย์ (19:00 ไทย)
0 19 * * 0 cd /opt/ttp-trading && ./run-weekly-lstm.sh >> logs/weekly-pipeline.log 2>&1
```

---

## ไฟล์ที่เกี่ยวข้อง (ไม่ต้องแก้)

| ไฟล์ | สถานะ | หมายเหตุ |
|---|---|---|
| `data_pipeline_manager.py` | **PATCH** | apply 3 patches |
| `models/technical_ml_analyzer.py` | ✅ OK | ใช้งานได้ตามเดิม |
| `config/config.py` | ✅ OK | มี GDRIVE_ROOT, LOCAL_CACHE แล้ว |
| `main.py` | ✅ OK | `--dry-run` ของ main.py ทำงานแยกจาก pipeline |
| `cache/watchlist.json` | Auto-gen | สร้างโดย `save_watchlist()` |
| `cache/universe.json` | Auto-gen | สร้างโดย `generate_ttp_universe()` |

---

## Backward Compatibility

- `run_daily_pipeline()` — เพิ่ม `dry_run=False` เป็น default → code เดิมที่ไม่ส่ง parameter จะยังทำงานเหมือนเดิม
- `run_weekly_pipeline()` — เพิ่ม `dry_run=False` เป็น default → เช่นกัน
- `MAIN_PY_PATCH` integration code ใน `data_pipeline_manager.py` — ไม่ถูกแก้ไข ยังใช้ได้
- Script เดิม (`run-batch-level-01.sh`, `test_step0.sh`) — ถูก update ให้ใช้ CLI ใหม่
