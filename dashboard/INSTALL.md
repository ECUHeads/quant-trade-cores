# Quant Agent — Merged Package
## Dashboard + Telegram + LINE Integration

---

## What's Inside

```
├── platform/
│   ├── signal_api.py           ← MERGED: original + dashboard mount (line 793-808)
│   ├── dashboard_routes.py     ← NEW: FastAPI static file serving
│   ├── telegram_setup.py       ← NEW: Interactive Telegram bot setup
│   ├── telegram_quick_test.py  ← NEW: Quick Telegram test
│   ├── line_setup.py           ← NEW: Interactive LINE bot setup
│   ├── line_quick_test.py      ← NEW: Quick LINE test
│   └── frontend/               ← NEW: React dashboard (Vite)
│       ├── package.json
│       ├── vite.config.js
│       ├── index.html
│       └── src/
│           ├── main.jsx
│           └── App.jsx         ← Dashboard with USE_API toggle
│
└── deploy/
    ├── setup.sh                ← MERGED: original + steps 6-8 (frontend, nginx, bots)
    ├── build_frontend.sh       ← NEW: Frontend build script
    ├── nginx-quant-agent.conf  ← NEW: Nginx reverse proxy
    ├── DASHBOARD_DEPLOY.md     ← NEW: Dashboard deploy guide
    ├── TELEGRAM_SETUP.md       ← NEW: Telegram setup guide
    └── LINE_SETUP.md           ← NEW: LINE setup guide
```

## Files NOT included (unchanged from GitHub)

These files are identical to what's already in the repo — no changes needed:

- `notifier_telegram.py` — already complete
- `notifier_line.py` — already complete
- `models.py` — already complete
- `signal_bridge.py` — already complete
- `ecosystem_config.js` — already complete
- `dashboard.jsx` — kept as reference (new App.jsx replaces it)
- All other files (config, main, ML, risk, orders, etc.)

---

## Installation

### Step 1: Extract into project

```bash
cd /opt/ttp-trading

# Extract — overwrites only signal_api.py and setup.sh
# All other existing files are untouched
tar xzf quant-agent-merged-package.tar.gz
```

### Step 2: Verify merge

```bash
# Check signal_api.py has dashboard mount
grep "mount_dashboard" platform/signal_api.py
# Should show: from platform.dashboard_routes import mount_dashboard

# Check setup.sh has new steps
grep "Frontend Dashboard\|Nginx\|Notification bots" deploy/setup.sh
# Should show steps 6, 7, 8
```

### Step 3: Build dashboard

```bash
cd platform/frontend
npm install
npm run build
# → Output: platform/static/index.html + assets/
```

### Step 4: Test

```bash
# Start API (now serves dashboard too)
uvicorn platform.signal_api:app --host 0.0.0.0 --port 8000

# Open: http://localhost:8000
# API:  http://localhost:8000/docs
```

### Step 5: Setup notifications

```bash
# Telegram
python platform/telegram_setup.py

# LINE
python platform/line_setup.py
```

---

## What Changed in Existing Files

### signal_api.py (+21 lines)

**Docstring** — added `/* → React Dashboard` and build instructions

**Lines 793-808** — added dashboard mount (at end, before `if __name__`):
```python
try:
    from platform.dashboard_routes import mount_dashboard
    mount_dashboard(app)
    logger.info("✅ Dashboard routes mounted")
except ImportError:
    logger.info("ℹ️  Dashboard routes not available")
```

**No existing code was modified or removed.**
All 17 original API routes work exactly the same.

### setup.sh (+89 lines)

**Steps 1-5** — completely unchanged (user, dirs, PM2, cron, alembic)

**Step 6 (new)** — Frontend build: checks Node.js → npm install → npm run build
**Step 7 (new)** — Nginx: checks config exists → shows install instructions  
**Step 8 (new)** — Bots: checks .env for TELEGRAM_BOT_TOKEN and LINE_CHANNEL_ACCESS_TOKEN

**No existing code was modified or removed.**
Original deployment still works exactly the same.
