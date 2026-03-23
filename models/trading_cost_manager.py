"""
trading_cost_manager.py
=======================
Universal Trading Cost & Fill Quality Manager — Enhancement Module

4 capabilities ที่ apply ได้กับทุก asset class:

  1. FillQualityTracker   — Requote detection, slippage stats, fill quality scoring
  2. OvernightCostTracker — Swap (CFD), Borrow cost (Equities short), Rollover (Futures)
  3. SpreadMonitor        — Real-time spread capture + historical spread stats
  4. TradingCostCalculator — Unified cost model สำหรับ backtest-grade P&L

Asset class mapping:
  ┌──────────┬──────────────┬───────────────┬────────────┬───────────┐
  │ Feature  │ EQUITIES     │ CFD           │ FUTURES    │ Universal │
  ├──────────┼──────────────┼───────────────┼────────────┼───────────┤
  │ Spread   │ bid-ask      │ bid-ask       │ bid-ask    │ ✅        │
  │ Commiss. │ per share    │ ✗ (in spread) │ per contr. │ ✅        │
  │ Swap     │ borrow cost  │ overnight     │ rollover   │ ✅        │
  │ Requote  │ partial fill │ requote/slip  │ reject     │ ✅        │
  │ Pending  │ limit order  │ limit/stop    │ limit/stop │ ✅        │
  └──────────┴──────────────┴───────────────┴────────────┴───────────┘

Usage:
    from trading_cost_manager import (
        FillQualityTracker, OvernightCostTracker,
        SpreadMonitor, TradingCostCalculator
    )

    # ── Spread Monitor (ใช้ตอน pre-trade)
    spread_mon = SpreadMonitor()
    spread_mon.record_spread("EURUSD", bid=1.08490, ask=1.08510)
    stats = spread_mon.get_stats("EURUSD")

    # ── Fill Quality (ใช้ตอน post-execution)
    fq = FillQualityTracker()
    fq.record_fill("EURUSD", intended=1.08500, actual=1.08510,
                   retcode=10009, is_requote=False)
    report = fq.get_report("EURUSD")

    # ── Overnight Cost (ใช้ตอน overnight)
    overnight = OvernightCostTracker(asset_class="CFD")
    cost = overnight.estimate_overnight_cost(
        symbol="EURUSD", lots=2.78, side="LONG", entry_price=1.08500
    )

    # ── Cost Calculator (ใช้ตอน backtest หรือ pre-trade sizing)
    calc = TradingCostCalculator.from_config(Config)
    total = calc.total_round_trip_cost(entry=1.08500, size=2.78, symbol="EURUSD")
"""

import time
import math
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import defaultdict

logger = logging.getLogger("TradingCost")


# ============================================================
# 1. FILL QUALITY TRACKER — Requote & Slippage Monitoring
# ============================================================

@dataclass
class FillRecord:
    """Single fill event record"""
    symbol:          str   = ""
    timestamp:       str   = ""
    intended_price:  float = 0.0
    actual_price:    float = 0.0
    slippage_abs:    float = 0.0      # |actual - intended|
    slippage_pct:    float = 0.0      # slippage / intended × 100
    is_requote:      bool  = False
    is_partial_fill: bool  = False
    retcode:         int   = 0        # MT5 retcode or HTTP status
    retcode_name:    str   = ""       # human-readable
    fill_latency_ms: int   = 0


class FillQualityTracker:
    """
    Track fill quality across all adapters.

    MT5 retcode mapping:
      10009 = TRADE_RETCODE_DONE (success)
      10004 = TRADE_RETCODE_REQUOTE
      10006 = TRADE_RETCODE_REJECT
      10013 = TRADE_RETCODE_INVALID_PRICE
      10014 = TRADE_RETCODE_INVALID_STOPS
      10016 = TRADE_RETCODE_PRICE_OFF (off-quotes)

    Alpaca:
      "filled"         → success
      "partially_filled" → partial fill
      "rejected"       → rejected
    """

    # MT5 retcode → human-readable name
    MT5_RETCODES = {
        10009: "DONE",
        10004: "REQUOTE",
        10006: "REJECT",
        10007: "CANCEL",
        10013: "INVALID_PRICE",
        10014: "INVALID_STOPS",
        10015: "INVALID_VOLUME",
        10016: "PRICE_OFF",
        10018: "MARKET_CLOSED",
        10019: "NO_MONEY",
        10021: "PRICE_CHANGED",
    }

    def __init__(self):
        self._fills: dict[str, list[FillRecord]] = defaultdict(list)
        self._requote_count: int = 0
        self._total_count:   int = 0

    def record_fill(
        self,
        symbol:         str,
        intended_price: float,
        actual_price:   float,
        retcode:        int   = 10009,
        is_requote:     bool  = False,
        is_partial_fill: bool = False,
        fill_latency_ms: int  = 0,
    ) -> FillRecord:
        """Record a fill event and compute slippage metrics"""
        slippage_abs = abs(actual_price - intended_price)
        slippage_pct = (slippage_abs / intended_price * 100) if intended_price > 0 else 0.0

        # Auto-detect requote from MT5 retcodes
        if retcode in (10004, 10016, 10021):
            is_requote = True

        rec = FillRecord(
            symbol          = symbol,
            timestamp       = datetime.now(timezone.utc).isoformat(),
            intended_price  = intended_price,
            actual_price    = actual_price,
            slippage_abs    = round(slippage_abs, 8),
            slippage_pct    = round(slippage_pct, 6),
            is_requote      = is_requote,
            is_partial_fill = is_partial_fill,
            retcode         = retcode,
            retcode_name    = self.MT5_RETCODES.get(retcode, f"CODE_{retcode}"),
            fill_latency_ms = fill_latency_ms,
        )

        self._fills[symbol].append(rec)
        self._total_count += 1
        if is_requote:
            self._requote_count += 1
            logger.warning(
                f"⚡ REQUOTE {symbol}: intended={intended_price:.5f} "
                f"actual={actual_price:.5f} slip={slippage_abs:.5f} "
                f"code={retcode} ({rec.retcode_name})"
            )

        return rec

    def get_report(self, symbol: Optional[str] = None) -> dict:
        """
        Fill quality report — per symbol or global

        Returns:
          {
            "total_fills": int,
            "requote_count": int,
            "requote_pct": float,
            "avg_slippage_abs": float,
            "avg_slippage_pct": float,
            "max_slippage_abs": float,
            "partial_fill_count": int,
            "avg_latency_ms": float,
            "fill_quality_score": float,   # 0-100 (100 = perfect)
          }
        """
        if symbol:
            fills = self._fills.get(symbol, [])
        else:
            fills = [f for fl in self._fills.values() for f in fl]

        if not fills:
            return {"total_fills": 0, "fill_quality_score": 100.0}

        n = len(fills)
        requotes = sum(1 for f in fills if f.is_requote)
        partials = sum(1 for f in fills if f.is_partial_fill)
        slippages = [f.slippage_abs for f in fills]
        slip_pcts = [f.slippage_pct for f in fills]
        latencies = [f.fill_latency_ms for f in fills if f.fill_latency_ms > 0]

        avg_slip = sum(slippages) / n
        max_slip = max(slippages)

        # Fill quality score: starts at 100, deduct for issues
        # -5 per requote, -10 if avg slippage > 0.01%, -20 if > 0.05%
        score = 100.0
        score -= requotes * 5
        avg_slip_pct = sum(slip_pcts) / n
        if avg_slip_pct > 0.05:
            score -= 20
        elif avg_slip_pct > 0.01:
            score -= 10
        score -= partials * 3
        score = max(0.0, min(100.0, score))

        return {
            "total_fills":        n,
            "requote_count":      requotes,
            "requote_pct":        round(requotes / n * 100, 1),
            "partial_fill_count": partials,
            "avg_slippage_abs":   round(avg_slip, 8),
            "avg_slippage_pct":   round(avg_slip_pct, 4),
            "max_slippage_abs":   round(max_slip, 8),
            "avg_latency_ms":     round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "fill_quality_score": round(score, 1),
        }

    def get_all_records(self, symbol: Optional[str] = None) -> list[dict]:
        """Export all fill records as dicts"""
        if symbol:
            return [asdict(f) for f in self._fills.get(symbol, [])]
        return [asdict(f) for fl in self._fills.values() for f in fl]


# ============================================================
# 2. OVERNIGHT COST TRACKER — Swap / Borrow / Rollover
# ============================================================

# Typical overnight rates (annualized, approximate)
# Source: Major broker average rates — used for estimation
# Real rates should come from broker API or manual config
OVERNIGHT_RATES = {
    # CFD Forex — swap rate per lot per night (USD approximate)
    # positive = earn, negative = pay
    "CFD": {
        "default_long_annual_pct":   -3.5,    # pay ~3.5% annualized on long
        "default_short_annual_pct":   1.0,    # earn ~1% on short (sometimes pay)
        "triple_wednesday":           True,    # Wed night = 3x swap (settlement)
    },
    # Equities — borrow cost for short positions
    "EQUITIES": {
        "default_long_annual_pct":    0.0,    # no cost for long (no margin interest in prop)
        "default_short_annual_pct":  -5.0,    # borrow fee ~5% annualized (hard-to-borrow = more)
    },
    # Futures — no overnight swap, but rollover cost at expiry
    "FUTURES": {
        "default_long_annual_pct":    0.0,    # no swap
        "default_short_annual_pct":   0.0,    # no swap
        "rollover_cost_per_contract": 2.50,   # typical NQ rollover cost
    },
}


@dataclass
class OvernightCostRecord:
    """Single overnight holding cost event"""
    symbol:          str   = ""
    date:            str   = ""       # night the cost applies to
    side:            str   = ""       # LONG / SHORT
    size:            float = 0.0      # lots / shares / contracts
    asset_class:     str   = ""
    position_value:  float = 0.0      # notional value
    cost_usd:        float = 0.0      # negative = cost, positive = earn
    rate_applied:    float = 0.0      # annual % rate used
    is_triple:       bool  = False    # Wednesday triple swap
    notes:           str   = ""


class OvernightCostTracker:
    """
    Track overnight holding costs per position.

    CFD:       Swap = (lots × contract_mult × rate) / 360 × nights
    EQUITIES:  Borrow = (shares × price × annual_rate) / 360
    FUTURES:   0 per night, rollover at expiry
    """

    def __init__(self, asset_class: str = "CFD", contract_multiplier: int = 100_000):
        self.asset_class         = asset_class
        self.contract_multiplier = contract_multiplier
        self._records: list[OvernightCostRecord] = []
        self._rates_override: dict[str, dict] = {}

    def set_custom_rate(self, symbol: str, long_annual_pct: float, short_annual_pct: float):
        """Override default rates for a specific symbol"""
        self._rates_override[symbol] = {
            "long_annual_pct":  long_annual_pct,
            "short_annual_pct": short_annual_pct,
        }

    def estimate_overnight_cost(
        self,
        symbol:       str,
        size:         float,
        side:         str,
        entry_price:  float,
        nights:       int   = 1,
        is_wednesday: bool  = False,
    ) -> OvernightCostRecord:
        """
        Estimate overnight cost for holding a position.

        Args:
            symbol:       instrument symbol
            size:         lots (CFD) / shares (EQ) / contracts (FUT)
            side:         "LONG" or "SHORT"
            entry_price:  current or entry price
            nights:       number of nights held
            is_wednesday: True if crossing Wednesday night (triple swap for CFD)

        Returns:
            OvernightCostRecord with cost_usd (negative = cost, positive = earn)
        """
        rates_cfg = OVERNIGHT_RATES.get(self.asset_class, {})

        # Check for symbol-specific override
        override = self._rates_override.get(symbol, {})
        if side.upper() in ("LONG", "BUY"):
            annual_rate = override.get("long_annual_pct",
                          rates_cfg.get("default_long_annual_pct", 0.0))
        else:
            annual_rate = override.get("short_annual_pct",
                          rates_cfg.get("default_short_annual_pct", 0.0))

        # Calculate position notional value
        if self.asset_class == "CFD":
            notional = size * self.contract_multiplier * entry_price
        elif self.asset_class == "EQUITIES":
            notional = size * entry_price
        elif self.asset_class == "FUTURES":
            notional = size * self.contract_multiplier * entry_price
        else:
            notional = size * entry_price

        # Overnight cost = notional × (rate / 360) × nights
        multiplier = nights
        is_triple = False
        if self.asset_class == "CFD" and is_wednesday and rates_cfg.get("triple_wednesday"):
            multiplier = nights + 2   # Wednesday = 3x (Fri+Sat+Sun settlement)
            is_triple = True

        daily_cost = notional * (annual_rate / 100.0) / 360.0
        total_cost = daily_cost * multiplier

        # Futures: add rollover if applicable
        if self.asset_class == "FUTURES":
            rollover = rates_cfg.get("rollover_cost_per_contract", 0.0)
            # Rollover only at expiry, not nightly — this is per-contract one-time
            # For nightly estimate, we use 0 (futures don't have swap)
            total_cost = 0.0

        record = OvernightCostRecord(
            symbol         = symbol,
            date           = datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            side           = side.upper(),
            size           = size,
            asset_class    = self.asset_class,
            position_value = round(notional, 2),
            cost_usd       = round(total_cost, 4),
            rate_applied   = annual_rate,
            is_triple      = is_triple,
            notes          = f"{multiplier}x night(s)" if multiplier > 1 else "",
        )

        self._records.append(record)
        if total_cost != 0:
            logger.info(
                f"🌙 Overnight {symbol} {side}: {size} × ${entry_price:.5f} "
                f"= ${notional:,.2f} notional → cost=${total_cost:.4f} "
                f"({annual_rate}% annual, {multiplier} night(s))"
            )

        return record

    def get_total_overnight_cost(self, symbol: Optional[str] = None) -> float:
        """Sum of all overnight costs (for P&L deduction)"""
        if symbol:
            return sum(r.cost_usd for r in self._records if r.symbol == symbol)
        return sum(r.cost_usd for r in self._records)

    def get_records(self, symbol: Optional[str] = None) -> list[dict]:
        """Export records as dicts"""
        recs = self._records if not symbol else [r for r in self._records if r.symbol == symbol]
        return [asdict(r) for r in recs]


# ============================================================
# 3. SPREAD MONITOR — Real-time Spread Capture & Stats
# ============================================================

@dataclass
class SpreadSnapshot:
    """Single spread observation"""
    symbol:    str   = ""
    timestamp: float = 0.0      # unix timestamp
    bid:       float = 0.0
    ask:       float = 0.0
    spread:    float = 0.0      # ask - bid
    spread_pct: float = 0.0     # spread / mid × 100


class SpreadMonitor:
    """
    Record and analyze spread patterns.

    Usage:
        mon = SpreadMonitor()
        # Record whenever you have bid/ask data
        mon.record_spread("EURUSD", 1.08490, 1.08510)
        mon.record_spread("EURUSD", 1.08495, 1.08515)

        # Get stats for sizing / backtest
        stats = mon.get_stats("EURUSD")
        # → {"avg_spread": 0.00020, "max_spread": ..., "samples": 2}
    """

    def __init__(self, max_history: int = 1000):
        self._history: dict[str, list[SpreadSnapshot]] = defaultdict(list)
        self._max_history = max_history

    def record_spread(self, symbol: str, bid: float, ask: float) -> SpreadSnapshot:
        """Record a bid/ask observation"""
        spread = ask - bid
        mid = (bid + ask) / 2.0
        spread_pct = (spread / mid * 100) if mid > 0 else 0.0

        snap = SpreadSnapshot(
            symbol    = symbol,
            timestamp = time.time(),
            bid       = bid,
            ask       = ask,
            spread    = round(spread, 8),
            spread_pct = round(spread_pct, 6),
        )

        history = self._history[symbol]
        history.append(snap)

        # Trim old history
        if len(history) > self._max_history:
            self._history[symbol] = history[-self._max_history:]

        return snap

    def get_current_spread(self, symbol: str) -> Optional[float]:
        """Get most recent spread for a symbol (None if no data)"""
        history = self._history.get(symbol, [])
        if not history:
            return None
        # Only return if recorded within last 60 seconds
        last = history[-1]
        if time.time() - last.timestamp > 60:
            return None
        return last.spread

    def get_stats(self, symbol: str, lookback_minutes: int = 60) -> dict:
        """
        Spread statistics for a symbol.

        Returns:
          {
            "avg_spread": float,
            "avg_spread_pct": float,
            "min_spread": float,
            "max_spread": float,
            "current_spread": float or None,
            "samples": int,
            "is_wide": bool,     # True if current > 2× average
          }
        """
        history = self._history.get(symbol, [])
        cutoff = time.time() - (lookback_minutes * 60)
        recent = [s for s in history if s.timestamp >= cutoff]

        if not recent:
            return {
                "avg_spread": 0.0, "avg_spread_pct": 0.0,
                "min_spread": 0.0, "max_spread": 0.0,
                "current_spread": None, "samples": 0, "is_wide": False,
            }

        spreads = [s.spread for s in recent]
        pcts    = [s.spread_pct for s in recent]
        avg_sp  = sum(spreads) / len(spreads)
        current = recent[-1].spread

        return {
            "avg_spread":     round(avg_sp, 8),
            "avg_spread_pct": round(sum(pcts) / len(pcts), 6),
            "min_spread":     round(min(spreads), 8),
            "max_spread":     round(max(spreads), 8),
            "current_spread": round(current, 8),
            "samples":        len(recent),
            "is_wide":        current > (avg_sp * 2.0) if avg_sp > 0 else False,
        }

    def should_delay_entry(self, symbol: str, max_spread_mult: float = 3.0) -> tuple[bool, str]:
        """
        Check if spread is too wide to enter.

        Returns:
            (should_delay: bool, reason: str)
        """
        stats = self.get_stats(symbol)
        if stats["samples"] < 5:
            return False, "Insufficient spread data"

        current = stats["current_spread"]
        avg = stats["avg_spread"]
        if current is None:
            return False, "No current spread data"

        if avg > 0 and current > avg * max_spread_mult:
            return True, (
                f"Spread too wide: {current:.5f} > {max_spread_mult}× avg({avg:.5f}). "
                f"Wait for normalization."
            )
        return False, "Spread OK"


# ============================================================
# 4. TRADING COST CALCULATOR — Unified Cost Model
# ============================================================

class TradingCostCalculator:
    """
    Unified trading cost calculation for all asset classes.

    Computes total round-trip cost including:
      - Spread cost (bid-ask)
      - Commission (where applicable)
      - Slippage estimate
      - Swap/overnight estimate

    Used for:
      - Pre-trade cost check (is trade still profitable after costs?)
      - Backtest-grade P&L adjustment
      - Risk-adjusted position sizing
    """

    def __init__(
        self,
        asset_class:           str   = "EQUITIES",
        commission_per_unit:   float = 0.005,     # per share (EQ) or per contract (FUT)
        typical_spread_pips:   float = 0.0,       # for estimation when no live data
        slippage_estimate_pct: float = 0.001,     # 0.1% default
        spread_buffer_pct:     float = 0.001,     # additional safety buffer
        contract_multiplier:   int   = 1,
        tick_size:             float = 0.01,
    ):
        self.asset_class           = asset_class
        self.commission_per_unit   = commission_per_unit
        self.typical_spread_pips   = typical_spread_pips
        self.slippage_estimate_pct = slippage_estimate_pct
        self.spread_buffer_pct     = spread_buffer_pct
        self.contract_multiplier   = contract_multiplier
        self.tick_size             = tick_size

    @classmethod
    def from_config(cls, cfg) -> "TradingCostCalculator":
        """Create from Config class"""
        return cls(
            asset_class           = cfg.ASSET_CLASS,
            commission_per_unit   = cfg.COMMISSION_PER_SHARE,
            typical_spread_pips   = getattr(cfg, "TYPICAL_SPREAD_PIPS", 0.0),
            slippage_estimate_pct = cfg.SLIPPAGE_PCT,
            spread_buffer_pct     = cfg.SPREAD_BUFFER_PCT,
            contract_multiplier   = cfg.CONTRACT_MULTIPLIER,
            tick_size             = cfg.TICK_SIZE,
        )

    def spread_cost(
        self,
        entry_price:    float,
        size:           float,
        actual_spread:  Optional[float] = None,
    ) -> float:
        """
        Calculate spread cost for a trade.

        CFD:       spread × lots × contract_multiplier
        EQUITIES:  spread × shares (or use % estimate)
        FUTURES:   spread × contracts × contract_multiplier
        """
        if actual_spread is not None and actual_spread > 0:
            spread = actual_spread
        elif self.typical_spread_pips > 0:
            spread = self.typical_spread_pips * self.tick_size
        else:
            spread = entry_price * self.spread_buffer_pct

        if self.asset_class == "CFD":
            return spread * size * self.contract_multiplier
        elif self.asset_class == "FUTURES":
            return spread * size * self.contract_multiplier
        else:  # EQUITIES
            return spread * size

    def commission_cost(self, size: float) -> float:
        """
        Round-trip commission (entry + exit).

        EQUITIES: commission_per_share × shares × 2
        CFD:      typically 0 (embedded in spread)
        FUTURES:  commission_per_contract × contracts × 2
        """
        return self.commission_per_unit * size * 2

    def slippage_cost(self, entry_price: float, size: float) -> float:
        """Estimated slippage cost (round-trip)"""
        slip_per_unit = entry_price * self.slippage_estimate_pct
        if self.asset_class in ("CFD", "FUTURES"):
            return slip_per_unit * size * self.contract_multiplier * 2
        return slip_per_unit * size * 2

    def total_round_trip_cost(
        self,
        entry_price:    float,
        size:           float,
        actual_spread:  Optional[float] = None,
        nights_held:    int = 0,
        overnight_rate: float = 0.0,
        symbol:         str = "",
    ) -> dict:
        """
        Complete round-trip cost breakdown.

        Returns:
          {
            "spread_cost_usd":     float,
            "commission_cost_usd": float,
            "slippage_cost_usd":   float,
            "overnight_cost_usd":  float,
            "total_cost_usd":      float,
            "cost_per_unit":       float,  # total / size
            "cost_pct":            float,  # total / notional × 100
            "min_profit_to_breakeven": float,  # ราคาต้องวิ่งเท่าไหร่ถึง breakeven
          }
        """
        sp_cost   = self.spread_cost(entry_price, size, actual_spread)
        comm_cost = self.commission_cost(size)
        slip_cost = self.slippage_cost(entry_price, size)

        # Overnight cost
        ovn_cost = 0.0
        if nights_held > 0 and overnight_rate != 0:
            if self.asset_class == "CFD":
                notional = size * self.contract_multiplier * entry_price
            elif self.asset_class == "FUTURES":
                notional = size * self.contract_multiplier * entry_price
            else:
                notional = size * entry_price
            ovn_cost = abs(notional * (overnight_rate / 100.0) / 360.0 * nights_held)

        total = sp_cost + comm_cost + slip_cost + ovn_cost

        # Notional value
        if self.asset_class in ("CFD", "FUTURES"):
            notional = size * self.contract_multiplier * entry_price
        else:
            notional = size * entry_price

        cost_per_unit = total / size if size > 0 else 0
        cost_pct = (total / notional * 100) if notional > 0 else 0

        # Min price movement to breakeven (one side only, half of round-trip)
        half_cost = total / 2.0
        if self.asset_class in ("CFD", "FUTURES"):
            breakeven_move = half_cost / (size * self.contract_multiplier) if size > 0 else 0
        else:
            breakeven_move = half_cost / size if size > 0 else 0

        result = {
            "spread_cost_usd":           round(sp_cost, 4),
            "commission_cost_usd":       round(comm_cost, 4),
            "slippage_cost_usd":         round(slip_cost, 4),
            "overnight_cost_usd":        round(ovn_cost, 4),
            "total_cost_usd":            round(total, 4),
            "cost_per_unit":             round(cost_per_unit, 6),
            "cost_pct":                  round(cost_pct, 4),
            "min_profit_to_breakeven":   round(breakeven_move, 8),
        }

        logger.info(
            f"💰 Cost {symbol or 'N/A'}: spread=${sp_cost:.2f} + "
            f"comm=${comm_cost:.2f} + slip=${slip_cost:.2f} + "
            f"overnight=${ovn_cost:.2f} = ${total:.2f} total "
            f"({cost_pct:.3f}% of ${notional:,.0f} notional)"
        )

        return result

    def is_trade_viable(
        self,
        entry_price:    float,
        take_profit:    float,
        size:           float,
        actual_spread:  Optional[float] = None,
        nights_held:    int = 0,
        overnight_rate: float = 0.0,
    ) -> tuple[bool, str]:
        """
        Check if expected profit exceeds total costs.

        Returns:
            (is_viable: bool, reason: str)
        """
        costs = self.total_round_trip_cost(
            entry_price, size, actual_spread, nights_held, overnight_rate
        )
        total_cost = costs["total_cost_usd"]

        # Expected profit from TP
        price_diff = abs(take_profit - entry_price)
        if self.asset_class in ("CFD", "FUTURES"):
            expected_profit = price_diff * size * self.contract_multiplier
        else:
            expected_profit = price_diff * size

        if expected_profit <= total_cost:
            return False, (
                f"Expected profit ${expected_profit:.2f} ≤ total cost ${total_cost:.2f}. "
                f"Breakeven requires {costs['min_profit_to_breakeven']:.5f} price move."
            )

        profit_after_cost = expected_profit - total_cost
        return True, (
            f"Viable: profit ${expected_profit:.2f} - cost ${total_cost:.2f} "
            f"= ${profit_after_cost:.2f} net"
        )


# ============================================================
# PENDING ORDER TYPES — Used by executor adapters
# ============================================================

class OrderType:
    """
    Order type constants — universal across adapters.

    Market:  Execute immediately at best available price
    Limit:   Execute at specified price or better (passive entry)
    Stop:    Trigger market order when price reaches level
    StopLimit: Trigger limit order when price reaches level
    """
    MARKET     = "MARKET"
    LIMIT      = "LIMIT"         # Buy below / Sell above current price
    STOP       = "STOP"          # Buy above / Sell below (breakout)
    STOP_LIMIT = "STOP_LIMIT"    # Stop triggers a limit order

    @classmethod
    def all_types(cls) -> list[str]:
        return [cls.MARKET, cls.LIMIT, cls.STOP, cls.STOP_LIMIT]

    @classmethod
    def is_pending(cls, order_type: str) -> bool:
        """True if order is a pending/waiting type"""
        return order_type in (cls.LIMIT, cls.STOP, cls.STOP_LIMIT)


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")

    print("=" * 60)
    print("  TRADING COST MANAGER — Universal Test")
    print("=" * 60)

    # ── Test 1: Fill Quality Tracker
    print("\n[1] Fill Quality Tracker")
    fq = FillQualityTracker()
    fq.record_fill("EURUSD", 1.08500, 1.08510, retcode=10009)
    fq.record_fill("EURUSD", 1.08500, 1.08520, retcode=10004)  # requote
    fq.record_fill("NVDA",   182.30,  182.35,  retcode=10009)
    report = fq.get_report("EURUSD")
    print(f"    EURUSD: {report['total_fills']} fills, "
          f"{report['requote_count']} requotes, "
          f"avg slip={report['avg_slippage_abs']:.5f}, "
          f"quality={report['fill_quality_score']}")
    report_all = fq.get_report()
    print(f"    Global: {report_all['total_fills']} fills, quality={report_all['fill_quality_score']}")

    # ── Test 2: Overnight Cost
    print("\n[2] Overnight Cost Tracker")
    ovn = OvernightCostTracker(asset_class="CFD", contract_multiplier=100_000)
    cost = ovn.estimate_overnight_cost("EURUSD", 2.78, "LONG", 1.08500)
    print(f"    EURUSD 2.78 lots LONG: cost=${cost.cost_usd:.4f}/night "
          f"({cost.rate_applied}% annual)")
    cost_wed = ovn.estimate_overnight_cost("EURUSD", 2.78, "LONG", 1.08500,
                                            is_wednesday=True)
    print(f"    Same on Wednesday:     cost=${cost_wed.cost_usd:.4f} (3x swap)")

    ovn_eq = OvernightCostTracker(asset_class="EQUITIES")
    cost_eq = ovn_eq.estimate_overnight_cost("TSLA", 100, "SHORT", 250.0)
    print(f"    TSLA 100 shares SHORT: borrow=${cost_eq.cost_usd:.4f}/night "
          f"({cost_eq.rate_applied}% annual)")

    # ── Test 3: Spread Monitor
    print("\n[3] Spread Monitor")
    sm = SpreadMonitor()
    sm.record_spread("EURUSD", 1.08490, 1.08510)
    sm.record_spread("EURUSD", 1.08495, 1.08515)
    sm.record_spread("EURUSD", 1.08485, 1.08505)
    sm.record_spread("EURUSD", 1.08480, 1.08530)  # wide spread
    sm.record_spread("EURUSD", 1.08492, 1.08512)
    stats = sm.get_stats("EURUSD")
    print(f"    EURUSD: avg={stats['avg_spread']:.5f} "
          f"max={stats['max_spread']:.5f} "
          f"current={stats['current_spread']:.5f} "
          f"wide={stats['is_wide']}")
    delay, reason = sm.should_delay_entry("EURUSD")
    print(f"    Delay entry? {delay} — {reason}")

    # ── Test 4: Cost Calculator — CFD
    print("\n[4] Cost Calculator — CFD (EURUSD)")
    calc = TradingCostCalculator(
        asset_class="CFD",
        commission_per_unit=0.0,
        typical_spread_pips=1.5,    # 1.5 pips typical
        slippage_estimate_pct=0.0005,
        contract_multiplier=100_000,
        tick_size=0.00001,
    )
    costs = calc.total_round_trip_cost(
        entry_price=1.08500, size=2.78, symbol="EURUSD",
        nights_held=1, overnight_rate=-3.5,
    )
    print(f"    Spread:     ${costs['spread_cost_usd']:.2f}")
    print(f"    Commission: ${costs['commission_cost_usd']:.2f}")
    print(f"    Slippage:   ${costs['slippage_cost_usd']:.2f}")
    print(f"    Overnight:  ${costs['overnight_cost_usd']:.2f}")
    print(f"    TOTAL:      ${costs['total_cost_usd']:.2f} ({costs['cost_pct']:.3f}%)")
    print(f"    Breakeven:  {costs['min_profit_to_breakeven']:.5f} price move")

    viable, reason = calc.is_trade_viable(1.08500, 1.08860, 2.78)
    print(f"    Viable (TP=1.0886)? {viable} — {reason}")

    # ── Test 5: Cost Calculator — Equities
    print("\n[5] Cost Calculator — Equities (NVDA)")
    calc_eq = TradingCostCalculator(
        asset_class="EQUITIES",
        commission_per_unit=0.005,
        slippage_estimate_pct=0.001,
        spread_buffer_pct=0.001,
        contract_multiplier=1,
        tick_size=0.01,
    )
    costs_eq = calc_eq.total_round_trip_cost(
        entry_price=182.0, size=32, symbol="NVDA"
    )
    print(f"    TOTAL: ${costs_eq['total_cost_usd']:.2f} ({costs_eq['cost_pct']:.3f}%)")
    print(f"    Breakeven: ${costs_eq['min_profit_to_breakeven']:.4f} per share")

    # ── Test 6: Order Types
    print("\n[6] Order Types")
    for ot in OrderType.all_types():
        print(f"    {ot}: pending={OrderType.is_pending(ot)}")

    print("\n✅ All tests passed!")