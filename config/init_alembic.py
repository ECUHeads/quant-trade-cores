#!/usr/bin/env python3
"""
deploy/init_alembic.py
======================
ตั้งค่า Alembic Database Migration ให้พร้อมใช้

รัน:
  cd ttp-trading
  python deploy/init_alembic.py

ผลลัพธ์:
  platform/migrations/         ← Alembic migration directory
  platform/alembic.ini         ← Alembic config
  platform/migrations/env.py   ← patched ให้รู้จัก models.py

หลังจากนั้นใช้:
  cd platform
  alembic revision --autogenerate -m "initial"
  alembic upgrade head
"""

import os
import subprocess
import sys
from pathlib import Path


def main():
    platform_dir = Path(__file__).resolve().parent.parent / "platform"
    os.chdir(platform_dir)

    print(f"Working directory: {platform_dir}")

    # ── Check alembic installed
    try:
        import alembic
        print(f"✅ Alembic {alembic.__version__} found")
    except ImportError:
        print("❌ Alembic not installed. Run: pip install alembic")
        sys.exit(1)

    # ── Init alembic (skip if already exists)
    migrations_dir = platform_dir / "migrations"
    if migrations_dir.exists():
        print(f"⚠️  migrations/ already exists — skipping init")
    else:
        print("Running: alembic init migrations")
        subprocess.run(["alembic", "init", "migrations"], check=True)
        print("✅ alembic init done")

    # ── Patch alembic.ini — set database URL
    ini_path = platform_dir / "alembic.ini"
    if ini_path.exists():
        content = ini_path.read_text()
        old_url = "sqlalchemy.url = driver://user:pass@localhost/dbname"
        new_url = "sqlalchemy.url = sqlite:///signals.db"
        if old_url in content:
            content = content.replace(old_url, new_url)
            ini_path.write_text(content)
            print(f"✅ alembic.ini patched: {new_url}")
        else:
            print(f"⚠️  alembic.ini already patched or different format")

    # ── Patch migrations/env.py — import our models
    env_path = migrations_dir / "env.py"
    if env_path.exists():
        content = env_path.read_text()

        if "from models import Base" not in content:
            # Insert import after existing imports
            patch = '''
# ── Auto-patched by init_alembic.py
import sys, os
sys.path.insert(0, os.path.dirname(__file__) + "/..")
from models import Base
'''
            # Replace target_metadata = None
            if "target_metadata = None" in content:
                content = content.replace(
                    "target_metadata = None",
                    f"{patch}\ntarget_metadata = Base.metadata"
                )
                env_path.write_text(content)
                print("✅ env.py patched: target_metadata = Base.metadata")
            else:
                print("⚠️  env.py: target_metadata already set")
        else:
            print("⚠️  env.py: models already imported")

    print(f"""
{'='*55}
✅ Alembic setup complete!

Next steps:
  cd platform
  alembic revision --autogenerate -m "initial"
  alembic upgrade head

When you change models.py later:
  alembic revision --autogenerate -m "add xyz column"
  alembic upgrade head
{'='*55}
""")


if __name__ == "__main__":
    main()
