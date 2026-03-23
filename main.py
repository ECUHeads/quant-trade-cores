"""
main.py
=======
Universal 15m Quant Engine — Event-Driven Orchestrator

Pipeline (per 15m candle close):
  Data Fetch → ML Gate 6 → Risk Gate 1-7 → LLM CIO Gate 19 → Executor

Loop Timing:
  ตื่นเฉพาะนาทีที่ 00, 15, 30, 45 (cron-style)
  News scan ทำงาน background แยกจาก main loop

Session Control:
  No New Entry after  Config.NO_NEW_ENTRY_AFTER  (default 14:30 ET)
  Flatten All at      Config.FLATTEN_TIME_ET      (default 15:45 ET)

Timezone:
  ใช้ ZoneInfo("America/New_York") — DST-aware

Usage:
  python main.py --profile TTP_5K_FLEX --mode paper
  python main.py --profile FTMO_100K --mode live
  python main.py --profile TTP_5K_FLEX --dry-run
  python main.py --report
"""

import os
import time
import signal
import logging
import argparse
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

# ── Internal modules
from config.config import Config
from ext_data.news_scanner import (NewsScanner, NewsCandidate,
                           MarketSessionFilter, CatalystClassifier)
from orders.trade_journal import TradeJournal, TradeRecord
from gates.gate_19_llm_cio import Gate19LLMCio, CIOVerdict

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
        if self.dry_run:
            self.executor = MockExecutor()
        else:
            EX = _load_executor()
            if EX:
                # ── MT5 Proxy: inject LINE/Telegram alert callbacks ก่อนสร้าง executor
                if self.cfg.EXECUTION_METHOD == "MT5_PROXY":
                    self._setup_mt5_proxy_alerts()
                self.executor = EX.from_config(self.cfg)
            else:
                self.executor = MockExecutor()

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

        logger.info("✅ All modules loaded")

    # ------------------------------------------
    # MT5 PROXY: Alert Callbacks
    # ------------------------------------------

    def _setup_mt5_proxy_alerts(self):
        """
        เพิ่ม LINE/Telegram alert callbacks เมื่อ MT5 disconnect/reconnect
        ถูกเรียกก่อนสร้าง Executor — inject callbacks เข้า Config
        """
        def on_mt5_unhealthy(status):
            msg = (
                f"🚨 MT5 PROXY UNHEALTHY\n"
                f"Orders BLOCKED until recovery\n"
                f"Status: {status.get('status', 'UNKNOWN')}\n"
                f"Reconnects: {status.get('reconnect_count', '?')}"
            )
            logger.critical(msg)
            try:
                from notifier_line import send_alert_message
                send_alert_message(msg)
            except Exception:
                pass
            try:
                from notifier_telegram import send_alert_telegram
                send_alert_telegram(msg)
            except Exception:
                pass

        def on_mt5_recovery(status):
            acct = status.get("account", {})
            msg = (
                f"✅ MT5 PROXY RECOVERED\n"
                f"Orders RESUMED\n"
                f"Balance: ${acct.get('balance', 0):,.2f}\n"
                f"Equity: ${acct.get('equity', 0):,.2f}"
            )
            logger.info(msg)
            try:
                from notifier_line import send_alert_message
                send_alert_message(msg)
            except Exception:
                pass
            try:
                from notifier_telegram import send_alert_telegram
                send_alert_telegram(msg)
            except Exception:
                pass

        self.cfg._mt5_unhealthy_cb = on_mt5_unhealthy
        self.cfg._mt5_recovery_cb  = on_mt5_recovery
        logger.info("🔗 MT5 Proxy alert callbacks registered (LINE + Telegram)")

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
        score_result = self._score_stock(sym, sentiment)
        regime_score = score_result.get("Final_Weighted_Score", 0)
        if regime_score < 50:
            logger.info(f"⛔ {sym} regime={regime_score} < 50")
            return

        # ── Side
        side = self._determine_side(candidate.catalyst_type)

        # ── GATE ML: ML Analyzer
        ml_prediction = self._run_ml_gate(sym, candidate.catalyst_type,
                                           candidate.urgency_score, session)
        if ml_prediction and ml_prediction.ml_score < self.cfg.ML_SCORE_MIN:
            logger.info(f"⛔ ML {sym} score={ml_prediction.ml_score} < {self.cfg.ML_SCORE_MIN}")
            return

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
        # GATE 19: LLM CIO — Final Risk Veto
        # ══════════════════════════════════════════════════════
        verdict = self.gate19.evaluate_trade(
            market_data={
                "symbol": sym, "price": price,
                "vwap": 0, "atr_15m": atr, "vix": sentiment.get("vix", 20),
                "spy_trend": "up" if sentiment["sentiment_score"] > 0.5 else "down",
                "session": session,
            },
            ml_signal={
                "ml_score": ml_prediction.ml_score if ml_prediction else int(regime_score),
                "direction_prob": ml_prediction.direction_prob if ml_prediction else 0.5,
                "confidence": ml_prediction.confidence if ml_prediction else 0.5,
                "signal": side.upper(),
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
            ml_notes += f"ml={ml_prediction.ml_score} conf={ml_prediction.confidence:.2f}"

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

        logger.info(
            f"✅ TRADE [{trade_id}] {side.upper()} {adjusted_shares}x {sym} "
            f"@ {entry_price:.2f} SL={stop_price:.2f} TP={target_price:.2f} "
            f"| Gate19={verdict.action} | orders={self._daily_order_count}/{self.cfg.MAX_ORDERS_PER_DAY}"
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
        """ดึงราคาล่าสุด — route ตาม asset class"""
        # ── CFD/Forex: ดึงจาก MT5 Proxy
        if self.cfg.is_cfd() and hasattr(self.executor, 'adapter') and hasattr(self.executor.adapter, 'get_tick'):
            try:
                tick = self.executor.adapter.get_tick(symbol)
                if tick and tick.get("bid", 0) > 0:
                    mid = (tick["bid"] + tick["ask"]) / 2
                    return float(mid)
            except Exception as e:
                logger.debug(f"MT5 tick failed for {symbol}: {e}")
            try:
                info = self.executor.adapter.get_symbol_info(symbol)
                if info and info.get("bid", 0) > 0:
                    return float((info["bid"] + info["ask"]) / 2)
            except Exception:
                pass
            return None

        # ── Equities: Alpaca snapshot → yfinance fallback (เดิม)
        if self._tiers and self._tiers.snapshot.get(symbol):
            p = self._tiers.snapshot[symbol].get("price", 0)
            if p > 0:
                return float(p)
        try:
            from data_pipeline_manager import get_alpaca_snapshots
            snaps = get_alpaca_snapshots([symbol])
            if symbol in snaps and snaps[symbol]["price"] > 0:
                return float(snaps[symbol]["price"])
        except Exception:
            pass
        try:
            if yf:
                info = yf.Ticker(symbol).fast_info
                p = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                return float(p) if p else None
        except Exception:
            pass
        return None

    def _fetch_prev_close(self, symbol: str) -> float:
        """ดึง previous close — route ตาม asset class"""
        # ── CFD/Forex: ดึง 1D bar ล่าสุดจาก MT5
        if self.cfg.is_cfd() and hasattr(self.executor, 'adapter') and hasattr(self.executor.adapter, 'get_bars'):
            try:
                bars = self.executor.adapter.get_bars(symbol, timeframe="1d", count=2)
                if bars and len(bars) >= 2:
                    return float(bars[-2]["close"])
                elif bars and len(bars) == 1:
                    return float(bars[0]["close"])
            except Exception as e:
                logger.debug(f"MT5 prev_close failed for {symbol}: {e}")
            return 0.0

        # ── Equities (เดิม)
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
        """ดึง ATR 15m — route ตาม asset class"""
        # ── CFD/Forex: ดึง 15m bars จาก MT5 แล้วคำนวณ ATR
        if self.cfg.is_cfd() and hasattr(self.executor, 'adapter') and hasattr(self.executor.adapter, 'get_bars'):
            try:
                bars = self.executor.adapter.get_bars(symbol, timeframe="15m", count=50)
                if bars and len(bars) >= 14:
                    atr = self._compute_atr_from_bars(bars, period=14)
                    if atr > 0:
                        return atr
            except Exception as e:
                logger.debug(f"MT5 ATR failed for {symbol}: {e}")
            return 0.0

        # ── Equities (เดิม)
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

    @staticmethod
    def _compute_atr_from_bars(bars: list[dict], period: int = 14) -> float:
        """คำนวณ ATR จาก list of bar dicts (จาก MT5 proxy)"""
        if len(bars) < period + 1:
            return 0.0
        true_ranges = []
        for i in range(1, len(bars)):
            h = bars[i]["high"]
            l = bars[i]["low"]
            pc = bars[i - 1]["close"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            true_ranges.append(tr)
        if len(true_ranges) < period:
            return 0.0
        atr = sum(true_ranges[-period:]) / period
        return round(atr, 6)

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

def run_live(args):
    Config.load_profile(args.profile)
    Config.validate(args.mode)

    pipeline = TradingPipeline(mode=args.mode, dry_run=args.dry_run)
    pipeline.register_shutdown()

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


def run_dry_run(args):
    Config.load_profile(args.profile)
    logger.info("🧪 DRY-RUN MODE")

    pipeline = TradingPipeline(mode="paper", dry_run=True)

    mock_news = [
        NewsCandidate("NVDA", "NVIDIA beats Q2 EPS by 15%",
                       "EARNINGS", 90, "SEC_EDGAR"),
        NewsCandidate("TSLA", "Tesla lowers guidance",
                       "GUIDANCE_DOWN", 70, "BENZINGA"),
        NewsCandidate("META", "Goldman upgrades META",
                       "ANALYST_UP", 70, "BENZINGA"),
    ]

    for news in mock_news:
        pipeline.process_news(news)
        time.sleep(0.2)

    pipeline.journal.print_performance_report()
    stats = pipeline.gate19.get_stats()
    logger.info(f"Gate 19 stats: {stats}")


def run_report(args):
    Config.load_profile(args.profile)
    journal = TradeJournal(output_dir=Config.JOURNAL_DIR)
    journal.print_performance_report()


def run_test_proxy(args):
    """รัน FTMO proxy test script"""
    Config.load_profile(args.profile)
    try:
        from test_ftmo_proxy import run_tests
        run_tests(
            max_level=args.test_proxy_level,
            symbol=args.test_proxy_symbol,
        )
    except ImportError:
        logger.error(
            "test_ftmo_proxy.py not found!\n"
            "   Copy it to the same directory as main.py"
        )


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
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--test-proxy", action="store_true",
                        help="Run FTMO proxy connectivity & order test")
    parser.add_argument("--test-proxy-level", type=int, default=2, choices=[0,1,2,3,4],
                        help="Max test level for --test-proxy (default: 2)")
    parser.add_argument("--test-proxy-symbol", type=str, default="EURUSD",
                        help="Symbol for --test-proxy Level 3 (default: EURUSD)")
    args = parser.parse_args()

    if args.report:
        run_report(args)
    elif args.test_proxy:
        run_test_proxy(args)
    elif args.dry_run:
        run_dry_run(args)
    else:
        if args.mode == "live":
            confirm = input("\n⚠️ LIVE MODE! Type 'CONFIRM': ").strip()
            if confirm != "CONFIRM":
                print("Cancelled")
                return
        run_live(args)


if __name__ == "__main__":
    main()