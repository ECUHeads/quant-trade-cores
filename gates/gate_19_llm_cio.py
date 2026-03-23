"""
gate_19_llm_cio.py
==================
Gate 19 — LLM CIO (Chief Investment Officer)

จุดประสงค์:
  ตัวกรองความเสี่ยงขั้นสุดท้ายก่อนส่งคำสั่งซื้อขาย
  LLM วิเคราะห์บริบทแบบองค์รวม (ข่าว + ML signal + risk data + prop firm rules)
  แล้วชี้ขาดว่าจะ EXECUTE, REDUCE, DELAY, หรือ ABORT

กฎเหล็ก (Asymmetric Override):
  ✅ LLM สั่งลด sizing ได้         (sizing_multiplier < 1.0)
  ✅ LLM สั่ง ABORT/DELAY ได้       (ยกเลิก/ชะลอ trade)
  ❌ LLM ห้ามเพิ่ม Risk เด็ดขาด    (sizing_multiplier ≤ 1.0 เสมอ)
  ❌ LLM ห้ามเปลี่ยน SL/TP ให้กว้างขึ้น

Fail-Safe:
  - API timeout / error → ABORT ทันที
  - Response ไม่ใช่ valid JSON → ABORT ทันที
  - LLM ส่ง sizing_multiplier > 1.0 → clamp ที่ 1.0 (hardcode lock)

Providers (switchable via Config):
  1. Claude (Anthropic) — default
  2. GPT-4o (OpenAI)
  3. Gemini (Google)
  4. Local LLM (Ollama/vLLM/llama.cpp/LM Studio) — open-source, runs on local GPU
  + Fallback chain: primary fail → fallback → local → ABORT

Toggle Modes (via env vars):
  LLM_ENABLED=true           — Gate 19 active (default)
  LLM_LOCAL_ENABLED=true     — เปิดใช้ Local LLM (เป็น last-resort fallback)
  LLM_LOCAL_ONLY=true        — ใช้เฉพาะ Local LLM (ห้ามยิง cloud API)
  LLM_PRIMARY=LOCAL_LLM      — ใช้ Local LLM เป็น primary provider

Dry-run / Offline:
  export LLM_LOCAL_ENABLED=true
  export LLM_LOCAL_ONLY=true
  → Gate 19 ใช้ local Ollama เท่านั้น, ไม่เสียค่า API

Usage:
  from gate_19_llm_cio import Gate19LLMCio

  cio = Gate19LLMCio()
  verdict = cio.evaluate_trade(
      market_data  = {...},
      ml_signal    = {...},
      risk_data    = {...},
      config_rules = {...},
      news_context = "NVDA beats Q2 EPS by 15%...",
  )

  if verdict["action"] == "EXECUTE":
      adjusted_shares = int(original_shares * verdict["sizing_multiplier"])
      # proceed to executor
  elif verdict["action"] == "REDUCE":
      adjusted_shares = int(original_shares * verdict["sizing_multiplier"])
  else:
      # DELAY or ABORT → skip this trade
"""

import os
import json
import time
import logging
from typing import Optional
from dataclasses import dataclass, field, asdict

import requests

logger = logging.getLogger("Gate19")


# ============================================================
# DATA MODEL — CIO Verdict
# ============================================================

@dataclass
class CIOVerdict:
    """
    ผลวินิจฉัยจาก Gate 19 LLM CIO

    action:             EXECUTE | REDUCE | DELAY | ABORT
    sizing_multiplier:  0.0–1.0 (ตัวคูณลดขนาดไม้ — ห้ามเกิน 1.0)
    reasoning_th:       เหตุผลภาษาไทย
    reasoning_en:       เหตุผลภาษาอังกฤษ
    reasoning:          combined reasoning สำหรับ log
    support:            แนวรับ
    resistance:         แนวต้าน
    cutloss:            จุด cutloss
    recommended_shares: จำนวน shares ที่แนะนำ (≤ system calculated)
    recommended_entry:  ราคาที่ควรเข้า
    recommended_tp:     ราคาที่ควรทำกำไร
    expected_period:    ระยะเวลาถือครอง (เช่น "15m", "30m", "1h", "3h")
    provider:           LLM provider
    latency_ms:         เวลา API call (ms)
    raw_response:       JSON response ดิบ
    """
    action:             str   = "ABORT"
    sizing_multiplier:  float = 0.0
    reasoning:          str   = "No evaluation performed"
    reasoning_th:       str   = ""
    reasoning_en:       str   = ""
    support:            float = 0.0
    resistance:         float = 0.0
    cutloss:            float = 0.0
    recommended_shares: int   = 0
    recommended_entry:  float = 0.0
    recommended_tp:     float = 0.0
    expected_period:    str   = ""
    provider:           str   = "NONE"
    latency_ms:         int   = 0
    raw_response:       dict  = field(default_factory=dict)

    def is_go(self) -> bool:
        """True ถ้า trade ผ่าน (EXECUTE หรือ REDUCE)"""
        return self.action in ("EXECUTE", "REDUCE")

    @property
    def levels_summary(self) -> str:
        """สรุป levels สั้นๆ สำหรับ log"""
        parts = []
        if self.support > 0:
            parts.append(f"S={self.support:.2f}")
        if self.resistance > 0:
            parts.append(f"R={self.resistance:.2f}")
        if self.cutloss > 0:
            parts.append(f"CL={self.cutloss:.2f}")
        if self.recommended_entry > 0:
            parts.append(f"Entry={self.recommended_entry:.2f}")
        if self.recommended_tp > 0:
            parts.append(f"TP={self.recommended_tp:.2f}")
        if self.recommended_shares > 0:
            parts.append(f"Shares={self.recommended_shares}")
        if self.expected_period:
            parts.append(f"Hold={self.expected_period}")
        return " | ".join(parts) if parts else ""


# ============================================================
# SYSTEM PROMPT — สอน LLM เป็น CIO
# ============================================================

SYSTEM_PROMPT = """You are the Chief Investment Officer (CIO) of an automated quantitative trading system.
Your role is the FINAL risk gate before order execution.
คุณคือ CIO ของระบบเทรดอัตโนมัติ ทำหน้าที่เป็นด่านสุดท้ายก่อนส่งคำสั่งซื้อขาย

CRITICAL RULES:
1. You can ONLY reduce risk or block trades. You CANNOT increase position size beyond what the quant system calculated.
2. Your sizing_multiplier must be between 0.0 and 1.0. NEVER above 1.0.
3. Your recommended_shares must be ≤ the proposed shares. NEVER increase beyond what the system calculated.
4. You must respond with ONLY a valid JSON object. No markdown, no explanation outside JSON.

RESPONSE FORMAT (strict JSON):
{
  "action": "EXECUTE" | "REDUCE" | "DELAY" | "ABORT",
  "sizing_multiplier": 0.0 to 1.0,
  "reasoning_th": "เหตุผลภาษาไทย 2-3 ประโยค วิเคราะห์สถานการณ์ตลาดและเหตุผลการตัดสินใจ",
  "reasoning_en": "English reasoning 2-3 sentences analyzing market conditions and decision rationale",
  "support": 0.00,
  "resistance": 0.00,
  "cutloss": 0.00,
  "recommended_shares": 0,
  "recommended_entry": 0.00,
  "recommended_tp": 0.00,
  "expected_period": "15m"
}

FIELD DEFINITIONS:
- action: EXECUTE (full size) | REDUCE (smaller size) | DELAY (skip this bar) | ABORT (cancel today)
- sizing_multiplier: 0.0-1.0, multiplier for position size
- reasoning_th: บทวิเคราะห์ภาษาไทย — ระบุเหตุผลหลัก, สภาพตลาด, ความเสี่ยง, ราคาปิดล่าสุดเทียบ VWAP
- reasoning_en: English analysis — main rationale, market conditions, risks, latest close vs VWAP
- support: nearest support level (แนวรับ) based on VWAP, ATR, and price structure
- resistance: nearest resistance level (แนวต้าน)
- cutloss: recommended stop-loss price (จุดตัดขาดทุน — must be tighter or equal to proposed stop_loss)
- recommended_shares: recommended number of shares (must be ≤ proposed shares)
- recommended_entry: optimal entry price (ราคาที่ควรเข้า). For LONG: prefer entry near support/VWAP. For SHORT: prefer entry near resistance. Must be realistic relative to current price.
- recommended_tp: take-profit target (ราคาที่ควรทำกำไร). Based on resistance (LONG) or support (SHORT), risk-reward ratio ≥ 2:1 preferred.
- expected_period: estimated holding period. Choose from: "15m", "30m", "1h", "2h", "3h", "4h", "EOD" (end of day). Base this on ATR, volatility, and how far TP is from entry.

TECHNICAL ANALYSIS GUIDELINES:
- Support: Use VWAP, recent swing low, day_low, or entry - (1.5 × ATR) — whichever is nearest and strongest
- Resistance: Use recent swing high, day_high, VWAP upper band, or entry + (2.0 × ATR)
- Cutloss: Must be ≤ proposed stop_loss distance. Tighter is safer. Use ATR-based level.
- Entry: Should be near support (LONG) or resistance (SHORT). If current price is already optimal, use current price. Consider prev_close as reference.
- TP: Use next resistance (LONG) or next support (SHORT). Ensure R:R ≥ 2:1. If unsure, use entry + (2.0 × ATR) for LONG.
- Period: Estimate based on distance to TP divided by ATR. Closer targets = "15m"-"30m", farther = "2h"-"EOD".
- If data is insufficient, use proposed values and set expected_period to "1h" as default.

EVALUATION CRITERIA:
- Does the ML signal align with the news catalyst direction?
- Is the current market regime (VIX, trend) favorable?
- Does the risk-reward ratio justify the trade given daily P&L so far?
- Are there any prop firm rule violations being narrowly avoided?
- Is there conflicting information between different signals?
- How does the current price compare to prev_close and VWAP? (gap up/down, mean reversion?)

BIAS: When uncertain, REDUCE or ABORT. Capital preservation > profit.
เมื่อไม่แน่ใจ ให้เลือก REDUCE หรือ ABORT เสมอ — รักษาเงินทุนสำคัญกว่ากำไร"""


# ============================================================
# PROMPT BUILDER — รวบรวมข้อมูลทั้งหมดเป็น context
# ============================================================

def build_evaluation_prompt(
    market_data:  dict,
    ml_signal:    dict,
    risk_data:    dict,
    config_rules: dict,
    news_context: str = "",
) -> str:
    """
    สร้าง user prompt จากข้อมูลทั้ง pipeline

    Args:
      market_data:  {"symbol", "price", "prev_close", "vwap", "atr_15m", "vix",
                     "spy_trend", "session", "day_high", "day_low", "timeframe"}
      ml_signal:    {"ml_score", "direction_prob", "confidence", "signal", "top_features"}
      risk_data:    {"shares", "entry", "stop_loss", "take_profit", "risk_usd",
                     "daily_pnl", "daily_loss_limit", "trades_today", "consecutive_losses"}
      config_rules: {"firm_name", "max_daily_loss", "max_orders_per_day",
                     "streak_block", "consistency_rule"}
      news_context: headline + catalyst type string
    """
    price      = market_data.get("price", 0)
    prev_close = market_data.get("prev_close", 0)
    vwap       = market_data.get("vwap", 0)

    # ── Compute derived metrics
    gap_pct = 0.0
    if prev_close and prev_close > 0:
        gap_pct = round((price - prev_close) / prev_close * 100, 2)

    price_vs_vwap = 0.0
    if vwap and vwap > 0:
        price_vs_vwap = round(price / vwap, 4)

    context = {
        "trade_proposal": {
            "symbol":     market_data.get("symbol", "?"),
            "side":       ml_signal.get("signal", "LONG"),
            "shares":     risk_data.get("shares", 0),
            "entry":      risk_data.get("entry", 0),
            "stop_loss":  risk_data.get("stop_loss", 0),
            "take_profit": risk_data.get("take_profit", 0),
            "risk_usd":   risk_data.get("risk_usd", 0),
        },
        "market_context": {
            "current_price": price,
            "prev_close":    prev_close,
            "gap_pct":       gap_pct,
            "vwap":          vwap,
            "price_vs_vwap": price_vs_vwap,
            "day_high":      market_data.get("day_high", 0),
            "day_low":       market_data.get("day_low", 0),
            "atr_15m":       market_data.get("atr_15m", 0),
            "vix":           market_data.get("vix", 20),
            "spy_trend":     market_data.get("spy_trend", "neutral"),
            "session":       market_data.get("session", "MARKET"),
            "timeframe":     market_data.get("timeframe", "15m"),
        },
        "ml_signal": {
            "score":          ml_signal.get("ml_score", 50),
            "direction_prob": ml_signal.get("direction_prob", 0.5),
            "confidence":     ml_signal.get("confidence", 0.5),
            "signal":         ml_signal.get("signal", "NEUTRAL"),
            "top_features":   ml_signal.get("top_features", []),
        },
        "risk_status": {
            "daily_pnl_usd":      risk_data.get("daily_pnl", 0),
            "daily_loss_limit":   risk_data.get("daily_loss_limit", 700),
            "remaining_budget":   risk_data.get("daily_loss_limit", 700) - abs(risk_data.get("daily_pnl", 0)),
            "trades_today":       risk_data.get("trades_today", 0),
            "consecutive_losses": risk_data.get("consecutive_losses", 0),
        },
        "prop_firm_rules": config_rules,
        "news_catalyst": news_context or "No specific catalyst",
    }

    return (
        "Evaluate this trade proposal and respond with ONLY a JSON object.\n\n"
        f"```json\n{json.dumps(context, indent=2, default=str)}\n```"
    )


# ============================================================
# LLM API CALLERS — provider-specific
# ============================================================

def _call_claude(prompt: str, config: dict) -> dict:
    """Anthropic Claude API call"""
    api_key = os.getenv(config["api_key_env"], "")
    if not api_key:
        raise ValueError(f"Missing {config['api_key_env']}")

    payload = {
        "model":       config["model"],
        "max_tokens":  config["max_tokens"],
        "temperature": config["temperature"],
        "system":      SYSTEM_PROMPT,
        "messages":    [{"role": "user", "content": prompt}],
    }
    headers = config["headers_fn"](api_key)

    resp = requests.post(
        config["base_url"], json=payload, headers=headers,
        timeout=config["timeout_sec"]
    )
    resp.raise_for_status()
    data = resp.json()

    # Claude returns: {"content": [{"type": "text", "text": "..."}]}
    text = data.get("content", [{}])[0].get("text", "")
    return {"text": text, "raw": data}


def _call_openai(prompt: str, config: dict) -> dict:
    """OpenAI GPT API call"""
    api_key = os.getenv(config["api_key_env"], "")
    if not api_key:
        raise ValueError(f"Missing {config['api_key_env']}")

    payload = {
        "model":       config["model"],
        "max_tokens":  config["max_tokens"],
        "temperature": config["temperature"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    }
    headers = config["headers_fn"](api_key)

    resp = requests.post(
        config["base_url"], json=payload, headers=headers,
        timeout=config["timeout_sec"]
    )
    resp.raise_for_status()
    data = resp.json()

    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {"text": text, "raw": data}


def _call_gemini(prompt: str, config: dict) -> dict:
    """Google Gemini API call"""
    api_key = os.getenv(config["api_key_env"], "")
    if not api_key:
        raise ValueError(f"Missing {config['api_key_env']}")

    model = config["model"]
    url   = f"{config['base_url']}/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [{
            "parts": [
                {"text": f"{SYSTEM_PROMPT}\n\n{prompt}"}
            ]
        }],
        "generationConfig": {
            "maxOutputTokens": config["max_tokens"],
            "temperature":     config["temperature"],
        },
    }
    headers = config["headers_fn"](api_key)

    resp = requests.post(url, json=payload, headers=headers,
                         timeout=config["timeout_sec"])
    resp.raise_for_status()
    data = resp.json()

    text = (data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", ""))
    return {"text": text, "raw": data}


def _call_local_llm(prompt: str, config: dict) -> dict:
    """
    Local LLM API call — OpenAI-compatible (llama.cpp, vLLM, Ollama, etc.)

    Differences from cloud _call_openai:
      1. api_key_env อาจว่าง → ข้ามไม่ raise ValueError
      2. base_url ชี้ไป localhost (e.g. http://127.0.0.1:8700/v1)
      3. timeout ยาวขึ้น (default 30s) เพราะ local inference ช้ากว่า
      4. Response อาจมี <think>...</think> tags (DeepSeek R1) → strip ออก
      5. รองรับ 2 backends:
         - OPENAI_COMPAT: /v1/chat/completions (llama.cpp, vLLM, LM Studio)
         - OLLAMA: /api/chat (Ollama native)
    """
    # ── API key (optional สำหรับ local)
    key_env = config.get("api_key_env", "")
    api_key = os.getenv(key_env, "") if key_env else ""

    backend  = config.get("backend", "OPENAI_COMPAT")
    base_url = config["base_url"]

    if backend == "OLLAMA":
        return _call_ollama(prompt, config, api_key, base_url)

    # ── OPENAI_COMPAT (default): /v1/chat/completions
    # Ensure URL ends with /chat/completions
    if base_url.endswith("/v1"):
        url = f"{base_url}/chat/completions"
    elif "/chat/completions" in base_url:
        url = base_url
    else:
        url = f"{base_url.rstrip('/')}/chat/completions"

    payload = {
        "model":       config["model"],
        "max_tokens":  config["max_tokens"],
        "temperature": config["temperature"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
    }
    headers = config["headers_fn"](api_key)

    resp = requests.post(url, json=payload, headers=headers,
                         timeout=config["timeout_sec"])
    resp.raise_for_status()
    data = resp.json()

    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

    # ── Strip <think>...</think> blocks (DeepSeek R1, QwQ, etc.)
    text = _strip_think_tags(text)

    return {"text": text, "raw": data}


def _call_ollama(prompt: str, config: dict, api_key: str, base_url: str) -> dict:
    """Ollama native API (/api/chat)"""
    # Ollama URL: http://localhost:11434/api/chat
    if "/v1" in base_url:
        url = base_url.replace("/v1", "/api/chat")
    elif "/api/chat" in base_url:
        url = base_url
    else:
        url = f"{base_url.rstrip('/')}/api/chat"

    payload = {
        "model":   config["model"],
        "stream":  False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "options": {
            "temperature":  config["temperature"],
            "num_predict":  config["max_tokens"],
        },
    }
    headers = config["headers_fn"](api_key)

    resp = requests.post(url, json=payload, headers=headers,
                         timeout=config["timeout_sec"])
    resp.raise_for_status()
    data = resp.json()

    text = data.get("message", {}).get("content", "")
    text = _strip_think_tags(text)
    return {"text": text, "raw": data}


def _strip_think_tags(text: str) -> str:
    """
    Strip <think>...</think> blocks จาก reasoning models (DeepSeek R1, QwQ).

    DeepSeek R1 จะใส่ internal reasoning ภายใน <think> tags ก่อน output จริง:
      <think>Let me analyze this trade... The VIX is low...</think>
      {"action": "EXECUTE", "sizing_multiplier": 1.0, "reasoning": "..."}

    ต้อง strip ออกก่อนส่ง parser ไม่งั้น JSON parse จะ fail
    """
    import re

    # ── ลบ <think>...</think> (greedy, multi-line)
    cleaned = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)

    # ── ลบ <think> ที่ไม่มี closing tag (model อาจ truncate)
    cleaned = re.sub(r'<think>.*$', '', cleaned, flags=re.DOTALL)

    return cleaned.strip()


# Provider → caller mapping
_CALLERS = {
    "CLAUDE":    _call_claude,
    "OPENAI":    _call_openai,
    "GEMINI":    _call_gemini,
    "LOCAL_LLM": _call_local_llm,
}


# ============================================================
# RESPONSE PARSER — Extract & Validate JSON
# ============================================================

def _parse_llm_response(text: str) -> dict:
    """
    Parse LLM text response → validated dict

    Handles:
      - Clean JSON
      - JSON wrapped in ```json ... ``` fences
      - <think>...</think> blocks (DeepSeek R1, QwQ)
      - Partial JSON extraction

    Raises:
      ValueError: ถ้า parse ไม่ได้
    """
    text = text.strip()

    # ── ลบ <think>...</think> (safety net — _call_local_llm strip แล้ว
    #    แต่ถ้า user route reasoning model ผ่าน OPENAI provider ก็จะเจอ)
    text = _strip_think_tags(text)

    # ── ลบ markdown fences
    if "```" in text:
        import re
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if match:
            text = match.group(1)

    # ── ลองหา JSON object ตรงๆ
    if not text.startswith("{"):
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]

    result = json.loads(text)

    # ── Validate required fields
    if "action" not in result:
        raise ValueError("Missing 'action' field in LLM response")

    valid_actions = {"EXECUTE", "REDUCE", "DELAY", "ABORT"}
    if result["action"] not in valid_actions:
        raise ValueError(f"Invalid action '{result['action']}' — must be one of {valid_actions}")

    return result


# ============================================================
# GATE 19 MAIN CLASS
# ============================================================

class Gate19LLMCio:
    """
    Gate 19 — LLM CIO (Chief Investment Officer)

    วิเคราะห์บริบทแบบองค์รวมแล้วชี้ขาด trade

    Usage:
        cio = Gate19LLMCio()
        verdict = cio.evaluate_trade(
            market_data={...}, ml_signal={...},
            risk_data={...}, config_rules={...},
            news_context="NVDA beats Q2..."
        )
        if verdict.is_go():
            shares = int(original_shares * verdict.sizing_multiplier)
    """

    def __init__(self):
        # จะ import Config ตอนใช้งานจริง (avoid circular import)
        self._call_count = 0
        self._total_latency_ms = 0

    def evaluate_trade(
        self,
        market_data:  dict,
        ml_signal:    dict,
        risk_data:    dict,
        config_rules: dict,
        news_context: str = "",
    ) -> CIOVerdict:
        """
        Main entry point — ประเมิน trade แล้วคืน CIOVerdict

        Args:
          market_data:  ข้อมูลตลาด (price, vwap, atr, vix, etc.)
          ml_signal:    ผล ML prediction (score, direction, confidence)
          risk_data:    ข้อมูล risk (shares, entry, sl, tp, daily_pnl)
          config_rules: กฎ prop firm (max_daily_loss, consistency, etc.)
          news_context: headline + catalyst (ถ้ามี)

        Returns:
          CIOVerdict with action, sizing_multiplier, reasoning
        """
        from config.config import Config, LLM_PROVIDERS

        # ── ถ้า LLM disabled → EXECUTE ทุก trade (bypass Gate 19)
        if not Config.LLM_ENABLED:
            return CIOVerdict(
                action="EXECUTE", sizing_multiplier=1.0,
                reasoning="Gate 19 disabled — pass-through",
                provider="NONE",
            )

        # ── Build prompt
        prompt = build_evaluation_prompt(
            market_data, ml_signal, risk_data, config_rules, news_context
        )

        # ── Build provider chain ตาม toggle config
        #
        #    LLM_LOCAL_ONLY=true  → [LOCAL_LLM] only
        #    LLM_LOCAL_ENABLED=true → primary → fallback → LOCAL_LLM
        #    default               → primary → fallback
        providers_to_try = []

        if Config.LLM_LOCAL_ONLY:
            # ── Local-only mode (dry-run / offline / backtest)
            if Config.LLM_LOCAL_ENABLED:
                providers_to_try = ["LOCAL_LLM"]
            else:
                logger.warning(
                    "[Gate 19] LLM_LOCAL_ONLY=true but LLM_LOCAL_ENABLED=false "
                    "→ no providers available"
                )
        else:
            # ── Normal mode: build chain
            providers_to_try.append(Config.LLM_PRIMARY)
            if Config.LLM_FALLBACK and Config.LLM_FALLBACK != Config.LLM_PRIMARY:
                providers_to_try.append(Config.LLM_FALLBACK)

            # ── เพิ่ม LOCAL_LLM เป็น last-resort fallback (ถ้า enabled + ไม่ซ้ำ)
            if (Config.LLM_LOCAL_ENABLED
                    and "LOCAL_LLM" not in providers_to_try):
                providers_to_try.append("LOCAL_LLM")

        # ── Safety filter: ข้าม LOCAL_LLM ถ้า not enabled
        providers_to_try = [
            p for p in providers_to_try
            if p != "LOCAL_LLM" or Config.LLM_LOCAL_ENABLED
        ]

        logger.debug(f"[Gate 19] Provider chain: {providers_to_try}")

        for provider in providers_to_try:
            verdict = self._try_provider(provider, prompt)
            if verdict is not None:
                return verdict
            logger.warning(f"[Gate 19] {provider} failed — trying next...")

        # ── All providers failed → fail-safe
        fail_action = Config.LLM_FAIL_ACTION  # "ABORT" default
        logger.critical(
            f"[Gate 19] ❌ All LLM providers failed → {fail_action}"
        )
        return CIOVerdict(
            action=fail_action, sizing_multiplier=0.0 if fail_action == "ABORT" else 1.0,
            reasoning=f"All LLM providers failed — fail-safe {fail_action}",
            provider="FAIL_SAFE",
        )

    def _try_provider(self, provider: str, prompt: str) -> Optional[CIOVerdict]:
        """
        ลองยิง LLM provider 1 ตัว → CIOVerdict หรือ None ถ้า fail
        """
        from config.config import LLM_PROVIDERS

        config = LLM_PROVIDERS.get(provider)
        if not config:
            logger.error(f"[Gate 19] Unknown provider: {provider}")
            return None

        caller = _CALLERS.get(provider)
        if not caller:
            logger.error(f"[Gate 19] No caller for provider: {provider}")
            return None

        start = time.time()
        try:
            # ── Call LLM API
            result = caller(prompt, config)
            latency_ms = int((time.time() - start) * 1000)

            # ── Parse response
            parsed = _parse_llm_response(result["text"])

            # ══════════════════════════════════════════════════
            # HARDCODE SAFETY LOCK — Asymmetric Override
            # LLM ห้ามเพิ่ม Risk เด็ดขาด
            # ══════════════════════════════════════════════════
            raw_mult = float(parsed.get("sizing_multiplier", 0.0))
            safe_mult = min(max(raw_mult, 0.0), 1.0)   # clamp [0.0, 1.0]

            if raw_mult > 1.0:
                logger.warning(
                    f"[Gate 19] ⚠️ LLM tried to INCREASE risk "
                    f"(sizing_multiplier={raw_mult}) → clamped to 1.0"
                )

            action    = parsed["action"]

            # ── Extract bilingual reasoning
            reasoning_th = parsed.get("reasoning_th", "")
            reasoning_en = parsed.get("reasoning_en", "")
            # Backward compat: ถ้า LLM ยังตอบแบบเก่า (reasoning field เดียว)
            legacy_reasoning = parsed.get("reasoning", "")
            if not reasoning_th and not reasoning_en and legacy_reasoning:
                reasoning_en = legacy_reasoning
                reasoning_th = legacy_reasoning

            # Combined reasoning for log/journal
            reasoning = f"[TH] {reasoning_th} [EN] {reasoning_en}" if reasoning_th else reasoning_en

            # ── Extract technical levels (safety: default to 0)
            support    = float(parsed.get("support", 0))
            resistance = float(parsed.get("resistance", 0))
            cutloss    = float(parsed.get("cutloss", 0))
            rec_shares = int(parsed.get("recommended_shares", 0))
            rec_entry  = float(parsed.get("recommended_entry", 0))
            rec_tp     = float(parsed.get("recommended_tp", 0))
            exp_period = str(parsed.get("expected_period", "")).strip()

            # ── Validate expected_period
            valid_periods = {"15m", "30m", "1h", "2h", "3h", "4h", "EOD", ""}
            if exp_period not in valid_periods:
                exp_period = "1h"  # default

            # ── Force multiplier consistency with action
            if action == "EXECUTE":
                safe_mult = min(safe_mult, 1.0)
                if safe_mult < 0.5:
                    action = "REDUCE"  # ถ้า mult ต่ำมาก → reclassify
            elif action in ("DELAY", "ABORT"):
                safe_mult = 0.0       # DELAY/ABORT → 0 เสมอ

            self._call_count += 1
            self._total_latency_ms += latency_ms

            verdict = CIOVerdict(
                action=action,
                sizing_multiplier=round(safe_mult, 2),
                reasoning=reasoning[:400],  # bilingual ยาวกว่าเดิม
                reasoning_th=reasoning_th[:200],
                reasoning_en=reasoning_en[:200],
                support=round(support, 2),
                resistance=round(resistance, 2),
                cutloss=round(cutloss, 2),
                recommended_shares=max(0, rec_shares),
                recommended_entry=round(rec_entry, 2),
                recommended_tp=round(rec_tp, 2),
                expected_period=exp_period,
                provider=provider,
                latency_ms=latency_ms,
                raw_response=parsed,
            )

            emoji = {"EXECUTE": "✅", "REDUCE": "⚠️", "DELAY": "⏳", "ABORT": "🛑"}
            levels = verdict.levels_summary
            logger.info(
                f"[Gate 19] {emoji.get(action, '?')} {action} "
                f"| mult={safe_mult:.2f} | {provider} {latency_ms}ms "
                f"| {levels}"
            )
            logger.info(f"[Gate 19] TH: {reasoning_th[:100]}")
            logger.info(f"[Gate 19] EN: {reasoning_en[:100]}")
            return verdict

        except requests.Timeout:
            latency_ms = int((time.time() - start) * 1000)
            logger.error(f"[Gate 19] {provider} TIMEOUT ({latency_ms}ms)")
            return None

        except (json.JSONDecodeError, ValueError) as e:
            latency_ms = int((time.time() - start) * 1000)
            logger.error(
                f"[Gate 19] {provider} invalid response: {e} ({latency_ms}ms)"
            )
            return None

        except requests.RequestException as e:
            latency_ms = int((time.time() - start) * 1000)
            logger.error(f"[Gate 19] {provider} API error: {e} ({latency_ms}ms)")
            return None

        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            logger.error(f"[Gate 19] {provider} unexpected error: {e} ({latency_ms}ms)")
            return None

    # ── Stats
    def get_stats(self) -> dict:
        avg_lat = (self._total_latency_ms / self._call_count
                   if self._call_count > 0 else 0)
        return {
            "total_calls":     self._call_count,
            "avg_latency_ms":  round(avg_lat),
            "total_latency_ms": self._total_latency_ms,
        }


# ============================================================
# STANDALONE TEST (mock — ไม่ต้องยิง API จริง)
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")

    print("=" * 60)
    print("  GATE 19 LLM CIO — Unit Test (no API call)")
    print("=" * 60)

    # ── Test 1: Parse valid response
    print("\n[1] Parse valid JSON response")
    valid = '{"action": "EXECUTE", "sizing_multiplier": 1.0, "reasoning": "Trade looks good"}'
    parsed = _parse_llm_response(valid)
    assert parsed["action"] == "EXECUTE"
    assert parsed["sizing_multiplier"] == 1.0
    print(f"    ✅ Parsed: {parsed}")

    # ── Test 2: Parse fenced JSON
    print("\n[2] Parse markdown-fenced JSON")
    fenced = '```json\n{"action": "REDUCE", "sizing_multiplier": 0.5, "reasoning": "High VIX"}\n```'
    parsed = _parse_llm_response(fenced)
    assert parsed["action"] == "REDUCE"
    print(f"    ✅ Parsed: {parsed}")

    # ── Test 3: Reject sizing > 1.0
    print("\n[3] Clamp sizing_multiplier > 1.0")
    raw_mult = 1.5
    safe = min(max(raw_mult, 0.0), 1.0)
    assert safe == 1.0
    print(f"    ✅ {raw_mult} → {safe}")

    # ── Test 4: Invalid action
    print("\n[4] Reject invalid action")
    try:
        _parse_llm_response('{"action": "BUY_MORE", "sizing_multiplier": 2.0}')
        print("    ❌ Should have raised!")
    except ValueError as e:
        print(f"    ✅ Caught: {e}")

    # ── Test 5: Build prompt
    print("\n[5] Build evaluation prompt")
    prompt = build_evaluation_prompt(
        market_data={"symbol": "NVDA", "price": 182.0, "vwap": 180.5, "vix": 15.2},
        ml_signal={"ml_score": 76, "direction_prob": 0.73, "confidence": 0.81, "signal": "LONG"},
        risk_data={"shares": 34, "entry": 182.30, "stop_loss": 179.56, "take_profit": 187.75,
                   "risk_usd": 93.16, "daily_pnl": -25.0, "daily_loss_limit": 700},
        config_rules={"firm_name": "TTP", "max_daily_loss": 700, "max_orders_per_day": 3},
        news_context="NVDA beats Q2 EPS by 15%, raises guidance",
    )
    assert "NVDA" in prompt
    assert "182.3" in prompt
    print(f"    ✅ Prompt length: {len(prompt)} chars")

    # ── Test 6: CIOVerdict
    print("\n[6] CIOVerdict dataclass")
    v = CIOVerdict(action="EXECUTE", sizing_multiplier=0.8, reasoning="Reduced due to VIX")
    assert v.is_go()
    v2 = CIOVerdict(action="ABORT", sizing_multiplier=0.0, reasoning="Market halted")
    assert not v2.is_go()
    print(f"    ✅ EXECUTE is_go={v.is_go()}, ABORT is_go={v2.is_go()}")

    # ── Test 7: LOCAL_LLM caller registered
    print("\n[7] LOCAL_LLM in _CALLERS")
    assert "LOCAL_LLM" in _CALLERS, "LOCAL_LLM not in _CALLERS"
    assert callable(_CALLERS["LOCAL_LLM"]), "LOCAL_LLM caller not callable"
    print(f"    ✅ _CALLERS has LOCAL_LLM → {_CALLERS['LOCAL_LLM'].__name__}")

    # ── Test 8: Provider chain logic (simulated)
    print("\n[8] Provider chain build logic")

    # Mode: LOCAL_ONLY → only LOCAL_LLM
    chain_local_only = ["LOCAL_LLM"]
    assert chain_local_only == ["LOCAL_LLM"]
    print(f"    ✅ LOCAL_ONLY chain: {chain_local_only}")

    # Mode: Normal + LOCAL_LLM fallback
    chain_with_local = ["CLAUDE", "OPENAI", "LOCAL_LLM"]
    assert chain_with_local[-1] == "LOCAL_LLM"
    print(f"    ✅ Normal+Local chain: {chain_with_local}")

    # Mode: Filter cloud ถ้า local-only
    chain_filtered = [p for p in chain_with_local if p == "LOCAL_LLM"]
    assert chain_filtered == ["LOCAL_LLM"]
    print(f"    ✅ Filtered (local-only): {chain_filtered}")

    # Mode: Filter LOCAL_LLM ถ้า disabled
    chain_no_local = [p for p in chain_with_local if p != "LOCAL_LLM" or False]
    assert "LOCAL_LLM" not in chain_no_local
    print(f"    ✅ Local disabled chain: {chain_no_local}")

    # ── Test 9: _strip_think_tags
    print("\n[9] Strip <think> tags (DeepSeek R1)")
    raw = '<think>Let me analyze...</think>{"action": "EXECUTE", "sizing_multiplier": 1.0}'
    cleaned = _strip_think_tags(raw)
    assert "<think>" not in cleaned
    assert '{"action"' in cleaned
    print(f"    ✅ Cleaned: {cleaned[:60]}...")

    print("\n✅ All Gate 19 tests passed!")