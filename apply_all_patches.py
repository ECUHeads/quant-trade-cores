#!/usr/bin/env python3
"""
apply_all_patches.py — Apply all WC Gate patches to the codebase
=================================================================

Usage:
  python apply_all_patches.py                 # Apply all patches
  python apply_all_patches.py --dry-run       # Show what would change without modifying
  python apply_all_patches.py --check         # Check if patches are already applied
  python apply_all_patches.py --backup        # Create backups before applying

Requirements:
  - Run from project root directory
  - Files must exist: main.py, mode/shadow_runner.py, models/worst_case_detector.py
"""

import os
import re
import sys
import shutil
import argparse
from datetime import datetime
from pathlib import Path


# ============================================================
# PATCH DEFINITIONS
# ============================================================

PATCHES = []


# ── PATCH 1A: main.py — Import _load_wc_gate()
PATCHES.append({
    "id": "1A",
    "file": "main.py",
    "description": "Add _load_wc_gate() loader function",
    "find": """def _load_executor():
    try:
        from orders.universal_order_executor import UniversalOrderExecutor
        return UniversalOrderExecutor
    except ImportError:
        return None""",
    "replace": """def _load_executor():
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
        return None""",
    "check_applied": "_load_wc_gate",
})


# ── PATCH 1B: main.py — Initialize wc_gate in _init_modules()
PATCHES.append({
    "id": "1B",
    "file": "main.py",
    "description": "Initialize wc_gate in _init_modules()",
    "find": """        # 6. Session Filter
        self.session_filter = MarketSessionFilter()

        # 7.""",
    "replace": """        # 6. Session Filter
        self.session_filter = MarketSessionFilter()

        # 7. Worst Case Gate (Toxic Market Detector)
        WCG = _load_wc_gate()
        self.wc_gate = WCG(model_dir=self.cfg.MODEL_DIR if hasattr(self.cfg, 'MODEL_DIR') else "./models") if WCG else None
        if self.wc_gate:
            logger.info("[Init] ✅ Gate WC: Worst Case Detector loaded")
        else:
            logger.info("[Init] ⚠️ Gate WC: disabled (module not found)")

        # 8.""",
    "check_applied": "self.wc_gate",
})


# ── PATCH 1C: main.py — Gate WC in process_news()
PATCHES.append({
    "id": "1C",
    "file": "main.py",
    "description": "Add Gate WC between Gate 7 and Gate 19 in process_news()",
    "find": """        # ══════════════════════════════════════════════════════
        # GATE 19: LLM CIO — Final Risk Veto
        # ══════════════════════════════════════════════════════
        verdict = self.gate19.evaluate_trade(""",
    "replace": """        # ══════════════════════════════════════════════════════
        # GATE WC: Worst Case Detector — Toxic Market Veto
        # ══════════════════════════════════════════════════════
        if self.wc_gate:
            try:
                wc_df = None
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
                        return  # VETO: skip trade, save LLM API cost
                    else:
                        logger.info(
                            f"✅ Gate WC PASS: {sym} | "
                            f"danger={wc_verdict.danger_score:.3f}"
                        )
                else:
                    logger.debug(f"[WC] {sym}: insufficient data — pass")
            except Exception as e:
                logger.warning(f"[WC] {sym}: error {e} — pass (fail-open)")

        # ══════════════════════════════════════════════════════
        # GATE 19: LLM CIO — Final Risk Veto
        # ══════════════════════════════════════════════════════
        verdict = self.gate19.evaluate_trade(""",
    "check_applied": "GATE WC: Worst Case Detector",
})


# ── PATCH 2A: shadow_runner.py — Add "wc" to ALL_GATE_IDS
PATCHES.append({
    "id": "2A",
    "file": "mode/shadow_runner.py",
    "description": 'Add "wc" to ALL_GATE_IDS',
    "find": """ALL_GATE_IDS = {
    "daily_loss", "session", "sentiment", "price", "universe",
    "regime", "ml", "max_orders", "rate_limit", "wash_sale",
    "no_hedge", "streak", "risk", "gate19",
}""",
    "replace": """ALL_GATE_IDS = {
    "daily_loss", "session", "sentiment", "price", "universe",
    "regime", "ml", "max_orders", "rate_limit", "wash_sale",
    "no_hedge", "streak", "risk", "wc", "gate19",
}""",
    "check_applied": '"wc"',
})


# ── PATCH 2C: shadow_runner.py — Gate WC in process_candidate()
PATCHES.append({
    "id": "2C",
    "file": "mode/shadow_runner.py",
    "description": "Add Gate WC between Gate 7 and Gate 19 in process_candidate()",
    "find": """        # ── GATE 19: LLM CIO
        if self._is_skipped("gate19"):""",
    "replace": """        # ── GATE WC: Worst Case Detector
        if self._is_skipped("wc"):
            sc.gate_results.append(self._gate(
                "wc", "Gate WC: Worst Case Detector",
                True, "SKIPPED by --skip-gates",
            ))
        else:
            try:
                if hasattr(p, 'wc_gate') and p.wc_gate is not None:
                    wc_df = None
                    try:
                        from data_pipeline_manager import safe_yf_download
                        wc_df = safe_yf_download(sym, period="5d", interval="15m")
                    except Exception:
                        pass

                    if wc_df is not None and len(wc_df) >= 30:
                        wc_verdict = p.wc_gate.evaluate(
                            symbol=sym, df_15m=wc_df,
                            atr=atr if 'atr' in dir() else 0.0
                        )
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
        if self._is_skipped("gate19"):""",
    "check_applied": "Gate WC: Worst Case Detector",
})


# ============================================================
# APPLY ENGINE
# ============================================================

def check_patch(patch: dict, project_root: str) -> str:
    """Check if patch is already applied. Returns: 'applied', 'ready', 'mismatch'"""
    filepath = os.path.join(project_root, patch["file"])
    if not os.path.exists(filepath):
        return "file_missing"

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if patch["check_applied"] in content:
        return "applied"

    if patch["find"] in content:
        return "ready"

    return "mismatch"


def apply_patch(patch: dict, project_root: str, dry_run: bool = False) -> bool:
    """Apply a single patch. Returns True if successful."""
    filepath = os.path.join(project_root, patch["file"])

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    if patch["find"] not in content:
        return False

    new_content = content.replace(patch["find"], patch["replace"], 1)

    if not dry_run:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)

    return True


def backup_file(filepath: str):
    """Create timestamped backup"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{filepath}.bak_wc_{ts}"
    shutil.copy2(filepath, backup)
    return backup


def main():
    parser = argparse.ArgumentParser(description="Apply WC Gate patches")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--check", action="store_true", help="Check patch status only")
    parser.add_argument("--backup", action="store_true", help="Create backups before applying")
    parser.add_argument("--root", default=".", help="Project root directory")
    args = parser.parse_args()

    root = args.root

    print("=" * 60)
    print("  WC Gate Patch Applicator")
    print(f"  Project root: {os.path.abspath(root)}")
    print("=" * 60)

    # ── Check status
    results = {}
    for patch in PATCHES:
        status = check_patch(patch, root)
        results[patch["id"]] = status
        icon = {"applied": "✅", "ready": "🔧", "mismatch": "❌", "file_missing": "📁"}
        print(f"  [{icon.get(status, '?')}] Patch {patch['id']}: {patch['description']}")
        print(f"       File: {patch['file']} — Status: {status}")

    if args.check:
        applied = sum(1 for s in results.values() if s == "applied")
        total = len(PATCHES)
        print(f"\n  Summary: {applied}/{total} patches applied")
        return

    # ── Apply
    if args.dry_run:
        print("\n  [DRY-RUN] No files will be modified\n")

    applied = 0
    skipped = 0
    failed = 0

    for patch in PATCHES:
        status = results[patch["id"]]

        if status == "applied":
            print(f"  [SKIP] Patch {patch['id']}: already applied")
            skipped += 1
            continue

        if status == "mismatch":
            print(f"  [FAIL] Patch {patch['id']}: anchor text not found — manual fix needed")
            failed += 1
            continue

        if status == "file_missing":
            print(f"  [FAIL] Patch {patch['id']}: file {patch['file']} not found")
            failed += 1
            continue

        # ── Backup
        if args.backup and not args.dry_run:
            filepath = os.path.join(root, patch["file"])
            bak = backup_file(filepath)
            print(f"  [BAK]  {bak}")

        # ── Apply
        ok = apply_patch(patch, root, dry_run=args.dry_run)
        if ok:
            action = "WOULD APPLY" if args.dry_run else "APPLIED"
            print(f"  [{action}] Patch {patch['id']}: {patch['description']}")
            applied += 1
        else:
            print(f"  [FAIL] Patch {patch['id']}: apply failed")
            failed += 1

    print(f"\n{'='*60}")
    print(f"  Results: {applied} applied, {skipped} skipped, {failed} failed")
    if failed > 0:
        print(f"  ⚠️  {failed} patches need manual intervention")
    elif applied > 0 and not args.dry_run:
        print(f"  ✅ All patches applied successfully!")
        print(f"\n  Next steps:")
        print(f"    1. python models/worst_case_detector.py --symbol NVDA --train")
        print(f"    2. python main.py --profile TTP_5K_FLEX --mode shadow --skip-gates gate19")
        print(f"    3. python main.py --profile TTP_5K_FLEX --dry-run")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
