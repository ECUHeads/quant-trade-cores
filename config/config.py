"""
config.py
=========
Universal 15m Quant Engine — Centralized Configuration

Architecture:
  1. PROP_FIRM_PROFILES  — Dict-based profile สำหรับแต่ละ Prop Firm
  2. ASSET_CLASS_CONFIG  — ตัวคูณ, ทศนิยม, sizing method ต่อ asset class
  3. LLM_PROVIDERS       — Switchable multi-provider (Claude/GPT-4/Gemini)
  4. ConfigValidator      — ตรวจ contradiction ก่อนรัน (เช่น FUTURES + fractional)
  5. Config class         — โหลด profile + merge overrides + validate

Usage:
  # โหลด TTP profile
  Config.load_profile("TTP_5K_FLEX")
  Config.validate("paper")

  # โหลด FTMO profile
  Config.load_profile("FTMO_100K")
  Config.validate("live")

  # Custom profile จาก JSON file
  Config.load_profile_from_file("./profiles/my_firm.json")
"""

import os
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("Config")

# ============================================================
# PROP FIRM PROFILES — แต่ละ Prop Firm มีกฎต่างกัน
# ============================================================

PROP_FIRM_PROFILES: dict[str, dict] = {

    # ── Trade The Pool — $5K FLEX Evaluation
    "TTP_5K_FLEX": {
        "firm_name":            "Trade The Pool",
        "account_label":        "$5K FLEX Evaluation",
        "asset_class":          "EQUITIES",       # US Stocks
        "execution_method":     "JSON_DUMP",       # OpenClaw reads JSON
        "allow_fractional":     False,
        "account_size_usd":     5_000.0,
        "max_buying_power":     5_000.0,
        "max_daily_loss_pct":   2.0,               # 2% = $100
        "max_daily_loss_usd":   90.0,              # buffer $10 ก่อนชน $100
        "daily_loss_buffer":    10.0,
        "risk_per_trade_pct":   0.3,               # 0.3% ของ account
        "risk_per_trade_usd":   15.0,
        "profit_target_usd":    300.0,             # 6%
        "max_orders_per_day":   3,
        "min_hold_sec":         30,                # TTP 30s rule
        "min_trade_range":      0.10,              # TTP 10¢ rule
        "consistency_rule_pct": 0.50,              # FLEX 50%
        "overnight_allowed":    False,
        "overnight_limit_usd":  800.0,
        "flatten_time_et":      [15, 45],
        "no_new_entry_after":   [14, 30],
        "volume_rule_pct":      0.05,              # TTP 5% rule
        "streak_warn":          1,
        "streak_block":         2,
        "commission_per_share": 0.005,
        "min_price":            5.0,
        "min_adv":              1_000_000,
        "contract_multiplier":  1,                 # stocks = 1
        "tick_size":            0.01,
        # ── Cost Tracking (Enhancement)
        "typical_spread_pips":  0.0,               # equities: use spread_buffer_pct instead
        "overnight_swap_long":  0.0,               # no cost for long (prop firm)
        "overnight_swap_short": -5.0,              # borrow fee for short ~5% annualized
        "max_spread_multiplier": 3.0,
    },

    # ── Trade The Pool — $80K Super Pool
    "TTP_80K": {
        "firm_name":            "Trade The Pool",
        "account_label":        "$80K Super Pool",
        "asset_class":          "EQUITIES",
        "execution_method":     "JSON_DUMP",
        "allow_fractional":     False,
        "account_size_usd":     80_000.0,
        "max_buying_power":     80_000.0,
        "max_daily_loss_pct":   0.875,             # $700 / $80K
        "max_daily_loss_usd":   700.0,
        "daily_loss_buffer":    100.0,
        "risk_per_trade_pct":   0.125,             # $100 / $80K
        "risk_per_trade_usd":   100.0,
        "profit_target_usd":    0,                 # no fixed target
        "max_orders_per_day":   3,
        "min_hold_sec":         30,
        "min_trade_range":      0.10,
        "consistency_rule_pct": 0.50,
        "overnight_allowed":    False,
        "overnight_limit_usd":  0,
        "flatten_time_et":      [15, 45],
        "no_new_entry_after":   [14, 30],
        "volume_rule_pct":      0.05,
        "streak_warn":          1,
        "streak_block":         2,
        "commission_per_share": 0.005,
        "min_price":            5.0,
        "min_adv":              1_000_000,
        "contract_multiplier":  1,
        "tick_size":            0.01,
        # ── Cost Tracking (Enhancement)
        "typical_spread_pips":  0.0,
        "overnight_swap_long":  0.0,
        "overnight_swap_short": -5.0,
        "max_spread_multiplier": 3.0,
    },

    # ── FTMO — $100K Challenge (Forex/CFD)
    "FTMO_100K": {
        "firm_name":            "FTMO",
        "account_label":        "$100K Challenge",
        "asset_class":          "CFD",
        "execution_method":     "MT5",             # MetaTrader 5
        "allow_fractional":     True,              # CFD lots can be 0.01
        "account_size_usd":     100_000.0,
        "max_buying_power":     100_000.0,
        "max_daily_loss_pct":   5.0,               # FTMO 5% daily
        "max_daily_loss_usd":   5_000.0,
        "daily_loss_buffer":    500.0,
        "risk_per_trade_pct":   1.0,
        "risk_per_trade_usd":   1_000.0,
        "profit_target_usd":    10_000.0,          # 10%
        "max_orders_per_day":   3,
        "min_hold_sec":         0,                 # FTMO ไม่มี min hold
        "min_trade_range":      0.0,
        "consistency_rule_pct": 0.0,               # FTMO ไม่มี consistency rule
        "overnight_allowed":    True,              # FTMO อนุญาต
        "overnight_limit_usd":  0,
        "flatten_time_et":      [15, 55],          # กรณี day-trade only
        "no_new_entry_after":   [15, 30],
        "volume_rule_pct":      0.0,               # ไม่มี volume rule
        "streak_warn":          2,
        "streak_block":         3,
        "commission_per_share": 0.0,               # CFD ไม่มี commission (แต่มี spread)
        "min_price":            0.0,
        "min_adv":              0,
        "contract_multiplier":  100_000,           # 1 lot forex = 100K units
        "tick_size":            0.00001,            # 5 decimal forex
        # ── Cost Tracking (Enhancement)
        "typical_spread_pips":  1.5,               # avg spread in pips for backtest
        "overnight_swap_long":  -3.5,              # annual % paid on long overnight
        "overnight_swap_short": 1.0,               # annual % earned on short overnight
        "max_spread_multiplier": 3.0,              # delay entry if spread > 3× avg
    },

    # ── Topstep — $50K Futures (NQ)
    "TOPSTEP_50K_NQ": {
        "firm_name":            "Topstep",
        "account_label":        "$50K NQ Futures",
        "asset_class":          "FUTURES",
        "execution_method":     "API_REST",        # Tradovate API
        "allow_fractional":     False,             # Futures = integer contracts
        "account_size_usd":     50_000.0,
        "max_buying_power":     50_000.0,
        "max_daily_loss_pct":   2.0,
        "max_daily_loss_usd":   1_000.0,
        "daily_loss_buffer":    100.0,
        "risk_per_trade_pct":   1.0,
        "risk_per_trade_usd":   500.0,
        "profit_target_usd":    3_000.0,
        "max_orders_per_day":   3,
        "min_hold_sec":         0,
        "min_trade_range":      0.0,
        "consistency_rule_pct": 0.0,
        "overnight_allowed":    False,
        "overnight_limit_usd":  0,
        "flatten_time_et":      [15, 55],
        "no_new_entry_after":   [15, 30],
        "volume_rule_pct":      0.0,
        "streak_warn":          2,
        "streak_block":         3,
        "commission_per_share": 0.0,               # futures = per contract
        "min_price":            0.0,
        "min_adv":              0,
        "contract_multiplier":  20,                # NQ micro = $20/point
        "tick_size":            0.25,              # NQ tick = 0.25 pts
        # ── Cost Tracking (Enhancement)
        "typical_spread_pips":  0.0,               # futures: use tick_size-based spread
        "overnight_swap_long":  0.0,               # futures: no overnight swap
        "overnight_swap_short": 0.0,
        "max_spread_multiplier": 3.0,
        "rollover_cost_per_contract": 2.50,        # NQ rollover cost at expiry
    },

    # ══════════════════════════════════════════════════════
    # ALPACA PAPER TRADE — ส่ง Order ตรงผ่าน Alpaca API
    # ══════════════════════════════════════════════════════
    #
    # ใช้สำหรับ:
    #   - ทดสอบ strategy จริงกับ Alpaca Paper (fill จริง, slippage จริง)
    #   - ฝึกซ้อมก่อนขึ้น Prop Firm
    #   - Walk-forward validation ก่อน live
    #
    # ข้อดีเทียบ JSON_DUMP:
    #   - ได้ fill confirmation จริง (price, qty, timestamp)
    #   - เห็น slippage + partial fill จริง
    #   - ทดสอบ bracket order (SL/TP) ทำงานจริง
    #
    # Alpaca Paper Account defaults:
    #   - Buying power: $100,000
    #   - Margin: 4x day trade / 2x overnight
    #   - Commission: $0 (commission-free)
    #   - Fractional shares: supported
    #   - PDT rule: applies if equity < $25K

    # ── Alpaca Paper — $100K (Standard)
    "ALPACA_PAPER_100K": {
        "firm_name":            "Alpaca Paper",
        "account_label":        "$100K Paper Trade",
        "asset_class":          "EQUITIES",
        "execution_method":     "API_REST",        # ส่ง order ตรง Alpaca API
        "allow_fractional":     True,              # Alpaca รองรับ fractional
        "account_size_usd":     100_000.0,
        "max_buying_power":     400_000.0,         # 4x day trade margin
        "max_daily_loss_pct":   2.0,               # self-imposed $2,000
        "max_daily_loss_usd":   2_000.0,
        "daily_loss_buffer":    200.0,
        "risk_per_trade_pct":   0.5,               # $500/trade
        "risk_per_trade_usd":   500.0,
        "profit_target_usd":    0,                 # ไม่มี target (ไม่ใช่ challenge)
        "max_orders_per_day":   10,                # เปิดมากกว่า prop firm ได้
        "min_hold_sec":         0,                 # ไม่มี hold rule
        "min_trade_range":      0.0,               # ไม่มี 10¢ rule
        "consistency_rule_pct": 0.0,               # ไม่มี consistency rule
        "overnight_allowed":    True,              # ถือข้ามคืนได้
        "overnight_limit_usd":  50_000.0,          # 50% ของ account
        "flatten_time_et":      [15, 55],          # flatten 15:55 (ถ้าไม่ถือข้ามคืน)
        "no_new_entry_after":   [15, 30],
        "volume_rule_pct":      0.0,               # ไม่มี volume rule
        "streak_warn":          2,
        "streak_block":         3,                 # หยุดหลังแพ้ 3 ไม้
        "commission_per_share": 0.0,               # Alpaca = commission-free
        "min_price":            1.0,               # เล่น penny stock ได้ (paper)
        "min_adv":              500_000,            # ลด ADV ลง (ทดสอบ)
        "contract_multiplier":  1,
        "tick_size":            0.01,
        # ── Cost Tracking (Enhancement)
        "typical_spread_pips":  0.0,
        "overnight_swap_long":  0.0,               # Alpaca: no margin interest (paper)
        "overnight_swap_short": -5.0,              # borrow fee for shorts
        "max_spread_multiplier": 3.0,
    },

    # ── Alpaca Paper — $25K (PDT-safe)
    "ALPACA_PAPER_25K": {
        "firm_name":            "Alpaca Paper",
        "account_label":        "$25K Paper (PDT-safe)",
        "asset_class":          "EQUITIES",
        "execution_method":     "API_REST",
        "allow_fractional":     True,
        "account_size_usd":     25_000.0,
        "max_buying_power":     100_000.0,         # 4x day trade margin
        "max_daily_loss_pct":   2.0,               # $500
        "max_daily_loss_usd":   500.0,
        "daily_loss_buffer":    50.0,
        "risk_per_trade_pct":   0.4,               # $100/trade
        "risk_per_trade_usd":   100.0,
        "profit_target_usd":    0,
        "max_orders_per_day":   5,
        "min_hold_sec":         0,
        "min_trade_range":      0.0,
        "consistency_rule_pct": 0.0,
        "overnight_allowed":    False,             # ฝึกแบบ day trade
        "overnight_limit_usd":  0,
        "flatten_time_et":      [15, 50],
        "no_new_entry_after":   [14, 30],
        "volume_rule_pct":      0.0,
        "streak_warn":          1,
        "streak_block":         2,
        "commission_per_share": 0.0,
        "min_price":            5.0,
        "min_adv":              1_000_000,
        "contract_multiplier":  1,
        "tick_size":            0.01,
        # ── Cost Tracking (Enhancement)
        "typical_spread_pips":  0.0,
        "overnight_swap_long":  0.0,
        "overnight_swap_short": -5.0,
        "max_spread_multiplier": 3.0,
    },
}


# ============================================================
# ASSET CLASS CONFIG — sizing method + validation rules
# ============================================================

ASSET_CLASS_CONFIG = {
    "EQUITIES": {
        "sizing_unit":      "SHARES",
        "allow_fractional": False,    # default (override per profile)
        "decimal_places":   2,        # price decimals
        "has_commission":   True,
        "has_spread":       True,
        "description":      "US Stocks (Nasdaq/NYSE)",
    },
    "CFD": {
        "sizing_unit":      "LOTS",
        "allow_fractional": True,     # 0.01 lot minimum
        "decimal_places":   5,        # forex 5 decimals
        "has_commission":   False,    # spread only
        "has_spread":       True,
        "description":      "Contracts for Difference (Forex/Indices)",
    },
    "FUTURES": {
        "sizing_unit":      "CONTRACTS",
        "allow_fractional": False,    # integer contracts only
        "decimal_places":   2,
        "has_commission":   True,
        "has_spread":       True,
        "description":      "Exchange-traded Futures (NQ/ES/CL)",
    },
}


# ============================================================
# LLM PROVIDER CONFIG — Gate 19 CIO (switchable)
# ============================================================

LLM_PROVIDERS = {
    "CLAUDE": {
        "api_key_env":  "ANTHROPIC_API_KEY",
        "base_url":     "https://api.anthropic.com/v1/messages",
        "model":        "claude-sonnet-4-20250514",
        "max_tokens":   1024,
        "temperature":  0.1,       # ต่ำ = deterministic / consistent
        "timeout_sec":  15,
        "headers_fn":   lambda key: {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    },
    "OPENAI": {
        "api_key_env":  "OPENAI_API_KEY",
        "base_url":     "https://api.openai.com/v1/chat/completions",
        "model":        "gpt-4o",
        "max_tokens":   1024,
        "temperature":  0.1,
        "timeout_sec":  15,
        "headers_fn":   lambda key: {
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
        },
    },
    "GEMINI": {
        "api_key_env":  "GEMINI_API_KEY",
        "base_url":     "https://generativelanguage.googleapis.com/v1beta/models",
        "model":        "gemini-2.0-flash",
        "max_tokens":   1024,
        "temperature":  0.1,
        "timeout_sec":  15,
        "headers_fn":   lambda key: {
            "Content-Type": "application/json",
        },
    },
}


# ============================================================
# CONFIG VALIDATOR — ตรวจ contradiction ก่อนรัน
# ============================================================

class ConfigValidationError(Exception):
    """Raised when config has contradictory values"""
    pass


def validate_profile(profile: dict) -> list[str]:
    """
    ตรวจ contradiction ใน profile → คืน list ของ errors (ว่าง = OK)

    Rules:
      1. FUTURES + allow_fractional = True → ERROR
      2. max_daily_loss_usd ≤ 0 → ERROR
      3. risk_per_trade_usd > max_daily_loss_usd → ERROR
      4. flatten_time ≤ no_new_entry_after → ERROR (flatten ก่อน cutoff)
      5. EQUITIES + contract_multiplier ≠ 1 → WARNING
      6. execution_method ต้องอยู่ใน [MT5, API_REST, JSON_DUMP]
      7. asset_class ต้องอยู่ใน ASSET_CLASS_CONFIG
      8. streak_block ≤ 0 → ERROR
      9. min_hold_sec < 0 → ERROR
    """
    errors = []
    ac = profile.get("asset_class", "EQUITIES")

    # Rule 1: Futures ห้าม fractional
    if ac == "FUTURES" and profile.get("allow_fractional", False):
        errors.append(
            f"CONFLICT: asset_class=FUTURES but allow_fractional=True. "
            f"Futures trade in integer contracts only."
        )

    # Rule 2: max_daily_loss_usd
    mdl = profile.get("max_daily_loss_usd", 0)
    if mdl <= 0:
        errors.append(f"max_daily_loss_usd must be > 0, got {mdl}")

    # Rule 3: risk > daily loss
    rpt = profile.get("risk_per_trade_usd", 0)
    if rpt > mdl > 0:
        errors.append(
            f"risk_per_trade_usd (${rpt}) > max_daily_loss_usd (${mdl}). "
            f"Single trade could blow the daily limit."
        )

    # Rule 4: flatten vs no_new_entry timing
    flat = profile.get("flatten_time_et", [15, 45])
    cutoff = profile.get("no_new_entry_after", [14, 30])
    flat_min = flat[0] * 60 + flat[1]
    cutoff_min = cutoff[0] * 60 + cutoff[1]
    if flat_min <= cutoff_min:
        errors.append(
            f"flatten_time_et ({flat[0]}:{flat[1]:02d}) must be AFTER "
            f"no_new_entry_after ({cutoff[0]}:{cutoff[1]:02d})"
        )

    # Rule 5: EQUITIES + weird multiplier (warning, not error)
    if ac == "EQUITIES" and profile.get("contract_multiplier", 1) != 1:
        errors.append(
            f"WARNING: asset_class=EQUITIES but contract_multiplier="
            f"{profile['contract_multiplier']} (expected 1)"
        )

    # Rule 6: execution_method
    valid_methods = {"MT5", "API_REST", "JSON_DUMP"}
    em = profile.get("execution_method", "JSON_DUMP")
    if em not in valid_methods:
        errors.append(f"execution_method '{em}' not in {valid_methods}")

    # Rule 7: asset_class
    if ac not in ASSET_CLASS_CONFIG:
        errors.append(f"asset_class '{ac}' not in {list(ASSET_CLASS_CONFIG.keys())}")

    # Rule 8: streak_block
    sb = profile.get("streak_block", 2)
    if sb <= 0:
        errors.append(f"streak_block must be > 0, got {sb}")

    # Rule 9: min_hold_sec
    mh = profile.get("min_hold_sec", 0)
    if mh < 0:
        errors.append(f"min_hold_sec must be >= 0, got {mh}")

    return errors


# ============================================================
# CONFIG CLASS — Main entry point
# ============================================================

class Config:
    """
    Universal Config — โหลด profile แล้ว flatten เป็น class attributes

    Usage:
        Config.load_profile("TTP_5K_FLEX")
        Config.validate("paper")
        print(Config.RISK_PER_TRADE_USD)  # 15.0
        print(Config.ASSET_CLASS)         # "EQUITIES"
    """

    # ══════════════════════════════════════════════════════════
    # CORE ENGINE SETTINGS (ไม่ขึ้นกับ Prop Firm)
    # ══════════════════════════════════════════════════════════

    TIMEFRAME           = "15m"
    TIMEFRAME_DAILY     = "1d"
    LOOKBACK_PERIOD     = "60d"
    CANDLE_INTERVAL_SEC = 15 * 60       # 900s

    # ── API Keys (จาก env)
    ALPACA_PAPER_KEY    = os.getenv("ALPACA_PAPER_KEY",    "")
    ALPACA_PAPER_SECRET = os.getenv("ALPACA_PAPER_SECRET", "")
    ALPACA_LIVE_KEY     = os.getenv("ALPACA_LIVE_KEY",     "")
    ALPACA_LIVE_SECRET  = os.getenv("ALPACA_LIVE_SECRET",  "")
    ALPACA_FEED         = os.getenv("ALPACA_FEED", "iex")   # "iex" (free) | "sip" (paid)
    BENZINGA_API_KEY    = os.getenv("BENZINGA_API_KEY",    "")

    # ── LLM Gate 19
    LLM_PRIMARY         = os.getenv("LLM_PRIMARY",   "CLAUDE")   # CLAUDE | OPENAI | GEMINI
    LLM_FALLBACK        = os.getenv("LLM_FALLBACK",  "OPENAI")   # fallback ถ้า primary fail
    LLM_ENABLED         = os.getenv("LLM_ENABLED", "true").lower() == "true"
    LLM_TIMEOUT_SEC     = 15
    LLM_FAIL_ACTION     = "ABORT"     # ABORT | EXECUTE (ถ้า LLM fail → default action)

    # ── Directories
    SIGNAL_DIR          = os.getenv("SIGNAL_DIR",  "./signals/")
    DEPLOY_MODE         = os.getenv("DEPLOY_MODE",  "local")
    GDRIVE_ROOT         = os.getenv("GDRIVE_ROOT",  "./gdrive_test")
    LOCAL_CACHE         = os.getenv("LOCAL_CACHE", "./cache_test")
    JOURNAL_DIR         = "./journal"
    MODEL_DIR           = "./models"
    PROFILE_DIR         = "./profiles"

    # ── ML Analyzer
    ML_SCORE_MIN        = 45
    ML_CONFIDENCE_MIN   = 0.40
    SCORE_REGIME_WEIGHT = 0.40
    SCORE_ML_WEIGHT     = 0.60
    ML_RETRAIN_HOUR_ET  = 8
    ML_WATCHLIST        = ["NVDA", "TSLA", "META", "AAPL",
                           "AMZN", "MRNA", "NFLX", "AMD"]
    DAILY_HORIZEN_BAR_MIN = 2

    # ── Abuse Prevention (global defaults)
    ORDER_COOLDOWN_SEC  = 3.0
    SYMBOL_COOLDOWN_SEC = 300.0
    PDT_WARN_THRESHOLD  = 3
    PDT_BLOCK_THRESHOLD = 4
    MAX_CANCEL_RATIO    = 0.60

    # ── LULD Halt
    LULD_FALLBACK_PCT   = 10.0
    LULD_FALLBACK_MIN   = 5
    HALT_CACHE_TTL_SEC  = 60.0

    # ── ATR-based SL/TP
    ATR_STOP_MULT       = 1.5
    RR_TARGET           = 2.0
    MIN_TP_CENTS        = 0.30

    # ── Scanner
    MIN_URGENCY         = 60
    MIN_GAP_PCT         = 2.0
    MIN_MORNING_RVOL    = 1.5

    # ══════════════════════════════════════════════════════════
    # PROFILE-DRIVEN SETTINGS (set by load_profile)
    # ══════════════════════════════════════════════════════════

    FIRM_NAME            = "Generic"
    ACCOUNT_LABEL        = ""
    ASSET_CLASS          = "EQUITIES"
    EXECUTION_METHOD     = "JSON_DUMP"
    ALLOW_FRACTIONAL     = False
    ACCOUNT_SIZE_USD     = 80_000.0
    MAX_BUYING_POWER     = 80_000.0
    MAX_DAILY_LOSS_PCT   = 2.0
    MAX_DAILY_LOSS_USD   = 700.0
    DAILY_LOSS_BUFFER    = 100.0
    RISK_PER_TRADE_PCT   = 0.125
    RISK_PER_TRADE_USD   = 100.0
    PROFIT_TARGET_USD    = 0
    MAX_ORDERS_PER_DAY   = 3
    MIN_HOLD_SEC         = 30
    MIN_TRADE_RANGE      = 0.10
    CONSISTENCY_RULE_PCT = 0.50
    OVERNIGHT_ALLOWED    = False
    OVERNIGHT_LIMIT_USD  = 0
    FLATTEN_TIME_ET      = (15, 45)
    NO_NEW_ENTRY_AFTER   = (14, 30)
    VOLUME_RULE_PCT      = 0.05
    STREAK_WARN          = 1
    STREAK_BLOCK         = 2
    COMMISSION_PER_SHARE = 0.005
    SLIPPAGE_PCT         = 0.001
    SPREAD_BUFFER_PCT    = 0.001
    MIN_PRICE            = 5.0
    MIN_ADV              = 1_000_000
    CONTRACT_MULTIPLIER  = 1
    TICK_SIZE            = 0.01

    # ── Cost Tracking (Enhancement)
    TYPICAL_SPREAD_PIPS      = 0.0        # avg spread in pips (CFD backtest)
    OVERNIGHT_SWAP_LONG      = 0.0        # annual % for long overnight
    OVERNIGHT_SWAP_SHORT     = 0.0        # annual % for short overnight
    MAX_SPREAD_MULTIPLIER    = 3.0        # delay entry if spread > N× avg
    ROLLOVER_COST_PER_CONTRACT = 0.0      # futures rollover cost

    # ── Profile metadata
    _active_profile_name = None
    _active_profile_raw  = {}

    # ══════════════════════════════════════════════════════════
    # PROFILE LOADER
    # ══════════════════════════════════════════════════════════

    @classmethod
    def load_profile(cls, profile_name: str):
        """
        โหลด Prop Firm profile จาก PROP_FIRM_PROFILES dict

        Args:
          profile_name: key ใน PROP_FIRM_PROFILES เช่น "TTP_5K_FLEX", "FTMO_100K"

        Raises:
          KeyError: ถ้า profile_name ไม่มีใน PROP_FIRM_PROFILES
          ConfigValidationError: ถ้า profile มี contradiction
        """
        if profile_name not in PROP_FIRM_PROFILES:
            available = list(PROP_FIRM_PROFILES.keys())
            raise KeyError(
                f"Profile '{profile_name}' not found. Available: {available}"
            )

        profile = PROP_FIRM_PROFILES[profile_name]
        cls._apply_profile(profile, profile_name)

    @classmethod
    def load_profile_from_file(cls, filepath: str):
        """โหลด profile จาก JSON file"""
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Profile file not found: {filepath}")

        with open(path, encoding="utf-8") as f:
            profile = json.load(f)

        name = profile.get("account_label", path.stem)
        cls._apply_profile(profile, name)

    @classmethod
    def load_profile_from_dict(cls, profile: dict, name: str = "custom"):
        """โหลด profile จาก dict (programmatic)"""
        cls._apply_profile(profile, name)

    @classmethod
    def _apply_profile(cls, profile: dict, name: str):
        """Flatten profile dict เข้าเป็น class attributes"""
        # ── Validate ก่อน apply
        errors = validate_profile(profile)
        hard_errors = [e for e in errors if not e.startswith("WARNING")]
        warnings    = [e for e in errors if e.startswith("WARNING")]

        if hard_errors:
            raise ConfigValidationError(
                f"Profile '{name}' has {len(hard_errors)} error(s):\n"
                + "\n".join(f"  ❌ {e}" for e in hard_errors)
            )
        for w in warnings:
            logger.warning(f"[Config] {w}")

        # ── Apply mapping (profile key → Config attribute)
        MAPPING = {
            "firm_name":            "FIRM_NAME",
            "account_label":        "ACCOUNT_LABEL",
            "asset_class":          "ASSET_CLASS",
            "execution_method":     "EXECUTION_METHOD",
            "allow_fractional":     "ALLOW_FRACTIONAL",
            "account_size_usd":     "ACCOUNT_SIZE_USD",
            "max_buying_power":     "MAX_BUYING_POWER",
            "max_daily_loss_pct":   "MAX_DAILY_LOSS_PCT",
            "max_daily_loss_usd":   "MAX_DAILY_LOSS_USD",
            "daily_loss_buffer":    "DAILY_LOSS_BUFFER",
            "risk_per_trade_pct":   "RISK_PER_TRADE_PCT",
            "risk_per_trade_usd":   "RISK_PER_TRADE_USD",
            "profit_target_usd":    "PROFIT_TARGET_USD",
            "max_orders_per_day":   "MAX_ORDERS_PER_DAY",
            "min_hold_sec":         "MIN_HOLD_SEC",
            "min_trade_range":      "MIN_TRADE_RANGE",
            "consistency_rule_pct": "CONSISTENCY_RULE_PCT",
            "overnight_allowed":    "OVERNIGHT_ALLOWED",
            "overnight_limit_usd":  "OVERNIGHT_LIMIT_USD",
            "volume_rule_pct":      "VOLUME_RULE_PCT",
            "streak_warn":          "STREAK_WARN",
            "streak_block":         "STREAK_BLOCK",
            "commission_per_share": "COMMISSION_PER_SHARE",
            "min_price":            "MIN_PRICE",
            "min_adv":              "MIN_ADV",
            "contract_multiplier":  "CONTRACT_MULTIPLIER",
            "tick_size":            "TICK_SIZE",
            # ── Cost Tracking (Enhancement)
            "typical_spread_pips":      "TYPICAL_SPREAD_PIPS",
            "overnight_swap_long":      "OVERNIGHT_SWAP_LONG",
            "overnight_swap_short":     "OVERNIGHT_SWAP_SHORT",
            "max_spread_multiplier":    "MAX_SPREAD_MULTIPLIER",
            "rollover_cost_per_contract": "ROLLOVER_COST_PER_CONTRACT",
        }

        for profile_key, attr_name in MAPPING.items():
            if profile_key in profile:
                setattr(cls, attr_name, profile[profile_key])

        # ── Special: tuple conversions
        if "flatten_time_et" in profile:
            cls.FLATTEN_TIME_ET = tuple(profile["flatten_time_et"])
        if "no_new_entry_after" in profile:
            cls.NO_NEW_ENTRY_AFTER = tuple(profile["no_new_entry_after"])

        cls._active_profile_name = name
        cls._active_profile_raw  = profile

        logger.info(
            f"✅ Profile loaded: {name}\n"
            f"   Firm:       {cls.FIRM_NAME} — {cls.ACCOUNT_LABEL}\n"
            f"   Asset:      {cls.ASSET_CLASS} | Exec: {cls.EXECUTION_METHOD}\n"
            f"   Account:    ${cls.ACCOUNT_SIZE_USD:,.0f}\n"
            f"   Risk/Trade: ${cls.RISK_PER_TRADE_USD:.0f} | "
            f"Daily Loss: ${cls.MAX_DAILY_LOSS_USD:.0f}\n"
            f"   Streak:     warn@{cls.STREAK_WARN} block@{cls.STREAK_BLOCK}\n"
            f"   LLM CIO:   {'ON' if cls.LLM_ENABLED else 'OFF'} "
            f"({cls.LLM_PRIMARY} → {cls.LLM_FALLBACK})"
        )

    # ══════════════════════════════════════════════════════════
    # HELPER: GET ACTIVE API KEYS
    # ══════════════════════════════════════════════════════════

    @classmethod
    def get_alpaca_keys(cls, mode: str = "paper") -> tuple:
        """
        คืน (api_key, api_secret) สำหรับ Alpaca ตาม mode

        Priority:
          paper → ALPACA_PAPER_KEY → fallback ALPACA_LIVE_KEY
          live  → ALPACA_LIVE_KEY  → fallback ALPACA_PAPER_KEY

        Usage:
          from config import Config
          key, secret = Config.get_alpaca_keys("paper")
        """
        if mode == "live":
            key    = cls.ALPACA_LIVE_KEY    or cls.ALPACA_PAPER_KEY
            secret = cls.ALPACA_LIVE_SECRET or cls.ALPACA_PAPER_SECRET
        else:
            key    = cls.ALPACA_PAPER_KEY    or cls.ALPACA_LIVE_KEY
            secret = cls.ALPACA_PAPER_SECRET or cls.ALPACA_LIVE_SECRET
        return key, secret

    # ══════════════════════════════════════════════════════════
    # VALIDATION
    # ══════════════════════════════════════════════════════════

    @classmethod
    def validate(cls, mode: str = "paper"):
        """
        Validate Config ก่อนรัน — ตรวจ API keys + LLM keys + profile

        Raises:
          EnvironmentError: ถ้าขาด required API key
          ConfigValidationError: ถ้า profile ขัดแย้ง
        """
        # ── ตรวจ profile loaded
        if cls._active_profile_name is None:
            logger.warning("[Config] No profile loaded — using defaults")

        # ── ตรวจ Alpaca keys (สำหรับ EQUITIES data feed)
        if cls.ASSET_CLASS == "EQUITIES":
            if mode == "paper":
                missing = [k for k, v in [
                    ("ALPACA_PAPER_KEY",    cls.ALPACA_PAPER_KEY),
                    ("ALPACA_PAPER_SECRET", cls.ALPACA_PAPER_SECRET),
                ] if not v]
            else:
                missing = [k for k, v in [
                    ("ALPACA_LIVE_KEY",    cls.ALPACA_LIVE_KEY),
                    ("ALPACA_LIVE_SECRET", cls.ALPACA_LIVE_SECRET),
                ] if not v]
            if missing:
                raise EnvironmentError(
                    f"Missing env vars for EQUITIES: {missing}\n"
                    f"Set: export {missing[0]}='your_key'"
                )

        # ── ตรวจ LLM keys
        if cls.LLM_ENABLED:
            primary_cfg  = LLM_PROVIDERS.get(cls.LLM_PRIMARY, {})
            fallback_cfg = LLM_PROVIDERS.get(cls.LLM_FALLBACK, {})

            primary_key  = os.getenv(primary_cfg.get("api_key_env", ""), "")
            fallback_key = os.getenv(fallback_cfg.get("api_key_env", ""), "")

            if not primary_key and not fallback_key:
                logger.warning(
                    f"[Config] Gate 19 LLM enabled but no API keys found "
                    f"({cls.LLM_PRIMARY} / {cls.LLM_FALLBACK}). "
                    f"Gate 19 will {cls.LLM_FAIL_ACTION} all trades."
                )

        # ── สร้าง directories
        for d in [cls.SIGNAL_DIR, cls.JOURNAL_DIR, cls.MODEL_DIR, cls.PROFILE_DIR]:
            os.makedirs(d, exist_ok=True)

        logger.info(
            f"✅ Config validated | mode={mode} | "
            f"profile={cls._active_profile_name} | "
            f"timeframe={cls.TIMEFRAME}"
        )

    # ══════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════

    @classmethod
    def get_asset_config(cls) -> dict:
        """คืน asset class config สำหรับ active profile"""
        return ASSET_CLASS_CONFIG.get(cls.ASSET_CLASS, ASSET_CLASS_CONFIG["EQUITIES"])

    @classmethod
    def get_llm_config(cls, provider: str = None) -> dict:
        """คืน LLM provider config"""
        provider = provider or cls.LLM_PRIMARY
        return LLM_PROVIDERS.get(provider, LLM_PROVIDERS["CLAUDE"])

    @classmethod
    def get_sizing_unit(cls) -> str:
        """คืน 'SHARES' | 'LOTS' | 'CONTRACTS' ตาม asset class"""
        return cls.get_asset_config()["sizing_unit"]

    @classmethod
    def is_futures(cls) -> bool:
        return cls.ASSET_CLASS == "FUTURES"

    @classmethod
    def is_cfd(cls) -> bool:
        return cls.ASSET_CLASS == "CFD"

    @classmethod
    def is_equities(cls) -> bool:
        return cls.ASSET_CLASS == "EQUITIES"

    @classmethod
    def export_active_profile(cls, filepath: str = None):
        """Export active profile เป็น JSON file"""
        if not cls._active_profile_raw:
            logger.warning("No active profile to export")
            return
        filepath = filepath or f"{cls.PROFILE_DIR}/{cls._active_profile_name}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(cls._active_profile_raw, f, indent=2, ensure_ascii=False)
        logger.info(f"Profile exported → {filepath}")

    @classmethod
    def summary(cls) -> str:
        """One-line summary ของ active config"""
        return (
            f"[{cls._active_profile_name or 'default'}] "
            f"{cls.FIRM_NAME} {cls.ASSET_CLASS} | "
            f"${cls.ACCOUNT_SIZE_USD:,.0f} | "
            f"risk=${cls.RISK_PER_TRADE_USD:.0f}/trade | "
            f"exec={cls.EXECUTION_METHOD} | "
            f"LLM={'ON' if cls.LLM_ENABLED else 'OFF'}"
        )


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")

    print("=" * 60)
    print("  CONFIG — Universal Profile Loader Test")
    print("=" * 60)

    # ── Test 1: Load TTP
    print("\n[1] Load TTP_5K_FLEX")
    Config.load_profile("TTP_5K_FLEX")
    print(f"    {Config.summary()}")
    assert Config.ASSET_CLASS == "EQUITIES"
    assert Config.ALLOW_FRACTIONAL is False
    assert Config.RISK_PER_TRADE_USD == 15.0

    # ── Test 2: Load FTMO
    print("\n[2] Load FTMO_100K")
    Config.load_profile("FTMO_100K")
    print(f"    {Config.summary()}")
    assert Config.ASSET_CLASS == "CFD"
    assert Config.ALLOW_FRACTIONAL is True
    assert Config.CONTRACT_MULTIPLIER == 100_000

    # ── Test 3: Load Topstep
    print("\n[3] Load TOPSTEP_50K_NQ")
    Config.load_profile("TOPSTEP_50K_NQ")
    print(f"    {Config.summary()}")
    assert Config.ASSET_CLASS == "FUTURES"
    assert Config.ALLOW_FRACTIONAL is False
    assert Config.get_sizing_unit() == "CONTRACTS"

    # ── Test 4: Contradiction detection
    print("\n[4] Contradiction: FUTURES + fractional=True")
    bad_profile = {
        "asset_class": "FUTURES", "allow_fractional": True,
        "execution_method": "API_REST",
        "max_daily_loss_usd": 1000, "risk_per_trade_usd": 500,
        "flatten_time_et": [15, 45], "no_new_entry_after": [14, 30],
        "streak_block": 2,
    }
    try:
        Config.load_profile_from_dict(bad_profile, "BAD")
        print("    ❌ Should have raised!")
    except ConfigValidationError as e:
        print(f"    ✅ Caught: {e}")

    # ── Test 5: Risk > Daily Loss
    print("\n[5] Contradiction: risk > daily_loss")
    bad2 = {
        "asset_class": "EQUITIES", "execution_method": "JSON_DUMP",
        "max_daily_loss_usd": 100, "risk_per_trade_usd": 200,
        "flatten_time_et": [15, 45], "no_new_entry_after": [14, 30],
        "streak_block": 2,
    }
    try:
        Config.load_profile_from_dict(bad2, "BAD2")
        print("    ❌ Should have raised!")
    except ConfigValidationError as e:
        print(f"    ✅ Caught: {e}")

    print("\n✅ All tests passed!")
