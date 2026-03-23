"""
patch_sec_edgar_historical.py
=============================
Patch สำหรับ ext_data/news_scanner.py

เพิ่ม SecEdgarFullTextSearch class ที่ใช้ EDGAR EFTS API
ดึง filings ย้อนหลัง 1-2 วัน สำหรับ dry-run / shadow mode

EDGAR EFTS API (Free, no API key):
  https://efts.sec.gov/LATEST/search-index?q=...&dateRange=custom&startdt=...&enddt=...

Usage:
  # ดึง 8-K filings ย้อนหลัง 2 วัน ที่ match กับ watchlist
  from ext_data.news_scanner import SecEdgarFullTextSearch
  efts = SecEdgarFullTextSearch()
  candidates = efts.fetch_historical(
      lookback_days=2,
      watchlist=["NVDA", "TSLA", "META", "AAPL"]
  )

Integration:
  เพิ่มลงใน news_scanner.py หลัง class SecEdgarRssSource
  และเพิ่มให้ _fetch_real_candidates() ใน shadow_runner.py เรียกใช้

Apply:
  1. Copy class SecEdgarFullTextSearch ไปใส่ใน news_scanner.py
  2. Copy function _fetch_historical_candidates ไปใส่ใน shadow_runner.py
  3. แก้ _fetch_real_candidates() ให้เรียก _fetch_historical_candidates() ด้วย
"""

import re
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("SecEdgarEFTS")


# ============================================================
# เพิ่มใน ext_data/news_scanner.py — หลัง class SecEdgarRssSource
# ============================================================

class SecEdgarFullTextSearch:
    """
    SEC EDGAR Full-Text Search (EFTS) — ดึง filings ย้อนหลัง

    ใช้ EFTS API ฟรี (ไม่ต้อง API key):
      https://efts.sec.gov/LATEST/search-index?q=...

    ข้อดีเทียบกับ RSS:
      - RSS: ดึงได้แค่ล่าสุด (~20 รายการ)
      - EFTS: query ย้อนหลังได้ + filter ตาม form type + date range

    Rate Limit: SEC ขอไม่เกิน 10 req/sec → เราใช้ delay 0.15s ต่อ request

    Usage (dry-run / shadow mode):
      efts = SecEdgarFullTextSearch()
      candidates = efts.fetch_historical(
          lookback_days=2,
          watchlist=["NVDA", "TSLA", "META"]
      )
      for c in candidates:
          pipeline.process_news(c)  # shadow mode — ไม่ order จริง
    """

    # EFTS search endpoint
    EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

    # Alternative: EDGAR Full-Text Search System
    EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

    # SEC requires User-Agent with contact info
    HEADERS = {
        "User-Agent": "TTPTradingSystem research@ttp-trading.com",
        "Accept": "application/json",
    }

    # Form types สำคัญสำหรับ trading
    FORM_TYPES = ["8-K", "SC TO-T", "SC 13D", "SC 13G"]

    def __init__(self):
        # Use CatalystClassifier from the same module
        try:
            from ext_data.news_scanner import CatalystClassifier, NewsCandidate
            self.classifier = CatalystClassifier()
            self._NewsCandidate = NewsCandidate
        except ImportError:
            self.classifier = None
            self._NewsCandidate = None
        self._request_count = 0

    def fetch_historical(
        self,
        lookback_days: int = 2,
        watchlist: list = None,
        form_types: list = None,
        max_results: int = 100,
    ) -> list:
        """
        ดึง filings ย้อนหลังจาก SEC EDGAR EFTS

        Args:
            lookback_days: จำนวนวันย้อนหลัง (1-7)
            watchlist: list ของ symbols ที่สนใจ (None = ดึงทั้งหมด)
            form_types: list ของ form types (default: 8-K, SC TO-T)
            max_results: จำนวน results สูงสุด

        Returns:
            list[NewsCandidate] — filtered ตาม watchlist
        """
        if form_types is None:
            form_types = self.FORM_TYPES

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=lookback_days)

        all_candidates = []

        for form_type in form_types:
            try:
                filings = self._search_efts(
                    form_type=form_type,
                    start_date=start_date,
                    end_date=end_date,
                    max_results=max_results,
                )
                candidates = self._parse_filings(filings, form_type)
                all_candidates.extend(candidates)

                # Rate limit: SEC asks for max 10 req/sec
                time.sleep(0.15)

            except Exception as e:
                logger.error(f"[EFTS] Error fetching {form_type}: {e}")
                continue

        # Filter by watchlist
        if watchlist:
            watchlist_set = set(s.upper() for s in watchlist)
            filtered = [c for c in all_candidates if c.symbol in watchlist_set]
            logger.info(
                f"[EFTS] Fetched {len(all_candidates)} total, "
                f"{len(filtered)} match watchlist ({len(watchlist_set)} symbols)"
            )
            return filtered

        logger.info(f"[EFTS] Fetched {len(all_candidates)} candidates (no watchlist filter)")
        return all_candidates

    def _search_efts(
        self,
        form_type: str,
        start_date: datetime,
        end_date: datetime,
        max_results: int = 100,
    ) -> list:
        """
        Query EDGAR EFTS (Full-Text Search) API

        EFTS search-index endpoint (Elasticsearch):
          GET https://efts.sec.gov/LATEST/search-index
          ?q="8-K"
          &dateRange=custom
          &startdt=2026-03-21
          &enddt=2026-03-23

        Response fields per hit._source:
          display_names: ["COMPANY NAME  (TICKER)  (CIK 000...)"]
          ciks: ["0001234567"]
          root_forms: ["8-K"]
          file_description: "8-K" / "8-K/A" / "CURRENT REPORT"
          file_date: "2026-03-23"

        Returns: list of filing dicts (raw hits)
        """
        params = {
            "q": f'"{form_type}"',
            "dateRange": "custom",
            "startdt": start_date.strftime("%Y-%m-%d"),
            "enddt": end_date.strftime("%Y-%m-%d"),
            "from": 0,
            "size": min(max_results, 50),
        }

        all_filings = []
        page = 0

        while len(all_filings) < max_results:
            params["from"] = page * 50

            try:
                resp = requests.get(
                    self.EFTS_SEARCH_URL,
                    params=params,
                    headers=self.HEADERS,
                    timeout=15,
                )
                self._request_count += 1

                if resp.status_code == 429:
                    # Rate limited — back off
                    logger.warning("[EFTS] Rate limited — waiting 2s")
                    time.sleep(2)
                    continue

                resp.raise_for_status()
                data = resp.json()

            except requests.RequestException as e:
                logger.error(f"[EFTS] Request failed: {e}")
                break

            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break

            all_filings.extend(hits)
            page += 1

            # Stop if we've gotten all results
            total = data.get("hits", {}).get("total", {}).get("value", 0)
            if len(all_filings) >= total or len(all_filings) >= max_results:
                break

            time.sleep(0.12)  # Rate limit compliance

        return all_filings[:max_results]

    def _parse_filings(self, filings: list, form_type: str) -> list:
        """
        Parse EFTS search results → NewsCandidate list

        EFTS _source fields:
          display_names: ["COMPANY NAME  (TICKER)  (CIK 000...)"]
          ciks:          ["0001234567"]
          root_forms:    ["8-K"]
          file_description: "8-K" / "8-K/A" / "CURRENT REPORT"
          file_date:     "2026-03-23"
        """
        if self._NewsCandidate is None:
            try:
                from ext_data.news_scanner import NewsCandidate
                self._NewsCandidate = NewsCandidate
            except ImportError:
                logger.error("[EFTS] Cannot import NewsCandidate")
                return []

        candidates = []

        for hit in filings:
            source = hit.get("_source", {})

            # ── Extract company name and ticker from display_names
            # Format: "COMPANY NAME  (TICKER)  (CIK 000...)"
            display_names = source.get("display_names", [])
            company_name = display_names[0] if display_names else ""
            file_description = source.get("file_description", "")
            display_date = source.get("file_date", "")
            root_forms = source.get("root_forms", [])
            actual_form = root_forms[0] if root_forms else form_type

            # Filter: only process if root_form matches what we want
            form_match = any(
                f.upper().startswith(form_type.replace(" ", "").upper()[:3])
                for f in root_forms
            ) if root_forms else True

            if not form_match:
                continue

            # ── Extract ticker from display_names using regex
            # Pattern: "(TICKER)" where TICKER is 1-5 uppercase letters
            tickers = []
            if company_name:
                ticker_matches = re.findall(
                    r'\(([A-Z]{1,5})\)', company_name
                )
                # Filter out CIK patterns — CIK is always digits
                tickers = [t for t in ticker_matches if not t.startswith("CIK")]

            # Fallback: try classifier on the headline
            if not tickers and self.classifier:
                extracted = self.classifier.extract_symbol(company_name)
                if extracted:
                    tickers = [extracted]

            if not tickers:
                continue

            # ── Build headline
            headline = company_name.split("(CIK")[0].strip()  # Remove CIK part
            if file_description and file_description not in headline:
                headline += f" — {file_description}"

            # ── Classify catalyst
            if form_type in ("SC TO-T", "SC 13D"):
                catalyst_type, urgency = "MA", 85
            elif self.classifier:
                result = self.classifier.classify(headline)
                if result:
                    catalyst_type, urgency = result
                else:
                    catalyst_type, urgency = "OTHER", 35
            else:
                catalyst_type, urgency = "OTHER", 35

            # ── Parse timestamp
            try:
                ts = datetime.strptime(display_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                ts = datetime.now(timezone.utc)

            # ── Build filing URL
            ciks = source.get("ciks", [])
            cik = ciks[0] if ciks else ""

            for ticker in tickers[:2]:
                candidates.append(self._NewsCandidate(
                    symbol=ticker.upper(),
                    headline=headline[:200],
                    catalyst_type=catalyst_type,
                    urgency_score=urgency,
                    source="SEC_EDGAR_EFTS",
                    url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}&dateb=&owner=include&count=10"
                        if cik else "",
                    timestamp=ts,
                ))

        return candidates

    def get_stats(self) -> dict:
        return {
            "total_requests": self._request_count,
        }


# ============================================================
# เพิ่มใน shadow_runner.py — function ใหม่
# ============================================================

def _fetch_historical_candidates(pipeline, symbols: list, lookback_days: int = 2) -> list:
    """
    ดึง candidates ย้อนหลังจาก SEC EDGAR EFTS สำหรับ shadow/dry-run mode

    ใช้ EFTS Full-Text Search API (ฟรี):
      - ดึง 8-K, SC TO-T filings ย้อนหลัง 1-2 วัน
      - Filter เฉพาะ symbols ใน watchlist
      - ส่งกลับเป็น NewsCandidate list

    เรียกใช้ใน: _fetch_real_candidates() (shadow_runner.py)
    """
    candidates = []
    try:
        efts = SecEdgarFullTextSearch()
        historical = efts.fetch_historical(
            lookback_days=lookback_days,
            watchlist=symbols,
            max_results=50,
        )
        candidates.extend(historical)
        logger.info(
            f"[Shadow] EFTS historical: {len(historical)} candidates "
            f"(lookback={lookback_days}d, watchlist={len(symbols)} symbols)"
        )
    except Exception as e:
        logger.debug(f"[Shadow] EFTS historical fetch failed: {e}")

    return candidates


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    )

    print("=" * 60)
    print("  SEC EDGAR EFTS — Historical Search Test")
    print("=" * 60)

    efts = SecEdgarFullTextSearch()

    # Test 1: ดึง 8-K ย้อนหลัง 2 วัน (ไม่ filter watchlist)
    print("\n[Test 1] Fetch 8-K filings (2 days, no filter)")
    candidates = efts.fetch_historical(lookback_days=2, max_results=10)
    print(f"  Found: {len(candidates)} candidates")
    for c in candidates[:5]:
        print(f"  {c}")

    # Test 2: ดึงแบบ filter watchlist
    print("\n[Test 2] Fetch with watchlist filter")
    watchlist = ["NVDA", "TSLA", "META", "AAPL", "AMZN", "MSFT", "GOOG"]
    filtered = efts.fetch_historical(lookback_days=2, watchlist=watchlist)
    print(f"  Found: {len(filtered)} candidates matching {len(watchlist)} symbols")
    for c in filtered[:5]:
        print(f"  {c}")

    # Test 3: SC TO-T (M&A tender offers)
    print("\n[Test 3] Fetch SC TO-T (M&A) filings")
    ma_candidates = efts.fetch_historical(
        lookback_days=7,
        form_types=["SC TO-T"],
        max_results=10,
    )
    print(f"  Found: {len(ma_candidates)} M&A candidates")
    for c in ma_candidates[:5]:
        print(f"  {c}")

    print(f"\n  Stats: {efts.get_stats()}")
    print("\n✅ Test complete")
