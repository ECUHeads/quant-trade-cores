"""
mt5_proxy_integration.py
=========================
Integration guide: เชื่อม Mt5ProxyAdapter เข้ากับ UniversalOrderExecutor

ไฟล์นี้แสดง:
  1. Patch สำหรับ universal_order_executor.py (Mt5Adapter → Mt5ProxyAdapter)
  2. Patch สำหรับ config.py (เพิ่ม FTMO symbol mapping + proxy settings)
  3. ตัวอย่างการใช้งาน

=== PATCH 1: universal_order_executor.py ===

แก้ class Mt5Adapter section (line ~124-226) ให้เป็น proxy-based
หรือเพิ่ม routing logic ใน UniversalOrderExecutor.__init__
"""


# ============================================================
# OPTION A: แก้ routing ใน UniversalOrderExecutor.__init__
# (แนะนำ — แก้น้อยที่สุด)
# ============================================================

"""
เปลี่ยนใน __init__ ของ UniversalOrderExecutor (ประมาณ line 341):

เดิม:
    elif execution_method == "MT5":
        self.adapter = Mt5Adapter()

ใหม่:
    elif execution_method == "MT5":
        # ถ้าอยู่บน Linux → ใช้ proxy adapter
        # ถ้าอยู่บน Windows → ใช้ Mt5Adapter ตรง (legacy)
        import platform as _plat
        if _plat.system() != "Windows":
            from mt5_proxy_client import Mt5ProxyAdapter
            self.adapter = Mt5ProxyAdapter(
                proxy_url=kwargs.get("mt5_proxy_url", ""),
                api_key=kwargs.get("mt5_proxy_api_key", ""),
                hmac_secret=kwargs.get("mt5_proxy_hmac_secret", ""),
                unhealthy_callback=kwargs.get("mt5_unhealthy_cb"),
                recovery_callback=kwargs.get("mt5_recovery_cb"),
            )
        else:
            self.adapter = Mt5Adapter()
"""


# ============================================================
# OPTION B: เพิ่ม execution_method ใหม่ "MT5_PROXY"
# (ชัดเจนกว่า — ไม่ auto-detect OS)
# ============================================================

"""
เพิ่มใน __init__ ของ UniversalOrderExecutor:

    elif execution_method == "MT5_PROXY":
        from mt5_proxy_client import Mt5ProxyAdapter
        self.adapter = Mt5ProxyAdapter(
            proxy_url=kwargs.get("mt5_proxy_url", ""),
            api_key=kwargs.get("mt5_proxy_api_key", ""),
            hmac_secret=kwargs.get("mt5_proxy_hmac_secret", ""),
            unhealthy_callback=kwargs.get("mt5_unhealthy_cb"),
            recovery_callback=kwargs.get("mt5_recovery_cb"),
        )
    elif execution_method == "MT5":
        self.adapter = Mt5Adapter()


แล้วใน config.py FTMO profile เปลี่ยนจาก:
    "execution_method": "MT5",
เป็น:
    "execution_method": "MT5_PROXY",
"""


# ============================================================
# PATCH 2: config.py — เพิ่ม MT5 Proxy settings + Symbol map
# ============================================================

"""
เพิ่มใน FTMO_100K profile (ใน PROP_FIRM_PROFILES dict):

    "FTMO_100K": {
        ...existing fields...

        # ── MT5 Proxy Settings (NEW)
        "mt5_proxy_url":         os.getenv("MT5_PROXY_URL", "https://your-windows-vps:8500"),
        "mt5_proxy_api_key":     os.getenv("MT5_PROXY_API_KEY", "change-me"),
        "mt5_proxy_hmac_secret": os.getenv("MT5_PROXY_HMAC_SECRET", "change-me"),

        # ── Symbol Mapping: engine name → MT5 broker name
        # (override ผ่าน env MT5_SYMBOL_MAP ได้)
        "symbol_map": {
            # Forex — FTMO ส่วนใหญ่ใช้ชื่อตรง
            "EURUSD": "EURUSD",  "GBPUSD": "GBPUSD",
            "USDJPY": "USDJPY",  "AUDUSD": "AUDUSD",
            "USDCAD": "USDCAD",  "NZDUSD": "NZDUSD",
            "EURGBP": "EURGBP",  "EURJPY": "EURJPY",
            "GBPJPY": "GBPJPY",  "USDCHF": "USDCHF",
            # Indices — FTMO ใช้ .cash suffix
            "US30":   "US30.cash",   "NAS100": "US100.cash",
            "SPX500": "US500.cash",  "GER40":  "GER40.cash",
            # Commodities
            "XAUUSD": "XAUUSD",     "XAGUSD": "XAGUSD",
            "USOIL":  "USOIL.cash", "UKOIL":  "UKOIL.cash",
            # Crypto
            "BTCUSD": "BTCUSD",     "ETHUSD": "ETHUSD",
        },
    },
"""


# ============================================================
# PATCH 3: Config.from_config() — pass proxy kwargs
# ============================================================

"""
แก้ UniversalOrderExecutor.from_config() (ประมาณ line 364):

เดิม:
    @classmethod
    def from_config(cls, cfg) -> "UniversalOrderExecutor":
        kwargs = {"signal_dir": cfg.SIGNAL_DIR}
        if cfg.EXECUTION_METHOD == "API_REST":
            key, secret = cfg.get_alpaca_keys()
            kwargs["api_key"] = key
            kwargs["secret"]  = secret
        return cls(execution_method=cfg.EXECUTION_METHOD, **kwargs)

ใหม่:
    @classmethod
    def from_config(cls, cfg) -> "UniversalOrderExecutor":
        kwargs = {"signal_dir": cfg.SIGNAL_DIR}
        if cfg.EXECUTION_METHOD == "API_REST":
            key, secret = cfg.get_alpaca_keys()
            kwargs["api_key"] = key
            kwargs["secret"]  = secret
        elif cfg.EXECUTION_METHOD in ("MT5", "MT5_PROXY"):
            kwargs["mt5_proxy_url"]         = getattr(cfg, "MT5_PROXY_URL", "")
            kwargs["mt5_proxy_api_key"]     = getattr(cfg, "MT5_PROXY_API_KEY", "")
            kwargs["mt5_proxy_hmac_secret"] = getattr(cfg, "MT5_PROXY_HMAC_SECRET", "")
        return cls(execution_method=cfg.EXECUTION_METHOD, **kwargs)
"""


# ============================================================
# EXAMPLE: End-to-end usage
# ============================================================

def example_usage():
    """ตัวอย่างการใช้งานจริง (จาก main.py)"""

    import os
    os.environ["MT5_PROXY_URL"] = "https://192.168.1.100:8500"
    os.environ["MT5_PROXY_API_KEY"] = "my-secure-key-here"
    os.environ["MT5_PROXY_HMAC_SECRET"] = "my-hmac-secret-here"

    # ── Load config
    # Config.load_profile("FTMO_100K")

    # ── Alert callback เมื่อ MT5 disconnect
    def on_mt5_unhealthy(status):
        print(f"🚨 MT5 UNHEALTHY: {status}")
        # ส่ง LINE/Telegram alert
        # from notifier_line import send_alert
        # send_alert("MT5 disconnected! Orders blocked.")

    def on_mt5_recovery(status):
        print(f"✅ MT5 RECOVERED: {status}")
        # from notifier_line import send_alert
        # send_alert("MT5 reconnected. Orders resumed.")

    # ── สร้าง adapter ตรง
    from mt5_proxy_client import Mt5ProxyAdapter

    adapter = Mt5ProxyAdapter(
        unhealthy_callback=on_mt5_unhealthy,
        recovery_callback=on_mt5_recovery,
    )

    # ── ตรวจ health ก่อนเทรด
    health = adapter.check_system_health(max_daily_loss=5000)
    print(f"System: {health['status']}")

    if health["status"] == "HALT":
        print("Daily loss limit reached — no trading today")
        return

    # ── ส่ง order
    from universal_order_executor import ExecutionResult
    result = adapter.submit(
        symbol="EURUSD",
        side="BUY",
        size=0.10,          # 0.10 lot
        sizing_unit="LOTS",
        entry_price=0,      # 0 = market order
        stop_loss=1.08000,
        take_profit=1.09500,
        metadata={"signal_id": "SIG-20260323-0001"},
    )

    if result.success:
        print(f"Order filled: ticket={result.metadata.get('ticket')}")
        print(f"  Price: {result.entry_price}")
        print(f"  Execution: {result.metadata.get('execution_ms', 0):.0f}ms")
    else:
        print(f"Order failed: {result.message}")

    # ── ดู positions
    positions = adapter.get_positions()
    for p in positions:
        print(f"  Open: #{p['ticket']} {p['type']} {p['volume']} {p['symbol']} P&L=${p['profit']}")

    adapter.stop()


if __name__ == "__main__":
    example_usage()
