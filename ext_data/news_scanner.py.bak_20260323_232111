"""
news_scanner.py
===============
News Scanner สำหรับ 15m Intraday VWAP Trading (Nasdaq focus)

Architecture Pivot (15m):
  - เน้นกวาดข่าวช่วง Pre-market เพื่อหา Daily Story
  - Earnings/Analyst/Product Launch ได้ weight สูงขึ้น (สร้างเทรนด์ยาว)
  - ลดความสำคัญของ noise ข่าว intraday

รองรับ 2 sources:
  1. Benzinga Pro API (paid)
  2. SEC EDGAR RSS Feed (free)

Output:
  NewsCandidate → ส่งต่อให้ pipeline ใน main.py
"""

import os
import re
import time
import logging
import requests
import feedparser
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo  # DST-aware ET timezone (Python 3.9+)
from dataclasses import dataclass, field
from typing import Optional, Callable
from queue import Queue, Empty

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(name)s] %(message)s"
)
logger = logging.getLogger("NewsScanner")


# ============================================================
# DATA MODEL — สิ่งที่ส่งออกจาก Scanner
# ============================================================

@dataclass
class NewsCandidate:
    """
    หุ้นตัวนึงที่ผ่านการกรองข่าวแล้ว พร้อมส่งต่อให้ pipeline
    """
    symbol:         str
    headline:       str
    catalyst_type:  str          # "EARNINGS", "FDA", "MA", "GUIDANCE", "ANALYST", "OTHER"
    urgency_score:  int          # 1–100 (ยิ่งสูง ยิ่งต้องรีบ)
    source:         str          # "BENZINGA" หรือ "SEC_EDGAR"
    url:            str = ""
    timestamp:      datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self):
        return (
            f"[{self.catalyst_type}] {self.symbol} | "
            f"urgency={self.urgency_score} | "
            f"{self.headline[:80]}..."
        )


# ============================================================
# CATALYST RULES — กฎการกรองและให้คะแนน
# ============================================================

# Map keyword → (catalyst_type, urgency_score)
# urgency_score สูง = ต้องตัดสินใจไว ราคาเคลื่อนเร็ว
CATALYST_RULES: list[tuple[list[str], str, int]] = [

    # Tier 1: HIGH IMPACT (70–100) — สร้างเทรนด์ยาวทั้งวัน (15m focus)
    (["fda approval", "fda approved", "fda approves", "fda grants",
      "fda accepts", "fda clears", "breakthrough therapy",
      "nda approval", "bla approval", "pdufa",
      "clinical trial results", "phase 3 results",
      "phase 2 results", "complete response letter"],
     "FDA", 95),                    # ★ 90 → 95

    (["merger", "acquisition", "acquires", "to acquire",
      "buyout", "takeover", "going private",
      "strategic alternatives", "letter of intent",
      "definitive agreement", "tender offer"],
     "MA", 90),                     # ★ 85 → 90

    (["earnings beat", "beats estimates", "beats expectations",
      "quarterly results", "q1 results", "q2 results",
      "q3 results", "q4 results", "fiscal year results",
      "eps of", "revenue of", "net income",
      "quarterly earnings", "reports earnings"],
     "EARNINGS", 90),               # ★ 80 → 90 (เทรนด์ยาวทั้งวัน)

    (["raises guidance", "raises full-year", "raises outlook",
      "raises revenue guidance", "raises eps guidance",
      "increases guidance", "updated guidance",
      "raises forecast", "increases forecast"],
     "GUIDANCE_UP", 85),            # ★ 75 → 85

    # Tier 2: MEDIUM IMPACT (50–69)
    (["lowers guidance", "cuts guidance", "reduces guidance",
      "lowers outlook", "withdraws guidance", "below expectations",
      "misses estimates", "earnings miss", "lowered forecast",
      "reduces forecast"],
     "GUIDANCE_DOWN", 70),          # ★ 65 → 70

    (["upgraded to buy", "upgraded to outperform",
      "upgraded to overweight", "price target raised",
      "price target increased", "initiated with buy",
      "initiated with overweight", "raises price target",
      "upgraded", "upgrade"],
     "ANALYST_UP", 70),             # ★ 50 → 70 (สร้างเทรนด์ใน 15m ได้ดี)

    (["downgraded to sell", "downgraded to underperform",
      "downgraded to underweight", "price target cut",
      "price target lowered", "initiated with sell",
      "lowers price target", "downgraded", "downgrade"],
     "ANALYST_DOWN", 70),           # ★ 55 → 70

    # ★ NEW: Product Launch / Major Announcement (15m trend catalyst)
    (["launches", "new product", "product launch", "unveiled",
      "announces new", "introduces", "first-ever",
      "breakthrough", "revolutionary"],
     "PRODUCT_LAUNCH", 65),         # ★ ใหม่: ข่าวผลิตภัณฑ์สร้างเทรนด์ 15m

    (["share buyback", "stock repurchase", "special dividend",
      "dividend increase", "dividend raise"],
     "SHAREHOLDER", 45),

    (["partnership", "collaboration agreement", "licensing deal",
      "supply agreement", "strategic partnership",
      "joint venture"],
     "DEAL", 40),
]

# คำที่ต้องตัดออกทันที (noise / ไม่เกี่ยวกับ price action)
BLACKLIST_KEYWORDS: list[str] = [
    "conference", "webinar", "presentation", "award",
    "appoints", "appointment", "joins board", "ceo comments",
    "interview", "podcast", "speaking at", "will present at",
]

# หุ้นที่ไม่เล่นตามกฎ TTP (penny stock + ETF + warrant)
BLACKLIST_SUFFIXES: tuple = ("-W", "-R", "-U", "W", "RT")


# ============================================================
# CATALYST CLASSIFIER
# ============================================================

class CatalystClassifier:
    """
    รับ headline → คืน (catalyst_type, urgency_score) หรือ None ถ้าไม่น่าสนใจ
    """

    def classify(self, headline: str) -> Optional[tuple[str, int]]:
        text = headline.lower()

        # ตัด blacklist ออกก่อน
        for bad in BLACKLIST_KEYWORDS:
            if bad in text:
                logger.debug(f"BLACKLISTED: {headline[:60]}")
                return None

        # หา catalyst ที่ match
        for keywords, catalyst_type, urgency in CATALYST_RULES:
            for kw in keywords:
                if kw in text:
                    return catalyst_type, urgency

        return None  # ไม่มี catalyst ที่น่าสนใจ

    def extract_symbol(self, text: str) -> Optional[str]:
        """
        พยายามดึง ticker symbol จาก text (รูปแบบ $AAPL หรือ (AAPL))
        """
        # รูปแบบ $TICKER
        m = re.search(r'\$([A-Z]{1,5})\b', text)
        if m:
            return self._validate_symbol(m.group(1))

        # รูปแบบ (TICKER:NASDAQ) หรือ (TICKER)
        m = re.search(r'\(([A-Z]{1,5})(?::[A-Z]+)?\)', text)
        if m:
            return self._validate_symbol(m.group(1))

        # รูปแบบ "Nasdaq: TICKER"
        m = re.search(r'(?:Nasdaq|NYSE|NASDAQ):\s*([A-Z]{1,5})\b', text)
        if m:
            return self._validate_symbol(m.group(1))

        return None

    def _validate_symbol(self, symbol: str) -> Optional[str]:
        """กรองออก ถ้าเป็น blacklist suffix"""
        for suffix in BLACKLIST_SUFFIXES:
            if symbol.endswith(suffix):
                return None
        # ความยาว ticker ปกติ 1–5 ตัว
        if len(symbol) < 1 or len(symbol) > 5:
            return None
        return symbol


# ============================================================
# SOURCE 1: BENZINGA PRO API
# ============================================================

class BenzingaNewsSource:
    """
    ดึงข่าวจาก Benzinga Pro REST API
    Docs: https://docs.benzinga.com/benzinga/newsfeed-v2.html
    
    ต้องสมัครแพ็กเกจ Pro ก่อน: https://pro.benzinga.com
    ใส่ API Key ใน env: BENZINGA_API_KEY=...
    """

    BASE_URL = "https://api.benzinga.com/api/v2/news"

    def __init__(self, api_key: str, poll_interval_sec: int = 30):
        self.api_key       = api_key
        self.poll_interval = poll_interval_sec
        self.classifier    = CatalystClassifier()
        self._seen_ids: set[str] = set()  # dedup
        self.name          = "BENZINGA"

    def fetch_latest(self, page_size: int = 20) -> list[NewsCandidate]:
        """
        ดึงข่าวล่าสุดจาก Benzinga API
        """
        params = {
            "token":       self.api_key,
            "pageSize":    page_size,
            "displayOutput": "full",
            "dateFrom":    (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            resp = requests.get(self.BASE_URL, params=params, timeout=10)
            resp.raise_for_status()
            articles = resp.json()
        except requests.RequestException as e:
            logger.error(f"[Benzinga] API error: {e}")
            return []

        candidates = []
        for article in articles:
            article_id = str(article.get("id", ""))
            if article_id in self._seen_ids:
                continue
            self._seen_ids.add(article_id)

            headline = article.get("title", "")
            stocks   = article.get("stocks", [])

            # Benzinga ให้ ticker มาเลย (ดีมาก)
            symbols = [s["name"] for s in stocks if s.get("name")]

            # ถ้าไม่มี ticker ใน metadata ลอง extract จาก headline
            if not symbols:
                sym = self.classifier.extract_symbol(headline)
                if sym:
                    symbols = [sym]

            result = self.classifier.classify(headline)
            if result is None or not symbols:
                continue

            catalyst_type, urgency = result
            pub_date = article.get("created", "")

            for symbol in symbols[:3]:  # ไม่เกิน 3 ตัวต่อข่าว
                candidates.append(NewsCandidate(
                    symbol        = symbol.upper(),
                    headline      = headline,
                    catalyst_type = catalyst_type,
                    urgency_score = urgency,
                    source        = self.name,
                    url           = article.get("url", ""),
                    timestamp     = self._parse_timestamp(pub_date),
                ))

        return candidates

    def _parse_timestamp(self, date_str: str) -> datetime:
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)


# ============================================================
# SOURCE 2: SEC EDGAR RSS (FREE)
# ============================================================

class SecEdgarRssSource:
    """
    ดึง official filings จาก SEC EDGAR RSS Feed (ฟรี ไม่ต้อง API key)
    
    Forms ที่สำคัญสำหรับ News Trading:
      - 8-K  : Material events (earnings, M&A, FDA, guidance)
      - SC TO-T : Tender Offers (M&A)
      - DEF 14A : Proxy (บางครั้งมี M&A embedded)
    
    Delay: ~5–15 นาทีหลัง filing จริง (ช้ากว่า Benzinga)
    เหมาะ: Paper trade ทดสอบ logic / cross-check กับ Benzinga
    """

    FEEDS: dict[str, str] = {
        "8-K":     "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=20&search_text=&output=atom",
        "SC_TO_T": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+TO-T&dateb=&owner=include&count=10&search_text=&output=atom",
    }
    HEADERS = {"User-Agent": "NewsScanner research@example.com"}  # SEC ต้องการ User-Agent

    def __init__(self, poll_interval_sec: int = 60):
        self.poll_interval = poll_interval_sec
        self.classifier    = CatalystClassifier()
        self._seen_ids: set[str] = set()
        self.name          = "SEC_EDGAR"

    def fetch_latest(self) -> list[NewsCandidate]:
        candidates = []

        for form_type, url in self.FEEDS.items():
            try:
                resp = requests.get(url, headers=self.HEADERS, timeout=15)
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
            except Exception as e:
                logger.error(f"[SEC EDGAR] Feed error ({form_type}): {e}")
                continue

            for entry in feed.entries:
                entry_id = entry.get("id", "")
                if entry_id in self._seen_ids:
                    continue
                self._seen_ids.add(entry_id)

                title = entry.get("title", "")
                # format ปกติ: "COMPANY NAME (TICKER) (8-K)"
                symbol  = self.classifier.extract_symbol(title)
                summary = entry.get("summary", "")
                full_text = f"{title} {summary}"

                # สำหรับ SC TO-T ให้ urgency สูงสุดเสมอ (M&A tender offer)
                if form_type == "SC_TO_T":
                    catalyst_type, urgency = "MA", 90
                else:
                    result = self.classifier.classify(full_text)
                    if result is None:
                        # 8-K แต่ไม่มี keyword ที่รู้จัก — ใช้ urgency ต่ำ
                        catalyst_type, urgency = "OTHER", 30
                    else:
                        catalyst_type, urgency = result

                if not symbol:
                    continue

                pub_date = entry.get("published_parsed")
                ts = (datetime(*pub_date[:6], tzinfo=timezone.utc)
                      if pub_date else datetime.now(timezone.utc))

                candidates.append(NewsCandidate(
                    symbol        = symbol,
                    headline      = title,
                    catalyst_type = catalyst_type,
                    urgency_score = urgency,
                    source        = self.name,
                    url           = entry.get("link", ""),
                    timestamp     = ts,
                ))

        return candidates


# ============================================================
# NEWS SCANNER — orchestrates หลาย sources พร้อมกัน
# ============================================================

class NewsScanner:
    """
    Main Scanner — รวม sources ทั้งหมด, filter ตาม urgency,
    และส่ง NewsCandidate เข้า Queue เพื่อให้ pipeline หยิบไปใช้
    
    วิธีใช้:
        def on_news(candidate: NewsCandidate):
            # ส่งต่อให้ pipeline
            engine.run_news_trade(candidate.symbol, ...)

        scanner = NewsScanner(
            benzinga_api_key="YOUR_KEY",   # ถ้าไม่มีใส่ None → ใช้แค่ SEC
            use_sec_edgar=True,
            min_urgency=50,
            callback=on_news
        )
        scanner.start()
    """

    def __init__(
        self,
        benzinga_api_key: Optional[str] = None,
        use_sec_edgar:    bool = True,
        min_urgency:      int  = 50,      # กรองเฉพาะ urgency ≥ 50
        callback:         Optional[Callable[[NewsCandidate], None]] = None,
    ):
        self.min_urgency = min_urgency
        self.callback    = callback
        self.queue:  Queue[NewsCandidate] = Queue()
        self._stop   = threading.Event()
        self._threads: list[threading.Thread] = []

        # ── News result cache (Architecture Pivot: ไม่ต้องยิง API ซ้ำทุก 15m)
        # cache symbol → (NewsCandidate, timestamp)
        self._news_cache: dict[str, tuple] = {}
        self._news_cache_ttl_sec = 900      # 15 นาที

        # Setup sources
        self.sources: list = []

        if benzinga_api_key:
            self.sources.append(BenzingaNewsSource(api_key=benzinga_api_key))
            logger.info("✅ Benzinga Pro source enabled")
        else:
            logger.warning("⚠️  Benzinga API key ไม่ได้ตั้ง → ข้าม Benzinga source")

        if use_sec_edgar:
            self.sources.append(SecEdgarRssSource())
            logger.info("✅ SEC EDGAR RSS source enabled")

        if not self.sources:
            raise ValueError("ต้องมี source อย่างน้อย 1 ตัว")
            

    # ------------------------------------------
    # PUBLIC: start / stop
    # ------------------------------------------

    def start(self):
        """เริ่ม polling ทุก source ใน background threads"""
        logger.info(f"🚀 NewsScanner เริ่มทำงาน | sources={len(self.sources)} | min_urgency={self.min_urgency}")
        self._stop.clear()

        for source in self.sources:
            t = threading.Thread(
                target=self._poll_loop,
                args=(source,),
                name=f"poll-{source.name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        # ถ้ามี callback ให้รัน dispatcher ด้วย
        if self.callback:
            dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="dispatcher",
                daemon=True,
            )
            dispatcher.start()
            self._threads.append(dispatcher)

    def stop(self):
        """หยุดทุก threads"""
        logger.info("🛑 NewsScanner หยุดทำงาน")
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)

    def get_candidates(self, block: bool = True, timeout: float = 5.0) -> Optional[NewsCandidate]:
        """
        ดึง candidate จาก queue (ใช้ถ้าไม่ได้ตั้ง callback)
        """
        try:
            return self.queue.get(block=block, timeout=timeout)
        except Empty:
            return None

    # ------------------------------------------
    # INTERNAL: polling + filtering
    # ------------------------------------------

    def _poll_loop(self, source):
        """วน poll source ตาม interval จนกว่าจะ stop"""
        poll_interval = getattr(source, "poll_interval", 60)

        while not self._stop.is_set():
            try:
                candidates = source.fetch_latest()
                filtered   = self._filter(candidates)

                for c in filtered:
                    logger.info(f"📰 [{c.source}] {c}")
                    self.queue.put(c)

            except Exception as e:
                logger.error(f"[{source.name}] poll error: {e}", exc_info=True)

            self._stop.wait(poll_interval)

    def _filter(self, candidates: list[NewsCandidate]) -> list[NewsCandidate]:
        """
        กรองตาม urgency, dedup ข้าม sources, และ cache dedup (15m TTL)
        """
        now = time.time()
        result = []
        for c in candidates:
            if c.urgency_score < self.min_urgency:
                continue
            if c.catalyst_type == "OTHER" and c.urgency_score < 60:
                continue

            # ── Cache dedup: ไม่ส่งข่าวซ้ำภายใน TTL
            cache_key = f"{c.symbol}:{c.catalyst_type}"
            if cache_key in self._news_cache:
                _, cached_ts = self._news_cache[cache_key]
                if now - cached_ts < self._news_cache_ttl_sec:
                    logger.debug(f"[Cache] {cache_key} already sent within {self._news_cache_ttl_sec}s → skip")
                    continue

            # ── เพิ่มเข้า cache
            self._news_cache[cache_key] = (c, now)
            result.append(c)

        # ── ลบ cache entries เก่า (housekeeping)
        stale_keys = [k for k, (_, ts) in self._news_cache.items()
                      if now - ts > self._news_cache_ttl_sec * 4]
        for k in stale_keys:
            del self._news_cache[k]

        result.sort(key=lambda x: x.urgency_score, reverse=True)
        return result

    def get_cached_news(self, symbol: str = None) -> list[NewsCandidate]:
        """
        ดึงข่าวจาก cache (ไม่ต้องยิง API ใหม่)
        ใช้ใน Gate 19 LLM CIO เพื่อส่ง news context
        """
        now = time.time()
        results = []
        for key, (candidate, ts) in self._news_cache.items():
            if now - ts > self._news_cache_ttl_sec:
                continue
            if symbol and candidate.symbol != symbol:
                continue
            results.append(candidate)
        return results

    def _dispatch_loop(self):
        """ส่ง candidate จาก queue ไปหา callback"""
        while not self._stop.is_set():
            c = self.get_candidates(block=True, timeout=1.0)
            if c and self.callback:
                try:
                    self.callback(c)
                except Exception as e:
                    logger.error(f"[Dispatcher] callback error: {e}", exc_info=True)


# ============================================================
# PRE-MARKET HELPER — ช่วยกรองช่วงเวลาที่เหมาะ
# ============================================================

class MarketSessionFilter:
    """
    ตรวจสอบว่าตอนนี้อยู่ใน session ไหน (ET timezone)
    ใช้ป้องกันการยิง order ในเวลาที่ liquidity ต่ำเกินไป
    """

    # DST-aware ET timezone — อัปเดตอัตโนมัติ มีนาคม (EDT=UTC-4) / พฤศจิกายน (EST=UTC-5)
    _TZ_ET = ZoneInfo("America/New_York")

    @staticmethod
    def now_et() -> datetime:
        """คืนเวลาปัจจุบัน ET (DST-aware) — ใช้แทน UTC - timedelta(hours=4)"""
        return datetime.now(MarketSessionFilter._TZ_ET)

    @classmethod
    def current_session(cls) -> str:
        """
        Returns: "PRE_MARKET" | "MARKET" | "AFTER_HOURS" | "CLOSED"

        เวลา ET — ใช้ zoneinfo ปรับ DST อัตโนมัติ:
          EDT (Mar–Nov): UTC-4   |   EST (Nov–Mar): UTC-5
        Bug เดิม: hardcode UTC-4 ทำให้ผิด 1 ชั่วโมงช่วง EST (Nov–Mar)
        """
        now_et  = cls.now_et()
        h, m    = now_et.hour, now_et.minute
        minutes = h * 60 + m

        if 240 <= minutes < 570:    # 4:00–9:30 AM ET
            return "PRE_MARKET"
        elif 570 <= minutes < 960:  # 9:30 AM–4:00 PM ET
            return "MARKET"
        elif 960 <= minutes < 1200: # 4:00–8:00 PM ET
            return "AFTER_HOURS"
        else:
            return "CLOSED"

    @staticmethod
    def is_tradeable(session: str, catalyst_type: str) -> bool:
        """
        กฎ: บาง catalyst เล่นได้แค่บาง session
        """
        if session == "CLOSED":
            return False

        # Earnings / FDA / M&A / Product Launch เล่นได้ทุก session (15m focus)
        high_impact = {"EARNINGS", "FDA", "MA", "GUIDANCE_UP", "GUIDANCE_DOWN",
                       "ANALYST_UP", "ANALYST_DOWN", "PRODUCT_LAUNCH"}
        if catalyst_type in high_impact:
            return session in ("PRE_MARKET", "MARKET", "AFTER_HOURS")

        # Analyst / deal เล่นแค่ Market hours
        return session == "MARKET"


# ============================================================
# INTEGRATION WRAPPER — เชื่อมกับ pipeline เดิม
# ============================================================

class NewsScannerIntegration:
    """
    Wrapper สำหรับเชื่อมกับ NewsTradingEngine (จาก RegimeWeightedScorer)
    
    วิธีใช้งาน:
        from news_scanner import NewsScannerIntegration
        from regime_scorer import NewsTradingEngine  # โค้ดเดิม

        integration = NewsScannerIntegration(
            engine=NewsTradingEngine(),
            benzinga_api_key=os.getenv("BENZINGA_API_KEY"),
            entry_offset_pct=0.002,   # entry ที่ ask + 0.2%
            stop_offset_pct=0.015,    # stop ที่ 1.5% จาก entry
        )
        integration.run()
    """

    def __init__(
        self,
        engine,                          # NewsTradingEngine instance
        benzinga_api_key: Optional[str] = None,
        entry_offset_pct: float = 0.002, # entry ห่างจากราคาปัจจุบัน
        stop_offset_pct:  float = 0.015, # stop ห่างจาก entry
        min_urgency:      int   = 60,
    ):
        self.engine           = engine
        self.entry_offset_pct = entry_offset_pct
        self.stop_offset_pct  = stop_offset_pct
        self.session_filter   = MarketSessionFilter()

        self.scanner = NewsScanner(
            benzinga_api_key = benzinga_api_key,
            use_sec_edgar    = True,
            min_urgency      = min_urgency,
            callback         = self._on_news,
        )

    def _on_news(self, candidate: NewsCandidate):
        """
        รับ NewsCandidate → ตรวจ session → คำนวณ entry/stop → ส่ง pipeline
        """
        session = self.session_filter.current_session()

        if not self.session_filter.is_tradeable(session, candidate.catalyst_type):
            logger.info(f"⏸  {candidate.symbol} skip — session={session}")
            return

        logger.info(f"⚡ Processing {candidate.symbol} [{candidate.catalyst_type}] urgency={candidate.urgency_score}")

        # ดึงราคาปัจจุบัน (ผ่าน yfinance เป็น fallback สำหรับ paper trade)
        current_price = self._get_current_price(candidate.symbol)
        if not current_price:
            logger.warning(f"ไม่สามารถดึงราคา {candidate.symbol}")
            return

        # กำหนด side ตาม catalyst
        side = self._determine_side(candidate.catalyst_type)

        # คำนวณ entry / stop แบบ simple offset (สำหรับ paper trade)
        # ใน production ควรใช้ Level 2 / VWAP แทน
        if side == "buy":
            entry_price = round(current_price * (1 + self.entry_offset_pct), 2)
            stop_price  = round(entry_price   * (1 - self.stop_offset_pct),  2)
        else:
            entry_price = round(current_price * (1 - self.entry_offset_pct), 2)
            stop_price  = round(entry_price   * (1 + self.stop_offset_pct),  2)

        logger.info(f"→ {side.upper()} {candidate.symbol} entry={entry_price} stop={stop_price}")

        # ส่งเข้า engine (RegimeWeightedScorer + TTPRiskManager + TTPOrderExecutor)
        self.engine.run_news_trade(
            symbol      = candidate.symbol,
            side        = side,
            entry_price = entry_price,
            stop_price  = stop_price,
        )

    def _determine_side(self, catalyst_type: str) -> str:
        """Catalyst บางตัวมักเป็น short play"""
        bearish = {"GUIDANCE_DOWN", "ANALYST_DOWN"}
        return "sell" if catalyst_type in bearish else "buy"

    def _get_current_price(self, symbol: str) -> Optional[float]:
        """ดึงราคาล่าสุดผ่าน yfinance (ใช้ใน paper trade)"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            info   = ticker.fast_info
            price  = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
            return float(price) if price else None
        except Exception as e:
            logger.error(f"yfinance error ({symbol}): {e}")
            return None

    def run(self, block: bool = True):
        """เริ่ม scanner"""
        self.scanner.start()
        logger.info("🟢 NewsScannerIntegration running — Ctrl+C to stop")
        if block:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                self.scanner.stop()
                logger.info("Scanner stopped")

def load_watchlist_from_json(filepath: str) -> list[str]:
    """
    อ่านไฟล์ watchlist.json และดึงเฉพาะ list ของ symbols ออกมา
    พร้อมแปลงเป็นตัวพิมพ์ใหญ่ทั้งหมดเพื่อป้องกันความผิดพลาด
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        symbols = data.get("symbols", [])
        if not symbols:
            logging.warning(f"ไม่พบข้อมูล 'symbols' ในไฟล์ {filepath}")
            return []
            
        # คืนค่า list ของหุ้นที่แปลงเป็น Upper Case แล้ว
        return [str(sym).upper() for sym in symbols]
        
    except FileNotFoundError:
        logging.error(f"ไม่พบไฟล์: {filepath}")
        return []
    except json.JSONDecodeError:
        logging.error(f"รูปแบบไฟล์ JSON ไม่ถูกต้อง: {filepath}")
        return []
    except Exception as e:
        logging.error(f"เกิดข้อผิดพลาดในการอ่านไฟล์ {filepath}: {e}")
        return []
# ============================================================
# MAIN — ทดสอบแบบ standalone
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("  NEWS SCANNER — Standalone Test")
    print("=" * 60)

    # ── Test 1: Catalyst Classifier
    print("\n[TEST 1] CatalystClassifier")
    clf = CatalystClassifier()
    headlines = [
        "$NVDA beats estimates with Q2 EPS of $0.68, raises guidance",
        "FDA approves $MRNA new mRNA vaccine",
        "Company XYZ to acquire $ACME for $45 per share",
        "CEO of $AAPL to present at conference next week",     # should be None
        "AMZN (NASDAQ:AMZN) lowers guidance for Q3",
        "Goldman Sachs upgraded $META to Buy with $600 target",
    ]
    for h in headlines:
        sym    = clf.extract_symbol(h)
        result = clf.classify(h)
        print(f"  symbol={sym:6s}  result={str(result):40s}  | {h[:60]}")

    # ── Test 2: Market Session
    print("\n[TEST 2] MarketSessionFilter")
    sf      = MarketSessionFilter()
    session = sf.current_session()
    print(f"  Current session (ET): {session}")
    for cat in ["EARNINGS", "FDA", "ANALYST_UP", "DEAL"]:
        ok = sf.is_tradeable(session, cat)
        print(f"  tradeable [{cat:15s}] in {session}: {ok}")

    # ── Test 3: SEC EDGAR RSS (live)
    print("\n[TEST 3] SEC EDGAR RSS (live fetch)")
    sec = SecEdgarRssSource()
    candidates = sec.fetch_latest()
    print(f"  พบ {len(candidates)} candidates จาก SEC EDGAR")
    for c in candidates[:5]:
        print(f"  {c}")

    # ── Test 4: Benzinga (ต้องมี API key)
    BENZINGA_KEY = os.getenv("BENZINGA_API_KEY")
    if BENZINGA_KEY:
        print("\n[TEST 4] Benzinga Pro (live fetch)")
        bz         = BenzingaNewsSource(api_key=BENZINGA_KEY)
        candidates = bz.fetch_latest()
        print(f"  พบ {len(candidates)} candidates จาก Benzinga")
        for c in candidates[:5]:
            print(f"  {c}")
    else:
        print("\n[TEST 4] Benzinga — ข้าม (ไม่มี BENZINGA_API_KEY)")
        print("  → ตั้งค่า: export BENZINGA_API_KEY=your_key_here")

    # ── Test 5: Scanner แบบ queue (30 วินาที)
    print("\n[TEST 5] NewsScanner queue mode (30 วินาที)")
    scanner = NewsScanner(
        benzinga_api_key = BENZINGA_KEY,
        use_sec_edgar    = True,
        min_urgency      = 40,           # ลดลงเพื่อเห็นผลตอนทดสอบ
    )
    scanner.start()

    deadline = time.time() + 30
    count    = 0
    while time.time() < deadline:
        c = scanner.get_candidates(timeout=2.0)
        if c:
            print(f"  → QUEUE: {c}")
            count += 1

    scanner.stop()
    print(f"\n  รับข่าวได้ {count} รายการใน 30 วินาที")
    print("\n✅ ทดสอบเสร็จสิ้น")
