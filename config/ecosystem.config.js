// ecosystem.config.js
// ===================
// PM2 Process Manager — จัดการทุก process ให้ auto-restart
//
// ติดตั้ง:
//   npm install -g pm2
//
// ใช้งาน:
//   pm2 start ecosystem.config.js          # เริ่มทุก process
//   pm2 status                             # ดูสถานะ
//   pm2 logs                               # ดู logs real-time
//   pm2 logs quant-engine --lines 50       # ดู log เฉพาะ engine
//   pm2 restart all                        # restart ทั้งหมด
//   pm2 stop all                           # หยุดทั้งหมด
//   pm2 save && pm2 startup                # ตั้งให้ start ตอน boot
//
// ⚠️ ห้ามใช้ screen/tmux สำหรับ production — PM2 ทำ:
//   ✅ Auto-restart เมื่อ crash
//   ✅ Memory limit restart (ป้องกัน memory leak)
//   ✅ Log rotation ในตัว
//   ✅ Monitoring dashboard (pm2 monit)
//   ✅ Boot startup (pm2 startup)

module.exports = {
  apps: [

    // ═══════════════════════════════════════
    // 1. QUANT ENGINE (Core Trading Bot)
    // ═══════════════════════════════════════
    {
      name:          "quant-engine",
      script:        "main.py",
      interpreter:   "/opt/ttp-trading/venv/bin/python3",  // ← แก้ path venv
      cwd:           "/opt/ttp-trading",                    // ← แก้ path โปรเจกต์
      args:          "--profile TTP_5K_FLEX --mode paper",  // ← แก้ profile/mode

      // Auto-restart
      autorestart:   true,
      max_restarts:  10,               // restart ได้ไม่เกิน 10 ครั้งต่อ 15 นาที
      min_uptime:    "30s",            // ต้องรันอย่างน้อย 30s ถึงนับว่า "stable"
      restart_delay: 5000,             // รอ 5 วินาทีก่อน restart

      // Memory limit — restart ถ้าใช้เกิน 1GB (ป้องกัน memory leak)
      max_memory_restart: "1G",

      // Cron restart — restart ทุกวัน 08:00 ET (20:00 ไทย)
      // เพื่อ clean state ก่อน trading session
      cron_restart:  "0 8 * * 1-5",   // จันทร์-ศุกร์ 08:00 (ET timezone ของ VPS)

      // Environment
      env: {
        NODE_ENV:       "production",
        PYTHONUNBUFFERED: "1",         // ให้ print/log ออกทันที (ไม่ buffer)
      },

      // Logs
      log_file:      "/opt/ttp-trading/logs/engine-combined.log",
      out_file:      "/opt/ttp-trading/logs/engine-out.log",
      error_file:    "/opt/ttp-trading/logs/engine-error.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      merge_logs:    true,
    },

    // ═══════════════════════════════════════
    // 2. SIGNAL API (FastAPI Gateway)
    // ═══════════════════════════════════════
    {
      name:          "signal-api",
      script:        "-m",
      interpreter:   "/opt/ttp-trading/venv/bin/python3",
      cwd:           "/opt/ttp-trading",
      args:          "uvicorn platform.signal_api:app --host 0.0.0.0 --port 8000",

      autorestart:   true,
      max_restarts:  20,               // API ควร restart ได้บ่อยกว่า engine
      min_uptime:    "10s",
      restart_delay: 3000,
      max_memory_restart: "512M",

      env: {
        PYTHONUNBUFFERED: "1",
      },

      log_file:      "/opt/ttp-trading/logs/api-combined.log",
      out_file:      "/opt/ttp-trading/logs/api-out.log",
      error_file:    "/opt/ttp-trading/logs/api-error.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      merge_logs:    true,
    },

    // ═══════════════════════════════════════
    // 3. SIGNAL BRIDGE (JSON → API dispatcher)
    // ═══════════════════════════════════════
    {
      name:          "signal-bridge",
      script:        "-m",
      interpreter:   "/opt/ttp-trading/venv/bin/python3",
      cwd:           "/opt/ttp-trading",
      args:          "platform.signal_bridge --watch ./signals/ --api-url http://localhost:8000",

      autorestart:   true,
      max_restarts:  20,
      min_uptime:    "10s",
      restart_delay: 3000,
      max_memory_restart: "256M",

      env: {
        PYTHONUNBUFFERED: "1",
      },

      log_file:      "/opt/ttp-trading/logs/bridge-combined.log",
      out_file:      "/opt/ttp-trading/logs/bridge-out.log",
      error_file:    "/opt/ttp-trading/logs/bridge-error.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      merge_logs:    true,
    },
  ],
};
