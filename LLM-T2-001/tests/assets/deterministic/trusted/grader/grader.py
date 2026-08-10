"""Fault-isolated Phase 04B machine grader."""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grader.hidden_fixtures import REQUIRED_VARIANTS, suite_fingerprint
from grader.invariants import check_invariants
from grader.io import compare_validation_records, validate_warning_messages
from grader.reference import CSV_COLUMNS, load_inputs, render_reference_outputs
from grader.rubric import RUBRIC_ITEMS
from grader.tolerances import NUMERIC_FIELD_KINDS, close_numeric, tolerance_for


def _warning_identity(record: dict[str, Any]) -> tuple[Any, ...]:
    return (record.get("code"), record.get("file"), record.get("row_key"), record.get("field"))


@dataclass(frozen=True)
class Finding:
    item_id: str
    detail: str


@dataclass(frozen=True)
class GradeResult:
    score: int
    findings: tuple[Finding, ...]
    passed_items: tuple[str, ...]
    failed_items: tuple[str, ...]


FILE_ITEMS = {
    "positions_eod.csv": {"POS-1", "POS-2", "POS-3"},
    "valuation.csv": {"PRC-1", "PRC-2", "PRC-3", "PNL-1"},
    "greeks.csv": {"GRK-1", "GRK-2", "GRK-3", "GRK-4", "GRK-5"},
    "pnl_attribution.csv": {"PNL-1", "PNL-2", "PNL-3", "ATT-1", "ATT-2", "ATT-3", "ATT-4"},
    "stress_report.json": {"STR-1", "STR-2", "STR-3"},
    "validation_report.json": {"VAL-1", "VAL-2", "VAL-3", "VAL-4"},
}


def _field_item(filename: str, field: str, row: dict[str, Any], inputs: dict[str, Any]) -> str:
    if filename == "positions_eod.csv":
        return {"quantity_t0": "POS-1", "net_trade_quantity": "POS-2", "quantity_t1": "POS-3"}[field]
    if filename == "valuation.csv":
        if field.startswith("market_value") or field in {"quantity_t0", "quantity_t1", "contract_multiplier"}:
            return "PNL-1"
        meta = next(item for item in inputs["metadata_rows"] if item["instrument_name"] == row["instrument_name"])
        state = "t0" if "t0" in field else "t1"; clock = inputs["config"][f"benchmark_{state}_utc"]
        expiry_dt = datetime.fromisoformat(meta["expiration_timestamp"][:-1] + "+00:00")
        clock_dt = datetime.fromisoformat(clock[:-1] + "+00:00")
        if (expiry_dt - clock_dt).total_seconds() <= 2 * 86400:
            return "PRC-3"
        return "PRC-1" if meta["option_type"] == "call" else "PRC-2"
    if filename == "greeks.csv":
        if field.startswith("position_") or row.get("aggregation_level") == "PORTFOLIO": return "GRK-5"
        if "vega" in field: return "GRK-2"
        if "theta" in field: return "GRK-3"
        if "vanna" in field or "volga" in field: return "GRK-4"
        return "GRK-1"
    if filename == "pnl_attribution.csv":
        if field in {"beginning_market_value_usd", "ending_market_value_usd"}: return "PNL-1"
        if field in {"trade_cashflow_usd", "fees_usd", "trade_to_t1_pnl_usd"}: return "PNL-2"
        if field in {"carry_actual_pnl_usd", "total_pnl_usd"}: return "PNL-3"
        if field in {"delta_pnl_usd", "gamma_pnl_usd"}: return "ATT-1"
        if field in {"vega_pnl_usd", "theta_pnl_usd"}: return "ATT-2"
        if field in {"vanna_pnl_usd", "volga_pnl_usd"}: return "ATT-3"
        return "ATT-4"
    if filename == "stress_report.json":
        if field in {"spot_shock_pct", "vol_shock_abs", "stressed_spot_usd", "stressed_iv_decimal"}: return "STR-1"
        if field.startswith("portfolio_"): return "STR-3"
        return "STR-2"
    raise KeyError((filename, field))


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _read_json(path: Path) -> Any:
    def reject(value):
        raise ValueError(f"non-standard JSON constant {value}")
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject)


def _is_json_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def _json_keys_sorted(value: Any) -> bool:
    if isinstance(value, dict):
        return list(value) == sorted(value) and all(_json_keys_sorted(item) for item in value.values())
    if isinstance(value, list):
        return all(_json_keys_sorted(item) for item in value)
    return True


def _add(store: dict[str, set[str]], item_id: str, detail: str) -> None:
    store.setdefault(item_id, set()).add(detail)


def _greek_position_is_internally_consistent(
    field: str, row: dict[str, Any], rows: list[dict[str, str]],
) -> bool:
    """Keep upstream unit errors from cascading into the scaling capability."""
    if not field.startswith("position_"):
        return False
    try:
        if row.get("aggregation_level") == "INSTRUMENT":
            suffix = field.removeprefix("position_")
            expected = float(row["quantity"]) * float(row["contract_multiplier"]) * float(row[f"unit_{suffix}"])
        else:
            expected = sum(
                float(item[field]) for item in rows
                if item.get("aggregation_level") == "INSTRUMENT"
                and item.get("valuation_state") == row.get("valuation_state")
            )
    except (KeyError, TypeError, ValueError):
        return False
    return close_numeric(expected, row.get(field), tolerance_for(field))


def _attribution_identity_is_internally_consistent(field: str, row: dict[str, Any]) -> bool:
    """Avoid cascading an upstream component error into the residual-identity item."""
    if field not in {"explained_pnl_usd", "residual_pnl_usd"}:
        return False
    try:
        components = sum(
            float(row[name])
            for name in (
                "delta_pnl_usd", "gamma_pnl_usd", "vega_pnl_usd", "theta_pnl_usd",
                "vanna_pnl_usd", "volga_pnl_usd",
            )
        )
        explained = float(row["explained_pnl_usd"])
        residual = float(row["residual_pnl_usd"])
        carry = float(row["carry_actual_pnl_usd"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        close_numeric(components, explained, tolerance_for("explained_pnl_usd"))
        and close_numeric(carry - explained, residual, tolerance_for("residual_pnl_usd"))
    )


def _compare_csv(filename, expected, path, inputs, findings):
    try:
        columns, rows = _read_csv(path)
    except Exception as exc:
        for item in FILE_ITEMS[filename]: _add(findings, item, f"unreadable {filename}: {type(exc).__name__}")
        _add(findings, "OUT-2", f"unreadable {filename}"); return
    if columns != expected["columns"]:
        _add(findings, "OUT-2", f"{filename} columns differ")
    expected_rows = expected["rows"]
    key_fields = {
        "positions_eod.csv": ("instrument_name",),
        "valuation.csv": ("instrument_name",),
        "greeks.csv": ("aggregation_level", "instrument_name", "valuation_state"),
        "pnl_attribution.csv": ("aggregation_level", "instrument_name"),
    }[filename]
    def row_key(row): return tuple(str(row.get(field, "")) for field in key_fields)
    expected_keys, actual_keys = [row_key(row) for row in expected_rows], [row_key(row) for row in rows]
    if actual_keys != expected_keys:
        _add(findings, "OUT-4", f"{filename} row ordering/identity differs")
    if len(rows) != len(expected_rows):
        _add(findings, "OUT-4", f"{filename} row count differs")
        for item in FILE_ITEMS[filename]: _add(findings, item, f"{filename} row granularity differs")
    if len(set(actual_keys)) != len(actual_keys) or set(actual_keys) != set(expected_keys):
        for item in FILE_ITEMS[filename]: _add(findings, item, f"{filename} row identities differ")
    actual_by_key = {row_key(row): row for row in rows}
    for index, want in enumerate(expected_rows):
        got = actual_by_key.get(row_key(want))
        if got is None:
            continue
        identity_fields = [field for field in want if field not in NUMERIC_FIELD_KINDS and field not in {"quantity", "contract_multiplier"}]
        for field in identity_fields:
            if str(got.get(field, "")) != str(want[field]):
                _add(findings, "OUT-4", f"{filename}[{index}].{field} differs")
        for field, value in want.items():
            if field not in NUMERIC_FIELD_KINDS:
                continue
            if value == "" and got.get(field, "") == "":
                continue
            if not close_numeric(value, got.get(field), tolerance_for(field)):
                if filename == "greeks.csv" and _greek_position_is_internally_consistent(field, got, rows):
                    continue
                if filename == "pnl_attribution.csv" and _attribution_identity_is_internally_consistent(field, got):
                    continue
                _add(findings, _field_item(filename, field, want, inputs), f"{filename}[{index}].{field} outside tolerance")


def _compare_validation(expected, actual, findings):
    if not isinstance(actual, dict) or set(actual) != {"schema_version", "status", "warnings", "errors"}:
        _add(findings, "VAL-1", "validation_report top-level schema differs"); _add(findings, "OUT-3", "validation JSON schema differs"); return
    if type(actual.get("schema_version")) is not int or actual.get("schema_version") != 1 or actual.get("status") != expected.get("status"):
        _add(findings, "VAL-1", "validation schema_version/status differs")
    expected_warnings, actual_warnings = expected.get("warnings", []), actual.get("warnings", [])
    if not isinstance(actual_warnings, list) or not isinstance(actual.get("errors"), list):
        _add(findings, "VAL-1", "warnings/errors must be arrays"); return
    record_fields = {"code", "file", "row_key", "field", "message"}
    for index, record in enumerate(actual_warnings):
        if not isinstance(record, dict) or set(record) != record_fields:
            _add(findings, "VAL-1", f"warning[{index}] record schema differs")
        elif not isinstance(record.get("code"), str) or any(record.get(field) is not None and not isinstance(record.get(field), str) for field in ("file", "row_key", "field")):
            _add(findings, "VAL-1", f"warning[{index}] record types differ")
    def warning_item(code):
        if code == "ROW_ORDER_NORMALIZED": return "VAL-4"
        if code == "EXACT_DUPLICATE_DROPPED": return "VAL-3"
        return "VAL-2"
    warning_codes = sorted({row.get("code") for row in expected_warnings + actual_warnings if isinstance(row, dict)}, key=str)
    for code in warning_codes:
        want = [row for row in expected_warnings if row.get("code") == code]
        got = [row for row in actual_warnings if isinstance(row, dict) and row.get("code") == code]
        want_ids = Counter(_warning_identity(row) for row in want)
        got_ids = Counter(_warning_identity(row) for row in got)
        if want_ids != got_ids: _add(findings, warning_item(code), f"warning identities/cardinality differ for {code}")
    if [_warning_identity(row) for row in expected_warnings] != [_warning_identity(row) for row in actual_warnings if isinstance(row, dict)]:
        expected_counter = sorted((_warning_identity(row) for row in expected_warnings), key=str)
        actual_counter = sorted((_warning_identity(row) for row in actual_warnings if isinstance(row, dict)), key=str)
        if expected_counter == actual_counter: _add(findings, "VAL-4", "warning ordering differs")
    for index, record in enumerate(actual_warnings):
        if isinstance(record, dict):
            for detail in validate_warning_messages([record]): _add(findings, warning_item(record.get("code")), f"warning[{index}]: {detail}")
    expected_errors = expected.get("errors", [])
    for index, record in enumerate(actual.get("errors", [])):
        if not isinstance(record, dict) or set(record) != record_fields:
            _add(findings, "VAL-1", f"error[{index}] record schema differs")
        elif not isinstance(record.get("code"), str) or any(record.get(field) is not None and not isinstance(record.get(field), str) for field in ("file", "row_key", "field")):
            _add(findings, "VAL-1", f"error[{index}] record types differ")
        elif validate_warning_messages([record]):
            _add(findings, "VAL-1", f"error[{index}] message quality differs")
    actual_errors = [row for row in actual.get("errors", []) if isinstance(row, dict)]
    expected_codes = {row.get("code") for row in expected_errors}
    actual_codes = {row.get("code") for row in actual_errors}
    if expected_codes != actual_codes:
        _add(findings, "VAL-1", "error category set differs")
    expected_files = {row.get("code"): row.get("file") for row in expected_errors}
    expected_keys = {row.get("code"): row.get("row_key") for row in expected_errors}
    for row in actual_errors:
        code = row.get("code")
        if code in expected_files and row.get("file") != expected_files[code]:
            _add(findings, "VAL-1", f"error file is inconsistent with public defect for {code}")
        if code in expected_keys and row.get("row_key") != expected_keys[code]:
            _add(findings, "VAL-1", f"error row_key is inconsistent with public defect for {code}")


def _compare_stress(expected, actual, findings):
    top_fields = {"schema_version", "reporting_currency", "base_valuation_timestamp_utc", "scenarios"}
    scenario_fields = {"scenario_id", "spot_shock_pct", "vol_shock_abs", "portfolio_base_value_usd", "portfolio_stressed_value_usd", "portfolio_stress_pnl_usd", "instruments"}
    instrument_fields = {"instrument_name", "quantity_t1", "contract_multiplier", "base_unit_value_usd", "stressed_spot_usd", "stressed_iv_decimal", "stressed_unit_value_usd", "base_market_value_usd", "stressed_market_value_usd", "stress_pnl_usd"}
    if not isinstance(actual, dict) or set(actual) != top_fields:
        _add(findings, "OUT-3", "stress top-level schema differs");
        for item in ("STR-1", "STR-2", "STR-3"): _add(findings, item, "stress schema differs")
        return
    if type(actual.get("schema_version")) is not int:
        _add(findings, "OUT-3", "stress.schema_version must be a JSON integer")
    for field in ("schema_version", "reporting_currency", "base_valuation_timestamp_utc"):
        if actual.get(field) != expected.get(field): _add(findings, "OUT-3", f"stress.{field} differs")
    wants, gots = expected["scenarios"], actual.get("scenarios", [])
    if not isinstance(gots, list) or len(wants) != len(gots):
        _add(findings, "OUT-4", "stress scenario granularity differs"); _add(findings, "STR-3", "stress scenario count differs"); return
    expected_scenario_ids = [row["scenario_id"] for row in wants]
    actual_scenario_ids = [row.get("scenario_id") for row in gots if isinstance(row, dict)]
    if actual_scenario_ids != expected_scenario_ids: _add(findings, "OUT-4", "stress scenario ordering/identity differs")
    if len(set(actual_scenario_ids)) != len(actual_scenario_ids) or set(actual_scenario_ids) != set(expected_scenario_ids):
        _add(findings, "STR-3", "stress scenario identities differ")
    actual_scenarios = {row.get("scenario_id"): row for row in gots if isinstance(row, dict)}
    for si, want_s in enumerate(wants):
        got_s = actual_scenarios.get(want_s["scenario_id"])
        if got_s is None: continue
        if not isinstance(got_s, dict) or set(got_s) != scenario_fields:
            _add(findings, "OUT-3", f"stress scenario[{si}] schema differs"); _add(findings, "STR-3", "scenario schema differs"); continue
        for field in ("spot_shock_pct", "vol_shock_abs", "portfolio_base_value_usd", "portfolio_stressed_value_usd", "portfolio_stress_pnl_usd"):
            if not _is_json_number(got_s.get(field)):
                _add(findings, "OUT-3", f"scenario[{si}].{field} must be a native finite JSON number")
                _add(findings, _field_item("stress_report.json", field, want_s, {}), f"scenario[{si}].{field} has wrong JSON type")
            elif not close_numeric(want_s[field], got_s.get(field), tolerance_for(field)):
                _add(findings, _field_item("stress_report.json", field, want_s, {}), f"scenario[{si}].{field} outside tolerance")
        want_i, got_i = want_s["instruments"], got_s.get("instruments", [])
        if not isinstance(got_i, list) or len(want_i) != len(got_i): _add(findings, "STR-3", f"scenario[{si}] instrument universe differs"); _add(findings, "OUT-4", "stress instrument granularity differs"); continue
        expected_instrument_ids = [row["instrument_name"] for row in want_i]
        actual_instrument_ids = [row.get("instrument_name") for row in got_i if isinstance(row, dict)]
        if actual_instrument_ids != expected_instrument_ids: _add(findings, "OUT-4", f"scenario[{si}] instrument ordering/identity differs")
        if len(set(actual_instrument_ids)) != len(actual_instrument_ids) or set(actual_instrument_ids) != set(expected_instrument_ids):
            _add(findings, "STR-3", f"scenario[{si}] instrument identities differ")
        actual_instruments = {row.get("instrument_name"): row for row in got_i if isinstance(row, dict)}
        for ii, want in enumerate(want_i):
            got = actual_instruments.get(want["instrument_name"])
            if got is None: continue
            if not isinstance(got, dict) or set(got) != instrument_fields: _add(findings, "OUT-3", f"stress instrument[{ii}] schema differs"); _add(findings, "STR-2", "instrument schema differs"); continue
            for field, value in want.items():
                if field == "instrument_name": continue
                if not _is_json_number(got.get(field)):
                    _add(findings, "OUT-3", f"stress[{si}][{ii}].{field} must be a native finite JSON number")
                    _add(findings, _field_item("stress_report.json", field, want, {}), f"stress[{si}][{ii}].{field} has wrong JSON type")
                elif not close_numeric(value, got.get(field), tolerance_for(field)):
                    _add(findings, _field_item("stress_report.json", field, want, {}), f"stress[{si}][{ii}].{field} outside tolerance")


def grade_variant(input_dir: Path, output_dir: Path, *, exit_code: int | None,
                  expected_input_fingerprint: str) -> GradeResult:
    if suite_fingerprint(input_dir) != expected_input_fingerprint:
        raise ValueError("hidden input fingerprint differs from the trusted pre-run anchor")
    expected = render_reference_outputs(input_dir)
    fatal_expected = set(expected) == {"validation_report.json"}
    inputs = {} if fatal_expected else load_inputs(input_dir)
    output_dir = Path(output_dir)
    findings: dict[str, set[str]] = {}
    expected_files, actual_files = set(expected), {path.name for path in output_dir.iterdir() if path.is_file()} if output_dir.exists() else set()
    unexpected_fatal_output = (
        not fatal_expected
        and exit_code is not None
        and exit_code != 0
        and actual_files == {"validation_report.json"}
    )
    if actual_files != expected_files and not unexpected_fatal_output:
        _add(findings, "OUT-1", f"output file set differs: missing={sorted(expected_files-actual_files)}, extra={sorted(actual_files-expected_files)}")
    if exit_code is None:
        _add(findings, "VAL-1", "candidate exit code observation is missing")
    elif fatal_expected and exit_code == 0:
        _add(findings, "VAL-1", "fatal input must return a nonzero exit code")
    elif not fatal_expected and exit_code != 0:
        _add(findings, "VAL-1", "successful input must return exit code zero")
    if unexpected_fatal_output:
        _add(findings, "VAL-1", "recoverable/success input incorrectly took the fatal-only output path")
    if fatal_expected and actual_files != expected_files:
        _add(findings, "VAL-1", "fatal input must write only validation_report.json")
    if not unexpected_fatal_output:
        for filename in sorted(expected_files - actual_files):
            for item in FILE_ITEMS[filename]: _add(findings, item, f"missing {filename}")
    for filename in sorted(expected_files & actual_files):
        if filename.endswith(".csv"):
            _compare_csv(filename, expected[filename], output_dir / filename, inputs, findings)
            continue
        try:
            actual = _read_json(output_dir / filename)
        except Exception as exc:
            _add(findings, "OUT-3", f"unreadable {filename}: {type(exc).__name__}")
            for item in FILE_ITEMS[filename]: _add(findings, item, f"unreadable {filename}")
            continue
        if not _json_keys_sorted(actual): _add(findings, "OUT-3", f"{filename} object keys are not lexicographically ordered")
        if filename == "validation_report.json": _compare_validation(expected[filename], actual, findings)
        else: _compare_stress(expected[filename], actual, findings)
    if set(expected) != {"validation_report.json"} and not unexpected_fatal_output:
        for finding in check_invariants(input_dir, output_dir).findings: _add(findings, finding.item_id, f"invariant {finding.check_id}: {finding.detail}")
    by_id = {item.item_id: item for item in RUBRIC_ITEMS}
    failed = tuple(sorted(findings))
    passed = tuple(sorted(set(by_id) - set(failed)))
    score = sum(by_id[item_id].points for item_id in passed)
    flattened = tuple(Finding(item_id, "; ".join(sorted(findings[item_id]))) for item_id in failed)
    return GradeResult(score, flattened, passed, failed)


def grade_suite(suite_root: Path, outputs_root: Path,
                exit_codes: dict[str, tuple[int, int] | list[int]], *,
                expected_suite_fingerprint: str) -> dict[str, object]:
    """Grade every manifest-bound variant with two observed Candidate runs."""
    suite_root, outputs_root = Path(suite_root), Path(outputs_root)
    if suite_fingerprint(suite_root) != expected_suite_fingerprint:
        raise ValueError("hidden suite fingerprint differs from the trusted pre-run anchor")
    manifest = json.loads((suite_root / "manifest.json").read_text(encoding="utf-8"))
    names = [row.get("name") for row in manifest.get("variants", []) if isinstance(row, dict)]
    if tuple(names) != REQUIRED_VARIANTS or len(set(names)) != len(names):
        raise ValueError("hidden manifest must contain every required unique variant")
    variant_anchors = {row["name"]: row.get("file_fingerprint") for row in manifest["variants"]}
    for name, anchor in variant_anchors.items():
        if suite_fingerprint(suite_root / name) != anchor:
            raise ValueError(f"hidden variant fingerprint mismatch: {name}")
    if set(exit_codes) != set(names) or any(not isinstance(values, (list, tuple)) or len(values) != 2 for values in exit_codes.values()):
        raise ValueError("exit code observations must contain two runs for every manifest variant")
    actual_output_names = {path.name for path in outputs_root.iterdir() if path.is_dir()} if outputs_root.exists() else set()
    if actual_output_names != set(names):
        raise ValueError("outputs root must contain exactly every manifest variant")
    results = [
        grade_repeated_variant(
            suite_root / name, outputs_root / name / "run-1", outputs_root / name / "run-2",
            first_exit_code=exit_codes[name][0], second_exit_code=exit_codes[name][1],
            expected_input_fingerprint=variant_anchors[name],
        )
        for name in names
    ]
    if suite_fingerprint(suite_root) != expected_suite_fingerprint:
        raise ValueError("hidden suite changed during grading")
    failed_items = sorted({item for result in results for item in result.failed_items})
    by_id = {item.item_id: item for item in RUBRIC_ITEMS}
    score = sum(item.points for item in RUBRIC_ITEMS if item.item_id not in failed_items)
    return {"variant_count": len(results), "score": score, "failed_items": failed_items,
            "scores": [result.score for result in results],
            "mean_score": sum(result.score for result in results) / len(results) if results else 0.0,
            "results": {name: {"score": result.score, "failed_items": list(result.failed_items),
                         "findings": [finding.__dict__ for finding in result.findings]} for name, result in zip(names, results)}}


def _repeat_semantically_equal(first_dir: Path, second_dir: Path) -> bool:
    first_files = {path.name for path in Path(first_dir).iterdir() if path.is_file()}
    second_files = {path.name for path in Path(second_dir).iterdir() if path.is_file()}
    if first_files != second_files:
        return False
    for filename in sorted(first_files):
        if filename.endswith(".csv"):
            first_columns, first_rows = _read_csv(Path(first_dir) / filename)
            second_columns, second_rows = _read_csv(Path(second_dir) / filename)
            if first_columns != second_columns or len(first_rows) != len(second_rows):
                return False
            for left, right in zip(first_rows, second_rows):
                if set(left) != set(right): return False
                for field in left:
                    if field in NUMERIC_FIELD_KINDS and left[field] != "" and right[field] != "":
                        if not close_numeric(left[field], right[field], tolerance_for(field)): return False
                    elif left[field] != right[field]:
                        return False
            continue
        left, right = _read_json(Path(first_dir) / filename), _read_json(Path(second_dir) / filename)
        if filename == "validation_report.json":
            if left != right:
                return False
        elif not _json_semantically_equal(left, right):
            return False
    return True


def _json_semantically_equal(left: Any, right: Any, field: str | None = None) -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return list(left) == list(right) and all(_json_semantically_equal(left[key], right[key], key) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_semantically_equal(a, b, field) for a, b in zip(left, right))
    if field in NUMERIC_FIELD_KINDS:
        return close_numeric(left, right, tolerance_for(field))
    return left == right


def grade_repeated_variant(input_dir: Path, first_output_dir: Path, second_output_dir: Path, *,
                           first_exit_code: int | None, second_exit_code: int | None,
                           expected_input_fingerprint: str) -> GradeResult:
    """Grade two runs and enforce repeat determinism without byte equality."""
    first = grade_variant(input_dir, first_output_dir, exit_code=first_exit_code,
                          expected_input_fingerprint=expected_input_fingerprint)
    second = grade_variant(input_dir, second_output_dir, exit_code=second_exit_code,
                           expected_input_fingerprint=expected_input_fingerprint)
    failed = set(first.failed_items) | set(second.failed_items)
    details: dict[str, set[str]] = {}
    for result, label in ((first, "first"), (second, "second")):
        for finding in result.findings:
            details.setdefault(finding.item_id, set()).add(f"{label}: {finding.detail}")
    try:
        equal = _repeat_semantically_equal(first_output_dir, second_output_dir)
    except Exception as exc:
        equal = False
        details.setdefault("OUT-5", set()).add(f"repeat comparison failed: {type(exc).__name__}")
    if not equal:
        failed.add("OUT-5"); details.setdefault("OUT-5", set()).add("two runs are not semantically deterministic")
    by_id = {item.item_id: item for item in RUBRIC_ITEMS}
    passed = tuple(sorted(set(by_id) - failed)); failed_tuple = tuple(sorted(failed))
    findings = tuple(Finding(item, "; ".join(sorted(details.get(item, {"failed"})))) for item in failed_tuple)
    return GradeResult(sum(by_id[item].points for item in passed), findings, passed, failed_tuple)
