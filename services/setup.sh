#!/bin/bash
# deploy/setup.sh
# ===============
# One-command production setup script
#
# Usage:
#   chmod +x deploy/setup.sh
#   sudo ./deploy/setup.sh
#
# This script:
#   1. Creates user 'trader' (if not exists)
#   2. Creates log directory
#   3. Installs PM2 (if Node.js available) OR sets up systemd services
#   4. Sets up cron for log archival
#   5. Initializes Alembic (if not done)

set -e

PROJECT_DIR="/opt/ttp-trading"
LOG_DIR="${PROJECT_DIR}/logs"
USER="trader"

echo "═══════════════════════════════════════"
echo "  Quant Agent — Production Setup"
echo "═══════════════════════════════════════"

# ── 1. Create user
if ! id "$USER" &>/dev/null; then
    echo "[1/5] Creating user: $USER"
    useradd -r -m -s /bin/bash "$USER"
else
    echo "[1/5] User $USER already exists"
fi

# ── 2. Create directories
echo "[2/5] Creating directories"
mkdir -p "$LOG_DIR"
mkdir -p "${PROJECT_DIR}/signals/processed"
mkdir -p "${PROJECT_DIR}/signals/failed"
mkdir -p "${PROJECT_DIR}/journal"
mkdir -p "${PROJECT_DIR}/models"
mkdir -p "${PROJECT_DIR}/profiles"
chown -R "$USER:$USER" "$PROJECT_DIR"

# ── 3. Process Manager
echo "[3/5] Setting up process manager"
if command -v pm2 &>/dev/null; then
    echo "  PM2 found — using PM2"
    echo "  Run: pm2 start ecosystem.config.js"
    echo "  Then: pm2 save && pm2 startup"
elif command -v npm &>/dev/null; then
    echo "  Installing PM2..."
    npm install -g pm2
    echo "  Run: pm2 start ecosystem.config.js"
    echo "  Then: pm2 save && pm2 startup"
else
    echo "  PM2 not available — using systemd"
    cp "${PROJECT_DIR}/deploy/quant-engine.service" /etc/systemd/system/
    cp "${PROJECT_DIR}/deploy/signal-api.service" /etc/systemd/system/
    cp "${PROJECT_DIR}/deploy/signal-bridge.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable quant-engine signal-api signal-bridge
    echo "  systemd services installed"
    echo "  Start: sudo systemctl start quant-engine signal-api signal-bridge"
fi

# ── 4. Cron for log archival
echo "[4/5] Setting up log archival cron"
CRON_CMD="30 4 * * * ${PROJECT_DIR}/venv/bin/python3 ${PROJECT_DIR}/deploy/log_archiver.py --cleanup-gdrive >> ${LOG_DIR}/archiver.log 2>&1"
(crontab -u "$USER" -l 2>/dev/null || true; echo "$CRON_CMD") | sort -u | crontab -u "$USER" -
echo "  Cron installed: daily 04:30 log archival"

# ── 5. Alembic
echo "[5/5] Checking Alembic setup"
if [ ! -d "${PROJECT_DIR}/platform/migrations" ]; then
    echo "  Run: python deploy/init_alembic.py"
else
    echo "  Alembic migrations/ already exists"
fi

echo ""
echo "═══════════════════════════════════════"
echo "  ✅ Setup complete!"
echo ""
echo "  Next steps:"
echo "    1. Edit .env with your API keys"
echo "    2. Train models (Phase 1)"
echo "    3. Start services:"
echo "       PM2:     pm2 start ecosystem.config.js"
echo "       systemd: sudo systemctl start quant-engine signal-api signal-bridge"
echo "═══════════════════════════════════════"
