"""
Main entry point.

Usage:
    python -m solution.main --input-dir /app/input_files/input --output-dir /app/output

Behavior:
- Load + validate inputs.
- On non-recoverable errors: write validation_report.json only, exit non-zero.
- On success: write all 6 deliverables, exit zero.
- No network, no wall clock, no machine timezone.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from .loader import load_and_validate
from .engine import (
    InvalidScenarioError,
    compute_greeks,
    compute_pnl,
    compute_positions,
    compute_stress,
    compute_valuation,
)
from .output import (
    write_greeks,
    write_pnl,
    write_positions_eod,
    write_stress_report,
    write_validation_report,
    write_valuation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BTC/ETH European option portfolio risk & PnL engine")
    parser.add_argument("--input-dir", required=True, help="Path to input directory")
    parser.add_argument("--output-dir", required=True, help="Path to output directory")
    args = parser.parse_args(argv)

    output_dir = os.path.abspath(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: load + validate
    data, vr = load_and_validate(args.input_dir)

    # If validation has errors, write only validation_report.json and exit non-zero
    if data is None or vr.has_errors:
        write_validation_report(os.path.join(output_dir, "validation_report.json"), vr)
        # Clean up any partial financial outputs that might exist
        for fname in ["positions_eod.csv", "valuation.csv", "greeks.csv",
                      "pnl_attribution.csv", "stress_report.json"]:
            p = os.path.join(output_dir, fname)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return 1

    # Step 2: compute everything
    try:
        positions = compute_positions(data)
        valuation = compute_valuation(data, positions)
        greeks_rows = compute_greeks(data, positions, valuation)
        pnl_rows = compute_pnl(data, positions, valuation, greeks_rows)
        stress_report = compute_stress(data, positions, valuation)
    except InvalidScenarioError as e:
        # Non-recoverable: INVALID_SCENARIO. Write validation_report only.
        from .loader import ValidationRecord
        vr.errors.append(ValidationRecord("INVALID_SCENARIO", "scenarios.json", None, None, str(e)))
        write_validation_report(os.path.join(output_dir, "validation_report.json"), vr)
        return 1
    except Exception as e:
        # Unexpected runtime error during computation -> treat as non-recoverable
        from .loader import ValidationRecord
        vr.errors.append(ValidationRecord("RUNTIME_ERROR", None, None, None, f"{e}\n{traceback.format_exc()}"))
        write_validation_report(os.path.join(output_dir, "validation_report.json"), vr)
        return 1

    # Step 3: write all outputs
    write_positions_eod(os.path.join(output_dir, "positions_eod.csv"), positions)
    write_valuation(os.path.join(output_dir, "valuation.csv"), valuation)
    write_greeks(os.path.join(output_dir, "greeks.csv"), greeks_rows)
    write_pnl(os.path.join(output_dir, "pnl_attribution.csv"), pnl_rows)
    write_stress_report(os.path.join(output_dir, "stress_report.json"), stress_report)
    write_validation_report(os.path.join(output_dir, "validation_report.json"), vr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
