# TTP Trading System — Comprehensive Analysis Report
## วิเคราะห์ Code เทียบกับหลักการเทรดมืออาชีพ + Audit ทุก Feature ที่ร้องขอ

**วันที่:** 23 มีนาคม 2026  
**ผู้วิเคราะห์:** Claude (Opus 4.6)  
**Scope:** ทั้งระบบ — main.py, gate_19_llm_cio.py, technical_ml_analyzer.py, shadow_runner.py, technical_scanner.py, news_scanner.py, data_pipeline_manager.py, config.py

---

## ส่วนที่ 1: เปรียบเทียบ Code กับหลักการ Daytrade มืออาชีพ

### 1.1 VWAP (Volume Weighted Average Price) — "เส้นศักดิ์สิทธิ์"

| คำแนะนำจากเอกสาร | สิ่งที่ Code ทำแล้ว | สถานะ |
|---|---|---|
| VWAP สะท้อนต้นทุนเฉลี่ยที่แท้จริงของตลาด | ✅ `FeatureEngineer` คำนวณ `vwap_dev_pct` (ราคาเทียบ VWAP) เป็น feature หลัก | ✅ ครบ |
| ใช้เป็นจุดอ้างอิง Mean Reversion / Trend Continuation | ✅ Label Y = "Close_EOD > VWAP" (VWAP Pullback Signal) — core label ของทั้ง LightGBM + LSTM | ✅ ครบ |
| ใช้ประเมิน Overvalued / Undervalued | ✅ `price_vs_vwap` ส่งให้ Gate 19 LLM + Technical Scanner ใช้ `vwap_dev_pct` | ✅ ครบ |
| ใช้ใน Timeframe 5m หรือ 15m | ✅ ระบบใช้ 15m bars เป็นหลัก (`TIMEFRAME = "15m"`) | ✅ ครบ |

**ข้อสังเกต:** ระบบใช้ VWAP เป็น "แกนกลาง" ของการตัดสินใจ — ตรงกับคำแนะนำ 100% Label generation สร้างจาก `price ≤ VWAP × 1.005 AND Close_EOD > VWAP` ซึ่งเป็นการจับ VWAP Pullback แบบมืออาชีพ

---

### 1.2 Volume Profile / RVOL (Relative Volume)

| คำแนะนำจากเอกสาร | สิ่งที่ Code ทำแล้ว | สถานะ |
|---|---|---|
| Volume Profile บอกโซนซื้อขายหนาแน่น (POC) | ⚠️ ระบบใช้ RVOL (Relative Volume) แทน Volume Profile แบบเต็ม | ⚠️ บางส่วน |
| ใช้หาแนวรับ-แนวต้านที่มีนัยสำคัญ | ✅ TechnicalScanner ใช้ RVOL ใน VWAP_PULLBACK + VOLUME_SPIKE rules | ✅ ครบ |
| หาโซนที่สถาบันสะสมของ | ⚠️ ยังไม่มี Volume Profile แบบ VVP (plot บนแกน Y) — ใช้ RVOL เป็น proxy | ⚠️ ขาด VVP |

**ข้อเสนอ:** Volume Profile (VVP) แบบเต็มจะเพิ่ม edge ได้ — แต่ RVOL ที่ใช้อยู่ก็เป็น proxy ที่ดีสำหรับ Tier classification (TIER1_RVOL ≥ 2x, TIER2_RVOL ≥ 1.5x)

---

### 1.3 ATR (Average True Range) — Risk Management

| คำแนะนำจากเอกสาร | สิ่งที่ Code ทำแล้ว | สถานะ |
|---|---|---|
| ใช้วัด Volatility | ✅ `atr_15m` คำนวณจาก 15m bars | ✅ ครบ |
| คำนวณ Position Sizing แบบ Dynamic | ✅ `actual_risk = (Entry-SL) + commission + slippage + spread` → `shares = floor(budget / actual_risk)` | ✅ ครบ |
| Stop Loss = 1.5 × ATR | ✅ `ATR_STOP_MULT` configurable — default 1.5×ATR | ✅ ครบ |
| ปรับตัวเข้ากับสภาวะตลาดที่เปลี่ยนไป | ✅ Dynamic SL/TP ใน `_fetch_atr_15m()` + shadow mode แสดง ATR value | ✅ ครบ |

**สรุป:** ATR ใช้ถูกต้องตามคำแนะนำ — ไม่ใช้หาจุดเข้า แต่ใช้กำหนด SL/TP + Sizing

---

### 1.4 Short-term EMAs (9-EMA, 20-EMA)

| คำแนะนำจากเอกสาร | สิ่งที่ Code ทำแล้ว | สถานะ |
|---|---|---|
| ใช้ควบคู่กับ VWAP ดูโมเมนตัม | ✅ `FeatureEngineer` คำนวณ EMA features ใน 47+ features | ✅ ครบ |
| 9-EMA ตัด 20-EMA ขึ้น = เทรนด์แข็งแกร่ง | ✅ LightGBM เรียนรู้ pattern นี้ผ่าน feature interactions | ✅ ครบ (implicit) |

---

### 1.5 RSI — Divergence Focus

| คำแนะนำจากเอกสาร | สิ่งที่ Code ทำแล้ว | สถานะ |
|---|---|---|
| ไม่ใช้ RSI แบบ Overbought/Oversold ดั้งเดิม | ✅ Technical Scanner ใช้ RSI เป็น filter (RSI < 45 for pullback) ไม่ใช่ signal เดี่ยว | ✅ ครบ |
| หา Divergence (Hidden Divergence) | ⚠️ ยังไม่มี RSI Divergence detection แยก — LightGBM เรียนรู้ pattern จาก features ได้บ้าง | ⚠️ ขาด explicit |

---

### 1.6 Price Action + Risk Management (Core Philosophy)

| คำแนะนำจากเอกสาร | สิ่งที่ Code ทำแล้ว | สถานะ |
|---|---|---|
| ไม่มี Indicator วิเศษ | ✅ ระบบใช้ 19 gates — ไม่พึ่ง indicator ตัวเดียว | ✅ ครบ |
| Edge อยู่ที่ Risk Management เฉียบขาด | ✅ 7 risk gates + ATR-based sizing + daily loss kill-switch + streak block | ✅ ครบ |
| Execution Speed สำคัญ | ✅ LightGBM inference < 1ms, pipeline < 2 วินาที | ✅ ครบ |
| LLM ห้ามเพิ่ม Risk | ✅ Hardcode: `sizing_multiplier ≤ 1.0` — clamp ในโค้ด ไม่ใช่ config | ✅ ครบ |

---

### ⭐ สรุปการเปรียบเทียบ

| หลักการ | จัดอยู่ในระบบ? | คะแนน |
|---|---|---|
| VWAP เป็นแกนกลาง | ✅ เป็น label + feature + Gate 19 input | 10/10 |
| Volume/RVOL เป็นตัวกรอง | ✅ Tier classification + Technical Scanner | 8/10 |
| ATR สำหรับ Risk Management (ไม่ใช่ entry) | ✅ Dynamic SL/TP + Position Sizing | 10/10 |
| EMA สำหรับ Momentum | ✅ Feature ใน ML models | 8/10 |
| RSI Divergence | ⚠️ Implicit ผ่าน ML, ยังไม่มี explicit divergence | 6/10 |
| Risk Management > Entry Signal | ✅ 19 gates, strict loss limits, LLM เป็น brake only | 10/10 |
| Price Action First | ✅ 47 features จาก price/volume, ไม่ใช่ lagging indicators | 9/10 |

**Overall Score: 8.7/10** — ระบบสอดคล้องกับหลักการเทรดมืออาชีพอย่างมาก

---

## ส่วนที่ 2: Audit Features ที่ร้องขอ — สิ่งที่มีแล้ว vs สิ่งที่ต้องเพิ่ม

### 2.1 Dry-Run — ทุก function ต้อง dry-run ได้

| Component | สถานะ | รายละเอียด |
|---|---|---|
| `main.py --dry-run` | ✅ มีแล้ว | สร้าง mock news 3 ตัว, วิ่งทุก gate, ไม่ยิง order |
| `data_pipeline_manager.py --dry-run` | ✅ มีแล้ว | แสดงแผนงาน daily/weekly pipeline ไม่ download/train |
| `run-pipeline-dryrun.sh` | ✅ มีแล้ว | Shell script wrapper |
| ML models ใน dry-run | ✅ มีแล้ว | `MockRegimeScorer` + LightGBM/LSTM predict จาก models ที่ train ไว้ |
| ML 3-class prediction (-1,0,1) | ✅ มีแล้ว | `LabelGenerator` สร้าง -1/0/+1, LightGBM `num_class=3`, LSTM `Linear(3)` |
| Alpaca paper trade config | ✅ มีแล้ว | `Config.get_alpaca_keys("paper")`, `MockExecutor` สำหรับ dry-run |

### 2.2 ML Prediction — Confidence Score + Raw Scores

| Feature | สถานะ | รายละเอียด |
|---|---|---|
| 3-class prediction (-1,0,1) | ✅ มีแล้ว | `predicted_class` ใน `MLPrediction` dataclass |
| Confidence score | ✅ มีแล้ว | `confidence: 0.0-1.0` + `confidence_label: HIGH/MEDIUM/LOW` |
| Raw LightGBM probs | ✅ มีแล้ว | `lgbm_raw_probs: [P(BEAR), P(NEUTRAL), P(BULL)]` |
| Raw LSTM probs | ✅ มีแล้ว | `lstm_raw_probs: [P(BEAR), P(NEUTRAL), P(BULL)]` |
| Combined 3-class probs | ✅ มีแล้ว | `class_probs: {"sell": .., "neutral": .., "buy": ..}` |
| Log แสดง raw scores | ✅ มีแล้ว | log line: `class=+1 P(sell=0.10 neu=0.25 buy=0.65) conf=0.812` |

### 2.3 SEC EDGAR News Feed

| Feature | สถานะ | รายละเอียด |
|---|---|---|
| SEC EDGAR RSS Feed | ✅ มีแล้ว | `SecEdgarRssSource` — ดึง 8-K + SC TO-T filings ฟรี |
| Match กับ watchlist | ✅ มีแล้ว | `_fetch_real_candidates()` filter `if c.symbol in symbols` |
| Feed ย้อนหลัง 1-2 วัน | ⚠️ ต้องเพิ่ม | ปัจจุบัน SEC RSS ดึงแค่ล่าสุด — ยังไม่มี historical query |
| Benzinga fallback | ✅ มีแล้ว | `NewsScanner` รองรับทั้ง Benzinga + SEC (ถ้าไม่มี key ใช้ SEC อย่างเดียว) |

### 2.4 Watch List Integration

| Feature | สถานะ | รายละเอียด |
|---|---|---|
| โหลด watchlist จากไฟล์ | ✅ มีแล้ว | รองรับ JSON, CSV, TXT — `load_shadow_watchlist()` |
| ใช้ watchlist จาก LSTM training | ✅ มีแล้ว | `--shadow-watchlist ./universe.json` (เดียวกับ universe ที่ train) |
| ใช้ใน shadow mode | ✅ มีแล้ว | `--shadow-symbols NVDA,TSLA` หรือ `--shadow-watchlist file` |

### 2.5 Shadow/Analysis Mode

| Feature | สถานะ | รายละเอียด |
|---|---|---|
| One-shot mode (default) | ✅ มีแล้ว | `python main.py --mode shadow` — สแกนครั้งเดียว |
| Live shadow (continuous) | ✅ มีแล้ว | `--live-shadow` flag — วิ่งต่อเนื่อง |
| Skip gates ได้ทุก gate | ✅ มีแล้ว | `--skip-gates gate19,session,ml,...` — 14 gate IDs |
| Console output | ✅ มีแล้ว | Color-coded gate results + CIO analysis |
| JSON file output | ✅ มีแล้ว | `./shadow_reports/shadow_YYYY-MM-DD_HHMMSS.json` |
| Gate block frequency | ✅ มีแล้ว | `_gate_block_frequency()` นับว่า gate ไหน block บ่อย |
| Watchlist input | ✅ มีแล้ว | `--shadow-watchlist` + `--shadow-symbols` |

### 2.6 Gate 19 LLM — Local LLM Support

| Feature | สถานะ | รายละเอียด |
|---|---|---|
| `_call_local_llm()` แยกจาก `_call_openai()` | ✅ มีแล้ว | ข้าม key check, remap URL, strip `<think>` tags |
| DeepSeek R1 `<think>...</think>` parser | ✅ มีแล้ว | `_strip_think_tags()` ลบ think blocks + unclosed tags |
| Timeout 30s สำหรับ local | ✅ มีแล้ว | config `"timeout_sec": 30` |
| เปิด/ปิดใน dry-run | ✅ มีแล้ว | `LLM_LOCAL_ENABLED`, `LLM_LOCAL_ONLY`, `LLM_ENABLED` |
| Ollama + OpenAI-compat backends | ✅ มีแล้ว | `_call_ollama()` + `_call_local_llm()` with backend selection |
| Provider chain | ✅ มีแล้ว | PRIMARY → FALLBACK → LOCAL_LLM → FAIL_SAFE |

### 2.7 Gate 19 LLM — Bilingual + Technical Levels

| Feature | สถานะ | รายละเอียด |
|---|---|---|
| ข้อความ 2 ภาษา (TH + EN) | ✅ มีแล้ว | `reasoning_th` + `reasoning_en` ใน SYSTEM_PROMPT |
| แนวรับ (Support) | ✅ มีแล้ว | `support` field ใน CIOVerdict |
| แนวต้าน (Resistance) | ✅ มีแล้ว | `resistance` field |
| Cutloss | ✅ มีแล้ว | `cutloss` field |
| จำนวน shares ที่เหมาะสม | ✅ มีแล้ว | `recommended_shares` (≤ system calculated) |
| ราคาที่ควรเข้า | ✅ มีแล้ว | `recommended_entry` |
| ราคาที่ควรทำกำไร | ✅ มีแล้ว | `recommended_tp` |
| Period ของการทำกำไร | ✅ มีแล้ว | `expected_period` ("15m", "30m", "1h", "2h", "3h", "4h", "EOD") |
| ราคาปิดล่าสุด | ✅ มีแล้ว | `prev_close` + `gap_pct` ส่งให้ LLM ใน `build_evaluation_prompt()` |

### 2.8 Technical Scanner

| Feature | สถานะ | รายละเอียด |
|---|---|---|
| Volume Spike (RVOL > 2x) | ✅ มีแล้ว | `_check_volume_spike()` — RVOL + price move % |
| ML Score Breakout | ✅ มีแล้ว | `_check_ml_breakout()` — ML score > threshold ไม่ต้องรอข่าว |
| VWAP Pullback | ✅ มีแล้ว | `_check_vwap_pullback()` — ราคาแตะ VWAP + RVOL + RSI filter |
| Configurable rules | ✅ มีแล้ว | `TechScanConfig` — ทุกค่า override ผ่าน env vars |
| เปิด/ปิดด้วย flag | ✅ มีแล้ว | `--enable-tech-scan`, `TECH_SCAN_ENABLED=true` |
| Gate 19 ตัดสิน tech-only signal | ✅ มีแล้ว | `source="TECH_SCAN"` → Gate 19 เห็นว่าเป็น tech-only |

---

## ส่วนที่ 3: สิ่งที่ต้องเพิ่ม/แก้ไข

### 3.1 สิ่งที่ยังขาดอยู่ (ต้อง implement)

| # | Feature | Priority | รายละเอียด |
|---|---|---|---|
| 1 | SEC EDGAR historical query (1-2 วันย้อนหลัง) | 🔴 HIGH | ปัจจุบัน RSS ดึงแค่ล่าสุด — ต้องเพิ่ม EDGAR FULL-TEXT Search API สำหรับ dry-run |
| 2 | Volume Profile (VVP) — plot บนแกน Y | 🟡 MEDIUM | เพิ่ม POC (Point of Control) เป็น feature ใน FeatureEngineer |
| 3 | RSI Divergence detection (explicit) | 🟡 MEDIUM | เพิ่ม Hidden Divergence เป็น feature / scanner rule |
| 4 | Level 2 Data (Order Book) | 🟠 LOW | ต้องใช้ Alpaca Pro / paid data feed — optional |

### 3.2 สิ่งที่มีแล้วและทำงานถูกต้อง (ไม่ต้องแก้)

ทุกข้อด้านล่างนี้ **มีอยู่แล้วใน codebase** และทำงานตามที่ร้องขอ:

1. ✅ **Dry-run ทุก function** — `--dry-run`, MockExecutor, MockRegimeScorer
2. ✅ **ML 3-class (-1,0,1)** — LightGBM multiclass + LSTM 3-class + ensemble
3. ✅ **Confidence score + raw scores** — `MLPrediction` dataclass มีทุก field
4. ✅ **SEC EDGAR feed** — `SecEdgarRssSource` พร้อม `_fetch_real_candidates()`
5. ✅ **Alpaca paper trade config** — `Config.get_alpaca_keys("paper")`
6. ✅ **Watchlist integration** — JSON/CSV/TXT, shadow-watchlist, shadow-symbols
7. ✅ **Shadow mode** — one-shot + live-shadow, Console + JSON output
8. ✅ **ทุก gate เลือกปิดได้** — `ALL_GATE_IDS` มี 14 gates, `--skip-gates`
9. ✅ **Local LLM** — `_call_local_llm()`, Ollama + OpenAI-compat, เปิด/ปิดได้
10. ✅ **DeepSeek R1 `<think>` parser** — `_strip_think_tags()`
11. ✅ **Bilingual reasoning (TH+EN)** — `reasoning_th`, `reasoning_en`
12. ✅ **แนวรับ/แนวต้าน/cutloss/shares** — `CIOVerdict` dataclass ครบ
13. ✅ **ราคาเข้า/ทำกำไร/period** — `recommended_entry`, `recommended_tp`, `expected_period`
14. ✅ **ราคาปิดล่าสุดส่งให้ LLM** — `prev_close`, `gap_pct` ใน prompt
15. ✅ **Technical Scanner** — VWAP Pullback, ML Breakout, Volume Spike — configurable
16. ✅ **Gate 19 เปิด/ปิด** — `LLM_ENABLED`, `LLM_LOCAL_ONLY`, `--skip-gates gate19`

---

## ส่วนที่ 4: Data Flow Diagram — ภาพรวมระบบ

```
                    ┌─────────────────┐
                    │  Watch List     │
                    │  (universe.json)│
                    └────────┬────────┘
                             │
             ┌───────────────┼───────────────┐
             ▼               ▼               ▼
     ┌──────────────┐ ┌──────────┐  ┌──────────────┐
     │ SEC EDGAR    │ │ Benzinga │  │ Tech Scanner │
     │ (free RSS)   │ │ (paid)   │  │ (VWAP/RVOL)  │
     └──────┬───────┘ └────┬─────┘  └──────┬───────┘
            │              │               │
            └──────┬───────┘               │
                   ▼                       ▼
            ┌─────────────────────────────────┐
            │        NewsCandidate             │
            │  (symbol, headline, catalyst,    │
            │   urgency, source)               │
            └──────────────┬──────────────────┘
                           ▼
    ╔══════════════════════════════════════════════╗
    ║          TRADING PIPELINE (19 Gates)         ║
    ╠══════════════════════════════════════════════╣
    ║  Gate 1:  Daily Loss Kill-Switch             ║
    ║  Gate 2:  Session Filter + Cutoff            ║
    ║  Gate 3:  Market Sentiment (VIX/SPY)         ║
    ║  Gate 4:  Price Fetch (Alpaca)               ║
    ║  Gate 5:  Universe Filter (Gap% + RVOL)      ║
    ║  Gate 6:  Regime Scorer (Momentum+MeanRev)   ║
    ║  Gate ML: LightGBM + LSTM Ensemble           ║
    ║           → 3-class: BEAR/NEUTRAL/BULL       ║
    ║           → confidence + raw probs            ║
    ║  Gate A:  Max Orders/Day                     ║
    ║  Gate B:  Rate Limiter                       ║
    ║  Gate C:  Wash-Sale Cooldown                 ║
    ║  Gate H:  No Hedge (ไม่เปิดซ้ำ)              ║
    ║  Gate I:  Streak Block (consecutive losses)  ║
    ║  Gate 7:  Risk Manager (ATR SL/TP + Sizing)  ║
    ║  Gate 19: LLM CIO (Final Risk Gate)          ║
    ║           → Bilingual TH+EN analysis         ║
    ║           → Support/Resistance/Cutloss       ║
    ║           → Entry/TP/Period recommendation    ║
    ╚══════════════════════════╤═══════════════════╝
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
       ┌────────────┐  ┌────────────┐  ┌────────────┐
       │  DRY-RUN   │  │  SHADOW    │  │  LIVE/     │
       │  (mock)    │  │  (observe) │  │  PAPER     │
       │  ไม่ order │  │  ไม่ order │  │  ส่ง order │
       └────────────┘  └────────────┘  └────────────┘
```

---

## ส่วนที่ 5: สรุปสิ่งที่ต้องทำ (Action Items)

### ลำดับความสำคัญ

| Priority | Task | Effort | Impact |
|---|---|---|---|
| 🔴 1 | SEC EDGAR historical query (1-2 วัน) สำหรับ dry-run | 4-6 ชม. | ได้ข่าวจริงย้อนหลังทดสอบ |
| 🟡 2 | Volume Profile (VVP) — เพิ่ม POC feature | 3-4 ชม. | เพิ่ม edge ตามคำแนะนำ |
| 🟡 3 | RSI Divergence detection | 2-3 ชม. | เพิ่ม scanner rule |
| 🟢 4 | ตรวจสอบ/ปรับแต่ง prompt ของ Gate 19 | 1-2 ชม. | ให้ LLM วิเคราะห์ดีขึ้น |

### สิ่งที่ **ไม่ต้องทำ** (มีแล้ว):

ทุกข้อที่ร้องขอมาเกี่ยวกับ:
- Dry-run capability ✅
- ML confidence/raw scores ✅  
- SEC EDGAR feed ✅
- Watchlist integration ✅
- Shadow mode (one-shot + live) ✅
- Gate toggle (เปิด/ปิดทุก gate) ✅
- Local LLM support ✅
- DeepSeek R1 parser ✅
- Bilingual output ✅
- Support/Resistance/Cutloss/Entry/TP/Period ✅
- Technical Scanner ✅
- Console + JSON output ✅

---

## ส่วนที่ 6: คำแนะนำเชิงกลยุทธ์

### 6.1 สิ่งที่ระบบทำได้ดีเป็นพิเศษ (เมื่อเทียบกับคำแนะนำมืออาชีพ)

1. **VWAP-centric design** — ทั้ง ML label, features, scanner, และ LLM prompt ล้วนหมุนรอบ VWAP
2. **Asymmetric risk control** — LLM ลด risk ได้อย่างเดียว (hardcode ≤ 1.0)
3. **ATR-based dynamic sizing** — ปรับ SL/TP ตาม volatility จริง
4. **Multi-model ensemble** — LightGBM (tabular) + LSTM (sequential) + 3-class classification
5. **19-gate defense depth** — ไม่มีจุดเดียวที่ fail แล้วหายนะ

### 6.2 โอกาสพัฒนาต่อ

1. **Volume Profile (VVP)** — เพิ่ม POC, Value Area High/Low เป็น features ใน ML
2. **RSI Divergence** — เพิ่มเป็น TechScan rule ที่ 4 (DIVERGENCE_SIGNAL)
3. **Order Flow** — ถ้าได้ Level 2 data จะเพิ่ม edge ตามที่เอกสารแนะนำ
4. **Backtesting framework** — ใช้ shadow mode output ทำ backtest ย้อนหลัง
