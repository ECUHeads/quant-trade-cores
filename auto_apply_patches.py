#!/usr/bin/env python3
"""
auto_apply_patches.py
=====================
Auto-apply ทั้ง 3 patches เข้า source code

Usage:
  cd /path/to/ttp-trading
  python auto_apply_patches.py

  # หรือ dry-run (แสดง diff ไม่แก้ไฟล์)
  python auto_apply_patches.py --dry-run

  # apply เฉพาะบาง patch
  python auto_apply_patches.py --only sec_edgar
  python auto_apply_patches.py --only volume_profile
  python auto_apply_patches.py --only rsi_divergence

สิ่งที่แก้:
  1. ext_data/news_scanner.py      — เพิ่ม SecEdgarFullTextSearch class
  2. mode/shadow_runner.py         — แก้ _fetch_real_candidates() ให้ดึง historical
  3. models/technical_ml_analyzer.py — เพิ่ม Volume Profile features
  4. mode/technical_scanner.py     — เพิ่ม RSI Divergence rule
"""

import os
import sys
import re
import shutil
import argparse
from datetime import datetime
from pathlib import Path


def backup_file(filepath: str) -> str:
    """สร้าง backup ก่อนแก้ไข"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{filepath}.bak_{ts}"
    shutil.copy2(filepath, backup)
    return backup


def patch_file(filepath: str, old_text: str, new_text: str, 
               description: str, dry_run: bool = False) -> bool:
    """Replace old_text with new_text in file"""
    if not os.path.exists(filepath):
        print(f"  ❌ File not found: {filepath}")
        return False

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if old_text not in content:
        # Try fuzzy match (strip whitespace differences)
        old_stripped = " ".join(old_text.split())
        content_stripped = " ".join(content.split())
        if old_stripped not in content_stripped:
            print(f"  ⚠️  Marker not found — may already be patched: {description}")
            return False

    if dry_run:
        print(f"  🔍 [DRY-RUN] Would patch: {description}")
        return True

    new_content = content.replace(old_text, new_text, 1)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"  ✅ {description}")
    return True


def append_to_file(filepath: str, text: str, 
                   description: str, marker: str = None,
                   dry_run: bool = False) -> bool:
    """Append text to file (before marker or at end)"""
    if not os.path.exists(filepath):
        print(f"  ❌ File not found: {filepath}")
        return False

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Check if already patched
    check_line = text.strip().split("\n")[1] if "\n" in text else text[:50]
    if check_line.strip() in content:
        print(f"  ⚠️  Already patched: {description}")
        return True

    if dry_run:
        print(f"  🔍 [DRY-RUN] Would append: {description}")
        return True

    if marker and marker in content:
        new_content = content.replace(marker, text + "\n" + marker, 1)
    else:
        new_content = content + "\n" + text

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"  ✅ {description}")
    return True


# ============================================================
# PATCH 1: SEC EDGAR EFTS Historical
# ============================================================

SEC_EDGAR_CLASS = '''

# ============================================================
# SOURCE 3: SEC EDGAR Full-Text Search (EFTS) — Historical
# ============================================================

class SecEdgarFullTextSearch:
    """
    SEC EDGAR Full-Text Search (EFTS) — ดึง filings ย้อนหลัง 1-7 วัน

    ใช้ EFTS API ฟรี (ไม่ต้อง API key):
      https://efts.sec.gov/LATEST/search-index

    ข้อดีเทียบกับ RSS:
      - RSS: ดึงได้แค่ล่าสุด (~20 รายการ)
      - EFTS: query ย้อนหลังได้ + filter ตาม form type + date range

    Usage (dry-run / shadow mode):
      efts = SecEdgarFullTextSearch()
      candidates = efts.fetch_historical(lookback_days=2, watchlist=["NVDA", "TSLA"])
    """

    EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
    HEADERS = {
        "User-Agent": "TTPTradingSystem research@ttp-trading.com",
        "Accept": "application/json",
    }
    FORM_TYPES = ["8-K", "SC TO-T", "SC 13D"]

    def __init__(self):
        self.classifier = CatalystClassifier()
        self._request_count = 0

    def fetch_historical(
        self,
        lookback_days: int = 2,
        watchlist: list = None,
        form_types: list = None,
        max_results: int = 100,
    ) -> list:
        """ดึง filings ย้อนหลังจาก SEC EDGAR EFTS"""
        if form_types is None:
            form_types = self.FORM_TYPES

        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=lookback_days)
        all_candidates = []

        for form_type in form_types:
            try:
                filings = self._search_efts(form_type, start_date, end_date, max_results)
                candidates = self._parse_filings(filings, form_type)
                all_candidates.extend(candidates)
                time.sleep(0.15)
            except Exception as e:
                logger.error(f"[EFTS] Error fetching {form_type}: {e}")

        if watchlist:
            watchlist_set = set(s.upper() for s in watchlist)
            filtered = [c for c in all_candidates if c.symbol in watchlist_set]
            logger.info(
                f"[EFTS] Fetched {len(all_candidates)} total, "
                f"{len(filtered)} match watchlist ({len(watchlist_set)} symbols)"
            )
            return filtered

        return all_candidates

    def _search_efts(self, form_type, start_date, end_date, max_results=100):
        params = {
            "q": f\'"{form_type}"\',
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
                resp = requests.get(self.EFTS_SEARCH_URL, params=params,
                                    headers=self.HEADERS, timeout=15)
                self._request_count += 1
                if resp.status_code == 429:
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
            total = data.get("hits", {}).get("total", {}).get("value", 0)
            if len(all_filings) >= total or len(all_filings) >= max_results:
                break
            time.sleep(0.12)

        return all_filings[:max_results]

    def _parse_filings(self, filings, form_type):
        candidates = []
        for hit in filings:
            source = hit.get("_source", {})
            display_names = source.get("display_names", [])
            company_name = display_names[0] if display_names else ""
            file_description = source.get("file_description", "")
            display_date = source.get("file_date", "")
            root_forms = source.get("root_forms", [])

            # Filter by form type
            form_match = any(
                f.upper().startswith(form_type.replace(" ", "").upper()[:3])
                for f in root_forms
            ) if root_forms else True
            if not form_match:
                continue

            # Extract ticker from display_names: "COMPANY (TICKER) (CIK ...)"
            tickers = []
            if company_name:
                ticker_matches = re.findall(r\'\\(([A-Z]{1,5})\\)\', company_name)
                tickers = [t for t in ticker_matches if not t.startswith("CIK")]

            if not tickers and self.classifier:
                extracted = self.classifier.extract_symbol(company_name)
                if extracted:
                    tickers = [extracted]

            if not tickers:
                continue

            headline = company_name.split("(CIK")[0].strip()
            if file_description and file_description not in headline:
                headline += f" — {file_description}"

            if form_type in ("SC TO-T", "SC 13D"):
                catalyst_type, urgency = "MA", 85
            else:
                result = self.classifier.classify(headline)
                catalyst_type, urgency = result if result else ("OTHER", 35)

            try:
                ts = datetime.strptime(display_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                ts = datetime.now(timezone.utc)

            ciks = source.get("ciks", [])
            cik = ciks[0] if ciks else ""

            for ticker in tickers[:2]:
                candidates.append(NewsCandidate(
                    symbol=ticker.upper(),
                    headline=headline[:200],
                    catalyst_type=catalyst_type,
                    urgency_score=urgency,
                    source="SEC_EDGAR_EFTS",
                    url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}"
                        if cik else "",
                    timestamp=ts,
                ))

        return candidates

    def get_stats(self):
        return {"total_requests": self._request_count}

'''


SHADOW_FETCH_NEW = '''def _fetch_real_candidates(pipeline, symbols: list, lookback_days: int = 2) -> list:
    """
    ดึง candidates จริงจาก SEC EDGAR / Benzinga สำหรับ symbols ที่กำหนด
    ใช้ใน one-shot mode เพื่อให้ได้ข่าวจริง (ถ้ามี)

    Enhanced: เพิ่ม EFTS historical search สำหรับ dry-run/shadow
    """
    candidates = []

    # ── Real-time candidates (RSS + Benzinga)
    try:
        from ext_data.news_scanner import NewsScanner, NewsCandidate
        scanner = NewsScanner(
            benzinga_api_key=pipeline.cfg.BENZINGA_API_KEY or None,
            use_sec_edgar=True,
            min_urgency=30,
        )
        raw = scanner._poll_all_sources()
        if raw:
            for c in raw:
                if c.symbol in symbols:
                    candidates.append(c)
    except Exception as e:
        logger.debug(f"Real candidate fetch failed: {e}")

    # ── Historical candidates (EFTS) — ดึง filings ย้อนหลัง 1-2 วัน
    try:
        from ext_data.news_scanner import SecEdgarFullTextSearch
        efts = SecEdgarFullTextSearch()
        historical = efts.fetch_historical(
            lookback_days=lookback_days,
            watchlist=symbols,
            max_results=50,
        )
        # Dedup by (symbol, headline[:50])
        seen = set((c.symbol, c.headline[:50]) for c in candidates)
        for c in historical:
            key = (c.symbol, c.headline[:50])
            if key not in seen:
                candidates.append(c)
                seen.add(key)
        logger.info(f"[Shadow] EFTS: +{len(historical)} historical ({lookback_days}d)")
    except Exception as e:
        logger.debug(f"EFTS historical fetch failed: {e}")

    return candidates'''


SHADOW_FETCH_OLD = '''def _fetch_real_candidates(pipeline, symbols: list) -> list:
    """
    ดึง candidates จริงจาก SEC EDGAR / Benzinga สำหรับ symbols ที่กำหนด
    ใช้ใน one-shot mode เพื่อให้ได้ข่าวจริง (ถ้ามี)
    """
    candidates = []
    try:
        from ext_data.news_scanner import NewsScanner, NewsCandidate
        scanner = NewsScanner(
            benzinga_api_key=pipeline.cfg.BENZINGA_API_KEY or None,
            use_sec_edgar=True,
            min_urgency=30,  # ลด threshold เพื่อเก็บข่าวมากขึ้นใน shadow mode
        )
        # Quick poll (ไม่เปิด background thread)
        raw = scanner._poll_all_sources()
        if raw:
            for c in raw:
                if c.symbol in symbols:
                    candidates.append(c)
    except Exception as e:
        logger.debug(f"Real candidate fetch failed: {e}")

    return candidates'''


# ============================================================
# PATCH 2: Volume Profile Features
# ============================================================

VOLUME_PROFILE_METHOD = '''
    # ── Volume Profile Features (POC, Value Area)
    def _volume_profile_features(self, df1: pd.DataFrame) -> dict:
        """
        Volume Profile: POC, Value Area High/Low
        
        ตามคำแนะนำ Day Trading มืออาชีพ:
          "Volume Profile บอกว่าช่วงราคาไหนมีการซื้อขายหนาแน่นที่สุด (POC)
           มือโปรใช้หาแนวรับ-แนวต้านที่มีนัยสำคัญจริงๆ"
        """
        c = df1["close"].astype(float)
        h = df1["high"].astype(float)
        l = df1["low"].astype(float)
        v = df1["volume"].astype(float)
        current_price = c.iloc[-1]

        lookback = min(60, len(df1) - 1)
        profile_df = df1.iloc[max(0, len(df1) - lookback - 1):len(df1) - 1]

        if len(profile_df) < 10:
            return {"dist_from_poc": 0.0, "price_in_value_area": 0.5,
                    "va_width_pct": 0.0, "dist_from_vah": 0.0, "dist_from_val": 0.0}

        # Build volume profile (40 bins)
        price_low = profile_df["low"].astype(float).min()
        price_high = profile_df["high"].astype(float).max()
        if price_high <= price_low:
            return {"dist_from_poc": 0.0, "price_in_value_area": 0.5,
                    "va_width_pct": 0.0, "dist_from_vah": 0.0, "dist_from_val": 0.0}

        n_bins = 40
        bin_edges = np.linspace(price_low, price_high, n_bins + 1)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        bin_volumes = np.zeros(n_bins)

        ph = profile_df["high"].astype(float).values
        pl = profile_df["low"].astype(float).values
        pv = profile_df["volume"].astype(float).values

        for i in range(len(profile_df)):
            if pv[i] <= 0 or ph[i] <= pl[i]:
                continue
            low_idx = max(0, np.searchsorted(bin_edges, pl[i], side="right") - 1)
            high_idx = min(n_bins, np.searchsorted(bin_edges, ph[i], side="left"))
            if high_idx <= low_idx:
                high_idx = low_idx + 1
            n_covered = high_idx - low_idx
            if n_covered > 0:
                bin_volumes[low_idx:high_idx] += pv[i] / n_covered

        poc = float(bin_centers[np.argmax(bin_volumes)])

        # Value Area (70% of total volume)
        total_vol = bin_volumes.sum()
        if total_vol <= 0:
            return {"dist_from_poc": 0.0, "price_in_value_area": 0.5,
                    "va_width_pct": 0.0, "dist_from_vah": 0.0, "dist_from_val": 0.0}

        poc_idx = int(np.argmax(bin_volumes))
        accumulated = bin_volumes[poc_idx]
        va_lo, va_hi = poc_idx, poc_idx
        while accumulated < total_vol * 0.70:
            lo_vol = bin_volumes[va_lo - 1] if va_lo > 0 else 0
            hi_vol = bin_volumes[va_hi + 1] if va_hi < n_bins - 1 else 0
            if lo_vol == 0 and hi_vol == 0:
                break
            if lo_vol >= hi_vol and va_lo > 0:
                va_lo -= 1
                accumulated += bin_volumes[va_lo]
            elif va_hi < n_bins - 1:
                va_hi += 1
                accumulated += bin_volumes[va_hi]
            else:
                break

        val = float(bin_edges[va_lo])
        vah = float(bin_edges[va_hi + 1])

        return {
            "dist_from_poc": round(float((current_price - poc) / (poc + 1e-9) * 100), 4),
            "price_in_value_area": 1.0 if val <= current_price <= vah else 0.0,
            "va_width_pct": round(float((vah - val) / (poc + 1e-9) * 100), 4),
            "dist_from_vah": round(float((current_price - vah) / (vah + 1e-9) * 100), 4),
            "dist_from_val": round(float((current_price - val) / (val + 1e-9) * 100), 4),
        }
'''


# ============================================================
# PATCH 3: RSI Divergence Scanner Rule
# ============================================================

RSI_DIV_CONFIG_FIELDS = '''
    # ── RSI Divergence params
    div_swing_order:     int   = 5      # bars ซ้าย-ขวาสำหรับ swing detection
    div_lookback_bars:   int   = 30     # ดูย้อนหลังกี่ bars
    div_min_confidence:  float = 0.3    # confidence ขั้นต่ำ
    div_rvol_min:        float = 1.0    # RVOL ขั้นต่ำ'''

RSI_DIV_METHOD = '''
    # ------------------------------------------
    # RULE 4: RSI DIVERGENCE
    # ------------------------------------------

    def _check_rsi_divergence(self, symbol, df, price, ind) -> Optional[TechSignal]:
        """
        RSI Divergence: จับ Hidden + Regular Divergence

        ตามคำแนะนำมืออาชีพ:
          "สิ่งที่พวกเขาหาคือ Divergence โดยเฉพาะ Hidden Divergence
           เพื่อหาจังหวะเข้าทำกำไรในจังหวะย่อตัว (Pullback)"

        Hidden Bullish:  Price HL + RSI LL → Pullback in uptrend → LONG
        Hidden Bearish:  Price LH + RSI HH → Pullback in downtrend → SHORT
        Regular Bullish: Price LL + RSI HL → Weakening downtrend
        Regular Bearish: Price HH + RSI LH → Weakening uptrend
        """
        if len(df) < 30:
            return None

        cfg = self.config
        if ind.get("rvol", 0) < getattr(cfg, "div_rvol_min", 1.0):
            return None

        c = df["close"].astype(float)
        lookback = getattr(cfg, "div_lookback_bars", 30)
        swing_order = getattr(cfg, "div_swing_order", 5)
        min_conf = getattr(cfg, "div_min_confidence", 0.3)

        # Calculate RSI
        delta = c.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + (gain / (loss.replace(0, np.nan)))))
        rsi = rsi.fillna(50)

        # Find swing points
        price_window = c.iloc[-lookback:].reset_index(drop=True)
        rsi_window = rsi.iloc[-lookback:].reset_index(drop=True)

        def _find_swings(series, order=5):
            vals = series.values
            n = len(vals)
            swings = []
            for i in range(order, n - order):
                is_hi = all(vals[i] > vals[i-j] and vals[i] > vals[i+j] for j in range(1, order+1))
                is_lo = all(vals[i] < vals[i-j] and vals[i] < vals[i+j] for j in range(1, order+1))
                if is_hi: swings.append((i, float(vals[i]), "high"))
                elif is_lo: swings.append((i, float(vals[i]), "low"))
            return swings[-10:]

        price_swings = _find_swings(price_window, swing_order)
        rsi_swings = _find_swings(rsi_window, swing_order)

        p_lows = [(i,v) for i,v,t in price_swings if t == "low"]
        p_highs = [(i,v) for i,v,t in price_swings if t == "high"]
        r_lows = [(i,v) for i,v,t in rsi_swings if t == "low"]
        r_highs = [(i,v) for i,v,t in rsi_swings if t == "high"]

        best = {"type": None, "conf": 0.0, "desc": ""}

        # Hidden Bullish: Price HL + RSI LL
        if len(p_lows) >= 2 and len(r_lows) >= 2:
            pl1, pl2 = p_lows[-2], p_lows[-1]
            rl1, rl2 = r_lows[-2], r_lows[-1]
            if pl2[0]-pl1[0] >= 5 and pl2[1] > pl1[1] and rl2[1] < rl1[1]:
                conf = min(1.0, abs(pl2[1]-pl1[1])/(pl1[1]+1e-9)*100*0.3 + abs(rl2[1]-rl1[1])*0.02)
                if conf > best["conf"]:
                    best = {"type": "hidden_bullish", "conf": conf,
                            "desc": f"Hidden Bull: Price HL→{pl2[1]:.0f} + RSI LL→{rl2[1]:.0f}"}

        # Hidden Bearish: Price LH + RSI HH
        if len(p_highs) >= 2 and len(r_highs) >= 2:
            ph1, ph2 = p_highs[-2], p_highs[-1]
            rh1, rh2 = r_highs[-2], r_highs[-1]
            if ph2[0]-ph1[0] >= 5 and ph2[1] < ph1[1] and rh2[1] > rh1[1]:
                conf = min(1.0, abs(ph2[1]-ph1[1])/(ph1[1]+1e-9)*100*0.3 + abs(rh2[1]-rh1[1])*0.02)
                if conf > best["conf"]:
                    best = {"type": "hidden_bearish", "conf": conf,
                            "desc": f"Hidden Bear: Price LH→{ph2[1]:.0f} + RSI HH→{rh2[1]:.0f}"}

        # Regular Bullish: Price LL + RSI HL
        if len(p_lows) >= 2 and len(r_lows) >= 2 and best["conf"] < 0.3:
            pl1, pl2 = p_lows[-2], p_lows[-1]
            rl1, rl2 = r_lows[-2], r_lows[-1]
            if pl2[0]-pl1[0] >= 5 and pl2[1] < pl1[1] and rl2[1] > rl1[1]:
                conf = min(0.8, abs(pl2[1]-pl1[1])/(pl1[1]+1e-9)*100*0.2 + abs(rl2[1]-rl1[1])*0.015)
                if conf > best["conf"]:
                    best = {"type": "regular_bullish", "conf": conf,
                            "desc": f"Reg Bull: Price LL→{pl2[1]:.0f} + RSI HL→{rl2[1]:.0f}"}

        # Regular Bearish: Price HH + RSI LH
        if len(p_highs) >= 2 and len(r_highs) >= 2 and best["conf"] < 0.3:
            ph1, ph2 = p_highs[-2], p_highs[-1]
            rh1, rh2 = r_highs[-2], r_highs[-1]
            if ph2[0]-ph1[0] >= 5 and ph2[1] > ph1[1] and rh2[1] < rh1[1]:
                conf = min(0.8, abs(ph2[1]-ph1[1])/(ph1[1]+1e-9)*100*0.2 + abs(rh2[1]-rh1[1])*0.015)
                if conf > best["conf"]:
                    best = {"type": "regular_bearish", "conf": conf,
                            "desc": f"Reg Bear: Price HH→{ph2[1]:.0f} + RSI LH→{rh2[1]:.0f}"}

        if best["type"] is None or best["conf"] < min_conf:
            return None

        side = "buy" if "bullish" in best["type"] else "sell"
        base_urg = 65 if "hidden" in best["type"] else 55
        urgency = min(90, base_urg + int(best["conf"] * 25))

        return TechSignal(
            symbol=symbol,
            rule_name="RSI_DIVERGENCE",
            side=side,
            urgency=urgency,
            detail=f"{best['desc']} | RVOL={ind.get('rvol',0):.1f}x conf={best['conf']:.2f}",
            metrics={"divergence_type": best["type"], "confidence": best["conf"],
                     "rsi_current": float(rsi.iloc[-1]), "rvol": ind.get("rvol", 0)},
        )
'''


# ============================================================
# MAIN: Apply all patches
# ============================================================

def apply_sec_edgar_patch(dry_run=False):
    """Patch 1: SEC EDGAR EFTS Historical"""
    print("\n" + "=" * 60)
    print("  PATCH 1: SEC EDGAR EFTS Historical Search")
    print("=" * 60)

    f1 = "ext_data/news_scanner.py"
    f2 = "mode/shadow_runner.py"

    if os.path.exists(f1):
        if not dry_run:
            backup_file(f1)

        # เพิ่ม class ก่อน __main__ block
        append_to_file(
            f1, SEC_EDGAR_CLASS,
            "เพิ่ม SecEdgarFullTextSearch class",
            marker='if __name__ == "__main__":',
            dry_run=dry_run,
        )
    else:
        print(f"  ❌ {f1} not found")

    if os.path.exists(f2):
        if not dry_run:
            backup_file(f2)

        patch_file(
            f2, SHADOW_FETCH_OLD, SHADOW_FETCH_NEW,
            "แก้ _fetch_real_candidates() → เพิ่ม EFTS historical",
            dry_run=dry_run,
        )
    else:
        print(f"  ❌ {f2} not found")


def apply_volume_profile_patch(dry_run=False):
    """Patch 2: Volume Profile Features"""
    print("\n" + "=" * 60)
    print("  PATCH 2: Volume Profile (POC, Value Area)")
    print("=" * 60)

    f1 = "models/technical_ml_analyzer.py"

    if os.path.exists(f1):
        if not dry_run:
            backup_file(f1)

        # เพิ่ม _volume_profile_features method
        # ใส่ก่อน method _volatility_features
        append_to_file(
            f1, VOLUME_PROFILE_METHOD,
            "เพิ่ม _volume_profile_features() method",
            marker="    # ── 4. FEATURE SCALING",
            dry_run=dry_run,
        )

        # เพิ่มการเรียกใน compute()
        # หา pattern: feats.update(self._volatility_features
        patch_file(
            f1,
            'feats.update(self._volatility_features(df1))',
            'feats.update(self._volatility_features(df1))\n'
            '        feats.update(self._volume_profile_features(df1))  # VVP: POC, Value Area',
            "เพิ่ม volume_profile_features() ใน compute()",
            dry_run=dry_run,
        )
    else:
        print(f"  ❌ {f1} not found")


def apply_rsi_divergence_patch(dry_run=False):
    """Patch 3: RSI Divergence Scanner"""
    print("\n" + "=" * 60)
    print("  PATCH 3: RSI Divergence Scanner Rule")
    print("=" * 60)

    f1 = "mode/technical_scanner.py"

    if os.path.exists(f1):
        if not dry_run:
            backup_file(f1)

        # 1. เพิ่ม div params ใน TechScanConfig
        patch_file(
            f1,
            '    vol_spike_price_pct: float = 1.5    # min % price move in last 2 bars',
            '    vol_spike_price_pct: float = 1.5    # min % price move in last 2 bars\n'
            + RSI_DIV_CONFIG_FIELDS,
            "เพิ่ม RSI Divergence params ใน TechScanConfig",
            dry_run=dry_run,
        )

        # 2. เพิ่ม "rsi_divergence" ใน active_rules
        patch_file(
            f1,
            '"vwap_pullback", "ml_breakout", "volume_spike",',
            '"vwap_pullback", "ml_breakout", "volume_spike", "rsi_divergence",',
            'เพิ่ม "rsi_divergence" ใน active_rules',
            dry_run=dry_run,
        )

        # 3. เพิ่ม "rsi_divergence" ใน ALL_RULES
        patch_file(
            f1,
            'ALL_RULES = {"vwap_pullback", "ml_breakout", "volume_spike"}',
            'ALL_RULES = {"vwap_pullback", "ml_breakout", "volume_spike", "rsi_divergence"}',
            'เพิ่ม "rsi_divergence" ใน ALL_RULES',
            dry_run=dry_run,
        )

        # 4. เพิ่ม import numpy
        with open(f1, "r") as f:
            content = f.read()
        if "import numpy" not in content:
            patch_file(
                f1,
                "from typing import Optional",
                "from typing import Optional\nimport numpy as np",
                "เพิ่ม import numpy",
                dry_run=dry_run,
            )

        # 5. เพิ่ม _check_rsi_divergence method
        # ใส่ก่อน scan_tick หรือ class closing
        append_to_file(
            f1, RSI_DIV_METHOD,
            "เพิ่ม _check_rsi_divergence() method",
            marker="    # ------------------------------------------\n    # SCAN: Single symbol",
            dry_run=dry_run,
        )

        # 6. เพิ่ม call ใน _scan_symbol
        patch_file(
            f1,
            '        if "volume_spike" in self.config.active_rules:',
            '        if "rsi_divergence" in self.config.active_rules:\n'
            '            sig = self._check_rsi_divergence(symbol, df, price, ind)\n'
            '            if sig:\n'
            '                signals.append(sig)\n\n'
            '        if "volume_spike" in self.config.active_rules:',
            "เพิ่ม _check_rsi_divergence call ใน _scan_symbol",
            dry_run=dry_run,
        )
    else:
        print(f"  ❌ {f1} not found")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-apply patches (SEC EDGAR EFTS + Volume Profile + RSI Divergence)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="แสดง diff ไม่แก้ไฟล์")
    parser.add_argument("--only", choices=["sec_edgar", "volume_profile", "rsi_divergence"],
                        help="Apply เฉพาะ patch ที่ระบุ")
    args = parser.parse_args()

    print("═" * 60)
    print("  AUTO APPLY PATCHES")
    print(f"  Mode: {'DRY-RUN' if args.dry_run else 'LIVE (will modify files)'}")
    if args.only:
        print(f"  Only: {args.only}")
    print("═" * 60)

    if not args.only or args.only == "sec_edgar":
        apply_sec_edgar_patch(args.dry_run)

    if not args.only or args.only == "volume_profile":
        apply_volume_profile_patch(args.dry_run)

    if not args.only or args.only == "rsi_divergence":
        apply_rsi_divergence_patch(args.dry_run)

    print("\n" + "═" * 60)
    if args.dry_run:
        print("  ✅ DRY-RUN complete — ไม่มีไฟล์ถูกแก้ไข")
        print("  ลบ --dry-run เพื่อ apply จริง")
    else:
        print("  ✅ All patches applied!")
        print("  Backup files: *.bak_YYYYMMDD_HHMMSS")
    print("═" * 60)

    print("\n📋 ทดสอบหลัง apply:")
    print("  python main.py --mode shadow --skip-gates gate19 --shadow-symbols NVDA,TSLA")
    print("  python main.py --profile TTP_5K_FLEX --dry-run")
    print("  python main.py --mode shadow --enable-tech-scan --shadow-symbols NVDA")


if __name__ == "__main__":
    main()
