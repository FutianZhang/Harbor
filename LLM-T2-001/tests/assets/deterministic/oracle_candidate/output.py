"""
Output serialization: deterministic CSV and JSON writers.

Rules (from output_schema.json):
- CSV: UTF-8, exactly one header row, no index column.
- JSON: strict UTF-8, keys written in lexicographic order.
- Numbers: full float64 precision, period as decimal separator, no NaN/Infinity.
- Sorting per schema specs.
"""

from __future__ import annotations

import csv
import json
import os
from typing import Any


def _fmt_num(x: Any) -> str:
    """Format a number for CSV output with full float64 precision."""
    if x is None:
        return ""
    f = float(x)
    # Normalize -0.0 to 0.0 to avoid negative-zero representation
    if f == 0.0:
        f = abs(f)  # abs(-0.0) == 0.0 (positive zero)
    # Use repr to preserve full precision; Python's repr gives shortest round-trip
    return repr(f)


def write_positions_eod(path: str, positions: dict[str, dict[str, float]]) -> None:
    cols = ["instrument_name", "quantity_t0", "net_trade_quantity", "quantity_t1"]
    rows = []
    for name in sorted(positions.keys()):
        p = positions[name]
        rows.append({
            "instrument_name": name,
            "quantity_t0": _fmt_num(p["quantity_t0"]),
            "net_trade_quantity": _fmt_num(p["net_trade_quantity"]),
            "quantity_t1": _fmt_num(p["quantity_t1"]),
        })
    _write_csv(path, cols, rows)


def write_valuation(path: str, valuation: dict[str, dict[str, float]]) -> None:
    cols = ["instrument_name", "quantity_t0", "quantity_t1", "contract_multiplier",
            "unit_value_t0_usd", "unit_value_t1_usd",
            "market_value_t0_usd", "market_value_t1_usd"]
    rows = []
    for name in sorted(valuation.keys()):
        v = valuation[name]
        rows.append({
            "instrument_name": name,
            "quantity_t0": _fmt_num(v["quantity_t0"]),
            "quantity_t1": _fmt_num(v["quantity_t1"]),
            "contract_multiplier": _fmt_num(v["contract_multiplier"]),
            "unit_value_t0_usd": _fmt_num(v["unit_value_t0_usd"]),
            "unit_value_t1_usd": _fmt_num(v["unit_value_t1_usd"]),
            "market_value_t0_usd": _fmt_num(v["market_value_t0_usd"]),
            "market_value_t1_usd": _fmt_num(v["market_value_t1_usd"]),
        })
    _write_csv(path, cols, rows)


GREEKS_COLS = [
    "aggregation_level", "instrument_name", "valuation_state", "valuation_timestamp_utc",
    "quantity", "contract_multiplier",
    "unit_delta", "unit_gamma", "unit_vega_decimal", "unit_vega_1vol",
    "unit_theta_year", "unit_theta_day", "unit_vanna", "unit_volga",
    "position_delta", "position_gamma", "position_vega_decimal", "position_vega_1vol",
    "position_theta_year", "position_theta_day", "position_vanna", "position_volga",
]


def write_greeks(path: str, greeks_rows: list[dict[str, Any]]) -> None:
    rows = []
    for r in greeks_rows:
        row = {}
        for c in GREEKS_COLS:
            v = r.get(c)
            if c in ("aggregation_level", "instrument_name", "valuation_state", "valuation_timestamp_utc"):
                row[c] = v if v is not None else ""
            elif v is None:
                # PORTFOLIO row: quantity/contract_multiplier and all unit_* fields are empty cells
                row[c] = ""
            else:
                row[c] = _fmt_num(v)
        rows.append(row)
    _write_csv(path, GREEKS_COLS, rows)


PNL_COLS = [
    "aggregation_level", "instrument_name",
    "beginning_market_value_usd", "ending_market_value_usd",
    "trade_cashflow_usd", "fees_usd",
    "carry_actual_pnl_usd", "trade_to_t1_pnl_usd", "total_pnl_usd",
    "delta_pnl_usd", "gamma_pnl_usd", "vega_pnl_usd", "theta_pnl_usd",
    "vanna_pnl_usd", "volga_pnl_usd", "explained_pnl_usd", "residual_pnl_usd",
]


def write_pnl(path: str, pnl_rows: list[dict[str, Any]]) -> None:
    rows = []
    for r in pnl_rows:
        row = {}
        for c in PNL_COLS:
            v = r.get(c)
            if c in ("aggregation_level", "instrument_name"):
                row[c] = v if v is not None else ""
            else:
                row[c] = _fmt_num(v)
        rows.append(row)
    _write_csv(path, PNL_COLS, rows)


def _write_csv(path: str, cols: list[str], rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def _sort_dict_keys(d: Any) -> Any:
    """Recursively sort dict keys lexicographically for JSON output."""
    if isinstance(d, dict):
        return {k: _sort_dict_keys(d[k]) for k in sorted(d.keys())}
    if isinstance(d, list):
        return [_sort_dict_keys(x) for x in d]
    return d


def _clean_json_num(x: Any) -> Any:
    """Ensure no NaN/Infinity leaks into JSON. Numbers stay as numbers."""
    if isinstance(x, float):
        if x != x or x == float("inf") or x == float("-inf"):
            return 0.0
        return x
    return x


def _sanitize_json(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _sanitize_json(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_sanitize_json(x) for x in d]
    return _clean_json_num(d)


def write_stress_report(path: str, report: dict[str, Any]) -> None:
    sanitized = _sanitize_json(report)
    ordered = _sort_dict_keys(sanitized)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def write_validation_report(path: str, vr) -> None:
    report = {
        "schema_version": 1,
        "status": vr.status,
        "warnings": vr.sorted_warnings(),
        "errors": vr.sorted_errors(),
    }
    ordered = _sort_dict_keys(report)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
