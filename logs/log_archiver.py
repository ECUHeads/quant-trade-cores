#!/usr/bin/env python3
"""
deploy/log_archiver.py
======================
Log Archiver — ย้ายไฟล์ log เก่าไป Google Drive

Flow:
  ./logs/trading.log.3    ← Python RotatingFileHandler สร้างให้
  ./logs/trading.log.4
  ./logs/trading.log.5    ← เก่าสุด จะถูกลบโดย RotatingFileHandler
           │
           ▼
  log_archiver.py จับไฟล์ .log.N (N >= retention_local)
  → gzip compress
  → ย้ายไป GDRIVE_PATH/logs/YYYY-MM/
  → ลบต้นฉบับ

Schedule (cron):
  # ทุกวัน 04:30 ET (หลัง flatten)
  30 4 * * * /opt/ttp-trading/venv/bin/python3 /opt/ttp-trading/deploy/log_archiver.py

  # หรือผ่าน PM2 cron:
  # ใส่ใน ecosystem.config.js เป็น app แยก

Config (environment variables):
  LOG_DIR         = ./logs              (default)
  GDRIVE_LOG_PATH = /mnt/gdrive/ttp-trading/logs   (Google Drive mount)
  LOG_RETENTION_LOCAL = 3               (เก็บ local 3 ไฟล์ล่าสุด, ที่เหลือ archive)
  LOG_RETENTION_GDRIVE_DAYS = 180       (เก็บบน GDrive 180 วัน)

Usage:
  python deploy/log_archiver.py                    # archive ทันที
  python deploy/log_archiver.py --dry-run          # แสดงว่าจะย้ายอะไร
  python deploy/log_archiver.py --cleanup-gdrive   # ลบ archive เก่าเกิน retention
"""

import os
import sys
import gzip
import shutil
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger("LogArchiver")

# ── Config
LOG_DIR               = os.getenv("LOG_DIR", "./logs")
GDRIVE_LOG_PATH       = os.getenv("GDRIVE_LOG_PATH", "./gdrive/logs")
LOG_RETENTION_LOCAL    = int(os.getenv("LOG_RETENTION_LOCAL", "3"))
LOG_RETENTION_GDRIVE   = int(os.getenv("LOG_RETENTION_GDRIVE_DAYS", "180"))


def get_rotated_logs(log_dir: str) -> list[Path]:
    """
    หาไฟล์ log ที่ถูก rotate แล้ว (trading.log.1, .2, .3, ...)
    รวมถึง api.log.N, bridge.log.N, engine.log.N
    """
    log_path = Path(log_dir)
    rotated = []
    for f in log_path.glob("*.log.*"):
        # ข้าม .gz ที่ compress แล้ว (ยังไม่ได้ย้าย)
        if f.suffix == ".gz":
            rotated.append(f)
            continue
        # เช็คว่า suffix เป็นตัวเลข (.1, .2, .3, ...)
        try:
            num = int(f.suffix.lstrip("."))
            rotated.append(f)
        except ValueError:
            continue
    return sorted(rotated)


def archive_logs(log_dir: str, gdrive_path: str, retention_local: int,
                 dry_run: bool = False):
    """
    ย้ายไฟล์ log เก่า (index ≥ retention_local) ไป GDrive

    Steps:
      1. หาไฟล์ .log.N ที่ N ≥ retention_local
      2. gzip compress
      3. ย้ายไป gdrive_path/YYYY-MM/
      4. ลบต้นฉบับ
    """
    rotated = get_rotated_logs(log_dir)
    if not rotated:
        logger.info("ไม่มีไฟล์ log ที่ต้อง archive")
        return

    # ── สร้าง GDrive destination
    now = datetime.now(timezone.utc)
    month_dir = Path(gdrive_path) / now.strftime("%Y-%m")

    if not dry_run:
        month_dir.mkdir(parents=True, exist_ok=True)

    archived = 0
    for f in rotated:
        # ── ดึงหมายเลข rotation
        try:
            if f.suffix == ".gz":
                # ไฟล์ .gz ที่ยังค้างอยู่ local → ย้ายไปเลย
                num = 999
            else:
                num = int(f.suffix.lstrip("."))
        except ValueError:
            continue

        # ── เก็บ N ไฟล์ล่าสุดไว้ local, ที่เหลือ archive
        if num < retention_local:
            logger.debug(f"  KEEP local: {f.name} (index {num} < {retention_local})")
            continue

        # ── Compress + Move
        gz_name = f"{f.stem}{f.suffix}_{now.strftime('%Y%m%d')}.gz"
        gz_dest = month_dir / gz_name

        if dry_run:
            logger.info(f"  [DRY] {f.name} → {gz_dest}")
            archived += 1
            continue

        try:
            # gzip compress
            with open(f, "rb") as f_in:
                with gzip.open(str(gz_dest), "wb") as f_out:
                    shutil.copyfileobj(f_in, f_out)

            # ตรวจว่า gz เขียนสำเร็จ
            if gz_dest.exists() and gz_dest.stat().st_size > 0:
                f.unlink()  # ลบต้นฉบับ
                logger.info(f"  ✅ {f.name} → {gz_dest.name} ({gz_dest.stat().st_size:,} bytes)")
                archived += 1
            else:
                logger.error(f"  ❌ {f.name}: gz file empty/missing")

        except Exception as e:
            logger.error(f"  ❌ {f.name}: {e}")

    logger.info(f"Archived {archived} log file(s) → {month_dir}")


def cleanup_old_archives(gdrive_path: str, retention_days: int,
                          dry_run: bool = False):
    """
    ลบ archive เก่าเกิน retention_days บน GDrive

    ตัวอย่าง: retention_days=180
      → ลบไฟล์ .gz ที่เก่ากว่า 6 เดือน
    """
    gdrive = Path(gdrive_path)
    if not gdrive.exists():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()

    deleted = 0
    total_freed = 0

    for gz_file in gdrive.rglob("*.gz"):
        if gz_file.stat().st_mtime < cutoff_ts:
            size = gz_file.stat().st_size
            if dry_run:
                logger.info(f"  [DRY] DELETE: {gz_file} ({size:,} bytes)")
            else:
                gz_file.unlink()
                logger.info(f"  🗑  {gz_file.name} ({size:,} bytes)")
            deleted += 1
            total_freed += size

    # ── ลบ empty month directories
    if not dry_run:
        for d in sorted(gdrive.iterdir()):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()

    if deleted:
        logger.info(
            f"Cleaned up {deleted} old archives "
            f"(freed {total_freed / 1024 / 1024:.1f} MB)"
        )
    else:
        logger.info(f"No archives older than {retention_days} days")


def show_status(log_dir: str, gdrive_path: str):
    """แสดงสถานะ log files ทั้ง local และ GDrive"""
    print(f"\n📁 Local logs ({log_dir}):")
    log_path = Path(log_dir)
    if log_path.exists():
        total = 0
        for f in sorted(log_path.glob("*")):
            if f.is_file():
                size = f.stat().st_size
                total += size
                print(f"  {f.name:40s} {size / 1024:>8.1f} KB")
        print(f"  {'─'*50}")
        print(f"  {'Total':40s} {total / 1024 / 1024:>8.1f} MB")
    else:
        print("  (ไม่มี)")

    print(f"\n📁 GDrive archives ({gdrive_path}):")
    gdrive = Path(gdrive_path)
    if gdrive.exists():
        total = 0
        count = 0
        for gz in gdrive.rglob("*.gz"):
            total += gz.stat().st_size
            count += 1
        print(f"  {count} archives, {total / 1024 / 1024:.1f} MB total")

        # แสดงแยกตามเดือน
        for month_dir in sorted(gdrive.iterdir()):
            if month_dir.is_dir():
                files = list(month_dir.glob("*.gz"))
                month_size = sum(f.stat().st_size for f in files)
                print(f"  {month_dir.name}: {len(files)} files, {month_size / 1024:.0f} KB")
    else:
        print("  (ยังไม่มี archive)")
    print()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Log Archiver — ย้าย rotated logs ไป Google Drive"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="แสดงว่าจะทำอะไร โดยไม่ย้าย/ลบจริง")
    parser.add_argument("--cleanup-gdrive", action="store_true",
                        help="ลบ archive เก่าเกิน retention บน GDrive")
    parser.add_argument("--status", action="store_true",
                        help="แสดงสถานะ log files")
    parser.add_argument("--log-dir", default=LOG_DIR)
    parser.add_argument("--gdrive-path", default=GDRIVE_LOG_PATH)
    parser.add_argument("--retention-local", type=int, default=LOG_RETENTION_LOCAL)
    parser.add_argument("--retention-gdrive-days", type=int, default=LOG_RETENTION_GDRIVE)
    args = parser.parse_args()

    if args.status:
        show_status(args.log_dir, args.gdrive_path)
        return

    # ── Archive rotated logs
    logger.info(f"{'='*50}")
    logger.info(f"Log Archiver | local={args.log_dir} → gdrive={args.gdrive_path}")
    logger.info(f"retention: local={args.retention_local} files, "
                f"gdrive={args.retention_gdrive_days} days")
    logger.info(f"{'='*50}")

    archive_logs(
        log_dir=args.log_dir,
        gdrive_path=args.gdrive_path,
        retention_local=args.retention_local,
        dry_run=args.dry_run,
    )

    # ── Cleanup old GDrive archives
    if args.cleanup_gdrive:
        logger.info("\nCleaning up old GDrive archives...")
        cleanup_old_archives(
            gdrive_path=args.gdrive_path,
            retention_days=args.retention_gdrive_days,
            dry_run=args.dry_run,
        )

    if not args.dry_run:
        show_status(args.log_dir, args.gdrive_path)


if __name__ == "__main__":
    main()
