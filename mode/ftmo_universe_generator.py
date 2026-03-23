"""
ftmo_universe_generator.py
===========================
FTMO Universe Discovery — Scan MT5 Terminal → Generate ftmo_universe.json

ทำหน้าที่เดียวกับ universe.json ของ Equities แต่สำหรับ FTMO CFD:
  - Scan MT5 terminal ดูว่า broker มี instrument อะไรบ้าง
  - Filter ด้วยเกณฑ์ที่เหมาะกับ CFD: spread, session, margin
  - Group ตาม asset class: forex, index, commodity, crypto
  - Generate ftmo_universe.json สำหรับ pipeline ใช้

Usage:
  # Standalone — scan แล้วสร้าง ftmo_universe.json
  python ftmo_universe_generator.py

  # จาก code
  from ftmo_universe_generator import FtmoUniverseGenerator
  gen = FtmoUniverseGenerator(proxy_adapter=adapter)
  universe = gen.generate()
  gen.save("ftmo_universe.json")

  # ดู universe ที่ generate แล้ว
  python ftmo_universe_generator.py --show
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("FtmoUniverse")


# ============================================================
# FILTER CRITERIA — เกณฑ์สำหรับ CFD (ต่างจาก equities)
# ============================================================

DEFAULT_FILTERS = {
    "forex": {
        "max_spread":       30,       # max spread (points) — EURUSD ~10, exotic ~50
        "min_contract_size": 100_000,  # standard forex lot = 100K
        "max_symbols":      15,       # เลือก top 15 pairs
        "prefer_majors":    True,     # เรียง major pairs ก่อน
    },
    "index": {
        "max_spread":       100,      # index spreads กว้างกว่า forex
        "min_contract_size": 1,       # index CFD = 1 contract per lot
        "max_symbols":      8,
    },
    "commodity": {
        "max_spread":       50,
        "min_contract_size": 1,
        "max_symbols":      6,
    },
    "crypto": {
        "max_spread":       500,      # crypto spreads กว้างมาก
        "min_contract_size": 1,
        "max_symbols":      5,
    },
}

# Major forex pairs — เรียงก่อนตอน filter
FOREX_MAJORS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
    "AUDUSD", "USDCAD", "NZDUSD",
]

FOREX_CROSSES = [
    "EURGBP", "EURJPY", "GBPJPY", "EURAUD",
    "EURCAD", "AUDCAD", "AUDNZD", "GBPAUD",
]


# ============================================================
# GENERATOR
# ============================================================

class FtmoUniverseGenerator:
    """
    Scan MT5 terminal → filter → generate ftmo_universe.json

    Output format (similar to universe.json for equities):
    {
      "generated_at": "2026-03-23T...",
      "broker": "FTMO",
      "stats": {
        "total_instruments": 150,
        "after_filter": 34,
        "by_category": {"forex": 15, "index": 8, ...}
      },
      "filters": {...},
      "symbols": ["EURUSD", "GBPUSD", ...],
      "symbol_data": {
        "EURUSD": {
          "mt5_name": "EURUSD",
          "category": "forex",
          "spread": 12,
          "digits": 5,
          "volume_min": 0.01,
          "contract_size": 100000,
          "bid": 1.08520,
          "ask": 1.08532,
          "swap_long": -5.2,
          "swap_short": 1.3,
          ...
        }
      },
      "symbol_map": {"EURUSD": "EURUSD", "US30": "US30.cash", ...}
    }
    """

    def __init__(
        self,
        proxy_adapter=None,
        filters: dict = None,
        categories: list = None,
    ):
        """
        Args:
          proxy_adapter: Mt5ProxyAdapter instance
          filters:       override DEFAULT_FILTERS
          categories:    which categories to include (default: all)
        """
        self._adapter = proxy_adapter
        self._filters = filters or DEFAULT_FILTERS
        self._categories = categories or ["forex", "index", "commodity", "crypto"]

        # Output
        self._raw_symbols: list[dict] = []
        self._filtered: list[dict] = []
        self._universe: dict = {}

    def generate(self) -> dict:
        """
        Full pipeline: scan → filter → build universe dict

        Returns:
          universe dict (same as ftmo_universe.json structure)
        """
        if not self._adapter:
            raise RuntimeError("No proxy adapter — pass proxy_adapter to constructor")

        logger.info("🔍 Scanning MT5 terminal for available instruments...")

        # ── Step 1: Fetch all symbols from MT5
        self._raw_symbols = self._adapter.get_all_symbols()
        if not self._raw_symbols:
            logger.error("No symbols returned from MT5 — check proxy connection")
            return {}

        total = len(self._raw_symbols)
        cats_raw = {}
        for s in self._raw_symbols:
            c = s.get("category", "other")
            cats_raw[c] = cats_raw.get(c, 0) + 1

        logger.info(f"  Total instruments: {total}")
        for c, n in sorted(cats_raw.items(), key=lambda x: -x[1]):
            logger.info(f"    {c:12s}: {n}")

        # ── Step 2: Filter per category
        self._filtered = []
        for cat in self._categories:
            cat_symbols = [s for s in self._raw_symbols if s.get("category") == cat]
            if not cat_symbols:
                logger.info(f"  {cat}: no instruments found")
                continue

            cat_filter = self._filters.get(cat, {})
            filtered = self._apply_filters(cat_symbols, cat_filter, cat)
            self._filtered.extend(filtered)
            logger.info(f"  {cat}: {len(cat_symbols)} → {len(filtered)} after filter")

        # ── Step 3: Build universe dict
        self._universe = self._build_universe()
        logger.info(f"✅ FTMO Universe: {len(self._filtered)} instruments across {len(self._categories)} categories")

        return self._universe

    def _apply_filters(self, symbols: list[dict], filters: dict, category: str) -> list[dict]:
        """Apply category-specific filters"""
        max_spread = filters.get("max_spread", 9999)
        min_contract = filters.get("min_contract_size", 0)
        max_symbols = filters.get("max_symbols", 20)

        # Filter by spread + contract size + has price
        result = []
        for s in symbols:
            spread = s.get("spread", 9999)
            contract = s.get("contract_size", 0)
            bid = s.get("bid", 0)

            if spread > max_spread:
                continue
            if contract < min_contract:
                continue
            if bid <= 0:
                continue  # no price = market closed or invalid
            if s.get("trade_mode", 0) == 0:
                continue  # disabled

            result.append(s)

        # Sort: forex → prefer majors first, then by spread
        if category == "forex" and filters.get("prefer_majors", False):
            def forex_sort_key(s):
                name = s.get("name", "").upper().replace(".", "").replace("M", "")
                # Strip common suffixes
                clean = name[:6] if len(name) >= 6 else name
                if clean in FOREX_MAJORS:
                    return (0, FOREX_MAJORS.index(clean), s.get("spread", 999))
                elif clean in FOREX_CROSSES:
                    return (1, FOREX_CROSSES.index(clean), s.get("spread", 999))
                return (2, 999, s.get("spread", 999))
            result.sort(key=forex_sort_key)
        else:
            # Sort by spread (tightest first)
            result.sort(key=lambda s: s.get("spread", 9999))

        # Limit count
        return result[:max_symbols]

    def _build_universe(self) -> dict:
        """Build output dict"""
        now = datetime.now(timezone.utc)

        symbols = []
        symbol_data = {}
        symbol_map = {}
        by_category = {}

        for s in self._filtered:
            # Canonical name — strip suffixes like .cash, m, etc.
            mt5_name = s.get("name", "")
            canonical = self._canonical_name(mt5_name)
            cat = s.get("category", "other")

            symbols.append(canonical)
            by_category[cat] = by_category.get(cat, 0) + 1
            symbol_map[canonical] = mt5_name

            symbol_data[canonical] = {
                "mt5_name":       mt5_name,
                "category":       cat,
                "description":    s.get("description", ""),
                "spread":         s.get("spread", 0),
                "digits":         s.get("digits", 5),
                "point":          s.get("point", 0.00001),
                "volume_min":     s.get("volume_min", 0.01),
                "volume_max":     s.get("volume_max", 100),
                "volume_step":    s.get("volume_step", 0.01),
                "contract_size":  s.get("contract_size", 100000),
                "bid":            s.get("bid", 0),
                "ask":            s.get("ask", 0),
                "swap_long":      s.get("swap_long", 0),
                "swap_short":     s.get("swap_short", 0),
                "currency_base":  s.get("currency_base", ""),
                "currency_profit": s.get("currency_profit", ""),
            }

        return {
            "generated_at": now.isoformat(),
            "tag":          now.strftime("%d-%m-%Y"),
            "broker":       "FTMO",
            "stats": {
                "total_instruments": len(self._raw_symbols),
                "after_filter":      len(self._filtered),
                "by_category":       by_category,
            },
            "filters": self._filters,
            "symbols": symbols,
            "symbol_data": symbol_data,
            "symbol_map": symbol_map,
        }

    def _canonical_name(self, mt5_name: str) -> str:
        """
        แปลง MT5 symbol name → canonical name สำหรับ engine

        Examples:
          EURUSD   → EURUSD    (forex — ไม่เปลี่ยน)
          US30.cash → US30     (index — ลบ .cash)
          US100.cash → NAS100  (index — rename)
          USOIL.cash → USOIL   (commodity — ลบ .cash)
        """
        # Reverse lookup from Config.SYMBOL_MAP (ถ้า load profile แล้ว)
        try:
            from config import Config
            symbol_map = getattr(Config, "SYMBOL_MAP", {}) or {}
        except ImportError:
            symbol_map = {}

        if symbol_map:
            reverse = {v: k for k, v in symbol_map.items()}
            if mt5_name in reverse:
                return reverse[mt5_name]

        # Generic: strip common suffixes
        clean = mt5_name
        for suffix in [".cash", ".pro", ".raw", "m", "."]:
            if clean.endswith(suffix):
                clean = clean[:-len(suffix)]
                break

        return clean.upper()

    # ------------------------------------------
    # SAVE / LOAD
    # ------------------------------------------

    def save(self, filepath: str = "ftmo_universe.json"):
        """Save universe to JSON file"""
        if not self._universe:
            logger.warning("No universe to save — run generate() first")
            return

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self._universe, f, indent=2, ensure_ascii=False)
        logger.info(f"💾 Saved → {filepath} ({len(self._filtered)} symbols)")

    @staticmethod
    def load(filepath: str = "ftmo_universe.json") -> dict:
        """Load universe from JSON file"""
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def load_symbols(filepath: str = "ftmo_universe.json") -> list[str]:
        """Load just the symbol list"""
        u = FtmoUniverseGenerator.load(filepath)
        return u.get("symbols", [])

    @staticmethod
    def load_symbol_map(filepath: str = "ftmo_universe.json") -> dict:
        """Load symbol mapping (canonical → MT5 name)"""
        u = FtmoUniverseGenerator.load(filepath)
        return u.get("symbol_map", {})

    # ------------------------------------------
    # DISPLAY
    # ------------------------------------------

    def print_universe(self):
        """พิมพ์ universe แบบสวยงาม"""
        if not self._universe:
            print("No universe — run generate() first")
            return

        u = self._universe
        stats = u.get("stats", {})
        print(f"\n{'='*65}")
        print(f"  FTMO Universe — {stats.get('after_filter', 0)} instruments")
        print(f"  Generated: {u.get('generated_at', '?')}")
        print(f"  Total scanned: {stats.get('total_instruments', 0)}")
        print(f"{'='*65}")

        # Per category
        for cat in ["forex", "index", "commodity", "crypto"]:
            cat_syms = [s for s in u.get("symbols", [])
                        if u.get("symbol_data", {}).get(s, {}).get("category") == cat]
            if not cat_syms:
                continue

            print(f"\n  {cat.upper()} ({len(cat_syms)})")
            print(f"  {'Symbol':<10} {'MT5 Name':<16} {'Spread':>7} {'Lot Min':>8} {'Bid':>12} {'Ask':>12}")
            print(f"  {'─'*10} {'─'*16} {'─'*7} {'─'*8} {'─'*12} {'─'*12}")

            for sym in cat_syms:
                d = u["symbol_data"][sym]
                print(f"  {sym:<10} {d['mt5_name']:<16} {d['spread']:>7} "
                      f"{d['volume_min']:>8.2f} {d['bid']:>12.5f} {d['ask']:>12.5f}")

        # Symbol map
        sm = u.get("symbol_map", {})
        remapped = {k: v for k, v in sm.items() if k != v}
        if remapped:
            print(f"\n  SYMBOL MAP (remapped only):")
            for k, v in remapped.items():
                print(f"    {k} → {v}")

        print(f"\n{'='*65}\n")


# ============================================================
# STANDALONE CLI
# ============================================================

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)-18s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="FTMO Universe Generator")
    parser.add_argument("--show", action="store_true",
                        help="Show existing ftmo_universe.json")
    parser.add_argument("--output", "-o", default="ftmo_universe.json",
                        help="Output file (default: ftmo_universe.json)")
    parser.add_argument("--categories", "-c", nargs="+",
                        default=["forex", "index", "commodity", "crypto"],
                        help="Categories to include")
    args = parser.parse_args()

    if args.show:
        gen = FtmoUniverseGenerator()
        try:
            gen._universe = gen.load(args.output)
            gen._filtered = list(gen._universe.get("symbol_data", {}).values())
            gen.print_universe()
        except FileNotFoundError:
            print(f"File not found: {args.output}")
            print("Run without --show to generate first")
    else:
        # Connect to proxy and generate
        from config import Config
        Config.load_profile("FTMO_100K")

        from mt5_proxy_client import Mt5ProxyAdapter
        adapter = Mt5ProxyAdapter(
            proxy_url=Config.MT5_PROXY_URL,
            api_key=Config.MT5_PROXY_API_KEY,
            hmac_secret=Config.MT5_PROXY_HMAC_SECRET,
            health_interval=9999,
        )

        gen = FtmoUniverseGenerator(
            proxy_adapter=adapter,
            categories=args.categories,
        )

        universe = gen.generate()
        if universe:
            gen.save(args.output)
            gen.print_universe()
        else:
            print("Failed to generate universe — check proxy connection")

        adapter.stop()
