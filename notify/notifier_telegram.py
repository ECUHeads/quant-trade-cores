"""
platform/notifier_telegram.py
=============================
Telegram Bot API — Signal Broadcaster

Features:
  1. send_signal_telegram()  — ส่งสัญญาณ real-time (VIP Private Channel)
  2. send_delayed_signal()   — ส่งสัญญาณดีเลย์ (Public Channel)
  3. send_cancel_telegram()  — แจ้งยกเลิก
  4. send_daily_digest()     — สรุปรายวัน (Public Channel)

Channel Separation:
  Public Channel  → สัญญาณดีเลย์ 1 ชั่วโมง + สรุปรายวัน (เหยื่อล่อ)
  Private VIP     → สัญญาณ real-time + บทวิเคราะห์ LLM ครบ

Message Format (HTML):
  🟢 <b>LONG NVDA</b> — 15m
  ━━━━━━━━━━━━━━━━━━
  📍 Entry:  <b>$182.00 – $182.50</b>
  🎯 TP:     <b>$187.75</b> (+3.1%)
  🛡️ SL:     <b>$179.56</b> (-1.3%)
  ⚖️ R:R:    <b>1:2.5</b>
  ━━━━━━━━━━━━━━━━━━
  🏄 Trend Following
  📰 NVDA beats Q2 EPS by 15%...
  🧠 ML: 76 | Conf: 81%
  ━━━━━━━━━━━━━━━━━━
  🤖 <b>AI CIO:</b>
  <i>"Strong catalyst aligned..."</i>

Setup:
  1. สร้าง Bot ผ่าน @BotFather → ได้ token
  2. ตั้ง env:
     TELEGRAM_BOT_TOKEN=xxx
     TELEGRAM_VIP_CHAT_ID=-100xxx    (Private Channel)
     TELEGRAM_PUBLIC_CHAT_ID=-100xxx (Public Channel)
"""

import os
import logging
import requests

logger = logging.getLogger("TG_Notifier")

TG_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_VIP_CHAT_ID    = os.getenv("TELEGRAM_VIP_CHAT_ID", "")
TG_PUBLIC_CHAT_ID = os.getenv("TELEGRAM_PUBLIC_CHAT_ID", "")
TG_API_URL        = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
DASHBOARD_URL     = os.getenv("DASHBOARD_URL", "https://your-dashboard.com")


# ============================================================
# MESSAGE BUILDERS (HTML format)
# ============================================================

def build_signal_html(signal: dict, is_vip: bool = True) -> str:
    """สร้าง HTML message สำหรับ Telegram"""

    action = signal.get("action", "BUY")
    side   = signal.get("side", "LONG")
    asset  = signal.get("asset", "???")

    is_bullish = action in ("BUY",) or side in ("LONG",)
    emoji = "🟢" if is_bullish else "🔴"
    side_text = "LONG" if is_bullish else "SHORT"

    pz = signal.get("pricing_zone", {})
    entry = pz.get("entry_range", [0, 0])
    tp = pz.get("take_profit", 0)
    sl = pz.get("stop_loss", 0)
    rr = pz.get("risk_reward", "1:2")

    mid_entry = (entry[0] + entry[1]) / 2 if entry[0] > 0 else 0
    tp_pct = ((tp - mid_entry) / mid_entry * 100) if mid_entry > 0 else 0
    sl_pct = ((sl - mid_entry) / mid_entry * 100) if mid_entry > 0 else 0

    strategy = signal.get("strategy_type", "")
    catalyst = signal.get("news_catalyst", "")
    llm_comment = signal.get("llm_cio_comment", "")
    ml_score = signal.get("ml_score", 0)
    confidence = signal.get("confidence", 0)
    signal_id = signal.get("signal_id", "")

    strategy_emoji = "🏄" if "Trend" in strategy else "🌾"

    lines = [
        f"{emoji} <b>{side_text} {asset}</b> — 15m",
        "━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:  <b>${entry[0]:,.2f} – ${entry[1]:,.2f}</b>",
        f"🎯 TP:     <b>${tp:,.2f}</b> ({tp_pct:+.1f}%)",
        f"🛡️ SL:     <b>${sl:,.2f}</b> ({sl_pct:+.1f}%)",
        f"⚖️ R:R:    <b>{rr}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"{strategy_emoji} {strategy}",
    ]

    if catalyst:
        lines.append(f"📰 {catalyst[:100]}")

    lines.append(f"🧠 ML: {ml_score} | Conf: {confidence:.0%}")

    # ── LLM Comment (VIP only)
    if is_vip and llm_comment:
        lines.extend([
            "━━━━━━━━━━━━━━━━━━",
            f"🤖 <b>AI CIO:</b>",
            f"<i>\"{llm_comment[:200]}\"</i>",
        ])

    # ── Footer
    lines.extend([
        "",
        f"🔗 <a href=\"{DASHBOARD_URL}/signal/{signal_id}\">ดูกราฟ</a>"
        f" | <a href=\"{DASHBOARD_URL}\">Dashboard</a>",
    ])

    if not is_vip:
        lines.insert(1, "⏰ <i>(สัญญาณดีเลย์ — สมัคร VIP เพื่อรับ Real-time)</i>")

    return "\n".join(lines)


def build_cancel_html(signal: dict) -> str:
    asset = signal.get("asset", "???")
    signal_id = signal.get("signal_id", "")
    return (
        f"⚠️ <b>CANCELLED — {asset}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Signal <code>{signal_id}</code> ถูกยกเลิกโดยแอดมิน\n"
        f"❗ <b>กรุณาปิดสถานะทันทีหากเปิดอยู่</b>"
    )


def build_daily_digest(stats: dict) -> str:
    return (
        f"📊 <b>สรุปประจำวัน</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Win Rate: <b>{stats.get('win_rate', 0)}%</b>\n"
        f"💰 P&L: <b>${stats.get('total_pnl', 0):+,.2f}</b>\n"
        f"🔢 Trades: {stats.get('total_trades', 0)}\n"
        f"📉 Max DD: ${stats.get('max_dd', 0):,.2f}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <a href=\"{DASHBOARD_URL}/performance\">ดูผลงานเต็ม</a>"
    )


# ============================================================
# SEND FUNCTIONS
# ============================================================

def _send_message(chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    """ส่งข้อความไปยัง chat_id"""
    if not TG_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set")
        return False

    url = f"{TG_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"[TG] Sent → {chat_id}")
            return True
        else:
            logger.error(f"[TG] Error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"[TG] Send error: {e}")
        return False


def send_signal_telegram(chat_id: str, signal: dict, is_vip: bool = True) -> bool:
    """ส่งสัญญาณ real-time ไปหา user/channel"""
    html = build_signal_html(signal, is_vip=is_vip)
    return _send_message(chat_id, html)


def send_signal_to_vip_channel(signal: dict) -> bool:
    """ส่งสัญญาณไป VIP Private Channel"""
    if not TG_VIP_CHAT_ID:
        return False
    return send_signal_telegram(TG_VIP_CHAT_ID, signal, is_vip=True)


def send_signal_to_public_channel(signal: dict) -> bool:
    """ส่งสัญญาณดีเลย์ไป Public Channel (ไม่มี LLM comment)"""
    if not TG_PUBLIC_CHAT_ID:
        return False
    return send_signal_telegram(TG_PUBLIC_CHAT_ID, signal, is_vip=False)


def send_cancel_telegram(chat_id: str, signal: dict) -> bool:
    html = build_cancel_html(signal)
    return _send_message(chat_id, html)


def send_daily_digest_to_public(stats: dict) -> bool:
    """ส่งสรุปรายวันไป Public Channel"""
    if not TG_PUBLIC_CHAT_ID:
        return False
    html = build_daily_digest(stats)
    return _send_message(TG_PUBLIC_CHAT_ID, html)


# ============================================================
# BOT COMMANDS HANDLER
# ============================================================

def handle_command(chat_id: str, command: str) -> bool:
    """จัดการ commands จาก user"""
    if command == "/start":
        text = (
            "🤖 <b>Quant Agent Signal Bot</b>\n\n"
            "สวัสดีครับ! บอทนี้ส่งสัญญาณเทรดจาก AI Engine\n\n"
            "คำสั่ง:\n"
            "/status — ดูสถานะ VIP\n"
            "/stats — ดูสถิติวันนี้\n"
            "/help — วิธีใช้งาน"
        )
        return _send_message(chat_id, text)

    elif command == "/help":
        text = (
            "📋 <b>วิธีใช้งาน</b>\n\n"
            "🟢 สัญญาณ BUY = เปิด Long\n"
            "🔴 สัญญาณ SELL = เปิด Short\n"
            "⚠️ CANCELLED = ปิดสถานะทันที\n\n"
            f"🔗 <a href=\"{DASHBOARD_URL}\">ดู Dashboard</a>"
        )
        return _send_message(chat_id, text)

    return False


if __name__ == "__main__":
    # Test: print formatted message
    test_signal = {
        "signal_id": "SIG-20260319-0001",
        "asset": "NVDA", "action": "BUY", "side": "LONG",
        "pricing_zone": {
            "entry_range": [182.00, 182.50],
            "take_profit": 187.75, "stop_loss": 179.56, "risk_reward": "1:2.5",
        },
        "strategy_type": "Trend Following",
        "news_catalyst": "NVDA beats Q2 EPS by 15%, raises guidance",
        "llm_cio_comment": "Strong earnings catalyst with low VIX. VWAP pullback confirmed.",
        "ml_score": 76, "confidence": 0.81,
    }
    print("=== VIP Message ===")
    print(build_signal_html(test_signal, is_vip=True))
    print("\n=== Public Message (delayed, no LLM) ===")
    print(build_signal_html(test_signal, is_vip=False))
