"""
PATCH 1: main.py — Worst Case Gate Integration
=================================================

ต้อง apply 4 patches ตามลำดับ:
  Patch 1A: Import + loader function (ส่วนหัวไฟล์)
  Patch 1B: _init_modules() — initialize wc_gate
  Patch 1C: process_news() — Gate WC ระหว่าง Gate 7 กับ Gate 19
  Patch 1D: startup_scan_and_train() + _daily_retrain_scheduler() — train WC models

วิธี apply:
  ใช้ str_replace ใน editor หรือ patch command
  *** สำคัญ: ทุก patch ต้อง apply ทั้งหมด ไม่งั้น Gate WC จะไม่ทำงาน ***
"""


# ============================================================
# PATCH 1A: เพิ่ม import + loader (วางใกล้ _load_executor)
# ============================================================

PATCH_1A_ANCHOR = """def _load_executor():
    try:
        from orders.universal_order_executor import UniversalOrderExecutor
        return UniversalOrderExecutor
    except ImportError:
        return None"""

PATCH_1A_REPLACEMENT = """def _load_executor():
    try:
        from orders.universal_order_executor import UniversalOrderExecutor
        return UniversalOrderExecutor
    except ImportError:
        return None


def _load_wc_gate():
    \"\"\"Load Worst Case Gate (optional — graceful fallback if not available)\"\"\"
    try:
        from models.worst_case_detector import WorstCaseGate
        return WorstCaseGate
    except ImportError:
        logger.warning("[WC] worst_case_detector not available — Gate WC disabled")
        return None"""


# ============================================================
# PATCH 1B: _init_modules() — เพิ่ม wc_gate initialization
# ============================================================

# หา comment "# 7." ที่อยู่ท้าย _init_modules แล้วเพิ่มข้างหลัง
# (ดูจาก code ปัจจุบัน จะมี # 6. Session Filter แล้วตามด้วย # 7.)

PATCH_1B_ANCHOR = """        # 6. Session Filter
        self.session_filter = MarketSessionFilter()

        # 7."""

PATCH_1B_REPLACEMENT = """        # 6. Session Filter
        self.session_filter = MarketSessionFilter()

        # 7. Worst Case Gate (Toxic Market Detector)
        WCG = _load_wc_gate()
        self.wc_gate = WCG(model_dir=self.cfg.MODEL_DIR if hasattr(self.cfg, 'MODEL_DIR') else "./models") if WCG else None
        if self.wc_gate:
            logger.info("[Init] ✅ Gate WC: Worst Case Detector loaded")
        else:
            logger.info("[Init] ⚠️ Gate WC: disabled (module not found)")

        # 8."""


# ============================================================
# PATCH 1C: process_news() — Gate WC ระหว่าง Gate 7 กับ Gate 19
# ============================================================

# หาจุดที่ Gate 7 จบ แล้ว Gate 19 เริ่ม
# จาก code: หลัง "shares = order_spec["size"]" + "else: shares = 10"
# แล้วก่อน "# GATE 19: LLM CIO"

PATCH_1C_ANCHOR = """        # ══════════════════════════════════════════════════════
        # GATE 19: LLM CIO — Final Risk Veto
        # ══════════════════════════════════════════════════════
        verdict = self.gate19.evaluate_trade("""

PATCH_1C_REPLACEMENT = """        # ══════════════════════════════════════════════════════
        # GATE WC: Worst Case Detector — Toxic Market Veto
        # ══════════════════════════════════════════════════════
        if self.wc_gate:
            try:
                # ดึง 15m data สำหรับ WC features (ใช้ data ที่มีอยู่แล้ว หรือดึงใหม่)
                wc_df = None
                if hasattr(self, '_wc_df_cache') and sym in self._wc_df_cache:
                    wc_df = self._wc_df_cache[sym]
                else:
                    try:
                        from data_pipeline_manager import safe_yf_download
                        wc_df = safe_yf_download(sym, period="5d", interval="15m")
                    except Exception:
                        pass

                if wc_df is not None and len(wc_df) >= 30:
                    wc_verdict = self.wc_gate.evaluate(
                        symbol=sym, df_15m=wc_df, atr=atr
                    )
                    if wc_verdict.is_danger:
                        logger.warning(
                            f"🛡️ Gate WC VETO: {sym} | "
                            f"danger={wc_verdict.danger_score:.3f} | "
                            f"top_features={wc_verdict.top_features[:3]}"
                        )
                        return  # ← VETO: ไม่ต้องเสีย LLM API call
                    else:
                        logger.info(
                            f"✅ Gate WC PASS: {sym} | "
                            f"danger={wc_verdict.danger_score:.3f}"
                        )
                else:
                    logger.debug(f"[WC] {sym}: insufficient data for WC check — pass")
            except Exception as e:
                logger.warning(f"[WC] {sym}: error {e} — pass (fail-open)")

        # ══════════════════════════════════════════════════════
        # GATE 19: LLM CIO — Final Risk Veto
        # ══════════════════════════════════════════════════════
        verdict = self.gate19.evaluate_trade("""


# ============================================================
# PATCH 1D: startup_scan_and_train() — เพิ่ม WC model training
# ============================================================

# หาใน startup_scan_and_train หลังจาก train ML models ปกติ
# เพิ่ม WC training ที่ piggyback กับ daily pipeline

PATCH_1D_DESCRIPTION = """
วาง code นี้ใน startup_scan_and_train() หลังจาก ML training เสร็จ:

        # ── Train Worst Case models (piggyback daily pipeline)
        if self.wc_gate:
            try:
                from data_pipeline_manager import safe_yf_download
                watchlist = self._tiers.tier1 + self._tiers.tier2 if self._tiers else []
                wc_trained = 0
                for sym in watchlist[:20]:  # cap at 20 symbols
                    if self.wc_gate.registry.needs_retrain(sym):
                        try:
                            df = safe_yf_download(sym, period="60d", interval="15m")
                            if df is not None and len(df) >= 200:
                                auc = self.wc_gate.train_symbol(sym, df)
                                if auc > 0:
                                    wc_trained += 1
                        except Exception as e:
                            logger.debug(f"[WC-Train] {sym}: {e}")
                logger.info(f"[WC] Daily train: {wc_trained}/{len(watchlist)} symbols")
            except Exception as e:
                logger.warning(f"[WC] Daily train error: {e}")
"""


# ============================================================
# PATCH SUMMARY
# ============================================================

def print_patches():
    print("=" * 60)
    print("  PATCH 1: main.py — 4 changes required")
    print("=" * 60)

    patches = [
        ("1A", "Import + loader function", PATCH_1A_ANCHOR[:60] + "..."),
        ("1B", "_init_modules() — add wc_gate", PATCH_1B_ANCHOR[:60] + "..."),
        ("1C", "process_news() — Gate WC before Gate 19", PATCH_1C_ANCHOR[:60] + "..."),
        ("1D", "startup_scan_and_train() — WC retrain", "See PATCH_1D_DESCRIPTION"),
    ]

    for pid, desc, anchor in patches:
        print(f"\n  Patch {pid}: {desc}")
        print(f"    Anchor: {anchor}")

    print("\n  ⚠️  ทั้ง 4 patches ต้อง apply ครบ")
    print("      ถ้าขาด 1A → NameError: _load_wc_gate")
    print("      ถ้าขาด 1B → AttributeError: 'TradingPipeline' has no attribute 'wc_gate'")
    print("      ถ้าขาด 1C → Gate WC ไม่ถูกเรียกใน live trade")
    print("      ถ้าขาด 1D → WC model ไม่มี → Gate WC จะ pass ทุกอัน (fail-open)")


if __name__ == "__main__":
    print_patches()
