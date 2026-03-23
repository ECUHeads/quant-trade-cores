"""
regime_scorer.py
================
Market Regime Detection + Stock Scoring Engine

Classes:
  RegimeWeightedScorer — ประเมินสภาวะตลาด (VIX/SPY) และให้คะแนนหุ้น
                         ผสม Momentum + Mean Reversion ตาม market sentiment

Integration กับ main.py:
  main.py โหลด class นี้ใน _load_regime_scorer()
  เรียกใช้ใน _get_market_sentiment() และ _score_stock()

Bug fixes จาก original:
  - process_stock(): แก้ tsla_data.columns → stock_df.columns
  - process_stock(): แก้ indentation ของ w_mom, w_mr ให้อยู่นอก if block
"""

import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("RegimeScorer")


class RegimeWeightedScorer:
    """
    Market Regime-Switching Scorer

    Pipeline:
      1. fetch_market_sentiment()  → VIX + SPY → sentiment_score (0.0–1.0)
      2. get_dynamic_weights()     → (momentum_weight, mean_rev_weight)
      3. calculate_momentum_score()    → 0–100
      4. calculate_mean_reversion_score() → 0–100
      5. process_stock()           → Final_Weighted_Score + Action_Signal
    """

    def __init__(self, lookback_days: int = 30):
        self.lookback_days = lookback_days
        self.current_spy   = 0.0
        self.current_vix   = 0.0

    # ------------------------------------------
    # PIPELINE 1: Market Sentiment
    # ------------------------------------------

    def fetch_market_sentiment(self) -> dict:
        """
        ดึง VIX + SPY → คำนวณ sentiment_score (0.0 = แพนิก / 1.0 = กระทิง)

        Sentiment Math:
          base = 0.5
          VIX < 15  → +0.3  (ตลาดชิล)
          VIX > 25  → -0.3  (ตลาดกลัว)
          SPY > SMA20 → +0.2 (uptrend)
          SPY < SMA20 → -0.2 (downtrend)
          clamp → [0.0, 1.0]
        """
        logger.info("ดึงข้อมูล Market Sentiment (VIX & SPY)...")

        try:
            from data_pipeline_manager import safe_yf_download, normalize_ohlcv
            data = safe_yf_download(
                "SPY ^VIX",
                period=f"{self.lookback_days}d",
                interval="1d",
            )

            # safe_yf_download ไม่ normalize multi-ticker → ทำเอง
            if isinstance(data.columns, pd.MultiIndex):
                close_data = data["Close"] if "Close" in data.columns.get_level_values(0) else data["close"]
            else:
                close_data = data[["Close"]] if "Close" in data.columns else data

            # ── Strip tz
            if hasattr(close_data.index, 'tz') and close_data.index.tz is not None:
                try:
                    close_data.index = close_data.index.tz_localize(None)
                except TypeError:
                    close_data.index = close_data.index.tz_convert("UTC").tz_localize(None)

            current_vix = float(close_data["^VIX"].iloc[-1])
            current_spy = float(close_data["SPY"].iloc[-1])
            spy_sma20   = float(close_data["SPY"].rolling(window=20).mean().iloc[-1])

        except Exception as e:
            logger.error(f"fetch_market_sentiment error: {e} → ใช้ค่า default")
            return {"sentiment_score": 0.5, "vix": 20.0}

        sentiment_score = 0.5

        self.current_vix = current_vix
        self.current_spy = current_spy

        # VIX signal
        if current_vix < 15:
            sentiment_score += 0.3
        elif current_vix > 25:
            sentiment_score -= 0.3

        # SPY trend signal
        if current_spy > spy_sma20:
            sentiment_score += 0.2
        else:
            sentiment_score -= 0.2

        sentiment_score = max(0.0, min(1.0, sentiment_score))

        logger.info(
            f"📊 Sentiment Score: {sentiment_score:.2f} "
            f"(VIX={current_vix:.2f} SPY={current_spy:.2f} SMA20={spy_sma20:.2f})"
        )
        return {"sentiment_score": sentiment_score, "vix": current_vix}

    # ------------------------------------------
    # PIPELINE 2: Dynamic Weights
    # ------------------------------------------

    def get_dynamic_weights(self, sentiment_score: float) -> tuple:
        """
        คำนวณ weight สำหรับ 2 strategies ตาม sentiment

        sentiment สูง (>0.6) → ตลาด trending → Momentum มากขึ้น
        sentiment ต่ำ (<0.4) → ตลาดแพนิก   → Mean Reversion มากขึ้น

        Returns: (momentum_weight, mean_reversion_weight)
        """
        momentum_w  = float(sentiment_score)
        mean_rev_w  = 1.0 - float(sentiment_score)
        return momentum_w, mean_rev_w

    # ------------------------------------------
    # PIPELINE 3a: Momentum Score
    # ------------------------------------------

    def calculate_momentum_score(self, df: pd.DataFrame) -> float:
        """
        Momentum Strategy Score (0–100)

        กฎ:
          +40: ราคาปิด > SMA20 (uptrend)
          +30: RVOL > 1.5x (volume surge)
          +30: Gap up > 2% จาก previous close
        """
        score  = 0
        latest = df.iloc[-1]

        # ── กฎ 1: Price vs SMA20
        sma20 = df["Close"].rolling(20).mean().iloc[-1]
        if not pd.isna(sma20) and latest["Close"] > sma20:
            score += 40

        # ── กฎ 2: Relative Volume
        adv_10 = df["Volume"].rolling(10).mean().iloc[-1]
        if not pd.isna(adv_10) and adv_10 > 0:
            if latest["Volume"] > (adv_10 * 1.5):
                score += 30

        # ── กฎ 3: Gap Up
        if len(df) > 1:
            prev_close = df["Close"].iloc[-2]
            if prev_close > 0 and latest["Open"] > prev_close * 1.02:
                score += 30

        return float(score)

    # ------------------------------------------
    # PIPELINE 3b: Mean Reversion Score
    # ------------------------------------------

    def calculate_mean_reversion_score(self, df: pd.DataFrame) -> float:
        """
        Mean Reversion Strategy Score (0–100)

        กฎ:
          +50: RSI-14 < 30 (oversold)
          +50: ราคาต่ำกว่า Bollinger Lower Band (panic sell)
        """
        score = 0

        # ── RSI-14
        delta = df["Close"].diff()
        gain  = delta.where(delta > 0, 0.0).rolling(window=14).mean()
        loss  = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]

        if not pd.isna(current_rsi) and current_rsi < 30:
            score += 50

        # ── Bollinger Lower Band
        sma20 = df["Close"].rolling(20).mean().iloc[-1]
        std20 = df["Close"].rolling(20).std().iloc[-1]
        if not pd.isna(sma20) and not pd.isna(std20) and std20 > 0:
            lower_band = sma20 - (2 * std20)
            if df["Close"].iloc[-1] < lower_band:
                score += 50

        return float(score)

    # ------------------------------------------
    # PIPELINE 4: Process Single Stock
    # ------------------------------------------

    def process_stock(
        self,
        symbol: str,
        stock_df: pd.DataFrame,
        market_sentiment: dict,
    ) -> dict:
        """
        คำนวณคะแนนรวมของหุ้น 1 ตัว

        Args:
          symbol:           ticker เช่น "NVDA"
          stock_df:         DataFrame OHLCV (ต้องมี Close, Open, High, Low, Volume)
          market_sentiment: dict จาก fetch_market_sentiment()

        Returns:
          dict ที่มี Final_Weighted_Score และ Action_Signal
        """
        # ── Flatten MultiIndex columns (yfinance v0.2+ returns MultiIndex)
        if isinstance(stock_df.columns, pd.MultiIndex):
            # ✅ BUG FIX: เปลี่ยนจาก tsla_data.columns → stock_df.columns
            stock_df = stock_df.copy()
            stock_df.columns = [
                col[1] if col[0] == "Price" else col[0]
                for col in stock_df.columns
            ]

        # ── Normalize column names (handle both Title and lowercase)
        col_map = {c.lower(): c for c in stock_df.columns}
        if "close" in col_map and col_map["close"] != "Close":
            stock_df = stock_df.rename(columns={
                col_map.get("close", "close"):  "Close",
                col_map.get("open",  "open"):   "Open",
                col_map.get("high",  "high"):   "High",
                col_map.get("low",   "low"):    "Low",
                col_map.get("volume","volume"): "Volume",
            })

        # ── ✅ BUG FIX: w_mom, w_mr ต้องอยู่นอก if block
        w_mom, w_mr = self.get_dynamic_weights(market_sentiment["sentiment_score"])

        # ── Score แต่ละ strategy
        score_mom = self.calculate_momentum_score(stock_df)
        score_mr  = self.calculate_mean_reversion_score(stock_df)

        # ── Weighted Average
        final_score = (w_mom * score_mom) + (w_mr * score_mr)

        result = {
            "Symbol":               symbol,
            "VIX":                  round(float(self.current_vix), 2),
            "SPY":                  round(float(self.current_spy), 2),
            "Regime_Sentiment":     round(float(market_sentiment["sentiment_score"]), 3),
            "Weight_Momentum":      round(float(w_mom), 2),
            "Weight_MeanRev":       round(float(w_mr), 2),
            "Raw_Score_Momentum":   round(float(score_mom), 2),
            "Raw_Score_MeanRev":    round(float(score_mr), 2),
            "Final_Weighted_Score": round(float(final_score), 2),
            "Action_Signal":        "🔥 STRONG BUY" if final_score >= 70 else "⏳ WAIT",
        }
        return result


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":
    print("=" * 55)
    print("  REGIME SCORER — Standalone Test")
    print("=" * 55)

    scorer = RegimeWeightedScorer(lookback_days=30)

    # ── Market Sentiment
    sentiment = scorer.fetch_market_sentiment()
    print(f"\nSentiment Score : {sentiment['sentiment_score']:.2f}")
    print(f"VIX             : {sentiment['vix']:.2f}")

    # ── Score NVDA
    print("\nดึงข้อมูล NVDA...")
    from data_pipeline_manager import safe_yf_download
    df = safe_yf_download("NVDA", period="2mo", interval="1d")
    if not df.empty:
        result = scorer.process_stock("NVDA", df, sentiment)
        print("\n=== ผลคะแนน NVDA ===")
        for k, v in result.items():
            print(f"  {k:25s}: {v}")
