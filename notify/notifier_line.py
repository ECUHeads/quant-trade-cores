"""
platform/notifier_line.py
=========================
LINE Messaging API — Flex Message Signal Broadcaster

Features:
  1. send_signal_flex()   — ส่งการ์ดสัญญาณ (Flex Message) สวยงาม
  2. send_cancel_message() — แจ้งยกเลิกสัญญาณ
  3. send_daily_summary()  — สรุปประจำวัน
  4. Rich Menu support     — "สถิติ", "VIP ของฉัน", "ติดต่อแอดมิน"

Flex Message Structure:
  ┌─────────────────────────────────┐
  │ 🟢 LONG NVDA          15m      │  ← Header (สีเขียว/แดง)
  ├─────────────────────────────────┤
  │ Entry    $120.00 – $120.50     │
  │ TP       $125.00  (+4.2%)      │  ← Body
  │ SL       $118.00  (-1.7%)      │
  │ R:R      1:2.5                 │
  ├─────────────────────────────────┤
  │ 📊 Trend Following             │
  │ VWAP pullback + earnings beat  │  ← Analysis
  │ ML Score: 76 | Conf: 81%      │
  ├─────────────────────────────────┤
  │ 🤖 LLM CIO:                    │
  │ "Strong catalyst aligned..."   │  ← LLM Comment
  ├─────────────────────────────────┤
  │  [ ดูกราฟ ]   [ ดูรายละเอียด ]   │  ← Footer buttons
  └─────────────────────────────────┘

Setup:
  1. สร้าง LINE Messaging API channel ที่ developers.line.biz
  2. ตั้ง env: LINE_CHANNEL_ACCESS_TOKEN=xxx
  3. ตั้ง Webhook URL: https://yourdomain.com/webhook/line
"""

import os
import json
import logging
import requests

logger = logging.getLogger("LINE_Notifier")

LINE_API_URL = "https://api.line.me/v2/bot/message/push"
LINE_TOKEN   = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://your-dashboard.com")


def _get_headers():
    return {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }


# ============================================================
# FLEX MESSAGE BUILDER — Signal Card
# ============================================================

def build_signal_flex(signal: dict) -> dict:
    """สร้าง Flex Message JSON สำหรับสัญญาณเทรด"""

    action = signal.get("action", "BUY")
    side   = signal.get("side", "LONG")
    asset  = signal.get("asset", "???")

    is_bullish = action in ("BUY",) or side in ("LONG",)

    # ── Colors
    header_bg = "#1DB446" if is_bullish else "#E74C3C"
    accent    = "#1DB446" if is_bullish else "#E74C3C"
    emoji     = "🟢" if is_bullish else "🔴"
    side_text = "LONG (ซื้อ)" if is_bullish else "SHORT (ขาย)"

    # ── Prices
    pz = signal.get("pricing_zone", {})
    entry_range = pz.get("entry_range", [0, 0])
    tp  = pz.get("take_profit", 0)
    sl  = pz.get("stop_loss", 0)
    rr  = pz.get("risk_reward", "1:2")

    entry_text = f"${entry_range[0]:,.2f} – ${entry_range[1]:,.2f}"
    tp_text = f"${tp:,.2f}"
    sl_text = f"${sl:,.2f}"

    # ── Analysis
    strategy = signal.get("strategy_type", "Trend Following")
    catalyst = signal.get("news_catalyst", "No catalyst")
    llm_comment = signal.get("llm_cio_comment", "")[:150]
    ml_score = signal.get("ml_score", 0)
    confidence = signal.get("confidence", 0)
    signal_id = signal.get("signal_id", "")

    strategy_emoji = "🏄" if "Trend" in strategy else "🌾"
    strategy_th = "ตามน้ำ (Trend)" if "Trend" in strategy else "ชาวสวน (Reversion)"

    flex = {
        "type": "flex",
        "altText": f"{emoji} {side_text} {asset} — Entry {entry_text}",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "header": {
                "type": "box", "layout": "horizontal",
                "backgroundColor": header_bg,
                "paddingAll": "15px",
                "contents": [
                    {"type": "text", "text": f"{emoji} {side_text}",
                     "color": "#FFFFFF", "size": "lg", "weight": "bold", "flex": 3},
                    {"type": "text", "text": asset, "color": "#FFFFFF",
                     "size": "xxl", "weight": "bold", "align": "end", "flex": 2},
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "paddingAll": "15px",
                "contents": [
                    # ── Price Zone
                    _flex_row("📍 Entry", entry_text, accent),
                    _flex_row("🎯 TP", tp_text, "#1DB446"),
                    _flex_row("🛡️ SL", sl_text, "#E74C3C"),
                    _flex_row("⚖️ R:R", rr, "#666666"),
                    {"type": "separator", "margin": "lg"},

                    # ── Strategy
                    {"type": "text",
                     "text": f"{strategy_emoji} กลยุทธ์: {strategy_th}",
                     "size": "sm", "color": "#555555", "margin": "md"},

                    # ── Catalyst
                    {"type": "text", "text": f"📰 {catalyst[:80]}",
                     "size": "xs", "color": "#888888", "wrap": True, "margin": "sm"},

                    # ── ML Score
                    {"type": "text",
                     "text": f"🧠 ML Score: {ml_score} | Confidence: {confidence:.0%}",
                     "size": "xs", "color": "#888888", "margin": "sm"},

                    {"type": "separator", "margin": "lg"},

                    # ── LLM CIO Comment
                    {"type": "box", "layout": "vertical",
                     "backgroundColor": "#F8F8F8",
                     "cornerRadius": "8px", "paddingAll": "10px", "margin": "md",
                     "contents": [
                         {"type": "text", "text": "🤖 AI CIO Analysis:",
                          "size": "xs", "color": "#555555", "weight": "bold"},
                         {"type": "text", "text": llm_comment or "No comment",
                          "size": "xs", "color": "#666666", "wrap": True,
                          "margin": "sm"},
                     ]},
                ]
            },
            "footer": {
                "type": "box", "layout": "horizontal", "spacing": "md",
                "paddingAll": "10px",
                "contents": [
                    {"type": "button", "style": "primary", "color": accent,
                     "action": {"type": "uri", "label": "📊 ดูกราฟ",
                                "uri": f"{DASHBOARD_URL}/signal/{signal_id}"}},
                    {"type": "button", "style": "secondary",
                     "action": {"type": "uri", "label": "📋 Dashboard",
                                "uri": DASHBOARD_URL}},
                ]
            }
        }
    }
    return flex


def _flex_row(label: str, value: str, color: str = "#333333") -> dict:
    return {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm",
             "color": "#888888", "flex": 2},
            {"type": "text", "text": value, "size": "md",
             "color": color, "weight": "bold", "align": "end", "flex": 3},
        ]
    }


# ============================================================
# CANCEL MESSAGE
# ============================================================

def build_cancel_flex(signal: dict) -> dict:
    asset = signal.get("asset", "???")
    signal_id = signal.get("signal_id", "")
    return {
        "type": "flex",
        "altText": f"⚠️ สัญญาณ {asset} ถูกยกเลิก",
        "contents": {
            "type": "bubble", "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical",
                "backgroundColor": "#FF9800", "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": f"⚠️ CANCELLED — {asset}",
                     "color": "#FFFFFF", "weight": "bold", "size": "md"},
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "12px",
                "contents": [
                    {"type": "text", "text": f"Signal {signal_id} ถูกยกเลิกโดยแอดมิน",
                     "size": "sm", "color": "#555555", "wrap": True},
                    {"type": "text", "text": "กรุณาปิดสถานะทันทีหากเปิดอยู่",
                     "size": "sm", "color": "#E74C3C", "weight": "bold",
                     "margin": "md"},
                ]
            },
        }
    }


# ============================================================
# SEND FUNCTIONS
# ============================================================

def send_signal_flex(line_user_id: str, signal: dict):
    """ส่ง Flex Message สัญญาณไปหา user"""
    if not LINE_TOKEN:
        logger.warning("LINE_CHANNEL_ACCESS_TOKEN not set")
        return False

    flex = build_signal_flex(signal)
    payload = {
        "to": line_user_id,
        "messages": [flex],
    }

    resp = requests.post(LINE_API_URL, json=payload,
                         headers=_get_headers(), timeout=10)
    if resp.status_code == 200:
        logger.info(f"[LINE] Signal sent → {line_user_id[:10]}...")
        return True
    else:
        logger.error(f"[LINE] Error {resp.status_code}: {resp.text[:200]}")
        return False


def send_cancel_message(line_user_id: str, signal: dict):
    """ส่ง cancel notification"""
    if not LINE_TOKEN:
        return False

    flex = build_cancel_flex(signal)
    payload = {"to": line_user_id, "messages": [flex]}
    resp = requests.post(LINE_API_URL, json=payload,
                         headers=_get_headers(), timeout=10)
    return resp.status_code == 200


def send_daily_summary(line_user_id: str, stats: dict):
    """ส่งสรุปประจำวัน"""
    if not LINE_TOKEN:
        return False

    text = (
        f"📊 สรุปวันนี้\n"
        f"Win Rate: {stats.get('win_rate', 0)}%\n"
        f"P&L: ${stats.get('total_pnl', 0):+,.2f}\n"
        f"Trades: {stats.get('total_trades', 0)}"
    )
    payload = {
        "to": line_user_id,
        "messages": [{"type": "text", "text": text}],
    }
    resp = requests.post(LINE_API_URL, json=payload,
                         headers=_get_headers(), timeout=10)
    return resp.status_code == 200


# ============================================================
# RICH MENU TEMPLATE
# ============================================================

RICH_MENU_TEMPLATE = {
    "size": {"width": 2500, "height": 843},
    "selected": True,
    "name": "Quant Agent Menu",
    "chatBarText": "เมนู",
    "areas": [
        {"bounds": {"x": 0, "y": 0, "width": 833, "height": 843},
         "action": {"type": "message", "text": "📊 สถิติเดือนนี้"}},
        {"bounds": {"x": 833, "y": 0, "width": 834, "height": 843},
         "action": {"type": "message", "text": "👑 สถานะ VIP ของฉัน"}},
        {"bounds": {"x": 1667, "y": 0, "width": 833, "height": 843},
         "action": {"type": "uri", "uri": f"{DASHBOARD_URL}/contact",
                    "label": "📞 ติดต่อแอดมิน"}},
    ]
}


if __name__ == "__main__":
    # ── Test: Generate Flex JSON
    test_signal = {
        "signal_id": "SIG-20260319-0001",
        "asset": "NVDA", "action": "BUY", "side": "LONG",
        "pricing_zone": {
            "entry_range": [182.00, 182.50],
            "take_profit": 187.75, "stop_loss": 179.56, "risk_reward": "1:2.5",
        },
        "strategy_type": "Trend Following",
        "news_catalyst": "NVDA beats Q2 EPS by 15%, raises guidance",
        "llm_cio_comment": "Strong earnings catalyst with VWAP pullback setup. ML confidence high.",
        "ml_score": 76, "confidence": 0.81,
    }
    flex = build_signal_flex(test_signal)
    print(json.dumps(flex, indent=2, ensure_ascii=False))
    print("\n✅ Flex JSON generated (paste into LINE Flex Message Simulator)")
