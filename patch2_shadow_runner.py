"""
PATCH 2: shadow_runner.py — Worst Case Gate in Shadow Mode
============================================================

ต้อง apply 3 patches:
  Patch 2A: ALL_GATE_IDS — เพิ่ม "wc"
  Patch 2B: ShadowCandidate — เพิ่ม wc_danger_score field
  Patch 2C: process_candidate() — เพิ่ม Gate WC ระหว่าง Gate 7 กับ Gate 19
"""


# ============================================================
# PATCH 2A: ALL_GATE_IDS — เพิ่ม "wc"
# ============================================================

PATCH_2A_ANCHOR = '''ALL_GATE_IDS = {
    "daily_loss", "session", "sentiment", "price", "universe",
    "regime", "ml", "max_orders", "rate_limit", "wash_sale",
    "no_hedge", "streak", "risk", "gate19",
}'''

PATCH_2A_REPLACEMENT = '''ALL_GATE_IDS = {
    "daily_loss", "session", "sentiment", "price", "universe",
    "regime", "ml", "max_orders", "rate_limit", "wash_sale",
    "no_hedge", "streak", "risk", "wc", "gate19",
}'''


# ============================================================
# PATCH 2B: ShadowCandidate — เพิ่ม wc fields
# ============================================================
# หา ShadowCandidate dataclass แล้วเพิ่ม field ใน section ที่เหมาะสม
# (ต้องดู code จริงว่า fields อยู่ตรงไหน)

PATCH_2B_DESCRIPTION = """
เพิ่ม fields เหล่านี้ใน @dataclass ShadowCandidate:

    # ── Worst Case Gate
    wc_danger_score: float = 0.0       # 0.0–1.0 probability of worst case
    wc_is_danger:    bool  = False      # True = would be VETO'd
    wc_top_features: list  = field(default_factory=list)
"""


# ============================================================
# PATCH 2C: process_candidate() — Gate WC ระหว่าง Gate 7 กับ Gate 19
# ============================================================

PATCH_2C_ANCHOR = """        # ── GATE 19: LLM CIO
        if self._is_skipped("gate19"):"""

PATCH_2C_REPLACEMENT = """        # ── GATE WC: Worst Case Detector
        if self._is_skipped("wc"):
            sc.gate_results.append(self._gate(
                "wc", "Gate WC: Worst Case Detector",
                True, "SKIPPED by --skip-gates",
            ))
        else:
            try:
                if hasattr(p, 'wc_gate') and p.wc_gate is not None:
                    # ดึง 15m data
                    wc_df = None
                    try:
                        from data_pipeline_manager import safe_yf_download
                        wc_df = safe_yf_download(sym, period="5d", interval="15m")
                    except Exception:
                        pass

                    if wc_df is not None and len(wc_df) >= 30:
                        wc_verdict = p.wc_gate.evaluate(
                            symbol=sym, df_15m=wc_df, atr=atr if 'atr' in dir() else 0.0
                        )
                        sc.wc_danger_score = wc_verdict.danger_score
                        sc.wc_is_danger    = wc_verdict.is_danger
                        sc.wc_top_features = wc_verdict.top_features

                        sc.gate_results.append(self._gate(
                            "wc", "Gate WC: Worst Case Detector",
                            not wc_verdict.is_danger,
                            f"danger={wc_verdict.danger_score:.3f} "
                            f"{'VETO' if wc_verdict.is_danger else 'PASS'} "
                            f"top={wc_verdict.top_features[:3]}",
                            value=wc_verdict.danger_score,
                        ))
                    else:
                        sc.gate_results.append(self._gate(
                            "wc", "Gate WC: Worst Case Detector",
                            True, "insufficient data — pass (fail-open)",
                        ))
                else:
                    sc.gate_results.append(self._gate(
                        "wc", "Gate WC: Worst Case Detector",
                        True, "no wc_gate module — pass",
                    ))
            except Exception as e:
                sc.gate_results.append(self._gate(
                    "wc", "Gate WC: Worst Case Detector",
                    True, f"error: {e} — pass (fail-open)",
                ))

        # ── GATE 19: LLM CIO
        if self._is_skipped("gate19"):"""


# ============================================================
# PATCH SUMMARY
# ============================================================

def print_patches():
    print("=" * 60)
    print("  PATCH 2: shadow_runner.py — 3 changes required")
    print("=" * 60)

    print("""
  Patch 2A: ALL_GATE_IDS — เพิ่ม "wc"
    → ทำให้ --skip-gates wc ทำงานได้

  Patch 2B: ShadowCandidate — เพิ่ม wc fields
    → report จะแสดง danger_score, is_danger, top_features

  Patch 2C: process_candidate() — Gate WC block
    → วาง Gate WC ระหว่าง Gate 7 กับ Gate 19
    → fail-open design: ถ้า error/no data/no model → pass
    → Shadow mode: บันทึกผลแต่ไม่ block
    """)


if __name__ == "__main__":
    print_patches()
