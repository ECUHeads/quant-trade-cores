"""
trade_journal.py
================
บันทึกและวิเคราะห์ผลการเทรดอัตโนมัติ — 15m VWAP Signal Engine

Architecture Pivot:
  - ปรับ TTP readiness สำหรับ 15m (trades ≥ 15 แทน 30 เพราะสัญญาณน้อยลง)
  - Streak Escalation ดุดันขึ้น (2 losses → daily halt) — ควบคุมผ่าน Config
  - ยังบันทึก catalyst, VWAP ratio, ATR ใน notes field

Features:
  - บันทึก entry/exit + P&L + R:R
  - Daily summary + TTP evaluation readiness
  - Consistency Rule check (FLEX 50%)
  - Export CSV
"""

import os
import csv
import json
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

logger = logging.getLogger("TradeJournal")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(name)s] %(message)s"
)


# ============================================================
# DATA MODEL
# ============================================================

@dataclass
class TradeRecord:
    """
    1 trade = 1 record ตั้งแต่ entry จน exit
    """
    # ── Identity
    trade_id:       str       = ""          # auto-generated
    alpaca_order_id: str      = ""          # จาก TTPOrderExecutor

    # ── Instrument
    symbol:         str       = ""
    side:           str       = ""          # "buy" | "sell"

    # ── News Catalyst (จาก NewsCandidate)
    catalyst_type:  str       = ""          # "EARNINGS", "FDA", "MA", ...
    urgency_score:  int       = 0
    news_headline:  str       = ""
    news_source:    str       = ""          # "BENZINGA" | "SEC_EDGAR"

    # ── Market Context (จาก RegimeWeightedScorer)
    regime_sentiment: float   = 0.0         # 0.0–1.0
    vix_at_entry:   float     = 0.0
    spy_at_entry:   float     = 0.0
    market_session: str       = ""          # "PRE_MARKET" | "MARKET" | "AFTER_HOURS"

    # ── Entry
    planned_entry:  float     = 0.0         # ราคาที่ตั้งใจจะเข้า
    actual_entry:   float     = 0.0         # fill price จริง
    shares:         int       = 0
    stop_price:     float     = 0.0
    target_price:   float     = 0.0

    # ── Scores (จาก RegimeWeightedScorer)
    momentum_score:    float  = 0.0
    mean_rev_score:    float  = 0.0
    final_score:       float  = 0.0

    # ── Exit
    actual_exit:    float     = 0.0
    exit_reason:    str       = ""          # "STOP_LOSS" | "TAKE_PROFIT" | "MANUAL" | "TIME_KILL"
    commission_usd: float     = 0.0

    # ── Timestamps
    entry_time:     str       = ""          # ISO format
    exit_time:      str       = ""

    # ── Computed (คำนวณตอน close_trade)
    pnl_usd:        float     = 0.0
    pnl_pct:        float     = 0.0
    rr_achieved:    float     = 0.0         # R:R จริงที่ได้
    rr_planned:     float     = 0.0         # R:R ที่ตั้งใจ
    is_winner:      bool      = False
    slippage_usd:   float     = 0.0         # actual_entry - planned_entry

    # ── Cost Tracking (Enhancement — universal across asset classes)
    spread_at_entry:  float   = 0.0         # bid-ask spread ตอน entry
    spread_at_exit:   float   = 0.0         # bid-ask spread ตอน exit
    spread_cost_usd:  float   = 0.0         # spread cost in USD
    overnight_cost_usd: float = 0.0         # swap/borrow/rollover cost
    total_cost_usd:   float   = 0.0         # all-in cost (spread + comm + slip + overnight)
    is_requote:       bool    = False        # True if entry was requoted
    fill_latency_ms:  int     = 0           # ms from submit to fill
    order_type:       str     = "MARKET"    # MARKET | LIMIT | STOP | STOP_LIMIT
    net_pnl_usd:     float    = 0.0         # pnl_usd - total_cost_usd

    # ── Notes
    notes:          str       = ""


# ============================================================
# TRADE JOURNAL
# ============================================================

class TradeJournal:
    """
    บันทึกทุก trade, คำนวณ P&L, และ export stats

    วิธีใช้:
        journal = TradeJournal(output_dir="./journal")

        # ตอนเปิด trade (หลังได้ fill จาก Alpaca)
        trade_id = journal.open_trade(
            symbol="NVDA", side="buy",
            catalyst_type="EARNINGS", urgency_score=80,
            planned_entry=135.50, actual_entry=135.62,
            shares=74, stop_price=133.00, target_price=140.50,
            final_score=78.0, ...
        )

        # ตอนปิด trade
        journal.close_trade(
            trade_id=trade_id,
            actual_exit=140.20,
            exit_reason="TAKE_PROFIT",
            commission_usd=0.74
        )

        # ดู stats
        journal.print_performance_report()
    """

    TRADES_FILE  = "trades.csv"
    DAILY_FILE   = "daily_summary.csv"
    PERF_FILE    = "performance.json"

    TRADE_FIELDS = [
        "trade_id", "alpaca_order_id", "symbol", "side",
        "catalyst_type", "urgency_score", "news_headline", "news_source",
        "regime_sentiment", "vix_at_entry", "spy_at_entry", "market_session",
        "planned_entry", "actual_entry", "shares", "stop_price", "target_price",
        "momentum_score", "mean_rev_score", "final_score",
        "actual_exit", "exit_reason", "commission_usd",
        "entry_time", "exit_time",
        "pnl_usd", "pnl_pct", "rr_achieved", "rr_planned",
        "is_winner", "slippage_usd",
        # ── Cost Tracking (Enhancement)
        "spread_at_entry", "spread_at_exit", "spread_cost_usd",
        "overnight_cost_usd", "total_cost_usd", "is_requote",
        "fill_latency_ms", "order_type", "net_pnl_usd",
        "notes",
    ]

    def __init__(self, output_dir: str = "./journal"):
        self.output_dir   = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.trades_path  = self.output_dir / self.TRADES_FILE
        self.daily_path   = self.output_dir / self.DAILY_FILE
        self.perf_path    = self.output_dir / self.PERF_FILE

        self._open_trades: dict[str, TradeRecord] = {}  # trade_id → record
        self._counter     = self._load_counter()

        # สร้าง CSV header ถ้ายังไม่มีไฟล์
        self._init_csv()
        logger.info(f"📓 TradeJournal ready → {self.output_dir.resolve()}")

    # ------------------------------------------
    # PUBLIC: open / close trade
    # ------------------------------------------

    def open_trade(
        self,
        symbol:           str,
        side:             str,
        catalyst_type:    str,
        urgency_score:    int,
        planned_entry:    float,
        actual_entry:     float,
        shares:           int,
        stop_price:       float,
        target_price:     float,
        # optional enrichment
        alpaca_order_id:  str   = "",
        news_headline:    str   = "",
        news_source:      str   = "",
        regime_sentiment: float = 0.0,
        vix_at_entry:     float = 0.0,
        spy_at_entry:     float = 0.0,
        market_session:   str   = "",
        momentum_score:   float = 0.0,
        mean_rev_score:   float = 0.0,
        final_score:      float = 0.0,
        notes:            str   = "",
        # ── Cost Tracking (Enhancement)
        spread_at_entry:  float = 0.0,
        is_requote:       bool  = False,
        fill_latency_ms:  int   = 0,
        order_type:       str   = "MARKET",
        spread_cost_usd:  float = 0.0,
    ) -> str:
        """
        เปิด trade ใหม่ → คืน trade_id สำหรับใช้ตอน close_trade()
        """
        self._counter += 1
        trade_id = f"T{datetime.now(timezone.utc).strftime('%Y%m%d')}-{self._counter:04d}"

        # คำนวณ planned R:R
        risk   = abs(actual_entry - stop_price)
        reward = abs(target_price - actual_entry)
        rr_planned = round(reward / risk, 2) if risk > 0 else 0.0

        slippage = round(actual_entry - planned_entry, 4) if side == "buy" else round(planned_entry - actual_entry, 4)

        record = TradeRecord(
            trade_id         = trade_id,
            alpaca_order_id  = alpaca_order_id,
            symbol           = symbol.upper(),
            side             = side.lower(),
            catalyst_type    = catalyst_type,
            urgency_score    = urgency_score,
            news_headline    = news_headline[:200],
            news_source      = news_source,
            regime_sentiment = round(regime_sentiment, 3),
            vix_at_entry     = round(vix_at_entry, 2),
            spy_at_entry     = round(spy_at_entry, 2),
            market_session   = market_session,
            planned_entry    = planned_entry,
            actual_entry     = actual_entry,
            shares           = shares,
            stop_price       = stop_price,
            target_price     = target_price,
            momentum_score   = momentum_score,
            mean_rev_score   = mean_rev_score,
            final_score      = final_score,
            entry_time       = datetime.now(timezone.utc).isoformat(),
            rr_planned       = rr_planned,
            slippage_usd     = round(slippage * shares, 4),
            notes            = notes,
            # ── Cost Tracking
            spread_at_entry  = spread_at_entry,
            spread_cost_usd  = spread_cost_usd,
            is_requote       = is_requote,
            fill_latency_ms  = fill_latency_ms,
            order_type       = order_type,
        )

        self._open_trades[trade_id] = record
        cost_note = f" spread={spread_at_entry:.5f}" if spread_at_entry > 0 else ""
        requote_note = " ⚡REQUOTE" if is_requote else ""
        logger.info(
            f"📂 OPEN  [{trade_id}] {side.upper()} {shares}x {symbol} "
            f"@ {actual_entry:.2f} | {catalyst_type} urgency={urgency_score}"
            f"{cost_note}{requote_note} [{order_type}]"
        )
        return trade_id

    def close_trade(
        self,
        trade_id:       str,
        actual_exit:    float,
        exit_reason:    str,          # "STOP_LOSS" | "TAKE_PROFIT" | "MANUAL" | "TIME_KILL"
        commission_usd: float = 0.0,
        notes:          str   = "",
        # ── Cost Tracking (Enhancement)
        spread_at_exit:     float = 0.0,
        overnight_cost_usd: float = 0.0,
    ) -> Optional[TradeRecord]:
        """
        ปิด trade → คำนวณ P&L ทั้งหมด → บันทึกลง CSV
        Now includes total_cost_usd and net_pnl_usd
        """
        if trade_id not in self._open_trades:
            logger.error(f"ไม่พบ trade_id: {trade_id}")
            return None

        rec = self._open_trades.pop(trade_id)
        rec.actual_exit    = actual_exit
        rec.exit_reason    = exit_reason
        rec.commission_usd = commission_usd
        rec.exit_time      = datetime.now(timezone.utc).isoformat()
        rec.spread_at_exit = spread_at_exit
        rec.overnight_cost_usd = overnight_cost_usd
        if notes:
            rec.notes = (rec.notes + " | " + notes).strip(" | ")

        # ── คำนวณ P&L
        if rec.side == "buy":
            gross_pnl = (actual_exit - rec.actual_entry) * rec.shares
        else:
            gross_pnl = (rec.actual_entry - actual_exit) * rec.shares

        rec.pnl_usd    = round(gross_pnl - commission_usd, 4)
        rec.pnl_pct    = round(rec.pnl_usd / (rec.actual_entry * rec.shares) * 100, 4) if rec.actual_entry else 0
        rec.is_winner  = rec.pnl_usd > 0

        # ── คำนวณ Total Cost (all-in) & Net P&L
        rec.total_cost_usd = round(
            commission_usd
            + abs(rec.spread_cost_usd)
            + abs(rec.slippage_usd)
            + abs(overnight_cost_usd),
            4
        )
        rec.net_pnl_usd = round(gross_pnl - rec.total_cost_usd, 4)

        # ── คำนวณ R:R ที่ได้จริง
        risk_per_share = abs(rec.actual_entry - rec.stop_price)
        if risk_per_share > 0:
            gain_per_share = abs(actual_exit - rec.actual_entry)
            rec.rr_achieved = round(gain_per_share / risk_per_share, 2)
            if rec.pnl_usd < 0:
                rec.rr_achieved = -rec.rr_achieved
        else:
            rec.rr_achieved = 0.0

        # ── บันทึกลง CSV
        self._append_csv(rec)
        self._save_counter()

        emoji = "✅" if rec.is_winner else "❌"
        cost_note = f" | cost=${rec.total_cost_usd:.2f} net=${rec.net_pnl_usd:+.2f}" if rec.total_cost_usd > 0 else ""
        logger.info(
            f"{emoji} CLOSE [{trade_id}] {rec.symbol} @ {actual_exit:.2f} | "
            f"P&L=${rec.pnl_usd:+.2f} ({rec.pnl_pct:+.2f}%) | "
            f"R:R={rec.rr_achieved:+.2f} | reason={exit_reason}{cost_note}"
        )

        # อัปเดต daily summary
        self._update_daily_summary(rec)

        return rec

    def update_notes(self, trade_id: str, notes: str):
        """เพิ่ม note ให้ open trade (เช่น บันทึก observation ระหว่างถือ)"""
        if trade_id in self._open_trades:
            self._open_trades[trade_id].notes += f" | {notes}"

    # ------------------------------------------
    # ANALYTICS
    # ------------------------------------------

    def load_all_trades(self) -> list[dict]:
        """โหลด trades ทั้งหมดจาก CSV"""
        if not self.trades_path.exists():
            return []
        with open(self.trades_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def compute_performance(self) -> dict:
        """
        คำนวณ stats ครบชุดจาก trades.csv
        คืน dict พร้อม save ลง performance.json

        Enhancement: เพิ่ม cost-adjusted metrics ตามแนวทางบทความ CFD
          - Net P&L (หักค่าใช้จ่ายทั้งหมดแล้ว)
          - Cost drag % (ค่าใช้จ่ายกิน profit ไปกี่ %)
          - Fill quality (slippage, requote, latency)
          - Order type breakdown (MARKET vs LIMIT performance)
          - Gross vs Net drawdown comparison
        """
        rows = self.load_all_trades()
        closed = [r for r in rows if r.get("exit_time")]

        if not closed:
            return {"status": "no_closed_trades"}

        def _f(x): return float(x) if x not in ("", "None") else 0.0
        def _b(x): return x in ("True", "true", "1")

        total        = len(closed)
        winners      = [r for r in closed if _b(r["is_winner"])]
        losers       = [r for r in closed if not _b(r["is_winner"])]
        win_rate     = round(len(winners) / total * 100, 1)

        pnls         = [_f(r["pnl_usd"]) for r in closed]
        total_pnl    = round(sum(pnls), 2)
        avg_pnl      = round(total_pnl / total, 2)

        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss   = abs(sum(p for p in pnls if p < 0))
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0

        avg_win  = round(sum(_f(r["pnl_usd"]) for r in winners) / max(len(winners), 1), 2)
        avg_loss = round(sum(_f(r["pnl_usd"]) for r in losers)  / max(len(losers),  1), 2)

        rr_values = [_f(r["rr_achieved"]) for r in closed]
        avg_rr    = round(sum(rr_values) / total, 2)

        # ══════════════════════════════════════════════════════
        # COST-ADJUSTED METRICS (Enhancement — จากบทความ CFD)
        # ══════════════════════════════════════════════════════

        net_pnls       = [_f(r.get("net_pnl_usd", r.get("pnl_usd", 0))) for r in closed]
        total_net_pnl  = round(sum(net_pnls), 2)
        total_costs    = [_f(r.get("total_cost_usd", 0)) for r in closed]
        sum_costs      = round(sum(total_costs), 2)
        avg_cost       = round(sum_costs / total, 2) if total > 0 else 0

        # Cost breakdown
        sum_spread_cost    = round(sum(_f(r.get("spread_cost_usd", 0)) for r in closed), 2)
        sum_commission     = round(sum(_f(r.get("commission_usd", 0)) for r in closed), 2)
        sum_overnight      = round(sum(_f(r.get("overnight_cost_usd", 0)) for r in closed), 2)
        sum_slippage       = round(sum(abs(_f(r.get("slippage_usd", 0))) for r in closed), 2)

        # Cost drag: ค่าใช้จ่ายกิน gross profit ไปกี่ %
        cost_drag_pct = round(sum_costs / gross_profit * 100, 1) if gross_profit > 0 else 0.0

        # Net Profit Factor (หลังหักค่าใช้จ่ายทั้งหมด)
        net_profits = sum(p for p in net_pnls if p > 0)
        net_losses  = abs(sum(p for p in net_pnls if p < 0))
        net_profit_factor = round(net_profits / net_losses, 2) if net_losses > 0 else 999.0

        # ══════════════════════════════════════════════════════
        # FILL QUALITY ANALYTICS (Enhancement)
        # ══════════════════════════════════════════════════════

        slippages   = [abs(_f(r.get("slippage_usd", 0))) for r in closed]
        latencies   = [_f(r.get("fill_latency_ms", 0)) for r in closed if _f(r.get("fill_latency_ms", 0)) > 0]
        requotes    = sum(1 for r in closed if _b(r.get("is_requote", "False")))

        fill_quality = {
            "avg_slippage_usd":  round(sum(slippages) / total, 4) if total > 0 else 0,
            "max_slippage_usd":  round(max(slippages), 4) if slippages else 0,
            "total_slippage_usd": round(sum(slippages), 2),
            "requote_count":     requotes,
            "requote_pct":       round(requotes / total * 100, 1) if total > 0 else 0,
            "avg_latency_ms":    round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "max_latency_ms":    round(max(latencies), 1) if latencies else 0,
        }

        # ══════════════════════════════════════════════════════
        # ORDER TYPE BREAKDOWN (Enhancement — MARKET vs LIMIT)
        # ══════════════════════════════════════════════════════

        order_type_stats: dict[str, dict] = {}
        for r in closed:
            otype = r.get("order_type", "MARKET") or "MARKET"
            if otype not in order_type_stats:
                order_type_stats[otype] = {"total": 0, "wins": 0, "pnl": 0.0, "net_pnl": 0.0, "slippage": 0.0}
            order_type_stats[otype]["total"]    += 1
            order_type_stats[otype]["wins"]     += 1 if _b(r["is_winner"]) else 0
            order_type_stats[otype]["pnl"]      += _f(r["pnl_usd"])
            order_type_stats[otype]["net_pnl"]  += _f(r.get("net_pnl_usd", r.get("pnl_usd", 0)))
            order_type_stats[otype]["slippage"] += abs(_f(r.get("slippage_usd", 0)))

        for otype, s in order_type_stats.items():
            s["win_rate"]     = round(s["wins"] / s["total"] * 100, 1) if s["total"] > 0 else 0
            s["pnl"]          = round(s["pnl"], 2)
            s["net_pnl"]      = round(s["net_pnl"], 2)
            s["avg_slippage"] = round(s["slippage"] / s["total"], 4) if s["total"] > 0 else 0

        # ── Breakdown by Catalyst
        catalyst_stats: dict[str, dict] = {}
        for r in closed:
            cat = r.get("catalyst_type", "UNKNOWN")
            if cat not in catalyst_stats:
                catalyst_stats[cat] = {"total": 0, "wins": 0, "pnl": 0.0}
            catalyst_stats[cat]["total"] += 1
            catalyst_stats[cat]["wins"]  += 1 if _b(r["is_winner"]) else 0
            catalyst_stats[cat]["pnl"]   += _f(r["pnl_usd"])

        for cat, s in catalyst_stats.items():
            s["win_rate"] = round(s["wins"] / s["total"] * 100, 1)
            s["pnl"]      = round(s["pnl"], 2)

        # ── Breakdown by Session
        session_stats: dict[str, dict] = {}
        for r in closed:
            sess = r.get("market_session", "UNKNOWN")
            if sess not in session_stats:
                session_stats[sess] = {"total": 0, "wins": 0, "pnl": 0.0}
            session_stats[sess]["total"] += 1
            session_stats[sess]["wins"]  += 1 if _b(r["is_winner"]) else 0
            session_stats[sess]["pnl"]   += _f(r["pnl_usd"])

        for sess, s in session_stats.items():
            s["win_rate"] = round(s["wins"] / s["total"] * 100, 1)
            s["pnl"]      = round(s["pnl"], 2)

        # ── Drawdown: GROSS vs NET (Enhancement — dual equity curve)
        equity_gross = 0.0; peak_gross = 0.0; max_dd_gross = 0.0
        equity_net   = 0.0; peak_net   = 0.0; max_dd_net   = 0.0
        for i, r in enumerate(closed):
            g = _f(r["pnl_usd"])
            n = _f(r.get("net_pnl_usd", r.get("pnl_usd", 0)))
            equity_gross += g
            equity_net   += n
            peak_gross = max(peak_gross, equity_gross)
            peak_net   = max(peak_net, equity_net)
            max_dd_gross = max(max_dd_gross, peak_gross - equity_gross)
            max_dd_net   = max(max_dd_net, peak_net - equity_net)

        # ── TTP Readiness Score (0–100)
        ttp_score = self._ttp_readiness(
            total=total, win_rate=win_rate, profit_factor=profit_factor,
            avg_rr=avg_rr, max_dd=max_dd_gross
        )

        stats = {
            "generated_at":     datetime.now(timezone.utc).isoformat(),
            "total_trades":     total,
            "winners":          len(winners),
            "losers":           len(losers),
            "win_rate_pct":     win_rate,
            # ── Gross P&L (before full cost deduction)
            "total_pnl_usd":    total_pnl,
            "avg_pnl_usd":      avg_pnl,
            "avg_win_usd":      avg_win,
            "avg_loss_usd":     avg_loss,
            "profit_factor":    profit_factor,
            "avg_rr":           avg_rr,
            "max_drawdown_usd": round(max_dd_gross, 2),
            # ── Cost-Adjusted (Enhancement)
            "total_net_pnl_usd":  total_net_pnl,
            "net_profit_factor":  net_profit_factor,
            "total_cost_usd":     sum_costs,
            "avg_cost_per_trade": avg_cost,
            "cost_drag_pct":      cost_drag_pct,
            "cost_breakdown": {
                "spread":     sum_spread_cost,
                "commission":  sum_commission,
                "slippage":    sum_slippage,
                "overnight":   sum_overnight,
            },
            "max_drawdown_net_usd": round(max_dd_net, 2),
            # ── Fill Quality (Enhancement)
            "fill_quality":     fill_quality,
            # ── Breakdowns
            "by_order_type":    order_type_stats,
            "by_catalyst":      catalyst_stats,
            "by_session":       session_stats,
            "ttp_readiness":    ttp_score,
        }

        with open(self.perf_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)

        return stats

    def _ttp_readiness(
        self,
        total: int,
        win_rate: float,
        profit_factor: float,
        avg_rr: float,
        max_dd: float,
    ) -> dict:
        """
        ประเมินว่าพร้อมข้าม TTP Evaluation หรือยัง
        เกณฑ์ขั้นต่ำที่ควรผ่านก่อนลงทุน real money
        """
        # ── PATCH ③: รวม Consistency Rule เข้า readiness check
        consistency_passed, consistency_violations = self.check_consistency_rule()

        checks = {
            "trades_≥15":           (total >= 15,            f"{total}/15 trades"),
            "win_rate_≥50%":        (win_rate >= 50.0,       f"{win_rate}%"),
            "profit_factor_≥1.5":   (profit_factor >= 1.5,   f"{profit_factor}"),
            "avg_rr_≥1.5":          (avg_rr >= 1.5,          f"{avg_rr}"),
            "max_dd_<$700":         (max_dd < 700,            f"${max_dd:.0f}"),
            "consistency_≤50%":     (consistency_passed,      "ผ่าน" if consistency_passed
                                     else f"ผิด: {list(consistency_violations.keys())}"),
        }
        passed = sum(1 for ok, _ in checks.values() if ok)
        score  = round(passed / len(checks) * 100)

        return {
            "score":      score,
            "ready":      score == 100,
            "passed":     passed,
            "total":      len(checks),
            "checks":     {k: {"pass": v[0], "value": v[1]} for k, v in checks.items()},
            "verdict":    "✅ READY FOR TTP" if score == 100 else f"⏳ NOT YET ({score}% criteria met)",
            "consistency_violations": consistency_violations,
        }

    # ------------------------------------------
    # PATCH ③ — TTP Consistency Rule Checker
    # ------------------------------------------

    def check_consistency_rule(self, threshold: float = 0.50) -> tuple[bool, dict]:
        """
        PATCH ③: TTP FLEX Rule — ห้าม 1 trade ทำกำไร > 50% ของ total profit

        ตัวอย่าง:
          total profit = $300  (profit target $5K eval)
          trade A      = $160  → 53% > 50% → FAIL
          trade B      = $140  → 47% ≤ 50% → PASS

        Args:
          threshold: 0.50 = 50% (FLEX) หรือ 0.30 (MAX disciplined)

        Returns:
          (passed: bool, violations: dict{symbol: ratio_str})
            passed = True  → ทุก trade ≤ threshold ของ total profit
            passed = False → มี trade ที่เกิน พร้อม dict แสดงว่า trade ไหน
        """
        rows = self.load_all_trades()
        if not rows:
            return True, {}

        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return 0.0

        # ── BUG FIX: ใช้ Net Profit ไม่ใช่ Gross Profit
        # TTP วัด "total valid profit" = net profit รวมทุก trade (ชนะ + แพ้)
        # เดิม: sum(pnl for pnl if pnl > 0)  → Gross wins เท่านั้น ❌
        #   wins=500, losses=-200 → gross=500 → max_per_trade=500 (มากเกินไป)
        # ใหม่: sum(pnl ทุก trade)            → Net profit ✅
        #   wins=500, losses=-200 → net=300   → max_per_trade=300 (ถูกต้อง)
        #
        # Enhancement: ใช้ net_pnl_usd (หักค่าใช้จ่ายแล้ว) แทน pnl_usd
        # เพราะ TTP วัดกำไรสุทธิจริง ไม่ใช่ก่อนหัก spread/swap
        all_pnls     = [_f(r.get("net_pnl_usd", r.get("pnl_usd", "0"))) for r in rows]
        total_profit = sum(all_pnls)  # net profit รวมทุก trade

        if total_profit <= 0:
            # net profit ≤ 0 = ยังไม่มีกำไรสุทธิ → ไม่มีอะไรให้วัด 50%
            return True, {}

        violations = {}
        for r in rows:
            pnl = _f(r.get("net_pnl_usd", r.get("pnl_usd", "0")))
            if pnl <= 0:
                continue
            ratio = pnl / total_profit
            if ratio > threshold:
                sym = r.get("symbol", "?")
                tid = r.get("trade_id", "?")
                violations[f"{sym}({tid[:8]})"] = f"{ratio:.1%}"

        passed = len(violations) == 0
        if not passed:
            logger.warning(
                f"[TTP Patch③] Consistency Rule ผิด: "
                f"total_profit=${total_profit:.2f} | violations={violations}"
            )
        return passed, violations

    # ------------------------------------------
    # SESSION-BASED STREAK TRACKING (Architecture Pivot)
    # ------------------------------------------

    def compute_daily_streak(self, session_date: str = None) -> int:
        """
        คำนวณ consecutive losses เฉพาะวันที่ระบุ (ไม่นับข้ามวัน)

        Args:
          session_date: "YYYY-MM-DD" (None = วันนี้)

        Returns:
          จำนวน consecutive losses ของวันนั้น (เริ่มนับจาก trade ล่าสุดย้อนกลับ)
        """
        if session_date is None:
            session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = self.load_all_trades()
        if not rows:
            return 0

        # กรองเฉพาะ trade ที่ปิดแล้วในวันนั้น
        day_trades = [
            r for r in rows
            if r.get("exit_time", "").startswith(session_date)
            and r.get("exit_time")
        ]

        if not day_trades:
            return 0

        # นับจากท้ายสุดย้อนกลับ — หยุดเมื่อเจอ winner
        streak = 0
        for r in reversed(day_trades):
            try:
                pnl = float(r.get("pnl_usd", 0))
            except (TypeError, ValueError):
                pnl = 0.0
            if pnl < 0:
                streak += 1
            else:
                break  # เจอ winner → หยุดนับ

        return streak

    def check_streak_halt(self, streak_block: int = 2, session_date: str = None) -> bool:
        """
        ตรวจว่าควร Daily Halt หรือไม่

        Args:
          streak_block: จำนวน consecutive losses ที่ trigger halt
          session_date: วันที่ตรวจ (None = วันนี้)

        Returns:
          True = ควรหยุดเทรด (streak ≥ streak_block)
        """
        streak = self.compute_daily_streak(session_date)
        if streak >= streak_block:
            logger.warning(
                f"🛑 [Streak] consecutive_losses={streak} ≥ {streak_block} "
                f"→ DAILY HALT"
            )
            return True
        return False

    def print_performance_report(self):
        """
        พิมพ์ report แบบ human-readable ออก console
        Enhancement: เพิ่ม Cost Impact, Fill Quality, Order Type sections
        """
        stats = self.compute_performance()

        if stats.get("status") == "no_closed_trades":
            print("⚠️  ยังไม่มี closed trades")
            return

        sep = "=" * 60

        print(f"\n{sep}")
        print(f"  TRADE JOURNAL — PERFORMANCE REPORT")
        print(f"  {stats['generated_at'][:10]}")
        print(sep)

        print(f"\n📊 OVERALL ({stats['total_trades']} trades)")
        print(f"  Win Rate    : {stats['win_rate_pct']}%  ({stats['winners']}W / {stats['losers']}L)")
        print(f"  Total P&L   : ${stats['total_pnl_usd']:+,.2f}  (gross)")
        print(f"  Net P&L     : ${stats.get('total_net_pnl_usd', 0):+,.2f}  (after all costs)")
        print(f"  Avg P&L     : ${stats['avg_pnl_usd']:+.2f} per trade")
        print(f"  Avg Win     : ${stats['avg_win_usd']:+.2f}")
        print(f"  Avg Loss    : ${stats['avg_loss_usd']:+.2f}")
        print(f"  Profit Factor: {stats['profit_factor']}  (net: {stats.get('net_profit_factor', '-')})")
        print(f"  Avg R:R     : {stats['avg_rr']}")
        print(f"  Max Drawdown: ${stats['max_drawdown_usd']:.2f}  (net: ${stats.get('max_drawdown_net_usd', 0):.2f})")

        # ── Cost Impact Section (Enhancement)
        cb = stats.get("cost_breakdown", {})
        drag = stats.get("cost_drag_pct", 0)
        total_cost = stats.get("total_cost_usd", 0)
        if total_cost > 0:
            print(f"\n💰 COST IMPACT — Total: ${total_cost:.2f}  (drag: {drag:.1f}% of gross profit)")
            print(f"  Spread     : ${cb.get('spread', 0):>10.2f}")
            print(f"  Commission : ${cb.get('commission', 0):>10.2f}")
            print(f"  Slippage   : ${cb.get('slippage', 0):>10.2f}")
            print(f"  Overnight  : ${cb.get('overnight', 0):>10.2f}")
            print(f"  Avg/trade  : ${stats.get('avg_cost_per_trade', 0):>10.2f}")
            # Visual cost drag bar
            drag_bar = "█" * min(20, int(drag / 5))
            print(f"  Cost Drag  :  {drag_bar} {drag:.1f}%")

        # ── Fill Quality Section (Enhancement)
        fq = stats.get("fill_quality", {})
        if fq.get("avg_slippage_usd", 0) > 0 or fq.get("requote_count", 0) > 0:
            print(f"\n⚡ FILL QUALITY")
            print(f"  Avg Slippage : ${fq['avg_slippage_usd']:.4f}  (total: ${fq['total_slippage_usd']:.2f})")
            print(f"  Max Slippage : ${fq['max_slippage_usd']:.4f}")
            print(f"  Requotes     : {fq['requote_count']}  ({fq['requote_pct']:.1f}%)")
            if fq.get("avg_latency_ms", 0) > 0:
                print(f"  Avg Latency  : {fq['avg_latency_ms']:.0f}ms  (max: {fq['max_latency_ms']:.0f}ms)")

        # ── Order Type Breakdown (Enhancement — MARKET vs LIMIT)
        ot_stats = stats.get("by_order_type", {})
        if len(ot_stats) > 0:
            print(f"\n📋 BY ORDER TYPE")
            for otype, s in sorted(ot_stats.items()):
                slip_note = f" | slip=${s['avg_slippage']:.4f}" if s['avg_slippage'] > 0 else ""
                print(f"  {otype:12s} {s['total']:3d} trades | WR={s['win_rate']:5.1f}% "
                      f"| P&L=${s['pnl']:+,.2f} → net=${s['net_pnl']:+,.2f}{slip_note}")

        print(f"\n📰 BY CATALYST TYPE")
        for cat, s in sorted(stats["by_catalyst"].items(), key=lambda x: -x[1]["pnl"]):
            bar = "█" * int(s["win_rate"] / 10)
            print(f"  {cat:15s} {s['total']:3d} trades | WR={s['win_rate']:5.1f}% {bar:10s} | P&L=${s['pnl']:+,.2f}")

        print(f"\n🕐 BY SESSION")
        for sess, s in stats["by_session"].items():
            print(f"  {sess:15s} {s['total']:3d} trades | WR={s['win_rate']:5.1f}% | P&L=${s['pnl']:+,.2f}")

        r = stats["ttp_readiness"]
        print(f"\n{'─'*60}")
        print(f"  TTP READINESS SCORE: {r['score']}%  ({r['passed']}/{r['total']} criteria)")
        for name, detail in r["checks"].items():
            icon = "✅" if detail["pass"] else "❌"
            print(f"  {icon} {name:25s} {detail['value']}")
        print(f"\n  {r['verdict']}")
        print(sep + "\n")

    def print_open_trades(self):
        """แสดง open positions ที่ยังไม่ปิด"""
        if not self._open_trades:
            print("ไม่มี open trades ขณะนี้")
            return
        print(f"\n🔓 OPEN TRADES ({len(self._open_trades)})")
        for tid, rec in self._open_trades.items():
            print(f"  [{tid}] {rec.side.upper()} {rec.shares}x {rec.symbol} "
                  f"@ {rec.actual_entry} | SL={rec.stop_price} | TP={rec.target_price} "
                  f"| {rec.catalyst_type}")

    # ------------------------------------------
    # ALPACA SYNC — ดึง fill จริงจาก API
    # ------------------------------------------

    def sync_from_alpaca(self, trading_client, trade_id: str, alpaca_order_id: str):
        """
        ดึง fill price จริงจาก Alpaca order → อัปเดต actual_entry
        เรียกหลัง submit_order() เพื่อให้ราคาแม่นยำ
        """
        try:
            from alpaca.trading.client import TradingClient
            order = trading_client.get_order_by_id(alpaca_order_id)
            fill  = float(order.filled_avg_price or 0)
            qty   = int(order.filled_qty or 0)

            if trade_id in self._open_trades and fill > 0:
                self._open_trades[trade_id].actual_entry    = fill
                self._open_trades[trade_id].shares          = qty
                self._open_trades[trade_id].alpaca_order_id = alpaca_order_id
                logger.info(f"🔄 Synced [{trade_id}] fill={fill} qty={qty}")
        except Exception as e:
            logger.warning(f"Alpaca sync failed ({alpaca_order_id}): {e}")

    # ------------------------------------------
    # INTERNAL: CSV helpers
    # ------------------------------------------

    def _init_csv(self):
        if not self.trades_path.exists():
            with open(self.trades_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.TRADE_FIELDS)
                writer.writeheader()
            logger.info(f"สร้าง {self.TRADE_FIELDS} header")

    def _append_csv(self, rec: TradeRecord):
        row = asdict(rec)
        # เอาเฉพาะ field ที่กำหนด
        filtered = {k: row.get(k, "") for k in self.TRADE_FIELDS}
        with open(self.trades_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.TRADE_FIELDS)
            writer.writerow(filtered)

    def _update_daily_summary(self, rec: TradeRecord):
        """อัปเดต (หรือสร้าง) แถวของวันนี้ใน daily_summary.csv"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # โหลด summary เดิม
        summaries: dict[str, dict] = {}
        if self.daily_path.exists():
            with open(self.daily_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    summaries[row["date"]] = row

        # อัปเดตวันนี้
        s = summaries.get(today, {
            "date": today, "trades": 0, "wins": 0,
            "total_pnl": 0.0, "total_commission": 0.0
        })
        s["trades"]           = int(s["trades"]) + 1
        s["wins"]             = int(s["wins"]) + (1 if rec.is_winner else 0)
        s["total_pnl"]        = round(float(s["total_pnl"]) + rec.pnl_usd, 2)
        s["total_commission"] = round(float(s["total_commission"]) + rec.commission_usd, 4)
        s["win_rate"]         = round(int(s["wins"]) / int(s["trades"]) * 100, 1)
        summaries[today]      = s

        # เขียนใหม่ทั้งหมด
        fields = ["date", "trades", "wins", "win_rate", "total_pnl", "total_commission"]
        with open(self.daily_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in sorted(summaries.values(), key=lambda x: x["date"]):
                writer.writerow({k: row[k] for k in fields})

    def _load_counter(self) -> int:
        """อ่าน counter จาก performance.json เพื่อ sequence trade_id ต่อเนื่อง"""
        if self.perf_path.exists():
            try:
                with open(self.perf_path, encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("total_trades", 0)
            except Exception:
                pass
        return 0

    def _save_counter(self):
        """บันทึก counter ชั่วคราว (performance จริงเซฟตอน compute)"""
        if self.perf_path.exists():
            try:
                with open(self.perf_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        else:
            data = {}
        data["total_trades"] = self._counter
        with open(self.perf_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


# ============================================================
# INTEGRATION HOOK — เชื่อมกับ TTPOrderExecutor
# ============================================================

class JournalledOrderExecutor:
    """
    Wrapper รอบ TTPOrderExecutor + TradeJournal
    ยิง order แล้วบันทึกลง journal อัตโนมัติ — ไม่ต้องเรียก journal เอง

    วิธีใช้:
        executor = JournalledOrderExecutor(
            api_key="...", secret_key="...",
            journal=TradeJournal("./journal"), paper=True
        )
        trade_id = executor.execute(
            symbol="NVDA", shares=50, side="LONG",
            entry_price=135.50, stop_price=133.00, target_price=140.50,
            catalyst_type="EARNINGS", urgency_score=80,
            regime_score=0.72, vix=14.5, spy=580.0,
            market_session="PRE_MARKET", final_score=78.0,
        )
    """

    def __init__(self, api_key: str, secret_key: str,
                 journal: TradeJournal, paper: bool = True):
        from alpaca.trading.client import TradingClient
        self.trading_client = TradingClient(api_key, secret_key, paper=paper)
        self.journal        = journal

    def execute(
        self,
        symbol:           str,
        shares:           int,
        side:             str,
        entry_price:      float,
        stop_price:       float,
        target_price:     float,
        # journal enrichment
        catalyst_type:    str   = "OTHER",
        urgency_score:    int   = 0,
        news_headline:    str   = "",
        news_source:      str   = "",
        regime_score:     float = 0.0,
        vix:              float = 0.0,
        spy:              float = 0.0,
        market_session:   str   = "",
        final_score:      float = 0.0,
        momentum_score:   float = 0.0,
        mean_rev_score:   float = 0.0,
    ) -> Optional[str]:
        """
        ยิง bracket order ผ่าน Alpaca + เปิด trade ใน journal
        คืน trade_id (ใช้ตอน close_trade)
        """
        from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums    import OrderSide, TimeInForce

        order_side = OrderSide.BUY if side.upper() == "LONG" else OrderSide.SELL

        order_request = LimitOrderRequest(
            symbol        = symbol,
            qty           = shares,
            side          = order_side,
            limit_price   = round(entry_price, 2),
            time_in_force = TimeInForce.DAY,
            take_profit   = TakeProfitRequest(limit_price=round(target_price, 2)),
            stop_loss     = StopLossRequest(stop_price=round(stop_price, 2)),
        )

        try:
            order = self.trading_client.submit_order(order_data=order_request)
            fill  = float(order.filled_avg_price or entry_price)   # ใช้ limit ถ้ายังไม่ fill
        except Exception as e:
            logger.error(f"Order failed {symbol}: {e}")
            return None

        trade_id = self.journal.open_trade(
            symbol           = symbol,
            side             = "buy" if side.upper() == "LONG" else "sell",
            catalyst_type    = catalyst_type,
            urgency_score    = urgency_score,
            planned_entry    = entry_price,
            actual_entry     = fill,
            shares           = shares,
            stop_price       = stop_price,
            target_price     = target_price,
            alpaca_order_id  = str(order.id),
            news_headline    = news_headline,
            news_source      = news_source,
            regime_sentiment = regime_score,
            vix_at_entry     = vix,
            spy_at_entry     = spy,
            market_session   = market_session,
            final_score      = final_score,
            momentum_score   = momentum_score,
            mean_rev_score   = mean_rev_score,
        )
        return trade_id


# ============================================================
# MAIN — test
# ============================================================

if __name__ == "__main__":
    import random

    print("=" * 60)
    print("  TRADE JOURNAL — Simulation Test (30 trades)")
    print("=" * 60)

    journal = TradeJournal(output_dir="./journal_test")

    CATALYSTS = [
        ("EARNINGS",      80, 0.60),  # (type, urgency, win_prob)
        ("FDA",           90, 0.55),
        ("MA",            85, 0.65),
        ("GUIDANCE_UP",   75, 0.58),
        ("GUIDANCE_DOWN", 65, 0.50),
        ("ANALYST_UP",    50, 0.52),
    ]
    SESSIONS  = ["PRE_MARKET", "MARKET", "AFTER_HOURS"]
    SYMBOLS   = ["NVDA", "AAPL", "TSLA", "META", "AMZN", "MRNA", "NFLX"]

    random.seed(42)

    for i in range(30):
        cat, urgency, win_prob = random.choice(CATALYSTS)
        symbol   = random.choice(SYMBOLS)
        session  = random.choice(SESSIONS)
        entry    = round(random.uniform(50, 400), 2)
        stop     = round(entry * random.uniform(0.97, 0.99), 2)
        target   = round(entry * random.uniform(1.02, 1.05), 2)
        shares   = random.randint(10, 100)
        vix      = round(random.uniform(12, 28), 1)
        regime   = round(random.uniform(0.3, 0.9), 2)
        score    = round(random.uniform(50, 95), 1)

        # จำลอง slight slippage
        actual_entry = round(entry + random.uniform(-0.05, 0.15), 2)

        tid = journal.open_trade(
            symbol=symbol, side="buy",
            catalyst_type=cat, urgency_score=urgency,
            planned_entry=entry, actual_entry=actual_entry,
            shares=shares, stop_price=stop, target_price=target,
            news_headline=f"[MOCK] {symbol} {cat} event triggered scanner",
            news_source="SEC_EDGAR",
            regime_sentiment=regime, vix_at_entry=vix,
            spy_at_entry=round(random.uniform(540, 600), 1),
            market_session=session, final_score=score,
        )

        # จำลอง outcome
        is_winner = random.random() < win_prob
        if is_winner:
            exit_price  = round(entry * random.uniform(1.01, 1.045), 2)
            exit_reason = "TAKE_PROFIT"
        else:
            exit_price  = stop
            exit_reason = "STOP_LOSS"

        commission = round(shares * 0.005 * 2, 4)

        journal.close_trade(
            trade_id=tid,
            actual_exit=exit_price,
            exit_reason=exit_reason,
            commission_usd=commission,
        )

    # ── พิมพ์ report
    journal.print_performance_report()

    # ── ตรวจสอบ output files
    print("📁 Output files:")
    for f in Path("./journal_test").iterdir():
        print(f"  {f.name}  ({f.stat().st_size:,} bytes)")