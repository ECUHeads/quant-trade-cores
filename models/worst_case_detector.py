"""
worst_case_detector.py
======================
Toxic Market Regime Detector — "The Security Guard Model"

แทนที่จะหาจุดเข้าทำกำไร โมเดลตัวนี้หาจุดที่ "ห้ามเข้าเด็ดขาด"
ทำหน้าที่เป็น Veto Gate ก่อน Gate 19 (LLM CIO)

Architecture:
  WorstCaseLabeler       — สร้าง Target Y จาก 3 Conditions (look-ahead)
  WorstCaseFeatures      — Features เฉพาะสำหรับตรวจจับ toxic regime
  WorstCaseModel         — LightGBM Binary Classifier (Y=1 = danger)
  WorstCaseGate          — Gate interface สำหรับ main.py

3 Conditions (Worst Case = Y=1):
  1. Whipsaw / Stop-Loss Hunter  — ราคาสะบัดชน SL ทั้ง 2 ฝั่ง
  2. Chop Zone / Death by Cuts   — ไซด์เวย์แคบ ER ต่ำ
  3. MAE Trap                     — Drawdown หนักเกินก่อนกำไร

Integration:
  วางระหว่าง Gate 7 (Risk Manager) กับ Gate 19 (LLM CIO)
  ถ้า is_danger=True → skip trade ทันที (ไม่ต้องเสีย LLM API call)

Training:
  Daily retrain พร้อมกับ LightGBM ปกติ (piggyback _daily_retrain_scheduler)
  ใช้ ModelRegistry pattern เดียวกัน (save/load per symbol)
"""

import math
import json
import logging
import joblib
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Tuple

from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import roc_auc_score, recall_score, f1_score
import lightgbm as lgb

logger = logging.getLogger("WorstCase")


# ============================================================
# CONFIG
# ============================================================

WC_LOOKFORWARD_BARS  = 8        # มองไปข้างหน้า 8 แท่ง (8 × 15m = 2 ชม.)
WC_ATR_WHIPSAW_MULT  = 1.2      # Condition 1: threshold = ATR × 1.2
WC_ER_THRESHOLD      = 0.20     # Condition 2: Efficiency Ratio < 0.20
WC_ER_RANGE_MULT     = 0.8      # Condition 2: range < ATR × 0.8 (แคบผิดปกติ)
WC_MAE_MFE_RATIO     = 2.0      # Condition 3: MAE > 2 × MFE
WC_MAE_ABS_MULT      = 2.0      # Condition 3: MAE > ATR × 2.0 (drawdown limit)
WC_MIN_TRAIN_BARS    = 200      # bars ขั้นต่ำก่อน train
WC_DANGER_THRESHOLD  = 0.45     # probability > 0.45 → VETO (เข้มกว่าปกติ 0.5)
WC_MODEL_DIR         = "./models"


# ============================================================
# DATA MODEL — OUTPUT
# ============================================================

@dataclass
class WorstCaseVerdict:
    """ผลการตัดสินจาก Worst Case Gate"""
    is_danger:     bool  = False       # True = VETO trade
    danger_score:  float = 0.0         # 0.0–1.0 (probability of worst case)
    conditions:    dict  = field(default_factory=dict)  # แต่ละ condition ที่ trigger
    top_features:  list  = field(default_factory=list)
    model_version: str   = ""
    latency_ms:    float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# LABELER — สร้าง Target Y จาก 3 Conditions
# ============================================================

class WorstCaseLabeler:
    """
    Look-ahead Labeling สำหรับ Toxic Market Detection

    มองไปข้างหน้า N แท่ง แล้วกลับมาแปะ label ให้แท่งปัจจุบัน:
      Y = 1 (Worst Case)  ถ้าเจอ condition อย่างน้อย 1 ข้อ
      Y = 0 (Normal)      ถ้าปลอดภัยทุก condition

    ใช้ ATR-based thresholds เพื่อปรับตาม volatility ของแต่ละหุ้น
    """

    def __init__(
        self,
        lookforward:       int   = WC_LOOKFORWARD_BARS,
        atr_whipsaw_mult:  float = WC_ATR_WHIPSAW_MULT,
        er_threshold:      float = WC_ER_THRESHOLD,
        er_range_mult:     float = WC_ER_RANGE_MULT,
        mae_mfe_ratio:     float = WC_MAE_MFE_RATIO,
        mae_abs_mult:      float = WC_MAE_ABS_MULT,
    ):
        self.N              = lookforward
        self.atr_whipsaw    = atr_whipsaw_mult
        self.er_threshold   = er_threshold
        self.er_range_mult  = er_range_mult
        self.mae_mfe_ratio  = mae_mfe_ratio
        self.mae_abs_mult   = mae_abs_mult

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        สร้าง label columns จาก OHLCV DataFrame

        Returns:
            DataFrame เดิม + columns:
              - wc_cond1 (Whipsaw)
              - wc_cond2 (Chop Zone)
              - wc_cond3 (MAE Trap)
              - wc_target (OR ของทั้ง 3)

        Note: ใช้ look-ahead → ไม่มี label สำหรับ N แท่งสุดท้าย (NaN)
        """
        c = df["close"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        N = self.N

        # ── ATR-14 สำหรับ dynamic threshold
        hl     = h - l
        hc     = (h - c.shift(1)).abs()
        lc     = (l - c.shift(1)).abs()
        tr     = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr14  = tr.rolling(14).mean()

        # ── Look-ahead: future highs & lows (shift กลับมา)
        future_max_high = h.rolling(window=N).max().shift(-N)
        future_min_low  = l.rolling(window=N).min().shift(-N)

        # ════════════════════════════════════════════════
        # CONDITION 1: Whipsaw / Stop-Loss Hunter
        # ════════════════════════════════════════════════
        # ราคาสะบัดขึ้นและลงเกิน threshold ทั้ง 2 ฝั่ง
        threshold = atr14 * self.atr_whipsaw
        swing_up   = future_max_high - c   # ระยะที่ขึ้นไปถึง
        swing_down = c - future_min_low     # ระยะที่ลงไปถึง

        cond1 = (swing_up > threshold) & (swing_down > threshold)

        # ════════════════════════════════════════════════
        # CONDITION 2: Chop Zone / Death by a Thousand Cuts
        # ════════════════════════════════════════════════
        # Efficiency Ratio ต่ำ + range แคบ → ไซด์เวย์ noise สูง
        future_close = c.shift(-N)
        net_move     = (future_close - c).abs()

        # Total path = sum of absolute bar-to-bar moves
        abs_changes = c.diff().abs()
        total_path  = abs_changes.rolling(window=N).sum().shift(-N + 1)

        # Efficiency Ratio: net / total (0 = pure noise, 1 = straight line)
        er = net_move / (total_path + 1e-9)

        # Range check: ถ้ากรอบแคบกว่าปกติ
        future_range = future_max_high - future_min_low
        range_narrow = future_range < (atr14 * self.er_range_mult)

        cond2 = (er < self.er_threshold) & range_narrow

        # ════════════════════════════════════════════════
        # CONDITION 3: MAE Trap (Maximum Adverse Excursion)
        # ════════════════════════════════════════════════
        # Drawdown หนักเกินก่อนถึง profit
        # วิเคราะห์ทั้ง Long และ Short direction

        # Long perspective
        mae_long = c - future_min_low        # ระยะลากลงจาก entry
        mfe_long = future_max_high - c       # ระยะกำไรสูงสุด

        # Short perspective
        mae_short = future_max_high - c      # ระยะลากขึ้นจาก entry
        mfe_short = c - future_min_low       # ระยะกำไรสูงสุด

        # ห้ามเข้าถ้า "ทั้ง Long และ Short" มี MAE แย่
        # (หมายความว่าไม่ว่าจะเข้าฝั่งไหน ก็โดนลากหนัก)
        mae_bad_long  = (mae_long > self.mae_mfe_ratio * mfe_long) | \
                        (mae_long > atr14 * self.mae_abs_mult)
        mae_bad_short = (mae_short > self.mae_mfe_ratio * mfe_short) | \
                        (mae_short > atr14 * self.mae_abs_mult)

        cond3 = mae_bad_long & mae_bad_short

        # ════════════════════════════════════════════════
        # COMBINE: OR ของทั้ง 3 conditions
        # ════════════════════════════════════════════════
        result = df.copy()
        result["wc_cond1"]  = cond1.astype(float)
        result["wc_cond2"]  = cond2.astype(float)
        result["wc_cond3"]  = cond3.astype(float)
        result["wc_target"] = (cond1 | cond2 | cond3).astype(float)

        # Debug stats
        valid_mask = result["wc_target"].notna()
        total      = valid_mask.sum()
        if total > 0:
            n_wc = result.loc[valid_mask, "wc_target"].sum()
            pct  = n_wc / total * 100
            logger.info(
                f"[Labeler] Total={total} | WorstCase={int(n_wc)} ({pct:.1f}%) | "
                f"C1(whipsaw)={int(result['wc_cond1'].sum())} "
                f"C2(chop)={int(result['wc_cond2'].sum())} "
                f"C3(mae)={int(result['wc_cond3'].sum())}"
            )

        return result


# ============================================================
# FEATURE ENGINEER — Worst-Case-Specific Features
# ============================================================

class WorstCaseFeatures:
    """
    Features เฉพาะสำหรับตรวจจับ Toxic Market Regime

    ใช้ features จาก FeatureEngineer ปกติ (47 features) เป็น base
    แล้วเพิ่ม features ที่เน้นจับ:
      - ความผันผวนที่ไม่มีทิศทาง (directionless volatility)
      - ความ "ฟันปลา" ของ price action
      - สัญญาณ liquidity ต่ำ
      - แรงสะบัดย้อนกลับ (mean reversion pressure)
    """

    @staticmethod
    def compute(df: pd.DataFrame) -> pd.DataFrame:
        """
        คำนวณ worst-case-specific features แบบ vectorized

        Input:  OHLCV DataFrame (15m bars)
        Output: DataFrame ของ features (same index)
        """
        c = df["close"].astype(float)
        o = df["open"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        v = df["volume"].astype(float)

        feat = pd.DataFrame(index=df.index)

        # ── ATR variants
        hl     = h - l
        hc     = (h - c.shift(1)).abs()
        lc     = (l - c.shift(1)).abs()
        tr     = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr14  = tr.rolling(14).mean()
        atr7   = tr.rolling(7).mean()

        # 1. ATR Expansion Ratio: short ATR vs long ATR
        #    > 1.3 = volatility spike (whipsaw territory)
        feat["atr_expansion"] = atr7 / (atr14 + 1e-9)

        # 2. Efficiency Ratio (ER) — rolling window
        #    วัด "ความตรง" ของ price path ย้อนหลัง
        net_move_8   = (c - c.shift(8)).abs()
        total_path_8 = c.diff().abs().rolling(8).sum()
        feat["efficiency_ratio_8"] = net_move_8 / (total_path_8 + 1e-9)

        net_move_16   = (c - c.shift(16)).abs()
        total_path_16 = c.diff().abs().rolling(16).sum()
        feat["efficiency_ratio_16"] = net_move_16 / (total_path_16 + 1e-9)

        # 3. Whipsaw Counter: จำนวนครั้งที่ direction เปลี่ยนใน 8 bars
        direction    = np.sign(c.diff())
        dir_changes  = (direction != direction.shift(1)).astype(float)
        feat["direction_changes_8"]  = dir_changes.rolling(8).sum()
        feat["direction_changes_16"] = dir_changes.rolling(16).sum()

        # 4. Wick Dominance: สัดส่วน wick vs body (wick มาก = rejection สูง)
        body     = (c - o).abs()
        full_rng = (h - l).replace(0, np.nan)
        feat["wick_body_ratio"] = 1 - (body / (full_rng + 1e-9))

        # 5. Upper/Lower Wick Balance:
        #    ใกล้ 0 = wicks สมมาตร = สะบัดทั้ง 2 ฝั่ง = อันตราย
        upper_wick = h - pd.concat([c, o], axis=1).max(axis=1)
        lower_wick = pd.concat([c, o], axis=1).min(axis=1) - l
        total_wick = upper_wick + lower_wick + 1e-9
        feat["wick_balance"] = (upper_wick - lower_wick).abs() / total_wick

        # Rolling average wick balance (ยิ่งต่ำยิ่งอันตราย)
        feat["wick_balance_ma8"] = feat["wick_balance"].rolling(8).mean()

        # 6. Range Contraction: กรอบแคบลงเรื่อยๆ (precursor to whipsaw)
        range_pct      = hl / (c + 1e-9)
        feat["range_pct_zscore"] = (
            (range_pct - range_pct.rolling(20).mean()) /
            (range_pct.rolling(20).std() + 1e-9)
        )

        # 7. Volume Dry-Up: volume ลดลงผิดปกติ (low liquidity warning)
        vol_ma20 = v.rolling(20).mean()
        feat["vol_dryup_ratio"] = v / (vol_ma20 + 1e-9)

        # Volume trend: ลดลงต่อเนื่องหรือไม่
        feat["vol_declining"] = (
            (v < v.shift(1)) & (v.shift(1) < v.shift(2)) &
            (v.shift(2) < v.shift(3))
        ).astype(float)

        # 8. Gap-Fill Pressure: ราคา gap แล้วมีแนวโน้ม fill กลับ
        daily_open = o.groupby(df.index.date).transform("first")
        gap_from_open = (c - daily_open) / (atr14 + 1e-9)
        feat["gap_fill_pressure"] = -gap_from_open  # ยิ่ง gap มาก ยิ่งมีแรงดึงกลับ

        # 9. RSI Extreme Divergence (ขัดแย้งใน timeframe)
        delta    = c.diff()
        gain     = delta.where(delta > 0, 0).rolling(14).mean()
        loss_    = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi_14   = 100 - 100 / (1 + gain / loss_.replace(0, np.nan))
        rsi_7    = 100 - 100 / (1 + delta.where(delta > 0, 0).rolling(7).mean() /
                                     (-delta.where(delta < 0, 0)).rolling(7).mean().replace(0, np.nan))
        feat["rsi_divergence_7_14"] = rsi_7 - rsi_14

        # RSI ใน neutral zone (40-60) = ไม่มีทิศทาง = chop
        feat["rsi_in_neutral"] = ((rsi_14 > 40) & (rsi_14 < 60)).astype(float)

        # 10. Bollinger Band Squeeze: BB แคบผิดปกติ (มักตามด้วย whipsaw)
        sma20    = c.rolling(20).mean()
        std20    = c.rolling(20).std()
        bb_width = (4 * std20) / (sma20 + 1e-9) * 100
        bb_width_ma = bb_width.rolling(20).mean()
        feat["bb_squeeze"] = bb_width / (bb_width_ma + 1e-9)
        # < 0.7 = squeeze = อันตราย

        # 11. Consecutive Same-Direction Bars (trend strength)
        #     ค่าน้อย = สลับไปมา = chop
        is_up    = (c > c.shift(1)).astype(float)
        streak   = is_up.copy()
        for i in range(1, 8):
            streak = streak + (is_up == is_up.shift(i)).astype(float)
        feat["trend_streak_8"] = streak - 4  # center around 0

        # 12. Price vs VWAP distance (normalized)
        typical = (h + l + c) / 3
        dates   = df.index.date
        vwap    = (typical * v).groupby(dates).cumsum() / \
                  v.groupby(dates).cumsum().replace(0, np.nan)
        feat["vwap_dist_atr"] = (c - vwap) / (atr14 + 1e-9)

        # 13. Intraday Range Used: ใช้ range ไปกี่ % แล้ว
        intra_high = h.groupby(dates).cummax()
        intra_low  = l.groupby(dates).cummin()
        intra_range = intra_high - intra_low
        feat["intraday_range_vs_atr"] = intra_range / (atr14 + 1e-9)

        # 14. Session period (เวลาส่งผลต่อ regime)
        if hasattr(df.index, 'hour'):
            minutes = df.index.hour * 60 + df.index.minute
            feat["minutes_since_930"] = (minutes - 570).clip(lower=0)
        else:
            feat["minutes_since_930"] = 0.0

        return feat.ffill().fillna(0)


# ============================================================
# LIGHTGBM MODEL — Binary Classifier (Danger vs Safe)
# ============================================================

class WorstCaseModel:
    """
    LightGBM Binary Classifier สำหรับ Worst Case Detection

    Objective: Binary (Y=1 = danger, Y=0 = safe)
    Primary Metric: Recall (class 1) — ยอม False Positive ดีกว่า False Negative
    Imbalance: ใช้ scale_pos_weight แทน SMOTE (เร็วกว่า, ไม่ต้องสร้างข้อมูลปลอม)
    """

    def __init__(self):
        self.model         = None
        self.feature_names = None
        self.trained_at    = None
        self.auc_score     = 0.0
        self.recall_score  = 0.0
        self.version       = f"wc-{datetime.now().strftime('%Y%m%d')}"

        self.PARAMS = {
            "objective":        "binary",
            "metric":           "auc",
            "learning_rate":    0.03,
            "num_leaves":       15,
            "max_depth":        4,
            "feature_fraction": 0.7,
            "bagging_fraction": 0.8,
            "bagging_freq":     5,
            "lambda_l1":        0.5,
            "lambda_l2":        0.5,
            "verbose":          -1,
            "n_jobs":           -1,
            # scale_pos_weight จะ set dynamic ตอน train
        }

    def train(self, X: pd.DataFrame, y: pd.Series) -> float:
        """
        Train model ด้วย TimeSeriesSplit

        Returns: avg AUC score (0.0 ถ้า fail)
        """
        if len(X) < WC_MIN_TRAIN_BARS:
            logger.warning(f"[WC-LGBM] ข้อมูลน้อยเกินไป ({len(X)} bars)")
            return 0.0

        # Clean NaN/Inf
        X = X.replace([np.inf, -np.inf], np.nan).fillna(0)
        y = y.fillna(0).astype(int)

        self.feature_names = X.columns.tolist()

        # Dynamic scale_pos_weight: n_negative / n_positive
        n_pos = max(1, (y == 1).sum())
        n_neg = max(1, (y == 0).sum())
        self.PARAMS["scale_pos_weight"] = n_neg / n_pos

        logger.info(
            f"[WC-LGBM] Training | samples={len(X)} | "
            f"pos={n_pos} ({n_pos/len(X)*100:.1f}%) | "
            f"scale_pos_weight={self.PARAMS['scale_pos_weight']:.2f}"
        )

        # GPU check (optional, graceful fallback to CPU)
        try:
            import lightgbm as _lgb
            self.PARAMS["device"]          = "gpu"
            self.PARAMS["gpu_platform_id"] = 0
            self.PARAMS["gpu_device_id"]   = 0
        except Exception:
            self.PARAMS.pop("device", None)
            self.PARAMS.pop("gpu_platform_id", None)
            self.PARAMS.pop("gpu_device_id", None)

        tscv     = TimeSeriesSplit(n_splits=3)
        aucs     = []
        recalls  = []
        best_model = None
        max_auc    = 0.0

        for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
            X_tr, X_val = X.iloc[train_idx], X.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            # Skip ถ้าไม่มี positive class ใน validation
            if y_val.sum() == 0 or y_tr.sum() == 0:
                logger.warning(f"[WC-LGBM] Fold {fold}: no positive class → skip")
                continue

            dtrain = lgb.Dataset(X_tr, label=y_tr)
            dval   = lgb.Dataset(X_val, label=y_val, reference=dtrain)

            try:
                tmp_model = lgb.train(
                    self.PARAMS,
                    dtrain,
                    num_boost_round=500,
                    valid_sets=[dval],
                    callbacks=[
                        lgb.early_stopping(stopping_rounds=30, verbose=False),
                        lgb.log_evaluation(-1),
                    ],
                )
            except Exception as e:
                # GPU fail → retry CPU
                logger.warning(f"[WC-LGBM] GPU train failed, fallback CPU: {e}")
                for k in ["device", "gpu_platform_id", "gpu_device_id"]:
                    self.PARAMS.pop(k, None)
                tmp_model = lgb.train(
                    self.PARAMS,
                    dtrain,
                    num_boost_round=500,
                    valid_sets=[dval],
                    callbacks=[
                        lgb.early_stopping(stopping_rounds=30, verbose=False),
                        lgb.log_evaluation(-1),
                    ],
                )

            # Evaluate
            preds = tmp_model.predict(X_val)
            auc   = roc_auc_score(y_val, preds)

            # Recall at threshold 0.45 (เข้มกว่า 0.5 เพราะเราอยากจับ danger ให้ได้)
            preds_binary = (preds >= WC_DANGER_THRESHOLD).astype(int)
            rec = recall_score(y_val, preds_binary, zero_division=0.0)

            aucs.append(auc)
            recalls.append(rec)

            if auc > max_auc:
                max_auc    = auc
                best_model = tmp_model

            logger.info(
                f"[WC-LGBM] Fold {fold}: AUC={auc:.4f} Recall={rec:.4f}"
            )

        if not best_model:
            logger.warning("[WC-LGBM] Training failed — no valid folds")
            return 0.0

        self.model        = best_model
        self.auc_score    = round(float(np.mean(aucs)), 4)
        self.recall_score = round(float(np.mean(recalls)), 4)
        self.trained_at   = datetime.now(timezone.utc).isoformat()

        logger.info(
            f"[WC-LGBM] ✅ Train done | Avg AUC={self.auc_score:.4f} | "
            f"Avg Recall={self.recall_score:.4f}"
        )
        return self.auc_score

    def predict(self, X: pd.DataFrame) -> float:
        """
        คืน probability ว่าเป็น Worst Case (0.0–1.0)
        ค่ายิ่งสูง = ยิ่งอันตราย
        """
        if self.model is None:
            return 0.0

        if isinstance(X, pd.Series):
            X = X.to_frame().T

        X = X.replace([np.inf, -np.inf], np.nan).fillna(0)

        try:
            prob = self.model.predict(X)
            return float(np.clip(prob[0], 0.0, 1.0))
        except Exception as e:
            logger.warning(f"[WC-LGBM] Predict error: {e}")
            return 0.0

    def feature_importance(self, top_n: int = 10) -> list:
        if not self.model or not self.feature_names:
            return []
        imp    = self.model.feature_importance(importance_type="gain")
        ranked = sorted(zip(self.feature_names, imp), key=lambda x: x[1], reverse=True)
        return ranked[:top_n]


# ============================================================
# MODEL REGISTRY — save/load per symbol (same pattern)
# ============================================================

class WorstCaseRegistry:
    """
    Save/Load worst case models per symbol

    Structure:
      ./models/{SYMBOL}/wc_lgbm_{date}.pkl
      ./models/{SYMBOL}/meta.json (extends existing meta)
    """

    def __init__(self, model_dir: str = WC_MODEL_DIR):
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)

    def save(self, symbol: str, model: WorstCaseModel):
        sym_dir  = self.model_dir / symbol
        sym_dir.mkdir(exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        path     = sym_dir / f"wc_lgbm_{date_str}.pkl"
        joblib.dump(model, path)

        # Extend existing meta.json
        meta = self._load_meta(symbol)
        meta["wc_version"]    = model.version
        meta["wc_auc"]        = model.auc_score
        meta["wc_recall"]     = model.recall_score
        meta["wc_trained_at"] = model.trained_at
        meta["wc_path"]       = str(path)
        self._save_meta(symbol, meta)

        logger.info(f"[WC-Registry] Saved {symbol} → {path.name}")

    def load(self, symbol: str) -> Optional[WorstCaseModel]:
        meta = self._load_meta(symbol)
        path = meta.get("wc_path")
        if path and Path(path).exists():
            try:
                model = joblib.load(path)
                logger.info(
                    f"[WC-Registry] Loaded {symbol} | "
                    f"AUC={model.auc_score} Recall={model.recall_score}"
                )
                return model
            except Exception as e:
                logger.warning(f"[WC-Registry] Load failed {symbol}: {e}")
        return None

    def needs_retrain(self, symbol: str) -> bool:
        meta    = self._load_meta(symbol)
        trained = meta.get("wc_trained_at", "")
        if not trained:
            return True
        try:
            last = datetime.fromisoformat(trained)
            return last.date() < datetime.now(timezone.utc).date()
        except Exception:
            return True

    def _load_meta(self, symbol: str) -> dict:
        path = self.model_dir / symbol / "meta.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_meta(self, symbol: str, meta: dict):
        sym_dir = self.model_dir / symbol
        sym_dir.mkdir(exist_ok=True)
        with open(sym_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)


# ============================================================
# LRU CACHE — ป้องกัน OOM (same pattern as TechnicalMLAnalyzer)
# ============================================================

class WCModelCache:
    """LRU Cache สำหรับ Worst Case models (แยกจาก ML model cache)"""

    def __init__(self, max_size: int = 50):
        from collections import OrderedDict
        self._cache: "OrderedDict" = OrderedDict()
        self.max_size = max_size

    def get(self, key: str) -> Optional[WorstCaseModel]:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, model: WorstCaseModel):
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
        self._cache[key] = model

    def clear(self):
        self._cache.clear()


# ============================================================
# GATE — Interface สำหรับ main.py
# ============================================================

class WorstCaseGate:
    """
    Gate WC: Worst Case Detector — Veto Gate

    ตำแหน่ง: ระหว่าง Gate 7 (Risk Manager) กับ Gate 19 (LLM CIO)

    Usage ใน main.py:
        from worst_case_detector import WorstCaseGate

        self.wc_gate = WorstCaseGate(model_dir="./models")

        # ใน process_news():
        verdict = self.wc_gate.evaluate(
            symbol=sym, df_15m=df_15m, atr=atr
        )
        if verdict.is_danger:
            logger.warning(f"🛡️ WC Gate VETO: {verdict.danger_score:.2f}")
            return  # skip trade
    """

    def __init__(
        self,
        model_dir:        str   = WC_MODEL_DIR,
        danger_threshold: float = WC_DANGER_THRESHOLD,
    ):
        self.registry  = WorstCaseRegistry(model_dir)
        self.cache     = WCModelCache(max_size=50)
        self.fe        = WorstCaseFeatures()
        self.threshold = danger_threshold

        # Stats
        self._total_checks = 0
        self._total_vetos  = 0
        self._condition_counts = {"cond1": 0, "cond2": 0, "cond3": 0}

    def evaluate(
        self,
        symbol:  str,
        df_15m:  pd.DataFrame,
        atr:     float = 0.0,
    ) -> WorstCaseVerdict:
        """
        ประเมินว่าสภาวะตลาดปัจจุบันเป็น Worst Case หรือไม่

        Args:
            symbol:  ชื่อหุ้น
            df_15m:  OHLCV DataFrame (15m bars, อย่างน้อย 30 bars)
            atr:     ATR_15m ปัจจุบัน (สำหรับ fallback)

        Returns:
            WorstCaseVerdict
        """
        import time
        t0 = time.time()
        self._total_checks += 1

        # ── Load model (cache → disk)
        model = self.cache.get(symbol)
        if model is None:
            model = self.registry.load(symbol)
            if model is not None:
                self.cache.put(symbol, model)

        # ── No model → pass (ไม่ block trade ถ้ายังไม่มี model)
        if model is None:
            return WorstCaseVerdict(
                is_danger=False, danger_score=0.0,
                conditions={"note": "no model available"},
                latency_ms=round((time.time() - t0) * 1000, 1),
            )

        # ── Compute features
        try:
            feat_df = self.fe.compute(df_15m)
            if feat_df.empty:
                return WorstCaseVerdict(is_danger=False, danger_score=0.0)

            # ดึง row สุดท้าย (current bar)
            features = feat_df.iloc[[-1]]

            # Align features กับ model training columns
            if model.feature_names:
                missing = set(model.feature_names) - set(features.columns)
                for col in missing:
                    features[col] = 0.0
                features = features[model.feature_names]

        except Exception as e:
            logger.warning(f"[WC-Gate] Feature error {symbol}: {e}")
            return WorstCaseVerdict(is_danger=False, danger_score=0.0)

        # ── Predict
        danger_prob = model.predict(features)
        is_danger   = danger_prob >= self.threshold

        # ── Feature importance (top 5)
        top_feats = []
        if is_danger:
            top_feats = [f[0] for f in model.feature_importance(5)]

        # ── Stats
        if is_danger:
            self._total_vetos += 1

        latency = round((time.time() - t0) * 1000, 1)

        verdict = WorstCaseVerdict(
            is_danger=is_danger,
            danger_score=round(danger_prob, 4),
            conditions={
                "threshold": self.threshold,
                "model_auc": model.auc_score,
                "model_recall": model.recall_score,
            },
            top_features=top_feats,
            model_version=model.version,
            latency_ms=latency,
        )

        if is_danger:
            logger.warning(
                f"🛡️ [WC-Gate] VETO {symbol} | "
                f"danger={danger_prob:.3f} > {self.threshold} | "
                f"top={top_feats[:3]} | {latency}ms"
            )
        else:
            logger.debug(
                f"[WC-Gate] PASS {symbol} | "
                f"danger={danger_prob:.3f} | {latency}ms"
            )

        return verdict

    def train_symbol(
        self,
        symbol: str,
        df_15m: pd.DataFrame,
    ) -> float:
        """
        Train Worst Case model สำหรับ symbol เดียว

        Args:
            symbol: ชื่อหุ้น
            df_15m: OHLCV DataFrame (15m bars, อย่างน้อย 200 bars)

        Returns:
            AUC score (0.0 ถ้า fail)
        """
        if len(df_15m) < WC_MIN_TRAIN_BARS:
            logger.warning(
                f"[WC-Train] {symbol}: {len(df_15m)} bars < {WC_MIN_TRAIN_BARS} → skip"
            )
            return 0.0

        # ── Step 1: Generate labels
        labeler  = WorstCaseLabeler()
        labeled  = labeler.generate(df_15m)

        # ── Step 2: Compute features
        feat_df = self.fe.compute(df_15m)

        # ── Step 3: Align features & labels (drop NaN from look-ahead)
        y = labeled["wc_target"]
        valid_mask = y.notna() & feat_df.notna().all(axis=1)
        X = feat_df.loc[valid_mask]
        y = y.loc[valid_mask].astype(int)

        if len(X) < WC_MIN_TRAIN_BARS:
            logger.warning(f"[WC-Train] {symbol}: valid samples {len(X)} < {WC_MIN_TRAIN_BARS}")
            return 0.0

        if y.sum() == 0:
            logger.warning(f"[WC-Train] {symbol}: no positive labels → skip")
            return 0.0

        # ── Step 4: Train
        model = WorstCaseModel()
        auc   = model.train(X, y)

        if auc > 0:
            # ── Step 5: Save
            self.registry.save(symbol, model)
            self.cache.put(symbol, model)
            logger.info(f"[WC-Train] ✅ {symbol} AUC={auc:.4f}")

        return auc

    def get_stats(self) -> dict:
        return {
            "total_checks": self._total_checks,
            "total_vetos":  self._total_vetos,
            "veto_rate":    round(self._total_vetos / max(1, self._total_checks), 4),
        }