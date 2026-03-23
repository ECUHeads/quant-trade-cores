# Universal 15m Quant Engine + LLM CIO (Gate 19) + SaaS Signal Platform

## ปรัชญาการออกแบบ (Design Philosophy)

ระบบนี้สร้างขึ้นจากความเชื่อ 3 ข้อ:

**1. สัญญาณน้อยแต่แม่น ดีกว่าสัญญาณเยอะแต่สับสน**
กราฟ 1 นาทีเต็มไปด้วย noise — เหมือนฟังคนพูดพร้อมกัน 100 คน
กราฟ 15 นาทีกรองเหลือเฉพาะ "เสียงของสถาบัน" ที่เข้าซื้อด้วยเงินจริง
ระบบนี้จึงยิงคำสั่งแค่ 1–3 ครั้ง/วัน โดยแต่ละครั้งต้องผ่าน 19 ด่านตรวจสอบ

**2. AI ควรทำหน้าที่เหยียบเบรก ไม่ใช่เหยียบคันเร่ง**
Gate 1–7 เป็นคณิตศาสตร์ล้วน (deterministic) — ตัดสินใจด้วยตัวเลข
Gate 19 (LLM CIO) เป็นปัญญาเชิงบริบท — มองภาพรวมที่ตัวเลขมองไม่เห็น
แต่ LLM ทำได้แค่ "ลดขนาด" หรือ "ยกเลิก" เท่านั้น — **ห้ามเพิ่ม Risk เด็ดขาด**
สิ่งนี้ Hardcode ไว้ในโค้ด ไม่ใช่ Config ไม่มีทางแก้ด้วย Prompt

**3. เปลี่ยนกองทุนได้ภายในบรรทัดเดียว**
ทุกกฎของ Prop Firm (daily loss, lot size, overnight limit) อยู่ใน Profile JSON
เปลี่ยนจาก TTP เป็น FTMO แค่: `python main.py --profile FTMO_100K`
ไม่ต้องแก้โค้ดแม้แต่ตัวเดียว

**4. สัญญาณที่ดี ควรส่งต่อถึงมือคนที่ต้องการได้ทันที**
Core Engine สร้างสัญญาณ → Signal Bridge กระจายไป Web + LINE + Telegram
VIP เห็น real-time พร้อม LLM analysis, Guest เห็นเฉพาะ delayed — สร้างแรงจูงใจ upgrade
Admin มีปุ่มฉุกเฉินยกเลิกสัญญาณ → แจ้งทุกช่องทางทันที

---

## สถาปัตยกรรมเชิงความคิด (Conceptual Architecture)

```
┌─────────────────────────────────────────────────────────────────────┐
│                     "ห้องประชุมก่อนยิงออเดอร์"                         │
│                                                                     │
│  ข่าว (News)  ─────────────┐                                        │
│  กราฟ 15m (OHLCV)  ────────┤                                        │
│  VIX / SPY (Regime) ───────┤                                        │
│                             ▼                                       │
│  ┌──────────────────────────────────────────┐                       │
│  │     Gate 1–7: "ทีมคณิตศาสตร์"             │                       │
│  │                                          │                       │
│  │  ① Daily Loss Kill-Switch               │                       │
│  │  ② Session Filter + No-Entry Cutoff     │                       │
│  │  ③ Market Sentiment (VIX/SPY)           │                       │
│  │  ④ Price Validation                     │                       │
│  │  ⑤ Universe Filter (Gap% + RVOL)       │                       │
│  │  ⑥ Regime Scorer (Momentum+MeanRev)    │                       │
│  │  ⑦ Risk Manager (ATR SL/TP + Sizing)   │                       │
│  │                                          │                       │
│  │  "ผ่านทุกด่าน = ออเดอร์มีเหตุผลทางตัวเลข"  │                       │
│  └──────────────────────┬───────────────────┘                       │
│                         ▼                                           │
│  ┌──────────────────────────────────────────┐                       │
│  │     Gate ML: "สมองกล"                     │                       │
│  │                                          │                       │
│  │  LightGBM  ── 47 features  ── P(bullish)│                       │
│  │  LSTM      ── 30-bar seq   ── P(bullish)│                       │
│  │  Ensemble  ── 60% LightGBM + 40% LSTM   │                       │
│  │                                          │                       │
│  │  "ราคาจะปิดเหนือ VWAP ตอนจบวันไหม?"       │                       │
│  └──────────────────────┬───────────────────┘                       │
│                         ▼                                           │
│  ┌──────────────────────────────────────────┐                       │
│  │    Gate A–I: "ตำรวจจราจร"                  │                       │
│  │                                          │                       │
│  │  A: Max 3 orders/day                    │                       │
│  │  B: Rate limiter (≥3s between orders)   │                       │
│  │  C: Wash-sale cooldown (5 min/symbol)   │                       │
│  │  D: PDT check (≤4 day-trades / 5 days) │                       │
│  │  E: Cancel ratio (≤60%)                 │                       │
│  │  F: LULD Exchange Halt check            │                       │
│  │  H: No hedge (ห้าม long+short ตัวเดิม)   │                       │
│  │  I: Streak block (แพ้ 2 ไม้ = หยุดวันนี้)  │                       │
│  │                                          │                       │
│  │  "ออเดอร์ไม่ผิดกฎกองทุน"                    │                       │
│  └──────────────────────┬───────────────────┘                       │
│                         ▼                                           │
│  ╔══════════════════════════════════════════╗                       │
│  ║  ★ Gate 19: LLM CIO — "ผู้ว่าการ"       ║                       │
│  ║                                          ║                       │
│  ║  รับข้อมูลทั้งหมดจากทุก Gate              ║                       │
│  ║  วิเคราะห์ "ภาพรวม" ที่ตัวเลขมองไม่เห็น    ║                       │
│  ║                                          ║                       │
│  ║  คำตัดสิน:                                ║                       │
│  ║    EXECUTE → ยิงเต็ม                     ║                       │
│  ║    REDUCE  → ยิงลดขนาด (×0.1–0.9)      ║                       │
│  ║    DELAY   → ข้ามรอบนี้ เช็คใหม่ bar ถัดไป ║                       │
│  ║    ABORT   → ยกเลิกทั้งวัน                ║                       │
│  ║                                          ║                       │
│  ║  ⛔ ห้ามเพิ่ม Risk (Hardcode ≤ 1.0)     ║                       │
│  ║  ⛔ API fail = ABORT ทันที (Fail-safe)   ║                       │
│  ╚══════════════════════╤═══════════════════╝                       │
│                         ▼                                           │
│  ┌──────────────────────────────────────────┐                       │
│  │     Executor: "หัวแปลงคำสั่ง"              │                       │
│  │                                          │                       │
│  │  JSON_DUMP → ไฟล์ .json ให้ OpenClaw อ่าน│                       │
│  │  MT5       → MetaTrader 5 (FTMO)        │                       │
│  │  API_REST  → HTTP REST (Alpaca/Tradovate)│                       │
│  └──────────────────────┬───────────────────┘                       │
│                         │                                           │
│                    ┌────┴────┐                                      │
│                    ▼         ▼                                      │
│  ┌─────────────────────────────────────────────────────────┐       │
│  │     📡 SaaS Signal Distribution Platform               │       │
│  │                                                         │       │
│  │  Signal Bridge → POST /api/signals → Database          │       │
│  │       │                                                 │       │
│  │       ├── 🌐 Web Dashboard (React)                     │       │
│  │       │   ├── VIP: Live Signal Feed + LLM Comment     │       │
│  │       │   ├── Guest: Delayed signals + Performance    │       │
│  │       │   └── Admin: Cancel button + Engine health    │       │
│  │       │                                                 │       │
│  │       ├── 💚 LINE Flex Message (VIP Push)              │       │
│  │       │   └── การ์ดสวย + ปุ่ม "ดูกราฟ"                   │       │
│  │       │                                                 │       │
│  │       └── ✈️  Telegram (Channel Separation)             │       │
│  │           ├── Public: Delayed + Daily Digest           │       │
│  │           └── VIP: Real-time + Full LLM Analysis      │       │
│  └─────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## หลักการทำงานของ ML Pipeline

### สมมติฐานที่ระบบกำลังทดสอบ

> "เมื่อราคาย่อตัวลงมาใกล้เส้น VWAP (Volume Weighted Average Price)
> ในช่วงที่มี News Catalyst บวก ราคามีแนวโน้มจะปิดวันเหนือ VWAP"

นี่คือสมมติฐาน "Pullback to VWAP" ที่ Institutional Traders ใช้จริง:
- VWAP = ราคาเฉลี่ยถ่วงน้ำหนักด้วย Volume = "ราคาที่สถาบันยอมซื้อ"
- เมื่อราคาย่อลงมาแตะ VWAP = โอกาสที่สถาบันจะเข้าซื้อเพิ่ม
- ถ้ามีข่าวดีเป็นตัวจุดชนวน = แนวโน้มวิ่งทั้งวันมีสูงขึ้น

### Label (Y) — สิ่งที่โมเดลเรียนรู้ทำนาย

```
Y = 1  ถ้า Close ของ bar สุดท้ายของวัน > VWAP ของวันนั้น
Y = 0  ถ้า Close ≤ VWAP
```

ไม่ใช่ทำนาย "5 นาทีข้างหน้าจะขึ้นหรือลง" (noise)
แต่ทำนาย "โครงสร้างวันนี้จะเป็นขาขึ้นหรือไม่" (structural)

### Features (X) — ข้อมูลที่โมเดลใช้ตัดสินใจ

47 features แบ่ง 7 กลุ่ม:

| กลุ่ม | จำนวน | ตัวอย่าง | ทำไมถึงสำคัญ |
|---|---|---|---|
| Price Structure | 8 | VWAP deviation, Gap%, Position in range | ราคาอยู่ตรงไหนเมื่อเทียบกับ VWAP |
| Momentum | 8 | RSI-14, MACD, ROC-5 | ทิศทางของแรงส่ง |
| Volatility | 7 | ATR-14, BB width, Historical Vol | ความกว้างของการแกว่ง |
| Volume Flow | 9 | RVOL, CVD proxy, Volume Profile | สถาบันกำลังซื้อหรือขาย |
| Candle Pattern | 6 | Body ratio, Wick %, Engulfing | รูปแบบแท่งเทียน |
| Multi-Timeframe | 5 | 15m vs Daily alignment | สัญญาณ 15m สอดคล้องกับรายวันไหม |
| Context | 2 | Time of day, Catalyst type | บริบทเวลาและข่าว |

### สองสมอง — Ensemble

```
LightGBM (เทรนทุกวัน)          LSTM (เทรนทุกสัปดาห์)
├─ 47 tabular features         ├─ 30 bars × 47 features
├─ ตัดสินใจจาก snapshot         ├─ ตัดสินใจจาก sequence
├─ เร็ว (<1ms inference)       ├─ ช้ากว่า แต่จำ pattern ได้
├─ Explainable (feature imp.)  ├─ ดีกับ temporal dependency
└─ Output: P(bullish)          └─ Output: P(bullish)

Final Score = 60% × LightGBM + 40% × LSTM
```

---

## ความต้องการของระบบ

| Component | สำหรับ Training | สำหรับ Trading |
|---|---|---|
| Machine | Local (GPU) | VPS (CPU OK) |
| GPU | RTX 3090 หรือเทียบเท่า (สำหรับ LSTM) | ไม่จำเป็น |
| RAM | ≥16GB | ≥4GB |
| Python | 3.9+ | 3.9+ |
| OS | Ubuntu 22+ / macOS / Windows | Ubuntu 22+ |
| Internet | จำเป็น (ดึงข้อมูล) | จำเป็น (News + Price) |

---

## Installation

```bash
git clone <repo-url> ttp-trading
cd ttp-trading
python -m venv venv && source venv/bin/activate

# Core dependencies
pip install -r requirements.txt

# PyTorch — GPU (สำหรับ training machine)
pip install torch --index-url https://download.pytorch.org/whl/cu121

# PyTorch — CPU only (สำหรับ VPS / inference)
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### Environment Variables (.env)

```bash
# สร้างไฟล์ .env
cp .env.example .env

# แก้ไขค่า:
ALPACA_PAPER_KEY=pk_xxxxxxxxxxxxxxxx
ALPACA_PAPER_SECRET=xxxxxxxxxxxxxxxx
BENZINGA_API_KEY=xxxxxxxxxxxxxxxx

# LLM Gate 19 (อย่างน้อย 1 ตัว)
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxx     # Claude
OPENAI_API_KEY=sk-xxxxxxxxxx           # GPT-4
GEMINI_API_KEY=xxxxxxxxxx              # Gemini

# LLM Config
LLM_PRIMARY=CLAUDE         # CLAUDE | OPENAI | GEMINI
LLM_FALLBACK=OPENAI        # fallback ถ้า primary fail
LLM_ENABLED=true           # false = bypass Gate 19
```

---

## 🔧 Phase 1: Training Models (ทำบน Local Machine + GPU)

ก่อนที่ `main.py` จะรันเทรดได้ ต้องมีไฟล์ model เหล่านี้:

```
models/
├── NVDA/
│   ├── lgbm_20260319.pkl          ← LightGBM model
│   ├── lstm_20260319.pt           ← LSTM weights
│   ├── lstm_scaler_20260319.pkl   ← LSTM feature scaler
│   └── meta.json                  ← version tracking
├── TSLA/
│   └── ...
├── META/
│   └── ...
```

### Step 1: สร้าง Universe (กรอง ~8,000 หุ้น → ~300 ตัว)

```python
# train_step1_universe.py
import os
os.environ["ALPACA_PAPER_KEY"]    = "pk_..."
os.environ["ALPACA_PAPER_SECRET"] = "..."

from data_pipeline_manager import DataPipelineManager

mgr = DataPipelineManager(mode="local", gdrive_root="./gdrive")
universe = mgr.generate_ttp_universe()
print(f"Universe: {len(universe)} symbols")
# Output: universe.json ถูกสร้างใน ./gdrive/
```

Universe ถูกกรองด้วยเกณฑ์:
- Price ≥ $5 (ไม่ใช่ penny stock)
- Average Daily Volume ≥ 1M shares (มี liquidity)
- Dollar Volume ≥ $5M/วัน (สถาบันเล่น = spread แคบ)
- Active + Marginable (เทรดได้จริง)

### Step 2: Daily Training — LightGBM (.pkl)

```python
# train_step2_lgbm.py
from data_pipeline_manager import DataPipelineManager

mgr = DataPipelineManager(mode="local", gdrive_root="./gdrive")

# วิธีที่ 1: เทรนจาก universe ทั้งหมด
mgr.run_daily_pipeline()
# ดึง OHLCV 15m → คำนวณ 47 features → เทรน LightGBM → save .pkl

# วิธีที่ 2: เทรนเฉพาะ watchlist
mgr.run_daily_pipeline(watchlist=["NVDA", "TSLA", "META", "AAPL"])
```

สิ่งที่เกิดขึ้นภายใน:
1. **Parallel Download** — ดึง OHLCV 15m + Daily ด้วย 20 threads พร้อมกัน
2. **Feature Engineering** — คำนวณ 47 features แบบ vectorized (O(n))
3. **Label Generation** — สร้าง Y = (Close_EOD > VWAP)
4. **Training** — LightGBM + TimeSeriesSplit (5-fold) ป้องกัน data leakage
5. **Save** — `models/{SYMBOL}/lgbm_{DATE}.pkl` + `meta.json`

ใช้เวลา: ~10 วินาทีต่อ symbol (300 symbols ≈ 50 นาที)

### Step 3: Weekly Training — LSTM (.pt)

```python
# train_step3_lstm.py (ต้องมี GPU)
from data_pipeline_manager import DataPipelineManager

mgr = DataPipelineManager(mode="local", gdrive_root="./gdrive")
universe = mgr.load_universe()

mgr.run_weekly_pipeline(watchlist=universe)
# สร้าง sequences (30 bars × 47 features) → เทรน LSTM → save .pt + scaler .pkl
```

LSTM Architecture:
```
Input:  30 × 47 (30 bars ของ 15m × 47 features)
  ↓
LSTM Layer 1: hidden=64, dropout=0.2
  ↓
LSTM Layer 2: hidden=64, dropout=0.2
  ↓
Linear: 64 → 1 (sigmoid)
  ↓
Output: P(Close_EOD > VWAP) ∈ [0, 1]
```

ใช้เวลา: ~2 นาทีต่อ symbol บน RTX 3090 (300 symbols ≈ 10 ชั่วโมง)

### Step 4: ตรวจสอบว่า Models พร้อม

```python
# verify_models.py
from technical_ml_analyzer import TechnicalMLAnalyzer

analyzer = TechnicalMLAnalyzer(model_dir="./models")
stats = analyzer.get_cache_stats()
print(stats)

# ทดสอบ prediction สำหรับ NVDA
import yfinance as yf
df1 = yf.download("NVDA", period="5d", interval="15m", progress=False)
df5 = yf.download("NVDA", period="30d", interval="1d", progress=False)

# normalize columns
for df in [df1, df5]:
    if hasattr(df.columns, 'get_level_values'):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]

pred = analyzer.analyze("NVDA", df1, df5, "EARNINGS", 80, "MARKET")
print(f"ML Score: {pred.ml_score}")
print(f"Direction: {pred.direction_prob:.3f}")
print(f"Signal: {pred.signal}")
print(f"Confidence: {pred.confidence:.2f}")
```

### Step 5: Sync Models ไป VPS (ถ้าเทรดบน VPS)

```bash
# บน Local Machine
rsync -avz ./models/ user@vps:/opt/ttp-trading/models/

# หรือผ่าน Google Drive
# (data_pipeline_manager ทำ sync อัตโนมัติ)
```

---

## 🚀 Phase 2: Running the Trading Engine

### ตรวจสอบก่อนรัน

```
ก่อนรัน main.py ต้องมี:
  ✅ models/{SYMBOL}/lgbm_*.pkl     — จาก Step 2
  ✅ models/{SYMBOL}/lstm_*.pt      — จาก Step 3 (optional)
  ✅ models/{SYMBOL}/meta.json      — สร้างอัตโนมัติ
  ✅ .env มี API keys ครบ
  ✅ LLM API key อย่างน้อย 1 ตัว    — สำหรับ Gate 19
```

### Dry Run — ทดสอบ pipeline ไม่ยิง order จริง

```bash
python main.py --profile TTP_5K_FLEX --dry-run
```

สิ่งที่เกิดขึ้น:
1. โหลด Profile "TTP_5K_FLEX" → ตรวจ Config Validator
2. สร้าง mock news 3 ตัว (NVDA, TSLA, META)
3. วิ่งผ่านทุก Gate (1–7 + ML + A–I + Gate 19)
4. แสดง verdict ของ Gate 19 (EXECUTE/REDUCE/ABORT)
5. ไม่มีการยิง order จริง — แค่ log

### Paper Trade — สัญญาณจริง ไม่ใช้เงินจริง

```bash
# TTP $5K FLEX (US Equities → JSON signals → OpenClaw)
python main.py --profile TTP_5K_FLEX --mode paper

# FTMO $100K (CFD → MetaTrader 5)
python main.py --profile FTMO_100K --mode paper

# Topstep $50K NQ (Futures → REST API → Tradovate)
python main.py --profile TOPSTEP_50K_NQ --mode paper
```

### สิ่งที่ระบบทำทุก 15 นาที

```
⏰ 09:30 ET — ตลาดเปิด
│
├─ 09:30  แท่ง 15m แรกปิด → ตรวจ News Cache → Pipeline
├─ 09:45  แท่ง 15m ที่ 2 ปิด → ตรวจ News Cache → Pipeline
├─ 10:00  แท่ง 15m ที่ 3 ปิด → Pipeline
│  ...
├─ 14:15  แท่งสุดท้ายที่รับ trade ใหม่ได้
├─ 14:30  ⛔ NO NEW ENTRY — ห้ามเปิด trade ใหม่
│         (เหลือ 75 นาที ไม่พอให้ 15m วิ่งจบเทรนด์)
│  ...
├─ 15:45  🛑 FLATTEN ALL — ปิดทุก position
│         (ส่ง flatten JSON → OpenClaw / MT5 / REST)
│
⏰ 16:00 ET — ตลาดปิด
```

### Live Trade — เงินจริง

```bash
python main.py --profile TTP_5K_FLEX --mode live
# พิมพ์ "CONFIRM" เพื่อยืนยัน
```

### Performance Report

```bash
python main.py --profile TTP_5K_FLEX --report
```

---

## Prop Firm Profiles — เปลี่ยนกองทุนบรรทัดเดียว

| Profile | Asset | Execution | Daily Loss | Risk/Trade | Overnight |
|---|---|---|---|---|---|
| `TTP_5K_FLEX` | US Stocks | JSON → OpenClaw | $90 | $15 | ❌ |
| `TTP_80K` | US Stocks | JSON → OpenClaw | $700 | $100 | ❌ |
| `FTMO_100K` | CFD (Forex) | MetaTrader 5 | $5,000 | $1,000 | ✅ |
| `TOPSTEP_50K_NQ` | Futures (NQ) | REST API | $1,000 | $500 | ❌ |

### สร้าง Profile ใหม่

```json
// profiles/my_firm.json
{
  "firm_name": "My Firm",
  "account_label": "$25K Challenge",
  "asset_class": "EQUITIES",
  "execution_method": "API_REST",
  "allow_fractional": false,
  "account_size_usd": 25000,
  "max_daily_loss_usd": 500,
  "risk_per_trade_usd": 50,
  "max_orders_per_day": 3,
  "streak_block": 2,
  "flatten_time_et": [15, 45],
  "no_new_entry_after": [14, 30],
  "contract_multiplier": 1,
  "tick_size": 0.01
}
```

```bash
python -c "
from config import Config
Config.load_profile_from_file('./profiles/my_firm.json')
Config.validate('paper')
print(Config.summary())
"
```

### Config Validator จะจับ Contradiction อัตโนมัติ

```python
# ❌ FUTURES + fractional = True
Config.load_profile_from_dict({
    "asset_class": "FUTURES",
    "allow_fractional": True,  # FUTURES ต้องเป็น integer contracts
    ...
})
# → ConfigValidationError: CONFLICT: asset_class=FUTURES but allow_fractional=True

# ❌ Risk > Daily Loss
Config.load_profile_from_dict({
    "risk_per_trade_usd": 500,
    "max_daily_loss_usd": 100,  # เทรดไม้เดียว blow daily limit
    ...
})
# → ConfigValidationError: risk_per_trade_usd ($500) > max_daily_loss_usd ($100)
```

---

## Gate 19: LLM CIO — การทำงานเชิงลึก

### ทำไมต้องมี LLM ทั้งที่มี Gate 1–7 แล้ว

Gate 1–7 เก่งเรื่องตัวเลข แต่ "ตาบอด" เรื่องบริบท:

| สถานการณ์ | Gate 1–7 มองเห็น | Gate 19 มองเห็น |
|---|---|---|
| NVDA ข่าวดี + VIX ต่ำ | ✅ ผ่านทุกด่าน | ✅ EXECUTE |
| NVDA ข่าวดี + แต่ Fed ประชุมพรุ่งนี้ | ✅ ผ่านทุกด่าน | ⚠️ REDUCE 50% |
| TSLA miss + แต่ sector rotation เข้า EV | ✅ ผ่าน (catalyst = short) | ⏳ DELAY |
| Signal ดี + แต่แพ้ 1 ไม้ + Daily PnL ใกล้ limit | ✅ ผ่าน (ยังไม่ถึง limit) | 🛑 ABORT |

### Prompt ที่ส่งให้ LLM

```json
{
  "trade_proposal": {
    "symbol": "NVDA", "side": "LONG", "shares": 34,
    "entry": 182.30, "stop_loss": 179.56, "take_profit": 187.75
  },
  "market_context": {
    "vwap": 180.50, "price_vs_vwap": 1.01, "atr_15m": 1.82,
    "vix": 15.2, "spy_trend": "up"
  },
  "ml_signal": {
    "score": 76, "direction_prob": 0.73, "confidence": 0.81
  },
  "risk_status": {
    "daily_pnl_usd": -25.0, "remaining_budget": 675.0,
    "trades_today": 1, "consecutive_losses": 0
  },
  "prop_firm_rules": {
    "firm_name": "TTP", "max_daily_loss": 700, "streak_block": 2
  },
  "news_catalyst": "EARNINGS: NVDA beats Q2 EPS by 15%..."
}
```

### Response ที่ได้กลับ (Structured JSON เท่านั้น)

```json
{
  "action": "EXECUTE",
  "sizing_multiplier": 1.0,
  "reasoning": "Strong earnings catalyst aligned with ML signal. VIX low, ample daily budget remaining."
}
```

### Fail-Safe Chain

```
Primary (Claude) ─── timeout/error ───→ Fallback (GPT-4)
                                                │
                                        timeout/error
                                                │
                                                ▼
                                        ABORT ทันที
                                    (Fail-safe hardcode)
```

LLM ตอบ `sizing_multiplier: 1.5` → ระบบ clamp เหลือ `1.0` (Hardcode ในโค้ด)
LLM ตอบไม่ใช่ JSON → ABORT ทันที
LLM ไม่ตอบภายใน 15 วินาที → ABORT ทันที

---

## Position Sizing — ตัวเลขที่ห้ามผิด

### Equities (Stocks)

```
actual_risk = (Entry - SL) + commission×2 + slippage + spread
shares      = floor(risk_budget / actual_risk)

ตัวอย่าง: NVDA entry=$182, SL=$179.30, budget=$100
  raw_risk    = $2.70
  commission  = $0.005 × 2 = $0.01
  slippage    = $182 × 0.1% = $0.182
  spread      = $182 × 0.1% = $0.182
  actual_risk = $2.70 + $0.01 + $0.182 + $0.182 = $3.074
  shares      = floor($100 / $3.074) = 32 shares
  position    = 32 × $182 = $5,824
  max loss    = 32 × $3.074 = $98.37 ← ไม่เกิน $100 budget ✅
```

### CFD (Forex/Indices)

```
lots = risk_budget / (SL_distance × contract_multiplier)
     = floor(result × 100) / 100    ← round down to 0.01 lot

ตัวอย่าง: EURUSD entry=1.08500, SL=1.08320, budget=$500
  SL_distance    = 0.00180
  contract_mult  = 100,000
  risk_per_lot   = 0.00180 × 100,000 = $180
  lots           = floor($500 / $180 × 100) / 100 = 2.77 lots
  max loss       = 2.77 × $180 = $498.60 ← ไม่เกิน $500 ✅
```

### Futures (NQ/ES)

```
contracts = floor(risk_budget / (SL_distance × contract_multiplier))

ตัวอย่าง: NQ Micro entry=20500, SL=20447.50, budget=$500
  SL_distance   = 52.50 points
  contract_mult = $20/point (NQ Micro)
  risk_per_cont = 52.50 × $20 = $1,050
  contracts     = floor($500 / $1,050) = 0 ← ❌ REJECTED (over-leverage)

  ถ้า budget=$1,500:
  contracts     = floor($1,500 / $1,050) = 1 contract
  max loss      = 1 × $1,050 = $1,050 ← ≤ $1,500 ✅
```

---

## โครงสร้างไฟล์

```
ttp-trading/
│
├── config.py                      ← Profile Loader + Validator + LLM Config
├── gate_19_llm_cio.py             ← LLM CIO (Claude/GPT-4/Gemini switchable)
├── main.py                        ← 15m Event-Driven Orchestrator
├── universal_risk_manager.py      ← Multi-asset Sizing (Shares/Lots/Contracts)
├── universal_order_executor.py    ← 3 Adapters (JSON/MT5/REST) + Retry
├── universe_preprocessor.py       ← Gap% + RVOL + Futures Bypass
│
├── news_scanner.py                ← News Cache + Pre-market Focus
├── regime_scorer.py               ← VIX/SPY Regime Detection
├── technical_ml_analyzer.py       ← LightGBM + LSTM Ensemble
├── data_pipeline_manager.py       ← 15m Data + VWAP/ATR Features
├── trade_journal.py               ← P&L + Session-based Streak Tracking
│
├── platform/                      ← ★ SaaS Signal Distribution
│   ├── signal_api.py              ← FastAPI Gateway (JWT + Rate Limiting)
│   ├── models.py                  ← Database Schema (SQLAlchemy + Alembic)
│   ├── signal_bridge.py           ← Core Engine → API bridge (retry + failed recovery)
│   ├── notifier_line.py           ← LINE Flex Message broadcaster
│   ├── notifier_telegram.py       ← Telegram Bot (VIP/Public channels)
│   └── requirements.txt           ← Platform dependencies
│
├── deploy/                        ← ★ Production Deployment
│   ├── ecosystem.config.js        ← PM2 config (3 processes + auto-restart)
│   ├── quant-engine.service       ← systemd service (Engine)
│   ├── signal-api.service         ← systemd service (API)
│   ├── signal-bridge.service      ← systemd service (Bridge)
│   ├── setup.sh                   ← One-command production setup
│   ├── init_alembic.py            ← Alembic migration initializer
│   └── log_archiver.py            ← Log rotation → GDrive archival
│
├── requirements.txt
├── .env                           ← API Keys (ห้าม commit)
│
├── signals/                       ← JSON signals สำหรับ OpenClaw + Bridge
├── journal/                       ← trades.csv, daily_summary.csv
├── models/                        ← .pkl, .pt, meta.json
└── profiles/                      ← Custom firm JSON profiles
```

---

## Training Schedule (Production)

| งาน | เวลา (ET) | เวลา (ไทย) | เครื่อง | Output |
|---|---|---|---|---|
| Universe Refresh | Sun 5:00 AM | อา. 17:00 | Local | `universe.json` |
| LSTM Weekly Train | Sun 7:00 AM | อา. 19:00 | Local (GPU) | `lstm_*.pt` |
| LightGBM Daily Train | Daily 8:00 AM | ทุกวัน 20:00 | Local | `lgbm_*.pkl` |
| Sync Models → VPS | Daily 8:30 AM | ทุกวัน 20:30 | Local → VPS | rsync |
| Trading Session Start | Daily 9:30 AM | ทุกวัน 21:30 | VPS | signals/ |
| No New Entry | Daily 2:30 PM | ทุกวัน 02:30+1 | VPS | — |
| Flatten All | Daily 3:45 PM | ทุกวัน 03:45+1 | VPS | flatten.json |

---

## 📡 Phase 3: SaaS Signal Distribution Platform

ระบบกระจายสัญญาณผ่าน Web Dashboard, LINE, Telegram สำหรับให้บริการแบบ Subscription

### Data Flow ภาพรวม

```
Core Engine (19 Gates + LLM)
  │
  ▼ JSON file → ./signals/
  │
Signal Bridge (file watcher daemon)
  │
  ▼ POST /api/signals
  │
FastAPI Gateway (signal_api.py)
  │
  ├── SQLite Database (signals, users, subscriptions)
  │
  ├── 🌐 Web Dashboard (React)
  │     ├── VIP:   Live Signal Cards + LLM Comment + TradingView
  │     ├── Guest: Delayed signals (1 ชม.) + Performance page
  │     └── Admin: Cancel button + Engine health + User management
  │
  ├── 💚 LINE Flex Message
  │     ├── VIP Broadcast: การ์ดสัญญาณ real-time (สวยงาม)
  │     ├── Cancel Alert: การ์ดแถบเหลือง "ถูกยกเลิก"
  │     └── Rich Menu: สถิติ / VIP ของฉัน / ติดต่อแอดมิน
  │
  └── ✈️ Telegram Bot
        ├── Private VIP Channel: Real-time + Full LLM Analysis
        ├── Public Channel: Delayed 1 ชม. + Daily Digest (เหยื่อล่อ)
        └── Bot Commands: /start, /status, /stats
```

### Installation (Platform)

```bash
# ติดตั้ง dependencies เพิ่มเติม
pip install -r platform/requirements.txt

# ตั้ง env เพิ่ม (ใน .env)
SIGNAL_API_SECRET=your-secret-key-here
ADMIN_API_KEY=your-admin-key-here
DASHBOARD_URL=https://your-domain.com

# LINE
LINE_CHANNEL_ACCESS_TOKEN=xxx    # จาก developers.line.biz

# Telegram
TELEGRAM_BOT_TOKEN=xxx           # จาก @BotFather
TELEGRAM_VIP_CHAT_ID=-100xxx     # Private channel ID
TELEGRAM_PUBLIC_CHAT_ID=-100xxx  # Public channel ID
```

### Running — 3 Processes ทำงานพร้อมกัน

```bash
# Terminal 1: API Gateway
uvicorn platform.signal_api:app --host 0.0.0.0 --port 8000

# Terminal 2: Signal Bridge (เฝ้าดู JSON จาก Core Engine)
python -m platform.signal_bridge --watch ./signals/ --api-url http://localhost:8000

# Terminal 3: Core Engine (สร้างสัญญาณ)
python main.py --profile TTP_5K_FLEX --mode paper
```

เมื่อ Core Engine สร้าง JSON signal → Bridge อ่าน → POST ไป API → ส่ง LINE + Telegram ทันที

### API Endpoints

| Method | Path | Auth | คำอธิบาย |
|---|---|---|---|
| `POST` | `/api/signals` | API Key | รับสัญญาณจาก Core Engine |
| `GET` | `/api/signals` | — | ดึงสัญญาณ (Guest=delayed, VIP=live) |
| `GET` | `/api/signals/{id}` | — | ดึงสัญญาณเดี่ยว |
| `PUT` | `/api/signals/{id}` | API Key | อัปเดต status (WON/LOST/CANCELLED) |
| `GET` | `/api/performance` | — | สถิติรวม (public) |
| `POST` | `/api/auth/register` | — | สมัครสมาชิก |
| `POST` | `/api/auth/login` | — | ล็อกอิน |
| `POST` | `/admin/cancel/{id}` | Admin Key | ยกเลิกสัญญาณ + แจ้ง LINE/Telegram |
| `GET` | `/admin/stats` | Admin Key | MRR + active users + engine health |
| `GET` | `/admin/engine-status` | Admin Key | เช็คว่าบอทยังทำงานอยู่ |
| `POST` | `/webhook/line` | — | LINE webhook callback |
| `POST` | `/webhook/telegram` | — | Telegram webhook callback |

### Database Schema

```
signals
├── signal_id (UUID)          — unique identifier
├── asset, timeframe, action  — NVDA, 15m, BUY
├── entry_low, entry_high     — Entry range
├── take_profit, stop_loss    — TP/SL prices
├── risk_reward               — "1:2.5"
├── technical_summary         — VWAP, ATR สรุป
├── news_catalyst             — ข่าวที่เป็นตัวขับเคลื่อน
├── strategy_type             — Trend Following / Mean Reversion
├── llm_cio_comment           — บทวิเคราะห์จาก Gate 19
├── status                    — ACTIVE / WON / LOST / CANCELLED
├── ml_score, confidence      — ML metrics
└── pnl_usd, pnl_pct         — ผลลัพธ์ (หลังปิด trade)

users
├── email, display_name, role — GUEST / VIP / ADMIN
├── line_user_id              — เชื่อมกับ LINE
├── telegram_chat_id          — เชื่อมกับ Telegram
└── vip_expires_at            — วันหมดอายุ VIP

subscriptions
├── user_id, plan             — monthly / yearly
├── amount_thb                — จำนวนเงิน
├── payment_ref               — Stripe/Omise reference
└── status                    — ACTIVE / EXPIRED
```

### LINE Flex Message — ตัวอย่าง Signal Card

```
┌─────────────────────────────────────┐
│ 🟢 LONG (ซื้อ)           NVDA      │  ← Header สีเขียว
├─────────────────────────────────────┤
│ 📍 Entry    $182.00 – $182.50      │
│ 🎯 TP       $187.75  (+3.1%)      │  ← ราคาตัวใหญ่ชัดเจน
│ 🛡️ SL       $179.56  (-1.3%)      │
│ ⚖️ R:R      1:2.5                  │
├─────────────────────────────────────┤
│ 🏄 กลยุทธ์: ตามน้ำ (Trend)          │
│ 📰 NVDA beats Q2 EPS by 15%...    │  ← Catalyst
│ 🧠 ML: 76 | Conf: 81%            │
├─────────────────────────────────────┤
│ 🤖 AI CIO Analysis:                │
│ "Strong earnings catalyst aligned  │  ← LLM Comment (VIP only)
│  with VWAP pullback. Low VIX."     │
├─────────────────────────────────────┤
│  [ 📊 ดูกราฟ ]   [ 📋 Dashboard ] │  ← ปุ่มลิงก์ไปเว็บ
└─────────────────────────────────────┘
```

### Telegram Message — ตัวอย่าง VIP vs Public

**VIP (Real-time + Full LLM):**
```
🟢 LONG NVDA — 15m
━━━━━━━━━━━━━━━━━━
📍 Entry:  $182.00 – $182.50
🎯 TP:     $187.75 (+3.1%)
🛡️ SL:     $179.56 (-1.3%)
⚖️ R:R:    1:2.5
━━━━━━━━━━━━━━━━━━
🏄 Trend Following
📰 NVDA beats Q2 EPS by 15%...
🧠 ML: 76 | Conf: 81%
━━━━━━━━━━━━━━━━━━
🤖 AI CIO:
"Strong earnings catalyst aligned..."
```

**Public (Delayed 1 ชม., ไม่มี LLM):**
```
🟢 LONG NVDA — 15m
⏰ (สัญญาณดีเลย์ — สมัคร VIP เพื่อรับ Real-time)
━━━━━━━━━━━━━━━━━━
📍 Entry:  $182.00 – $182.50
🎯 TP:     $187.75
🛡️ SL:     $179.56
...
```

### Web Dashboard — 3 หน้าหลัก

**1. Live Signal Feed (VIP)**
- การ์ดสัญญาณเรียงตามเวลา พร้อม Entry/TP/SL, ML Score
- กล่อง "AI CIO Analysis" แสดง LLM comment (VIP only)
- Guest เห็น banner "สมัคร VIP" + เห็นเฉพาะ closed signals

**2. Performance & Backtest (Public)**
- Stat cards: Win Rate, Profit Factor, Max Drawdown, Total P&L
- Equity Curve chart (Recharts)
- ตาราง closed signals พร้อม status WON/LOST

**3. Admin Control Panel**
- Engine Status: เช็คว่าบอทส่ง signal ล่าสุดกี่นาทีที่แล้ว
- Active Signals: ปุ่ม 🚨 CANCEL สำหรับยกเลิก (ยิง LINE/TG แจ้งทันที)
- User Management: ตาราง users + role + VIP expiry
- MRR: รายได้รายเดือน (จำนวน VIP × ราคา subscription)

### Admin Manual Override — ปุ่มฉุกเฉิน

เมื่อกด Cancel บน Admin Panel:

```
Admin กด "CANCEL" บน Dashboard
  │
  ▼ POST /admin/cancel/{signal_id}
  │
  ├── DB: status → "CANCELLED"
  │
  ├── LINE: ส่ง Flex Message แถบเหลือง "ถูกยกเลิก — ปิดสถานะทันที"
  │
  └── Telegram: ส่ง HTML message "⚠️ CANCELLED — ปิดสถานะทันที"
```

### User Roles & Access

| Feature | Guest (ฟรี) | VIP (฿999/เดือน) | Admin |
|---|---|---|---|
| Performance page | ✅ | ✅ | ✅ |
| Delayed signals (1 ชม.) | ✅ | ✅ | ✅ |
| Live signals (real-time) | ❌ | ✅ | ✅ |
| LLM CIO comment | ❌ | ✅ | ✅ |
| LINE Flex push | ❌ | ✅ | ✅ |
| Telegram VIP channel | ❌ | ✅ | ✅ |
| Admin panel | ❌ | ❌ | ✅ |
| Cancel signal | ❌ | ❌ | ✅ |

### Security & Operations Notes

**1. MT5 on Linux — Platform Limitation**

MetaTrader5 Python library รองรับ **Windows เท่านั้น** (ข้อจำกัดของ MetaQuotes)
ถ้ารันบน Linux/macOS ระบบจะแจ้ง warning และ MT5 adapter จะ return failure

| วิธีแก้ | คำอธิบาย |
|---|---|
| JSON_DUMP + OpenClaw/MT5 EA | เขียน JSON → ให้ MT5 Expert Advisor อ่าน (แนะนำ) |
| WINE + REST API wrapper | รัน MT5 Terminal ผ่าน WINE + ใช้ mt5-rest คั่นกลาง |
| Windows VPS | เช่า Windows VPS (Contabo ~$8/เดือน) สำหรับ FTMO |

ระบบตรวจ OS อัตโนมัติ — ถ้าไม่ใช่ Windows จะ log คำแนะนำทั้ง 3 วิธี

**2. API Authentication (JWT)**

ระบบใช้ **HMAC-SHA256 JWT** สำหรับ authentication (ไม่ใช่ mock token)

```
POST /api/auth/login → JWT token (72 ชั่วโมง)
GET  /api/signals    → ส่ง Authorization: Bearer <token>
                        VIP token → เห็น live signals
                        ไม่มี token → เห็นเฉพาะ delayed
GET  /api/users/me   → ต้องมี valid JWT
```

Production checklist:
- ตั้ง `JWT_SECRET` ใน `.env` (ห้ามใช้ default)
- ตั้ง `SIGNAL_API_SECRET` ให้เป็น random string ยาว
- ตั้ง `ADMIN_API_KEY` แยกจาก API secret

**3. Rate Limiting (DDoS Protection)**

ระบบมี in-memory rate limiter: **60 requests/minute per IP** (ปรับได้ผ่าน env)

```bash
RATE_LIMIT_PER_MINUTE=60    # default
```

ถ้าเกิน → HTTP 429 Too Many Requests พร้อม `retry_after_sec: 60`

Production: ใช้ Redis + `slowapi` library แทน in-memory สำหรับ multi-worker setup

**4. Signal Bridge Fault Tolerance**

```
ส่งสำเร็จ    → ย้ายไป processed/
ส่งไม่สำเร็จ → retry 3 ครั้ง (ห่างกัน 2 วินาที)
              → ยังไม่สำเร็จ → ย้ายไป failed/

Bridge restart → กวาด failed/ อัตโนมัติ
              → ไฟล์อายุ < 6 ชั่วโมง → retry ส่งอีกรอบ
              → ไฟล์อายุ > 6 ชั่วโมง → ลบทิ้ง (signal หมดอายุ)
```

ไม่มีสัญญาณหาย — ทุกไฟล์จะอยู่ใน processed/ หรือ failed/ เสมอ

**5. Database Migration (Alembic)**

เมื่อต้องเปลี่ยน Schema (เพิ่มคอลัมน์, เปลี่ยน type):

```bash
# ติดตั้ง (ครั้งแรก)
pip install alembic
cd platform
alembic init migrations

# แก้ migrations/env.py:
#   from models import Base
#   target_metadata = Base.metadata

# ใช้งาน (ทุกครั้งที่แก้ models.py)
alembic revision --autogenerate -m "add xyz column"
alembic upgrade head

# Rollback
alembic downgrade -1
```

`create_all()` ยังใช้ได้สำหรับ DB ใหม่ แต่ Alembic จำเป็นเมื่อแก้ตารางที่มีข้อมูลอยู่แล้ว

**6. Process Management (PM2 / systemd)**

⚠️ **ห้ามรัน `python main.py` ใน terminal ธรรมดา / screen / tmux** — ถ้า crash หรือ memory leak ระบบจะตายถาวร

**วิธีที่ 1: PM2 (แนะนำ)**

```bash
# ติดตั้ง
npm install -g pm2

# เริ่มทุก process (Engine + API + Bridge)
pm2 start ecosystem.config.js

# ดูสถานะ
pm2 status

# ดู log real-time
pm2 logs quant-engine --lines 50

# ตั้งให้ start ตอน boot
pm2 save && pm2 startup
```

PM2 ทำให้:
- ✅ Auto-restart เมื่อ crash (max 10 ครั้ง / 15 นาที)
- ✅ Memory limit restart (Engine: 1GB, API: 512MB, Bridge: 256MB)
- ✅ Cron restart ทุกเช้า 08:00 ET (clean state)
- ✅ Log management + monitoring dashboard (`pm2 monit`)

**วิธีที่ 2: systemd (ไม่ต้องติดตั้ง Node.js)**

```bash
# Copy service files
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + Start
sudo systemctl enable quant-engine signal-api signal-bridge
sudo systemctl start quant-engine signal-api signal-bridge

# ดูสถานะ
sudo systemctl status quant-engine
sudo journalctl -u quant-engine -f    # log real-time
```

**One-command setup:**

```bash
sudo chmod +x deploy/setup.sh
sudo ./deploy/setup.sh
```

Script นี้: สร้าง user `trader` → สร้าง directories → ติดตั้ง PM2/systemd → ตั้ง cron log archival

**7. Log Rotation + Google Drive Archival**

ระบบใช้ `RotatingFileHandler` ตัดไฟล์ log อัตโนมัติ:

```
logs/
├── trading.log         ← ปัจจุบัน (max 10MB)
├── trading.log.1       ← ก่อนหน้า
├── trading.log.2
├── trading.log.3       ← ถูก archive ไป GDrive
├── trading.log.4       ← ถูก archive ไป GDrive
└── trading.log.5       ← ถูก archive ไป GDrive
```

Local เก็บ 3 ไฟล์ล่าสุด (30MB) — ที่เหลือ archive ไป Google Drive (2TB)

```bash
# Archive ทันที
python deploy/log_archiver.py

# ดูสถานะ
python deploy/log_archiver.py --status

# ลบ archive เก่าเกิน 180 วัน
python deploy/log_archiver.py --cleanup-gdrive

# Dry run (ดูว่าจะทำอะไร ไม่ย้ายจริง)
python deploy/log_archiver.py --dry-run
```

Cron (ตั้งอัตโนมัติโดย `deploy/setup.sh`):

```
30 4 * * * python deploy/log_archiver.py --cleanup-gdrive
```

ไฟล์ archive บน GDrive:
```
gdrive/logs/
├── 2026-03/
│   ├── trading.log.3_20260315.gz
│   ├── trading.log.4_20260315.gz
│   ├── api.log.3_20260318.gz
│   └── ...
├── 2026-04/
│   └── ...
```

### Production Deployment Architecture

```
                    ┌─────────────────────┐
                    │   Cloudflare (CDN)  │
                    └──────┬──────────────┘
                           ▼
              ┌───────────────────────────┐
              │  VPS #1: Web + API        │
              │  ├── Nginx (reverse proxy)│
              │  ├── uvicorn (FastAPI)    │
              │  ├── React Dashboard      │
              │  └── SQLite → PostgreSQL  │
              └───────────┬───────────────┘
                          │ internal network
              ┌───────────┴───────────────┐
              │  VPS #2: Trading Engine   │
              │  ├── main.py (15m loop)   │
              │  ├── Signal Bridge        │
              │  └── ./signals/ → API     │
              └───────────────────────────┘
```

ค่าใช้จ่ายเพิ่มเติม:
| บริการ | ราคา/เดือน |
|---|---|
| VPS #1 (API + Web) | ~$6 (Hetzner CX21) |
| Domain + SSL | ~$1 (Cloudflare free) |
| LINE Messaging API | ฟรี (≤500 msg/เดือน) หรือ ฿0.075/msg |
| Telegram Bot | ฟรี |
| Stripe/Omise | 3.65% per transaction |

---

## Troubleshooting

**"No model found for NVDA"**
→ ยังไม่ได้ train. รัน Step 2 (LightGBM) ก่อน. ระบบจะใช้ Regime Score อย่างเดียว (ไม่ block)

**"Gate 19 ABORT: All LLM providers failed"**
→ ตรวจ API key ใน `.env`. ตรวจ internet. ถ้าต้องการ bypass: `LLM_ENABLED=false`

**"ConfigValidationError: FUTURES + fractional"**
→ Profile JSON ขัดแย้ง. แก้ `allow_fractional: false` สำหรับ FUTURES

**"Risk REJECTED: size = 0"**
→ ATR กว้างเกินจน risk/unit > budget. เพิ่ม `risk_per_trade_usd` หรือลด `atr_stop_mult`

**Timezone ผิด**
→ ต้องใช้ Python ≥ 3.9 (มี `zoneinfo` ใน stdlib). ตรวจ: `python -c "from zoneinfo import ZoneInfo; print('OK')"`

**"LINE push failed 401"**
→ LINE_CHANNEL_ACCESS_TOKEN ผิดหรือหมดอายุ. ไปสร้างใหม่ที่ developers.line.biz

**"Telegram send error 403: bot was blocked"**
→ User บล็อกบอท. ลบ user ออกจาก VIP list

**"Signal Bridge: Connection refused"**
→ API Gateway ยังไม่ start. รัน `uvicorn platform.signal_api:app --port 8000` ก่อน

**"Admin: Engine status STALE"**
→ Core Engine หยุดส่ง signal เกิน 60 นาที. ตรวจว่า `main.py` ยังทำงานอยู่ + ดู `trading.log`

**Dashboard ไม่แสดง signals**
→ ตรวจ CORS: API ต้องตั้ง `allow_origins=["*"]` (dev) หรือ domain จริง (prod)

---

## License & Disclaimer

ระบบนี้สร้างขึ้นเพื่อการศึกษาและทดสอบ Strategy
การเทรดด้วยเงินจริงมีความเสี่ยง — ผลการทดสอบในอดีตไม่การันตีผลลัพธ์ในอนาคต
ผู้ใช้รับความเสี่ยงเอง
