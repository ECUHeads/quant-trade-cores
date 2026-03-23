"""
universal_risk_manager.py
=========================
Universal Position Sizing & Risk Control — Level 2

Architecture:
  - ATR-based SL/TP: SL = Entry ± (1.5 × ATR_15m)
  - Multi-asset sizing: SHARES (equities) / LOTS (CFD) / CONTRACTS (futures)
  - Config-driven: อ่าน asset_class, contract_multiplier, tick_size จาก Config
  - Daily loss kill-switch + volume rule + min trade range

CRITICAL:
  ต้องเทสต์ทศนิยมและ contract multiplier ให้เป๊ะ
  ห้ามเกิด over-leverage เด็ดขาด
"""

import math
import logging
from typing import Optional

logger = logging.getLogger("UniversalRisk")


class UniversalRiskManager:
    """
    Level 2: Position Sizing + Risk Guard (Universal)

    Sizing by asset class:
      EQUITIES → shares = floor(risk_budget / actual_risk_per_share)
      CFD      → lots   = risk_budget / (sl_distance × contract_multiplier)
                          rounded to 0.01 lot
      FUTURES  → contracts = floor(risk_budget / (sl_distance × contract_multiplier))
                          integer only

    Usage:
        from config import Config
        Config.load_profile("TTP_5K_FLEX")

        rm = UniversalRiskManager.from_config(Config)
        sl, tp = rm.calculate_atr_levels(entry=182.0, atr_15m=1.80, side="LONG")
        order = rm.calculate_order("NVDA", "LONG", 182.0, sl, daily_loss=50.0)
    """

    def __init__(
        self,
        asset_class:          str   = "EQUITIES",
        max_buying_power:     float = 80_000.0,
        max_daily_loss_usd:   float = 700.0,
        daily_loss_buffer:    float = 100.0,
        risk_per_trade_usd:   float = 100.0,
        commission_per_share: float = 0.005,
        slippage_buffer_pct:  float = 0.001,
        spread_buffer_pct:    float = 0.001,
        volume_rule_pct:      float = 0.05,
        contract_multiplier:  int   = 1,
        tick_size:            float = 0.01,
        allow_fractional:     bool  = False,
        min_trade_range:      float = 0.10,
        atr_stop_mult:        float = 1.5,
        rr_target:            float = 2.0,
    ):
        self.asset_class          = asset_class
        self.max_buying_power     = max_buying_power
        self.max_daily_loss_usd   = max_daily_loss_usd
        self.daily_loss_buffer    = daily_loss_buffer
        self.risk_per_trade_usd   = risk_per_trade_usd
        self.commission_per_share = commission_per_share
        self.slippage_buffer_pct  = slippage_buffer_pct
        self.spread_buffer_pct    = spread_buffer_pct
        self.volume_rule_pct      = volume_rule_pct
        self.contract_multiplier  = contract_multiplier
        self.tick_size            = tick_size
        self.allow_fractional     = allow_fractional
        self.min_trade_range      = min_trade_range
        self.atr_stop_mult        = atr_stop_mult
        self.rr_target            = rr_target

    @classmethod
    def from_config(cls, cfg) -> "UniversalRiskManager":
        """สร้างจาก Config class"""
        return cls(
            asset_class          = cfg.ASSET_CLASS,
            max_buying_power     = cfg.MAX_BUYING_POWER,
            max_daily_loss_usd   = cfg.MAX_DAILY_LOSS_USD,
            daily_loss_buffer    = cfg.DAILY_LOSS_BUFFER,
            risk_per_trade_usd   = cfg.RISK_PER_TRADE_USD,
            commission_per_share = cfg.COMMISSION_PER_SHARE,
            slippage_buffer_pct  = cfg.SLIPPAGE_PCT,
            spread_buffer_pct    = cfg.SPREAD_BUFFER_PCT,
            volume_rule_pct      = cfg.VOLUME_RULE_PCT,
            contract_multiplier  = cfg.CONTRACT_MULTIPLIER,
            tick_size            = cfg.TICK_SIZE,
            allow_fractional     = cfg.ALLOW_FRACTIONAL,
            min_trade_range      = cfg.MIN_TRADE_RANGE,
            atr_stop_mult        = cfg.ATR_STOP_MULT,
            rr_target            = cfg.RR_TARGET,
        )

    # ------------------------------------------
    # ATR-BASED SL/TP LEVELS
    # ------------------------------------------

    def calculate_atr_levels(
        self, entry_price: float, atr_15m: float, side: str,
    ) -> tuple[float, float]:
        """
        SL/TP จาก ATR_15m

        LONG:  SL = entry - (mult × ATR), TP = entry + (mult × ATR × RR)
        SHORT: SL = entry + (mult × ATR), TP = entry - (mult × ATR × RR)
        """
        dist = self.atr_stop_mult * atr_15m

        if side.upper() in ("LONG", "BUY"):
            sl = self._round_to_tick(entry_price - dist)
            tp = self._round_to_tick(entry_price + dist * self.rr_target)
        else:
            sl = self._round_to_tick(entry_price + dist)
            tp = self._round_to_tick(entry_price - dist * self.rr_target)

        # ── Enforce min trade range
        if abs(entry_price - sl) < self.min_trade_range:
            if side.upper() in ("LONG", "BUY"):
                sl = self._round_to_tick(entry_price - self.min_trade_range)
            else:
                sl = self._round_to_tick(entry_price + self.min_trade_range)
        if abs(entry_price - tp) < self.min_trade_range:
            if side.upper() in ("LONG", "BUY"):
                tp = self._round_to_tick(entry_price + self.min_trade_range)
            else:
                tp = self._round_to_tick(entry_price - self.min_trade_range)

        return sl, tp

    # ------------------------------------------
    # MAIN: Position Sizing
    # ------------------------------------------

    def calculate_order(
        self,
        symbol:             str,
        side:               str,
        entry_price:        float,
        stop_loss_price:    float,
        current_daily_loss: float,
        prev_minute_volume: int   = 0,
        is_easy_to_borrow:  bool  = True,
        market_cap:         float = 50e9,
    ) -> dict:
        """
        คำนวณ position size ตาม asset class

        Returns:
          {"status": "APPROVED"/"REJECTED", "size": ..., "sizing_unit": ..., ...}
        """
        logger.info(f"─── Sizing: {symbol} ({side.upper()}) [{self.asset_class}] ───")

        # ── Gate: Daily Loss Kill-Switch
        loss_limit = self.max_daily_loss_usd - self.daily_loss_buffer
        if current_daily_loss >= loss_limit:
            return self._reject(
                f"Daily loss ${current_daily_loss:.2f} ≥ limit ${loss_limit:.2f}"
            )

        # ── Gate: Short selling (equities only)
        if self.asset_class == "EQUITIES" and side.upper() in ("SHORT", "SELL"):
            if not is_easy_to_borrow:
                return self._reject("Short rejected: Hard-to-Borrow")
            if entry_price < 50.0 and market_cap < 10e9:
                return self._reject("Short rejected: small cap squeeze risk")

        # ── Risk per share/point
        raw_risk = abs(entry_price - stop_loss_price)
        if raw_risk <= 0:
            return self._reject("SL must differ from entry")

        # ── Min trade range
        if raw_risk < self.min_trade_range - 1e-9:
            return self._reject(
                f"Trade range ${raw_risk:.4f} < min ${self.min_trade_range:.2f}"
            )

        # ── Sizing by asset class
        if self.asset_class == "EQUITIES":
            size, unit = self._size_equities(entry_price, raw_risk)
        elif self.asset_class == "CFD":
            size, unit = self._size_cfd(raw_risk)
        elif self.asset_class == "FUTURES":
            size, unit = self._size_futures(raw_risk)
        else:
            return self._reject(f"Unknown asset_class: {self.asset_class}")

        if size <= 0:
            return self._reject(f"Computed size = 0 ({unit})")

        # ── Buying power cap (equities only)
        if self.asset_class == "EQUITIES":
            pos_value = size * entry_price
            if pos_value > self.max_buying_power:
                size = math.floor(self.max_buying_power / entry_price)
                pos_value = size * entry_price
                if size <= 0:
                    return self._reject("Size=0 after buying power cap")

        # ── Volume rule (equities: 5% of prev candle)
        if prev_minute_volume > 0 and self.volume_rule_pct > 0 and self.asset_class == "EQUITIES":
            max_shares = math.floor(prev_minute_volume * self.volume_rule_pct)
            if size > max_shares:
                logger.warning(
                    f"[{symbol}] Volume Rule: {size} → {max_shares} shares"
                )
                size = max_shares
                if size <= 0:
                    return self._reject("Volume rule: size = 0")

        # ── Compute actual risk
        actual_risk_usd = self._compute_actual_risk(size, raw_risk, entry_price)

        result = {
            "status":                 "APPROVED",
            "symbol":                 symbol,
            "side":                   side.upper(),
            "size":                   size,
            "sizing_unit":            unit,
            "entry_price":            entry_price,
            "stop_loss_price":        stop_loss_price,
            "position_value_usd":     round(size * entry_price, 2) if self.asset_class == "EQUITIES"
                                      else round(size * self.contract_multiplier * entry_price, 2),
            "actual_risk_usd":        round(actual_risk_usd, 2),
            "raw_risk_per_unit":      round(raw_risk, 4),
            "contract_multiplier":    self.contract_multiplier,
        }
        logger.info(
            f"[{symbol}] ✅ {size} {unit} | risk=${actual_risk_usd:.2f} "
            f"| mult={self.contract_multiplier}"
        )
        return result

    # ------------------------------------------
    # SIZING METHODS per asset class
    # ------------------------------------------

    def _size_equities(self, entry_price: float, raw_risk: float) -> tuple:
        """EQUITIES: shares = floor(budget / actual_risk_per_share)"""
        slippage  = entry_price * self.slippage_buffer_pct
        spread    = entry_price * self.spread_buffer_pct
        actual_rps = raw_risk + (self.commission_per_share * 2) + slippage + spread
        shares = math.floor(self.risk_per_trade_usd / actual_rps)
        return shares, "SHARES"

    def _size_cfd(self, raw_risk_points: float) -> tuple:
        """
        CFD: lots = budget / (sl_distance × contract_multiplier)
        CFD lots สามารถเป็น 0.01 ได้ (fractional)
        """
        risk_per_lot = raw_risk_points * self.contract_multiplier
        if risk_per_lot <= 0:
            return 0, "LOTS"
        lots = self.risk_per_trade_usd / risk_per_lot
        # ── Round to 0.01 lot (never up)
        lots = math.floor(lots * 100) / 100
        return lots, "LOTS"

    def _size_futures(self, raw_risk_points: float) -> tuple:
        """
        FUTURES: contracts = floor(budget / (sl_distance × contract_multiplier))
        Futures = integer contracts เท่านั้น
        """
        risk_per_contract = raw_risk_points * self.contract_multiplier
        if risk_per_contract <= 0:
            return 0, "CONTRACTS"
        contracts = math.floor(self.risk_per_trade_usd / risk_per_contract)
        return contracts, "CONTRACTS"

    def _compute_actual_risk(self, size, raw_risk: float, entry_price: float) -> float:
        """คำนวณ risk จริง (USD) ตาม asset class"""
        if self.asset_class == "EQUITIES":
            slippage = entry_price * self.slippage_buffer_pct
            spread   = entry_price * self.spread_buffer_pct
            per_share = raw_risk + (self.commission_per_share * 2) + slippage + spread
            return size * per_share
        elif self.asset_class == "CFD":
            return size * raw_risk * self.contract_multiplier
        elif self.asset_class == "FUTURES":
            return size * raw_risk * self.contract_multiplier
        return size * raw_risk

    # ------------------------------------------
    # HELPERS
    # ------------------------------------------

    def _round_to_tick(self, price: float) -> float:
        """Round price to tick size"""
        if self.tick_size <= 0:
            return round(price, 2)
        return round(round(price / self.tick_size) * self.tick_size, 8)

    @staticmethod
    def _reject(reason: str) -> dict:
        logger.warning(f"⛔ REJECTED: {reason}")
        return {"status": "REJECTED", "reason": reason}


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")

    print("=" * 60)
    print("  UNIVERSAL RISK MANAGER — Test")
    print("=" * 60)

    # ── Equities
    print("\n[1] EQUITIES — NVDA")
    rm = UniversalRiskManager(asset_class="EQUITIES", risk_per_trade_usd=100)
    sl, tp = rm.calculate_atr_levels(182.0, 1.80, "LONG")
    print(f"    SL={sl} TP={tp}")
    r = rm.calculate_order("NVDA", "LONG", 182.0, sl, current_daily_loss=50)
    print(f"    {r['status']}: {r.get('size',0)} {r.get('sizing_unit','')} risk=${r.get('actual_risk_usd',0)}")

    # ── CFD
    print("\n[2] CFD — EURUSD")
    rm2 = UniversalRiskManager(
        asset_class="CFD", risk_per_trade_usd=500,
        contract_multiplier=100_000, tick_size=0.00001,
        min_trade_range=0.0, allow_fractional=True,
    )
    sl2, tp2 = rm2.calculate_atr_levels(1.08500, 0.00120, "LONG")
    print(f"    SL={sl2} TP={tp2}")
    r2 = rm2.calculate_order("EURUSD", "LONG", 1.08500, sl2, current_daily_loss=0)
    print(f"    {r2['status']}: {r2.get('size',0)} {r2.get('sizing_unit','')} risk=${r2.get('actual_risk_usd',0)}")

    # ── Futures
    print("\n[3] FUTURES — NQ Micro")
    rm3 = UniversalRiskManager(
        asset_class="FUTURES", risk_per_trade_usd=500,
        contract_multiplier=20, tick_size=0.25,
        min_trade_range=0.0, allow_fractional=False,
    )
    sl3, tp3 = rm3.calculate_atr_levels(20500.0, 35.0, "LONG")
    print(f"    SL={sl3} TP={tp3}")
    r3 = rm3.calculate_order("NQM2026", "LONG", 20500.0, sl3, current_daily_loss=0)
    print(f"    {r3['status']}: {r3.get('size',0)} {r3.get('sizing_unit','')} risk=${r3.get('actual_risk_usd',0)}")

    print("\n✅ Done")
