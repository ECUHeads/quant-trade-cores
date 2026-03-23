"""
main.py
=======
Universal 15m Quant Engine — Event-Driven Orchestrator

Pipeline (per 15m candle close):
  Data Fetch → ML Gate 6 → Risk Gate 1-7 → WC Gate → LLM CIO Gate 19 → Executor

Loop Timing:
  ตื่นเฉพาะนาทีที่ 00, 15, 30, 45 (cron-style)
  News scan ทำงาน background แยกจาก main loop

Session Control:
  No New Entry after  Config.NO_NEW_ENTRY_AFTER  (default 14:30 ET)
  Flatten All at      Config.FLATTEN_TIME_ET      (default 15:45 ET)

Timezone:
  ใช้ ZoneInfo("America/New_York") — DST-aware

Modes:
  paper   — ข้อมูลจริง, ไม่สั่ง order จริง (MockExecutor)
  live    — ข้อมูลจริง, สั่ง order จริง (ต้อง confirm)
  shadow  — ข้อมูลจริง, รันทุก gate แบบ non-blocking, ไม่สั่ง order
             บันทึกผลทุก gate → console + JSON report

Usage:
  python main.py --profile TTP_5K_FLEX --mode paper
  python main.py --profile FTMO_100K --mode live
  python main.py --profile TTP_5K_FLEX --dry-run
  python main.py --report

  # Shadow mode
  python main.py --mode shadow                                     # one-shot
  python main.py --mode shadow --live-shadow                       # live continuous
  python main.py --mode shadow --skip-gates gate19,session         # skip gates
  python main.py --mode shadow --shadow-symbols NVDA,TSLA,META     # specific symbols
  python main.py --mode shadow --shadow-watchlist ./universe.json  # from file

  # Technical Scanner (dual-trigger: news + indicator)
  python main.py --mode paper --enable-tech-scan
  python main.py --mode live --enable-tech-scan
  python main.py --mode shadow --enable-tech-scan --skip-gates gate19
"""

import os
import time
import signal
import logging
import argparse
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

# ── Internal modules
from config.config import Config
from ext_data.news_scanner import (NewsScanner, NewsCandidate,
                           MarketSessionFilter, CatalystClassifier)
from orders.trade_journal import TradeJournal, TradeRecord
from gates.gate_19_llm_cio import Gate19LLMCio, CIOVerdict

# ── Worst Case Detector (optional)
try:
    from models.worst_case_detector import WorstCaseGate, WorstCaseVerdict
except ImportError:
    WorstCaseGate = None
    WorstCaseVerdict = None

# ── Third-party (optional)
try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    import pandas as pd
except ImportError:
    pd = None

# ============================================================
# LOGGING — RotatingFileHandler (ป้องกัน disk เต็ม)
# ============================================================
#
# RotatingFileHandler ตัดไฟล์ log อัตโนมัติ:
#   trading.log       ← ไฟล์ปัจจุบัน (max 10MB)
#   trading.log.1     ← ไฟล์ก่อนหน้า
#   trading.log.2     ← ...
#   trading.log.5     ← เก่าสุด → ถูกลบเมื่อมีไฟล์ใหม่
#
# รวม: 10MB × 5 = 50MB สูงสุด (ไม่กิน disk เพิ่ม)
# ไฟล์ที่ถูก rotate ออก → deploy/log_archiver.py ส่งไป GDrive

from logging.handlers import RotatingFileHandler
import os as _os

_log_dir = _os.getenv("LOG_DIR", "./logs")
_os.makedirs(_log_dir, exist_ok=True)

_log_format = logging.Formatter(
    "%(asctime)s %(levelname)-8s [%(name)-20s] %(message)s",
    datefmt="%H:%M:%S",
)

# Console handler
_console = logging.StreamHandler()
_console.setFormatter(_log_format)

# Rotating file handler — 10MB per file, keep 5 backups
_file_handler = RotatingFileHandler(
    filename=f"{_log_dir}/trading.log",
    maxBytes=10 * 1024 * 1024,     # 10 MB
    backupCount=5,                  # keep trading.log.1 ~ .5
    encoding="utf-8",
)
_file_handler.setFormatter(_log_format)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console, _file_handler],
)
logger = logging.getLogger("Main")

_TZ_ET = ZoneInfo("America/New_York")

def now_et() -> datetime:
    return datetime.now(_TZ_ET)


# ============================================================
# LAZY IMPORTS
# ============================================================

def _load_regime_scorer():
    try:
        from models.regime_scorer import RegimeWeightedScorer
        return RegimeWeightedScorer
    except ImportError:
        return None

def _load_ml_analyzer():
    ###from technical_ml_analyzer import TechnicalMLAnalyzer
    ##self.ml_analyzer = TechnicalMLAnalyzer(model_dir="./models")
    ###
    try:
        from models.technical_ml_analyzer import TechnicalMLAnalyzer, MLPrediction
        return TechnicalMLAnalyzer, MLPrediction
    except ImportError:
        return None, None

def _load_universe_preprocessor():
    try:
        from orders.universe_preprocessor import UniversePreprocessor
        return UniversePreprocessor
    except ImportError:
        return None

def _load_risk_manager():
    try:
        from orders.universal_risk_manager import UniversalRiskManager
        return UniversalRiskManager
    except ImportError:
        return None

def _load_executor():
    try:
        from orders.universal_order_executor import UniversalOrderExecutor
        return UniversalOrderExecutor
    except ImportError:
        return None

def _load_wc_gate():
    try:
        from models.worst_case_detector import WorstCaseGate
        return WorstCaseGate
    except ImportError:
        return None


# ============================================================
# MOCK CLASSES (dry-run / missing modules)
# ============================================================

class MockRegimeScorer:
    current_vix = 16.0; current_spy = 578.0
    def fetch_market_sentiment(self):
        return {"sentiment_score": 0.70, "vix": 16.0}
    def process_stock(self, sym, df, sent):
        return {"Final_Weighted_Score": 65, "Raw_Score_Momentum": 65,
                "Raw_Score_MeanRev": 50, "Action_Signal": "⏳ WAIT"}

class MockExecutor:
    def check_system_health(self, **kw):
        return {"status": "OK", "today_pnl": 0.0, "buying_power": 80_000.0}
    def submit_bracket_order(self, symbol, shares, side, stop_loss_price,
                              take_profit_price, **kw):
        logger.info(f"[DRY] ORDER → {side} {shares}x {symbol}")
        class _O: id = f"DRY-{time.time():.0f}"; status = "dry_run"
        return _O()
    def flatten_all_positions(self):
        logger.info("[DRY] FLATTEN")
    def get_open_positions(self): return []
    def flush_queue(self): return 0


class DryRunExecutor:
    """
    Wrapper รอบ Alpaca executor จริง
    - READ operations (health, positions, account) → ส่งต่อให้ real executor
    - WRITE operations (submit order, flatten) → mock / log เท่านั้น
    """
    def __init__(self, real_executor):
        self._real = real_executor
        logger.info("[DryRunExecutor] Wrapped real Alpaca — read=REAL, write=MOCK")

    def check_system_health(self, **kw):
        try:
            result = self._real.check_system_health(**kw)
            logger.info(f"[DRY] Health (REAL Alpaca): {result}")
            return result
        except Exception as e:
            logger.warning(f"[DRY] Health fallback: {e}")
            return {"status": "OK", "today_pnl": 0.0, "buying_power": 80_000.0}

    def get_open_positions(self):
        try:
            return self._real.get_open_positions()
        except Exception:
            return []

    def submit_bracket_order(self, symbol, shares, side, stop_loss_price,
                              take_profit_price, **kw):
        entry = kw.get("entry_price", 0)
        logger.info(
            f"[DRY] ORDER (NOT SENT) → {side} {shares}x {symbol} "
            f"@ {entry} SL={stop_loss_price} TP={take_profit_price}"
        )
        class _O: id = f"DRY-{time.time():.0f}"; status = "dry_run"
        return _O()

    def flatten_all_positions(self):
        logger.info("[DRY] FLATTEN (NOT SENT)")

    def flush_queue(self):
        try:
            return self._real.flush_queue()
        except Exception:
            return 0


# ============================================================
# TRADING PIPELINE
# ============================================================

class TradingPipeline:
    """
    Universal 15m Pipeline

    Pipeline per NewsCandidate:
      Gate 1:  Daily Loss Kill-Switch
      Gate 2:  Session Filter + No-New-Entry cutoff
      Gate 3:  Market Sentiment (VIX/SPY)
      Gate 4:  Price Fetch
      Gate 5:  Universe Filter (Gap%, RVOL) — bypass สำหรับ Futures
      Gate 6:  RegimeWeightedScorer
      Gate ML: TechnicalMLAnalyzer
      Gate A:  Max orders/day
      Gate B:  Rate limiter
      Gate C:  Wash-sale cooldown
      Gate D:  PDT check
      Gate E:  Cancel ratio
      Gate F:  LULD halt check
      Gate H:  No hedge
      Gate I:  Streak escalation (session-based)
      Gate 7:  UniversalRiskManager (ATR-based SL/TP)
      Gate WC: WorstCaseDetector (Whipsaw/Chop/MAE Veto)
      Gate 19: LLM CIO (EXECUTE/REDUCE/DELAY/ABORT)
      →        UniversalOrderExecutor
      →        TradeJournal
    """

    def __init__(self, mode: str = "paper", dry_run: bool = False):
        self.mode    = mode
        self.dry_run = dry_run
        self.cfg     = Config
        self._stop   = threading.Event()

        logger.info(f"{'='*60}")
        logger.info(f"  UNIVERSAL 15m QUANT ENGINE + LLM CIO (Gate 19)")
        logger.info(f"  {Config.summary()}")
        logger.info(f"  mode={mode.upper()}  dry_run={dry_run}")
        logger.info(f"{'='*60}")

        self._init_modules()

        # ── State
        self._today_pnl:       float = 0.0
        self._trade_count:     int   = 0
        self._open_trades:     dict  = {}
        self._market_cache:    Optional[dict] = None
        self._market_cache_ts: float = 0.0
        self._last_order_time: float = 0.0
        self._symbol_cooldown: dict  = {}
        self._daily_order_count: int = 0
        self._pdt_dates:       list  = []
        self._orders_submitted: int  = 0
        self._orders_filled:    int  = 0
        self._entry_times:     dict  = {}
        self._halt_cache:      dict  = {}

        # ── Tiered Watchlist (Hybrid approach)
        from data_pipeline_manager import TieredWatchlist
        self._tiers: Optional[TieredWatchlist] = None

    def _init_modules(self):
        # 1. Regime Scorer
        RS = _load_regime_scorer()
        self.regime = RS(lookback_days=30) if RS else MockRegimeScorer()

        # 2. Universe Preprocessor
        UP = _load_universe_preprocessor()
        self.preprocessor = UP.from_config(self.cfg) if UP else None

        # 3. Risk Manager
        RM = _load_risk_manager()
        self.risk = RM.from_config(self.cfg) if RM else None

        # 4. Executor
        EX = _load_executor()
        if self.dry_run:
            if EX:
                try:
                    real_ex = EX.from_config(self.cfg)
                    self.executor = DryRunExecutor(real_ex)
                except Exception as e:
                    logger.warning(f"[DRY] ไม่สามารถเชื่อม Alpaca: {e} → fallback MockExecutor")
                    self.executor = MockExecutor()
            else:
                self.executor = MockExecutor()
        else:
            self.executor = EX.from_config(self.cfg) if EX else MockExecutor()

        # 5. Trade Journal
        self.journal = TradeJournal(output_dir=self.cfg.JOURNAL_DIR)

        # 6. Session Filter
        self.session_filter = MarketSessionFilter()

        # 7. ML Analyzer
        ml_mod = _load_ml_analyzer()
        if ml_mod[0]:
            
            self.ml_analyzer = ml_mod[0](model_dir=self.cfg.MODEL_DIR)
            self._MLPrediction = ml_mod[1]
        else:
            self.ml_analyzer = None
            self._MLPrediction = None

        # 8. Gate 19 LLM CIO
        self.gate19 = Gate19LLMCio()

        # 9. Worst Case Gate (Toxic Market Veto)
        WCG = _load_wc_gate()
        if WCG:
            self.wc_gate = WCG(
                model_dir=self.cfg.MODEL_DIR,
                danger_threshold=getattr(self.cfg, "WC_DANGER_THRESHOLD", 0.45),
            )
            logger.info("🛡️ Worst Case Gate loaded")
        else:
            self.wc_gate = None

        # 10. Technical Scanner (disabled by default, --enable-tech-scan)
        self.tech_scanner = None  # set by run_live/run_shadow if enabled

        logger.info("✅ All modules loaded")

    # ------------------------------------------
    # MAIN ENTRY: Process News Candidate
    # ------------------------------------------

    def process_news(self, candidate: NewsCandidate):
        sym = candidate.symbol
        logger.info(f"{'─'*55}")
        logger.info(f"📰 [{candidate.source}] {sym} | {candidate.catalyst_type} | urgency={candidate.urgency_score}")

        # ── Guard: no double-entry
        if sym in self._open_trades:
            logger.info(f"⏭ {sym} already open → skip")
            return

        # ── GATE 0: Tier Check + JIT Promotion
        # Tier 1/2 → ผ่าน (model trained แล้ว)
        # Tier 3   → promote + JIT train (~10s)
        # ไม่อยู่ใน universe → skip (liquidity risk)
        if not self._promote_and_jit_train(sym, candidate.catalyst_type):
            logger.info(f"⏭ {sym} JIT failed or not in universe → skip")
            return

        tier = self._tiers.get_tier(sym) if self._tiers else 0
        if tier > 0:
            tier_label = {1: "🔥HOT", 2: "🌡️WARM", 3: "❄️COLD→HOT"}
            logger.info(f"  Tier: {tier_label.get(tier, '?')} ({tier})")

        # ── GATE 1: Daily Loss Kill-Switch
        if not self._gate_daily_loss():
            return

        # ── GATE 2: Session + No-New-Entry after cutoff
        if self.dry_run:
            session = "MARKET"
            logger.info(f"  [DRY] bypass Gate 2 → session={session}")
        else:
            session = self.session_filter.current_session()
            if not self.session_filter.is_tradeable(session, candidate.catalyst_type):
                logger.info(f"⏸ {sym} session={session} → skip")
                return

            h_cut, m_cut = self.cfg.NO_NEW_ENTRY_AFTER
            t = now_et()
            if t.hour > h_cut or (t.hour == h_cut and t.minute >= m_cut):
                logger.info(f"⏸ No New Entry after {h_cut}:{m_cut:02d} ET → skip")
                return

        # ── GATE 3: Sentiment
        if self.dry_run:
            sentiment = {"sentiment_score": 0.70, "vix": 16.0}
            logger.info(f"  [DRY] bypass Gate 3 → mock sentiment={sentiment['sentiment_score']}")
        else:
            sentiment = self._get_market_sentiment()
            if sentiment["sentiment_score"] < 0.35:
                logger.warning(f"🚫 Sentiment {sentiment['sentiment_score']:.2f} too low")
                return

        # ── GATE 4: Price
        price = self._fetch_price(sym)
        if not price:
            return

        # ── GATE 5: Universe Filter (bypass for Futures/CFD)
        if self.preprocessor:
            prev_close = self._fetch_prev_close(sym)
            if not self.preprocessor.check_tradeable(sym, price, prev_close):
                return

        # ── GATE 6: Regime Scorer
        if self.dry_run:
            score_result = {"Final_Weighted_Score": 72, "Raw_Score_Momentum": 70,
                            "Raw_Score_MeanRev": 55, "Action_Signal": "🟢 BUY"}
            regime_score = score_result["Final_Weighted_Score"]
            logger.info(f"  [DRY] bypass Gate 6 → mock regime={regime_score}")
        else:
            score_result = self._score_stock(sym, sentiment)
            regime_score = score_result.get("Final_Weighted_Score", 0)
            if regime_score < 50:
                logger.info(f"⛔ {sym} regime={regime_score} < 50")
                return

        # ── GATE ML: ML Analyzer (ก่อน side — เพื่อให้ 3-class กำหนดทิศทาง)
        ml_prediction = self._run_ml_gate(sym, candidate.catalyst_type,
                                           candidate.urgency_score, session)
        if ml_prediction and ml_prediction.ml_score < self.cfg.ML_SCORE_MIN:
            logger.info(f"⛔ ML {sym} score={ml_prediction.ml_score} < {self.cfg.ML_SCORE_MIN}")
            return

        # ── Side: ให้ ML 3-class กำหนด, fallback catalyst
        catalyst_side = self._determine_side(candidate.catalyst_type)

        if ml_prediction and ml_prediction.signal != "NEUTRAL":
            ml_side = "buy" if ml_prediction.signal == "LONG" else "sell"
            if ml_side != catalyst_side:
                logger.info(
                    f"⚠️ ML override: ML={ml_prediction.signal} vs catalyst={catalyst_side.upper()} "
                    f"| class={ml_prediction.predicted_class:+d} "
                    f"P(sell={ml_prediction.class_probs['sell']:.2f} "
                    f"neu={ml_prediction.class_probs['neutral']:.2f} "
                    f"buy={ml_prediction.class_probs['buy']:.2f}) "
                    f"conf={ml_prediction.confidence:.2f} → ใช้ ML"
                )
            side = ml_side
        else:
            side = catalyst_side
            if ml_prediction:
                logger.info(
                    f"  ML={ml_prediction.signal} (class={ml_prediction.predicted_class:+d}) "
                    f"→ fallback catalyst side={side}"
                )

        combined_score = regime_score
        if ml_prediction:
            combined_score = (
                self.cfg.SCORE_REGIME_WEIGHT * regime_score +
                self.cfg.SCORE_ML_WEIGHT * ml_prediction.ml_score
            )
            if combined_score < 50:
                logger.info(f"⛔ Combined {combined_score:.1f} < 50")
                return

        # ── GATE A: Max orders/day
        if self._daily_order_count >= self.cfg.MAX_ORDERS_PER_DAY:
            logger.warning(f"🛑 Max orders {self.cfg.MAX_ORDERS_PER_DAY} reached")
            return

        # ── GATE B: Rate limiter
        elapsed = time.time() - self._last_order_time
        if elapsed < self.cfg.ORDER_COOLDOWN_SEC:
            time.sleep(self.cfg.ORDER_COOLDOWN_SEC - elapsed)

        # ── GATE C: Wash-sale
        unlock = self._symbol_cooldown.get(sym, 0.0)
        if time.time() < unlock:
            logger.info(f"⏳ {sym} wash-sale cooldown")
            return

        # ── GATE H: No hedge
        if sym in self._open_trades:
            return

        # ── GATE I: Streak (session-based, ห้ามนับข้ามวัน)
        if self.journal.check_streak_halt(self.cfg.STREAK_BLOCK):
            logger.critical(f"🛑 Streak halt: {self.cfg.STREAK_BLOCK} consecutive losses today")
            return

        # ── ATR + SL/TP
        atr = self._fetch_atr_15m(sym)
        if atr <= 0:
            atr = price * 0.005

        entry_price = round(price * (1.002 if side == "buy" else 0.998), 2)
        if self.risk:
            stop_price, target_price = self.risk.calculate_atr_levels(entry_price, atr, side)
        else:
            offset = atr * self.cfg.ATR_STOP_MULT
            stop_price = round(entry_price - offset if side == "buy" else entry_price + offset, 2)
            target_price = round(entry_price + offset * self.cfg.RR_TARGET if side == "buy"
                                 else entry_price - offset * self.cfg.RR_TARGET, 2)

        # ── GATE 7: Risk Manager
        if self.risk:
            order_spec = self.risk.calculate_order(
                symbol=sym, side=side, entry_price=entry_price,
                stop_loss_price=stop_price,
                current_daily_loss=abs(self._today_pnl) if self._today_pnl < 0 else 0,
            )
            if order_spec.get("status") != "APPROVED":
                logger.warning(f"⛔ Risk rejected: {order_spec.get('reason')}")
                return
            shares = order_spec["size"]
        else:
            shares = 10  # fallback

        # ══════════════════════════════════════════════════════
        # GATE WC: Worst Case Detector — Toxic Market Veto
        # ══════════════════════════════════════════════════════
        if self.wc_gate and getattr(self.cfg, "WC_ENABLED", True):
            df_15m = self._fetch_15m_bars(sym, bars=30)
            if df_15m is not None and len(df_15m) >= 20:
                wc_verdict = self.wc_gate.evaluate(
                    symbol=sym, df_15m=df_15m, atr=atr,
                )
                if wc_verdict.is_danger:
                    logger.warning(
                        f"🛡️ Gate WC VETO {sym} | "
                        f"danger={wc_verdict.danger_score:.3f} | "
                        f"top={wc_verdict.top_features[:3]} | "
                        f"{wc_verdict.latency_ms}ms"
                    )
                    return

        # ══════════════════════════════════════════════════════
        # GATE 19: LLM CIO — Final Risk Veto
        # ══════════════════════════════════════════════════════
        if self.dry_run:
            verdict = CIOVerdict(
                action="EXECUTE", sizing_multiplier=1.0,
                reasoning="DRY-RUN mock — auto EXECUTE",
                provider="DRY_RUN", latency_ms=0,
            )
            logger.info(f"  [DRY] bypass Gate 19 → mock verdict=EXECUTE")
        else:
            intraday = self._fetch_intraday_context(sym)
            verdict = self.gate19.evaluate_trade(
                market_data={
                    "symbol": sym, "price": price,
                    "prev_close": intraday["prev_close"],
                    "vwap": intraday["vwap"],
                    "day_high": intraday["day_high"],
                    "day_low": intraday["day_low"],
                    "atr_15m": atr, "vix": sentiment.get("vix", 20),
                    "spy_trend": "up" if sentiment["sentiment_score"] > 0.5 else "down",
                    "session": session,
                    "timeframe": self.cfg.TIMEFRAME,
                },
                ml_signal={
                    "ml_score": ml_prediction.ml_score if ml_prediction else int(regime_score),
                    "direction_prob": ml_prediction.direction_prob if ml_prediction else 0.5,
                    "confidence": ml_prediction.confidence if ml_prediction else 0.5,
                    "signal": ml_prediction.signal if ml_prediction else side.upper(),
                    "predicted_class": ml_prediction.predicted_class if ml_prediction else 0,
                    "class_probs": ml_prediction.class_probs if ml_prediction else {},
                    "top_features": (ml_prediction.top_features if ml_prediction else []),
                },
                risk_data={
                    "shares": shares, "entry": entry_price,
                    "stop_loss": stop_price, "take_profit": target_price,
                    "risk_usd": order_spec.get("actual_risk_usd", 0) if self.risk else 0,
                    "daily_pnl": self._today_pnl,
                    "daily_loss_limit": self.cfg.MAX_DAILY_LOSS_USD,
                    "trades_today": self._daily_order_count,
                    "consecutive_losses": self.journal.compute_daily_streak(),
                },
                config_rules={
                    "firm_name": self.cfg.FIRM_NAME,
                    "max_daily_loss": self.cfg.MAX_DAILY_LOSS_USD,
                    "max_orders_per_day": self.cfg.MAX_ORDERS_PER_DAY,
                    "streak_block": self.cfg.STREAK_BLOCK,
                    "consistency_rule": self.cfg.CONSISTENCY_RULE_PCT,
                },
                news_context=f"{candidate.catalyst_type}: {candidate.headline[:120]}",
            )

        # ── Apply CIO verdict
        if not verdict.is_go():
            logger.info(
                f"🛑 Gate 19 {verdict.action}: {verdict.reasoning} "
                f"| {verdict.provider} {verdict.latency_ms}ms"
            )
            return

        # ── Adjust size (CIO can only reduce, never increase)
        adjusted_shares = max(1, int(shares * verdict.sizing_multiplier))
        if adjusted_shares != shares:
            logger.info(f"[Gate 19] Size adjusted: {shares} → {adjusted_shares}")

        # ══════════════════════════════════════════════════════
        # EXECUTE ORDER
        # ══════════════════════════════════════════════════════
        order = self.executor.submit_bracket_order(
            symbol=sym, shares=adjusted_shares,
            side="LONG" if side == "buy" else "SHORT",
            stop_loss_price=stop_price, take_profit_price=target_price,
            entry_price=entry_price,
            metadata={
                "ml_score": ml_prediction.ml_score if ml_prediction else 0,
                "regime_score": regime_score,
                "cio_action": verdict.action,
                "cio_mult": verdict.sizing_multiplier,
                "catalyst": candidate.catalyst_type,
                "atr_15m": round(atr, 4),
            },
        )
        if not order:
            return

        # ── Update state
        now = time.time()
        self._last_order_time    = now
        self._daily_order_count += 1
        self._orders_submitted  += 1
        oid = str(getattr(order, "id", sym))
        self._entry_times[oid]   = now

        # ── Journal
        ml_notes = f"cio={verdict.action} mult={verdict.sizing_multiplier} "
        if ml_prediction:
            ml_notes += (
                f"ml={ml_prediction.ml_score} conf={ml_prediction.confidence:.2f} "
                f"class={ml_prediction.predicted_class:+d} sig={ml_prediction.signal} "
                f"P(s={ml_prediction.class_probs['sell']:.2f} "
                f"n={ml_prediction.class_probs['neutral']:.2f} "
                f"b={ml_prediction.class_probs['buy']:.2f})"
            )

        trade_id = self.journal.open_trade(
            symbol=sym, side=side, catalyst_type=candidate.catalyst_type,
            urgency_score=candidate.urgency_score,
            planned_entry=entry_price, actual_entry=entry_price,
            shares=adjusted_shares, stop_price=stop_price,
            target_price=target_price,
            alpaca_order_id=oid,
            news_headline=candidate.headline, news_source=candidate.source,
            regime_sentiment=sentiment["sentiment_score"],
            vix_at_entry=sentiment["vix"],
            spy_at_entry=getattr(self.regime, "current_spy", 0.0),
            market_session=session, final_score=round(combined_score, 2),
            notes=ml_notes,
        )
        self._open_trades[sym] = trade_id

        ml_class_info = ""
        if ml_prediction:
            ml_class_info = f" | ML={ml_prediction.signal}({ml_prediction.predicted_class:+d})"

        logger.info(
            f"✅ TRADE [{trade_id}] {side.upper()} {adjusted_shares}x {sym} "
            f"@ {entry_price:.2f} SL={stop_price:.2f} TP={target_price:.2f} "
            f"| Gate19={verdict.action}{ml_class_info} "
            f"| orders={self._daily_order_count}/{self.cfg.MAX_ORDERS_PER_DAY}"
        )

    # ------------------------------------------
    # HELPERS
    # ------------------------------------------

    def _gate_daily_loss(self) -> bool:
        try:
            health = self.executor.check_system_health(
                max_daily_loss=self.cfg.MAX_DAILY_LOSS_USD)
            if health["status"] == "HALT":
                logger.critical("🛑 Daily loss HALT")
                return False
            self._today_pnl = health.get("today_pnl", self._today_pnl)
        except Exception:
            pass
        limit = self.cfg.MAX_DAILY_LOSS_USD - self.cfg.DAILY_LOSS_BUFFER
        if abs(min(self._today_pnl, 0)) >= limit:
            logger.critical(f"🛑 Daily loss near limit (${self._today_pnl:.2f})")
            return False
        return True

    def _get_market_sentiment(self) -> dict:
        now = time.time()
        if self._market_cache and (now - self._market_cache_ts) < 900:
            return self._market_cache
        try:
            s = self.regime.fetch_market_sentiment()
            self._market_cache = s
            self._market_cache_ts = now
            return s
        except Exception:
            return {"sentiment_score": 0.5, "vix": 20.0}

    def _fetch_price(self, symbol: str) -> Optional[float]:
        """ดึงราคาล่าสุดจาก Alpaca snapshot → yfinance fallback"""
        # ── Try snapshot cache from tiered scan
        if self._tiers and self._tiers.snapshot.get(symbol):
            p = self._tiers.snapshot[symbol].get("price", 0)
            if p > 0:
                return float(p)
        # ── Alpaca snapshot (single)
        try:
            from data_pipeline_manager import get_alpaca_snapshots
            snaps = get_alpaca_snapshots([symbol])
            if symbol in snaps and snaps[symbol]["price"] > 0:
                return float(snaps[symbol]["price"])
        except Exception:
            pass
        # ── yfinance fallback
        try:
            if yf:
                info = yf.Ticker(symbol).fast_info
                p = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                return float(p) if p else None
        except Exception:
            pass
        return None

    def _fetch_prev_close(self, symbol: str) -> float:
        """ดึง previous close จาก snapshot cache → yfinance fallback"""
        if self._tiers and self._tiers.snapshot.get(symbol):
            pc = self._tiers.snapshot[symbol].get("prev_close", 0)
            if pc > 0:
                return float(pc)
        try:
            if yf:
                return float(yf.Ticker(symbol).fast_info.previous_close or 0)
        except Exception:
            pass
        return 0.0

    def _fetch_atr_15m(self, symbol: str) -> float:
        try:
            from data_pipeline_manager import safe_download, compute_atr_15m
            df = safe_download(symbol, period="5d", interval="15m")
            if not df.empty and len(df) >= 14:
                atr = compute_atr_15m(df)
                val = float(atr.iloc[-1])
                if val > 0:
                    return val
        except Exception:
            pass
        return 0.0

    def _fetch_intraday_context(self, symbol: str) -> dict:
        """
        ดึง VWAP, day_high, day_low, prev_close จาก 15m data
        ใช้ส่งให้ Gate 19 LLM เพื่อวิเคราะห์ entry/TP/period ได้แม่นยำ

        Returns:
            {"vwap": float, "day_high": float, "day_low": float, "prev_close": float}
        """
        result = {"vwap": 0.0, "day_high": 0.0, "day_low": 0.0, "prev_close": 0.0}
        try:
            from data_pipeline_manager import safe_download, compute_vwap
            df = safe_download(symbol, period="5d", interval="15m")
            if df.empty or len(df) < 5:
                return result

            # ── VWAP (latest value)
            vwap_series = compute_vwap(df)
            if not vwap_series.empty:
                result["vwap"] = round(float(vwap_series.iloc[-1]), 2)

            # ── Day high/low (today's bars only)
            if hasattr(df.index, 'date'):
                try:
                    idx = df.index
                    if hasattr(idx, 'tz') and idx.tz is not None:
                        today = idx.tz_localize(None).date[-1]
                        dates = idx.tz_localize(None).date
                    else:
                        today = idx.date[-1]
                        dates = idx.date
                    today_mask = [d == today for d in dates]
                    today_df = df[today_mask]
                    if not today_df.empty:
                        result["day_high"] = round(float(today_df["high"].max()), 2)
                        result["day_low"]  = round(float(today_df["low"].min()), 2)
                except Exception:
                    result["day_high"] = round(float(df["high"].iloc[-20:].max()), 2)
                    result["day_low"]  = round(float(df["low"].iloc[-20:].min()), 2)

            # ── Prev close
            result["prev_close"] = self._fetch_prev_close(symbol)

        except Exception as e:
            logger.debug(f"[{symbol}] _fetch_intraday_context error: {e}")

        return result

    def _fetch_15m_bars(self, symbol: str, bars: int = 30) -> Optional["pd.DataFrame"]:
        """ดึง 15m OHLCV bars สำหรับ Worst Case Gate"""
        if yf is None or pd is None:
            return None
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="3d", interval="15m")
            if df.empty:
                return None
            df.columns = [c.lower() for c in df.columns]
            return df.tail(bars)
        except Exception as e:
            logger.warning(f"[WC] Failed to fetch 15m bars {symbol}: {e}")
            return None

    def _fetch_15m_data_for_train(self, symbol: str, days: int = 60) -> Optional["pd.DataFrame"]:
        """ดึง 15m OHLCV bars สำหรับ Worst Case training (ข้อมูลยาว)"""
        if yf is None or pd is None:
            return None
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=f"{days}d", interval="15m")
            if df.empty:
                return None
            df.columns = [c.lower() for c in df.columns]
            return df
        except Exception as e:
            logger.warning(f"[WC-Train] Failed to fetch {symbol} {days}d: {e}")
            return None

    def _score_stock(self, symbol: str, sentiment: dict) -> dict:
        try:
            from data_pipeline_manager import safe_download
            df = safe_download(symbol, period="30d", interval="1d")
            if not df.empty and len(df) >= 20:
                return self.regime.process_stock(symbol, df, sentiment)
        except Exception:
            pass
        return {"Final_Weighted_Score": 65, "Raw_Score_Momentum": 65,
                "Raw_Score_MeanRev": 50}

    def _determine_side(self, catalyst_type: str) -> str:
        bearish = {"GUIDANCE_DOWN", "ANALYST_DOWN"}
        return "sell" if catalyst_type in bearish else "buy"

    def _run_ml_gate(self, symbol, catalyst_type, urgency, session):
        if not self.ml_analyzer:
            return None
        try:
            df1, df5 = self.ml_analyzer._fetch_data(symbol, days=5)
            if df1 is None or len(df1) < 30:
                return None
            return self.ml_analyzer.analyze(
                symbol=symbol, df1=df1, df5=df5,
                catalyst_type=catalyst_type,
                urgency_score=urgency, session=session)
        except Exception:
            return None

    # ------------------------------------------
    # TIERED WATCHLIST — Startup + JIT Promotion
    # ------------------------------------------

    def startup_scan_and_train(self):
        """
        Pre-Market routine (เรียกตอน 08:00 ET ก่อนตลาดเปิด)

        Flow:
          1. scan_pre_market() → จัด Tier 1/2/3
          2. Train Tier 1 + 2 (ข้าม Tier 3)
          3. VPS: sync models จาก GDrive ด้วย

        เรียกจาก run_live() ก่อน scanner.start()
        """
        logger.info("=" * 55)
        logger.info("  PRE-MARKET SCAN + TIERED TRAINING")
        logger.info("=" * 55)

        # ── Step 1: Scan
        self._tiers = self.data_mgr.scan_pre_market()
        logger.info(f"  {self._tiers.summary()}")

        # ── Step 2: Train (local only) หรือ Sync (VPS)
        if self.cfg.DEPLOY_MODE == "local":
            self._tiers = self.data_mgr.run_tiered_daily_pipeline(self._tiers)
        else:
            # VPS: sync models ที่ train แล้วบน local machine
            self.data_mgr.sync_latest_models(
                watchlist=self._tiers.trainable
            )

        logger.info(f"  Pre-Market done: {self._tiers.summary()}")

        # ── Step 3: Train Worst Case models (piggyback daily retrain)
        if self.wc_gate and self._tiers:
            trainable = list(self._tiers.trainable)
            wc_trained = 0
            for sym in trainable:
                if not self.wc_gate.registry.needs_retrain(sym):
                    continue
                try:
                    df_15m = self._fetch_15m_data_for_train(sym, days=60)
                    if df_15m is not None and len(df_15m) >= 200:
                        auc = self.wc_gate.train_symbol(sym, df_15m)
                        if auc > 0:
                            wc_trained += 1
                except Exception as e:
                    logger.warning(f"[WC-Train] {sym} error: {e}")
            logger.info(f"  🛡️ Worst Case models trained: {wc_trained}/{len(trainable)}")

    def _promote_and_jit_train(self, symbol: str, catalyst_type: str) -> bool:
        """
        Just-in-Time Promotion — เมื่อ breaking news สำหรับ Tier 3 symbol

        Flow:
          1. เช็คว่า symbol อยู่ใน universe ไหม
          2. Promote จาก Tier 3 → Tier 1
          3. เช็คว่ามี model แล้วไหม (จาก cache)
          4. ถ้าไม่มี → Just-in-Time Train (~10s)

        Returns:
          True ถ้าพร้อม trade, False ถ้า JIT fail
        """
        if not self._tiers:
            return True  # ถ้าไม่มี tiered system → ผ่านเลย

        tier = self._tiers.get_tier(symbol)

        if tier == 0:
            # ไม่อยู่ใน universe เลย → ข้าม (liquidity risk)
            logger.info(f"[JIT] {symbol} ไม่อยู่ใน universe → skip")
            return False

        if tier <= 2:
            return True  # Tier 1/2 train แล้ว → ผ่าน

        # ── Tier 3: Promote + JIT Train
        logger.info(f"[JIT] {symbol} Tier3 → promote Tier1 ({catalyst_type})")
        self._tiers.promote(symbol, to_tier=1)

        # เช็คว่ามี model อยู่แล้วไหม
        from data_pipeline_manager import daily_tag
        model_path = self.data_mgr.gdrive.lgbm_path(symbol, daily_tag())
        if model_path.exists():
            logger.info(f"[JIT] {symbol} model exists → skip train")
            return True

        # ── Check recent model (≤3 days)
        recent = self.data_mgr._find_recent_model(symbol, max_age_days=3)
        if recent:
            logger.info(f"[JIT] {symbol} recent model ({recent}) → skip train")
            return True

        # ── JIT Train (LightGBM only, ~10s)
        if self.cfg.DEPLOY_MODE == "local":
            auc = self.data_mgr.just_in_time_train(symbol)
            return auc > 0
        else:
            # VPS: ไม่ train → ใช้ regime score อย่างเดียว
            logger.info(f"[JIT] VPS mode → skip JIT train, use regime only")
            return True

    # ------------------------------------------
    # FLATTEN + SHUTDOWN
    # ------------------------------------------

    def flatten_all(self, reason="TIME_KILL"):
        if not self._open_trades:
            return
        logger.warning(f"⚠️ FLATTEN ALL ({reason})")
        self.executor.flatten_all_positions()
        now = time.time()
        for sym, tid in list(self._open_trades.items()):
            price = self._fetch_price(sym) or 0.0
            self.journal.close_trade(trade_id=tid, actual_exit=price,
                                      exit_reason=reason)
            self._symbol_cooldown[sym] = now + self.cfg.SYMBOL_COOLDOWN_SEC
        self._open_trades.clear()
        self._entry_times.clear()
        self._daily_order_count = 0

    def _time_kill_watcher(self):
        h_t, m_t = self.cfg.FLATTEN_TIME_ET
        while not self._stop.is_set():
            t = now_et()
            if t.hour == h_t and t.minute >= m_t:
                self.flatten_all("TIME_KILL")
                logger.info("Time Kill → sleeping 10min")
                time.sleep(600)
            self._stop.wait(30)

    def _daily_retrain_scheduler(self):
        """
        Daily retrain scheduler (cron-style, ทำงานตอน ML_RETRAIN_HOUR_ET)

        ใช้ Tiered Pipeline:
          Local → scan_pre_market + run_tiered_daily_pipeline (train Tier1+2)
          VPS   → sync models จาก GDrive + fallback LightGBM
        """
        while not self._stop.is_set():
            t = now_et()
            if t.hour == self.cfg.ML_RETRAIN_HOUR_ET and t.minute < 30:
                try:
                    logger.info("[Scheduler] Daily retrain triggered")
                    self.startup_scan_and_train()
                except Exception as e:
                    logger.error(f"Retrain error: {e}", exc_info=True)
            self._stop.wait(60)

    def register_shutdown(self):
        def handler(sig, frame):
            logger.info("🛑 Shutdown → flatten all")
            self.flatten_all("SHUTDOWN")
            self._stop.set()
            self.journal.print_performance_report()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)


# ============================================================
# RUNNERS
# ============================================================

def _get_scan_symbols(pipeline) -> list:
    """ดึง symbols ที่ TechnicalScanner ควรสแกน"""
    if pipeline._tiers:
        if pipeline.tech_scanner and pipeline.tech_scanner.config.scan_tier1_only:
            return pipeline._tiers.tier1_hot
        return pipeline._tiers.tier1_hot + pipeline._tiers.tier2_warm
    return list(pipeline.cfg.ML_WATCHLIST)


def run_live(args):
    Config.load_profile(args.profile)
    Config.validate(args.mode)

    pipeline = TradingPipeline(mode=args.mode, dry_run=args.dry_run)
    pipeline.register_shutdown()

    # ── Technical Scanner (if enabled)
    if getattr(args, 'enable_tech_scan', False):
        try:
            from technical_scanner import TechnicalScanner, TechScanConfig
            tech_cfg = TechScanConfig.from_env()
            tech_cfg.enabled = True
            pipeline.tech_scanner = TechnicalScanner(config=tech_cfg)
            logger.info(f"🔬 TechnicalScanner ENABLED | rules={sorted(tech_cfg.active_rules)}")
        except ImportError:
            logger.warning("⚠️ technical_scanner.py not found → TechScan disabled")

    threading.Thread(target=pipeline._time_kill_watcher,
                     name="time-kill", daemon=True).start()
    threading.Thread(target=pipeline._daily_retrain_scheduler,
                     name="retrain", daemon=True).start()

    # ── Pre-Market: Tiered Scan + Train (แทน flat daily_retrain_all)
    def _startup():
        try:
            pipeline.startup_scan_and_train()
        except Exception as e:
            logger.error(f"Startup scan error: {e}", exc_info=True)
    threading.Thread(target=_startup, name="startup-scan", daemon=True).start()

    def on_news(c: NewsCandidate):
        try:
            pipeline.process_news(c)
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)

    scanner = NewsScanner(
        benzinga_api_key=Config.BENZINGA_API_KEY or None,
        use_sec_edgar=True, min_urgency=Config.MIN_URGENCY,
        callback=on_news,
    )
    scanner.start()

    logger.info(f"🟢 LIVE — 15m Event-Driven | {Config.summary()}")

    try:
        while not pipeline._stop.is_set():
            t = now_et()
            if t.minute % 15 == 0:
                logger.debug(f"⏰ 15m tick: {t.strftime('%H:%M')} ET")

                # ── Technical Scanner: scan ทุก 15m candle close
                if pipeline.tech_scanner and pipeline.tech_scanner.config.enabled:
                    try:
                        scan_symbols = _get_scan_symbols(pipeline)
                        tech_candidates = pipeline.tech_scanner.scan_tick(
                            scan_symbols, pipeline,
                        )
                        for tc in tech_candidates:
                            try:
                                pipeline.process_news(tc)
                            except Exception as e:
                                logger.error(f"Tech signal pipeline error: {e}")
                    except Exception as e:
                        logger.error(f"TechScan tick error: {e}", exc_info=True)

            # Sleep until next 15m boundary
            secs_to_next = max(1, (15 - t.minute % 15) * 60 - t.second)
            pipeline._stop.wait(min(secs_to_next, 60))
    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
        pipeline.flatten_all("SHUTDOWN")
        pipeline.journal.print_performance_report()
        stats = pipeline.gate19.get_stats()
        logger.info(f"Gate 19 stats: {stats}")
        if pipeline.wc_gate:
            wc_stats = pipeline.wc_gate.get_stats()
            logger.info(f"Gate WC stats: {wc_stats}")
        if pipeline.tech_scanner:
            logger.info(f"TechScan stats: {pipeline.tech_scanner.get_stats()}")


def _load_watchlist_from_universe() -> list[str]:
    """โหลด watchlist จาก universe.json (ไฟล์เดียวกับที่ใช้ train LSTM)"""
    import json
    candidates = [
        Path("./universe.json"),
        Path("./gdrive/universe.json"),
        Path(Config.MODEL_DIR) / ".." / "universe.json",
    ]
    for p in candidates:
        try:
            p = p.resolve()
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
                symbols = data.get("symbols", [])
                tag     = data.get("tag", "?")
                stats   = data.get("stats", {})
                logger.info(f"📋 Loaded universe: {len(symbols)} symbols (tag={tag}, file={p.name})")
                logger.info(f"   Stats: {stats}")
                return symbols
        except Exception as e:
            logger.warning(f"load_universe {p}: {e}")
    logger.warning("ไม่พบ universe.json → ใช้ default watchlist")
    return ["NVDA", "TSLA", "META", "AAPL", "AMZN"]


def _fetch_edgar_historical(watchlist: list[str], lookback_days: int = 2) -> list:
    """
    ดึง SEC EDGAR filings ย้อนหลังจาก data.sec.gov สำหรับทุก symbol ใน watchlist
    SEC rate limit: 10 req/sec → sleep 0.12s ระหว่าง request
    """
    import requests as _req

    headers = {"User-Agent": "TTPDryRun research@example.com", "Accept": "application/json"}
    classifier = CatalystClassifier()
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # ── Step 1: Ticker → CIK mapping
    logger.info(f"[EDGAR] ดึง ticker→CIK mapping...")
    try:
        resp = _req.get("https://www.sec.gov/files/company_tickers.json",
                        headers=headers, timeout=15)
        resp.raise_for_status()
        ticker_cik = {}
        for item in resp.json().values():
            ticker_cik[item["ticker"]] = str(item["cik_str"]).zfill(10)
    except Exception as e:
        logger.error(f"[EDGAR] ดึง CIK mapping ไม่ได้: {e}")
        return []

    mapped = {sym: ticker_cik[sym] for sym in watchlist if sym in ticker_cik}
    logger.info(f"[EDGAR] CIK mapped: {len(mapped)}/{len(watchlist)} symbols")

    # ── Step 2: Query filings per symbol
    FORMS_INTEREST = {"8-K", "8-K/A", "SC TO-T", "SC TO-T/A"}
    candidates = []

    for sym, cik in mapped.items():
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        try:
            r = _req.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            recent = data.get("filings", {}).get("recent", {})
            forms  = recent.get("form", [])
            dates  = recent.get("filingDate", [])
            descs  = recent.get("primaryDocDescription", [])
            company_name = data.get("name", sym)

            for form, dt_str, desc in zip(forms, dates, descs):
                if form not in FORMS_INTEREST:
                    continue
                try:
                    filed = datetime.strptime(dt_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if filed < cutoff:
                    continue

                full_text = f"{company_name} {sym} {form} {desc or ''}"
                if form.startswith("SC TO"):
                    catalyst_type, urgency = "MA", 90
                else:
                    result = classifier.classify(full_text)
                    catalyst_type, urgency = result if result else ("EARNINGS", 75)

                headline = f"[SEC {form}] {company_name} ({sym}) — {desc or form}"
                candidates.append(NewsCandidate(
                    symbol=sym, headline=headline[:200],
                    catalyst_type=catalyst_type, urgency_score=urgency,
                    source="SEC_EDGAR",
                    url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form}",
                    timestamp=filed,
                ))
        except Exception as e:
            logger.debug(f"[EDGAR] {sym} fetch error: {e}")
        time.sleep(0.12)

    candidates.sort(key=lambda c: c.timestamp, reverse=True)
    logger.info(f"[EDGAR] Historical filings: {len(candidates)} matched from {lookback_days} days")
    for c in candidates:
        logger.info(f"  {c.symbol:6s} | {c.catalyst_type:15s} | {c.timestamp.strftime('%Y-%m-%d')} | {c.headline[:80]}")
    return candidates


def run_dry_run(args):
    # ── Dry-run ใช้ Alpaca Paper เป็น default
    dry_profile = args.profile
    alpaca_profiles = {"ALPACA_PAPER_100K", "ALPACA_PAPER_25K"}
    if dry_profile not in alpaca_profiles:
        dry_profile = "ALPACA_PAPER_100K"
        logger.info(f"[DRY] Override profile → {dry_profile} (Alpaca Paper)")

    Config.load_profile(dry_profile)
    logger.info("🧪 DRY-RUN MODE (Alpaca Paper + SEC EDGAR news)")

    pipeline = TradingPipeline(mode="paper", dry_run=True)
    pipeline.register_shutdown()

    watchlist = _load_watchlist_from_universe()

    # ══════════════════════════════════════════════════════════
    # Phase 1: Quick smoke-test (mock news 3 ตัว)
    # ══════════════════════════════════════════════════════════
    logger.info("── Phase 1: Quick smoke-test (3 mock news) ──")
    mock_news = [
        NewsCandidate("NVDA", "NVIDIA beats Q2 EPS by 15%",
                       "EARNINGS", 90, "DRY_RUN"),
        NewsCandidate("TSLA", "Tesla lowers guidance",
                       "GUIDANCE_DOWN", 70, "DRY_RUN"),
        NewsCandidate("META", "Goldman upgrades META",
                       "ANALYST_UP", 70, "DRY_RUN"),
    ]
    for news in mock_news:
        pipeline.process_news(news)
        time.sleep(0.1)
    logger.info("── Phase 1 done ──")

    # ══════════════════════════════════════════════════════════
    # Phase 2: Real SEC EDGAR filings ย้อนหลัง 1-2 วัน
    # ══════════════════════════════════════════════════════════
    logger.info("── Phase 2: Real SEC EDGAR historical filings ──")
    real_filings = _fetch_edgar_historical(watchlist, lookback_days=2)
    if not real_filings:
        logger.info("[EDGAR] ไม่พบ filings 2 วัน → ขยาย 7 วัน")
        real_filings = _fetch_edgar_historical(watchlist, lookback_days=7)

    if real_filings:
        logger.info(f"── Processing {len(real_filings)} real SEC filings through pipeline ──")
        for news in real_filings:
            pipeline.process_news(news)
            time.sleep(0.2)
        logger.info(f"── Phase 2 done: {len(real_filings)} real filings processed ──")
    else:
        logger.warning("── Phase 2: ไม่พบ filings สำหรับ watchlist ──")

    # ══════════════════════════════════════════════════════════
    # Phase 3: Live SEC EDGAR feed (รอข่าวใหม่)
    # ══════════════════════════════════════════════════════════
    logger.info("── Phase 3: Live news scanner — SEC EDGAR only (dry-run) ──")

    def on_news(c: NewsCandidate):
        if c.symbol not in watchlist:
            return
        try:
            pipeline.process_news(c)
        except Exception as e:
            logger.error(f"Pipeline error: {e}", exc_info=True)

    scanner = NewsScanner(
        benzinga_api_key=None, use_sec_edgar=True,
        min_urgency=Config.MIN_URGENCY, callback=on_news,
    )
    scanner.start()

    logger.info(f"🟡 DRY-RUN LIVE — SEC EDGAR feed | {Config.summary()}")
    logger.info(f"   Watching {len(watchlist)} symbols | Ctrl+C to stop")

    try:
        while not pipeline._stop.is_set():
            t = now_et()
            if t.minute % 15 == 0:
                logger.debug(f"⏰ 15m tick: {t.strftime('%H:%M')} ET")
            secs_to_next = max(1, (15 - t.minute % 15) * 60 - t.second)
            pipeline._stop.wait(min(secs_to_next, 60))
    except KeyboardInterrupt:
        pass
    finally:
        scanner.stop()
        pipeline.journal.print_performance_report()
        stats = pipeline.gate19.get_stats()
        logger.info(f"Gate 19 stats: {stats}")
        if pipeline.wc_gate:
            logger.info(f"Gate WC stats: {pipeline.wc_gate.get_stats()}")


def run_report(args):
    Config.load_profile(args.profile)
    journal = TradeJournal(output_dir=Config.JOURNAL_DIR)
    journal.print_performance_report()


def run_shadow(args):
    """
    Shadow Mode — Full pipeline observation without order execution.

    Two sub-modes:
      1. One-shot (default): สแกนครั้งเดียว แสดงผลทุก gate แล้วจบ
      2. Live shadow (--live-shadow): วิ่งคู่ตลาดจริง รอข่าว แสดงผล ไม่สั่ง order
    """
    Config.load_profile(args.profile)

    # ── Parse skip-gates
    skip_gates = set()
    if args.skip_gates:
        skip_gates = set(g.strip().lower() for g in args.skip_gates.split(",") if g.strip())

    # ── Parse shadow-symbols
    shadow_symbols = None
    if args.shadow_symbols:
        shadow_symbols = [s.strip().upper() for s in args.shadow_symbols.split(",") if s.strip()]

    # ── Parse watchlist file
    watchlist_file = args.shadow_watchlist if args.shadow_watchlist else None

    # ── Create pipeline (always dry_run=True for safety)
    pipeline = TradingPipeline(mode="paper", dry_run=True)

    # ── Technical Scanner (if enabled)
    if getattr(args, 'enable_tech_scan', False):
        try:
            from technical_scanner import TechnicalScanner, TechScanConfig
            tech_cfg = TechScanConfig.from_env()
            tech_cfg.enabled = True
            pipeline.tech_scanner = TechnicalScanner(config=tech_cfg)
            logger.info(f"🔬 TechnicalScanner ENABLED in Shadow | rules={sorted(tech_cfg.active_rules)}")
        except ImportError:
            logger.warning("⚠️ technical_scanner.py not found → TechScan disabled")

    # ── Run
    from shadow_runner import run_shadow_oneshot, run_shadow_live

    if args.live_shadow:
        run_shadow_live(pipeline, skip_gates=skip_gates,
                        shadow_symbols=shadow_symbols,
                        watchlist_file=watchlist_file)
    else:
        run_shadow_oneshot(pipeline, skip_gates=skip_gates,
                           shadow_symbols=shadow_symbols,
                           watchlist_file=watchlist_file)


# ============================================================
# CLI
# ============================================================

def main():
    from config.config import PROP_FIRM_PROFILES
    profiles = list(PROP_FIRM_PROFILES.keys())

    parser = argparse.ArgumentParser(
        description="Universal 15m Quant Engine + LLM CIO (Gate 19)")
    parser.add_argument("--profile", choices=profiles, default="TTP_5K_FLEX",
                        help=f"Prop firm profile: {profiles}")
    parser.add_argument("--mode", choices=["paper", "live", "shadow"], default="paper",
                        help="paper=mock exec, live=real orders, shadow=observe only")
    parser.add_argument("--dry-run", action="store_true",
                        help="Quick test with mock + SEC EDGAR data")
    parser.add_argument("--report", action="store_true",
                        help="Print journal report and exit")

    # ── Shadow mode options
    shadow_group = parser.add_argument_group("shadow mode options")
    shadow_group.add_argument("--live-shadow", action="store_true",
                              help="Run shadow mode continuously (like live, but no orders)")
    shadow_group.add_argument("--skip-gates", type=str, default="",
                              help="Comma-separated gate IDs to skip. "
                                   "Available: daily_loss,session,sentiment,price,"
                                   "universe,regime,ml,max_orders,rate_limit,"
                                   "wash_sale,no_hedge,streak,risk,gate19")
    shadow_group.add_argument("--shadow-symbols", type=str, default="",
                              help="Comma-separated symbols to analyze (default: watchlist)")
    shadow_group.add_argument("--shadow-watchlist", type=str, default="",
                              help="Path to watchlist file (.json/.csv/.txt). "
                                   "Supports: universe.json, JSON array, CSV with "
                                   "symbol/ticker column, or text (1 per line)")

    # ── Technical Scanner
    tech_group = parser.add_argument_group("technical scanner")
    tech_group.add_argument("--enable-tech-scan", action="store_true",
                            help="Enable 15m technical scanning (VWAP pullback, "
                                 "ML breakout, volume spike). Works in all modes.")

    args = parser.parse_args()

    if args.report:
        run_report(args)
    elif args.dry_run:
        run_dry_run(args)
    elif args.mode == "shadow":
        run_shadow(args)
    else:
        if args.mode == "live":
            confirm = input("\n⚠️ LIVE MODE! Type 'CONFIRM': ").strip()
            if confirm != "CONFIRM":
                print("Cancelled")
                return
        run_live(args)


if __name__ == "__main__":
    main()