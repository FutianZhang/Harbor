"""Internal observable-output oracle built only from the frozen public rules."""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from golden.accounting import instrument_pnl, portfolio_pnl
from golden.attribution import attribute_carry_pnl
from golden.greeks import aggregate_position_greeks, bs_greeks, scale_position_greeks
from golden.positions import Trade, reconstruct_positions
from golden.pricing import SECONDS_PER_YEAR, act365_time_years, bs_price


CSV_COLUMNS = {
    "positions_eod.csv": ["instrument_name", "quantity_t0", "net_trade_quantity", "quantity_t1"],
    "valuation.csv": ["instrument_name", "quantity_t0", "quantity_t1", "contract_multiplier", "unit_value_t0_usd", "unit_value_t1_usd", "market_value_t0_usd", "market_value_t1_usd"],
    "greeks.csv": ["aggregation_level", "instrument_name", "valuation_state", "valuation_timestamp_utc", "quantity", "contract_multiplier", "unit_delta", "unit_gamma", "unit_vega_decimal", "unit_vega_1vol", "unit_theta_year", "unit_theta_day", "unit_vanna", "unit_volga", "position_delta", "position_gamma", "position_vega_decimal", "position_vega_1vol", "position_theta_year", "position_theta_day", "position_vanna", "position_volga"],
    "pnl_attribution.csv": ["aggregation_level", "instrument_name", "beginning_market_value_usd", "ending_market_value_usd", "trade_cashflow_usd", "fees_usd", "carry_actual_pnl_usd", "trade_to_t1_pnl_usd", "total_pnl_usd", "delta_pnl_usd", "gamma_pnl_usd", "vega_pnl_usd", "theta_pnl_usd", "vanna_pnl_usd", "volga_pnl_usd", "explained_pnl_usd", "residual_pnl_usd"],
}

CONFIG_FIELDS = {
    "benchmark_id", "benchmark_t0_utc", "benchmark_t1_utc", "dataset_version",
    "generation_spec_sha256", "random_seed", "reporting_currency", "source_snapshot_sha256",
    "source_version", "year_fraction_convention",
}

INPUT_FILES = (
    "config.json", "initial_positions.csv", "trades.csv", "instrument_metadata.csv",
    "rates.csv", "market_t0.parquet", "market_t1.parquet", "scenarios.json",
)

CSV_INPUT_COLUMNS = {
    "initial_positions.csv": ({"instrument_name", "quantity"}, {"instrument_name", "quantity"}),
    "trades.csv": (
        {"trade_id", "timestamp", "instrument_name", "side", "quantity", "execution_price_usd", "fee_usd"},
        {"trade_id", "timestamp", "instrument_name", "side", "quantity", "execution_price_usd", "fee_usd"},
    ),
    "instrument_metadata.csv": (
        {"instrument_name", "underlying", "option_type", "strike_usd", "expiration_timestamp", "contract_multiplier"},
        {"instrument_name", "underlying", "option_type", "strike_usd", "expiration_timestamp", "contract_multiplier", "price_index", "maturity_bucket"},
    ),
    "rates.csv": (
        {"instrument_name", "rate_decimal", "benchmark_t0_utc", "benchmark_t1_utc"},
        {"instrument_name", "rate_decimal", "benchmark_t0_utc", "benchmark_t1_utc"},
    ),
}

MARKET_REQUIRED_COLUMNS = {
    "instrument_name": "string", "underlying": "string", "option_type": "string",
    "valuation_timestamp": "string", "spot_usd": "double", "strike_usd": "double",
    "expiration_timestamp": "string", "iv_decimal": "double", "rate_decimal": "double",
    "contract_multiplier": "double",
}
MARKET_OPTIONAL_COLUMNS = {
    "market_t0.parquet": {
        "source_bid_price_coin": "double", "source_ask_price_coin": "double",
        "source_mark_price_coin": "double", "source_mark_iv_pct": "double",
        "source_orderbook_timestamp_ms": "int64",
    },
    "market_t1.parquet": {"spot_shock_pct": "double", "iv_change_decimal": "double"},
}


class _InputReadError(Exception):
    def __init__(self, filename: str):
        super().__init__(filename)
        self.filename = filename


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be RFC3339 UTC with trailing Z")
    result = datetime.fromisoformat(value[:-1] + "+00:00")
    if result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return result


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or []), list(reader)
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise _InputReadError(path.name) from exc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise _InputReadError(path.name) from exc


def _warning(code: str, file: str, row_key: str | None = None,
             field: str | None = None) -> dict[str, object]:
    location = row_key or file
    return {"code": code, "file": file, "row_key": row_key, "field": field,
            "message": f"{code} detected for {location}."}


def _sort_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(records, key=lambda row: tuple("" if row[key] is None else str(row[key]) for key in ("code", "file", "row_key", "field")))


def _is_sorted(rows: list[dict[str, Any]], keys) -> bool:
    values = [keys(row) for row in rows]
    return values == sorted(values)


def _normalize_t0(rows: list[dict[str, Any]], warnings: list[dict[str, object]]) -> tuple[list[dict[str, Any]], list[str]]:
    if [row["instrument_name"] for row in rows] != sorted(row["instrument_name"] for row in rows):
        warnings.append(_warning("ROW_ORDER_NORMALIZED", "market_t0.parquet"))
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(row["instrument_name"], []).append(row)
    normalized, conflicts = [], []
    for name in sorted(by_name):
        group = by_name[name]
        first = group[0]
        if len(group) > 1:
            if not all(first == other for other in group[1:]):
                conflicts.append(name)
            else:
                warnings.append(_warning("EXACT_DUPLICATE_DROPPED", "market_t0.parquet", name))
        normalized.append(first)
    return normalized, conflicts


def _validation_warnings(payload: dict[str, Any]) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    sortable = (
        ("initial_positions.csv", payload["initial"], lambda row: row["instrument_name"]),
        ("trades.csv", payload["trades"], lambda row: (_parse_utc(row["timestamp"]), row["trade_id"])),
        ("instrument_metadata.csv", payload["metadata_rows"], lambda row: row["instrument_name"]),
        ("rates.csv", payload["rate_rows"], lambda row: row["instrument_name"]),
        ("market_t1.parquet", payload["market1_rows"], lambda row: row["instrument_name"]),
        ("scenarios.json", payload["scenarios"], lambda row: row["scenario_id"]),
    )
    for filename, rows, key in sortable:
        if not _is_sorted(rows, key):
            warnings.append(_warning("ROW_ORDER_NORMALIZED", filename))
    optional_quotes = ("source_bid_price_coin", "source_ask_price_coin", "source_mark_price_coin", "source_mark_iv_pct")
    optional_numeric = (*optional_quotes, "source_orderbook_timestamp_ms")
    for row in payload["market0_rows"]:
        name = row["instrument_name"]
        for field in optional_numeric:
            value = row.get(field)
            if value is None:
                if field in optional_quotes:
                    warnings.append(_warning("OPTIONAL_QUOTE_MISSING", "market_t0.parquet", name, field))
                continue
            numeric = float(value)
            invalid = not math.isfinite(numeric)
            if field == "source_bid_price_coin":
                invalid = invalid or numeric < 0
                if math.isfinite(numeric) and numeric == 0:
                    warnings.append(_warning("OPTIONAL_QUOTE_ZERO_BID", "market_t0.parquet", name, field))
            elif field in {"source_ask_price_coin", "source_mark_price_coin", "source_mark_iv_pct"}:
                invalid = invalid or numeric <= 0
            elif field == "source_orderbook_timestamp_ms":
                invalid = invalid or numeric < 0
            if invalid:
                warnings.append(_warning("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, field))
        bid, ask = row.get("source_bid_price_coin"), row.get("source_ask_price_coin")
        if bid is not None and ask is not None and math.isfinite(float(bid)) and math.isfinite(float(ask)) and float(bid) > float(ask):
            warnings.append(_warning("OPTIONAL_QUOTE_CROSSED", "market_t0.parquet", name))
    return _sort_records(warnings)


def _read_inputs(input_dir: Path) -> dict[str, Any]:
    input_dir = Path(input_dir)
    for filename in INPUT_FILES:
        if not (input_dir / filename).is_file():
            raise FileNotFoundError(2, "required input file missing", str(input_dir / filename))
    config = _read_json(input_dir / "config.json")
    csv_payload = {}
    for filename in CSV_INPUT_COLUMNS:
        csv_payload[filename] = _read_csv(input_dir / filename)
    try:
        market0_table = pq.read_table(input_dir / "market_t0.parquet")
    except Exception as exc:
        raise _InputReadError("market_t0.parquet") from exc
    try:
        market1_table = pq.read_table(input_dir / "market_t1.parquet")
    except Exception as exc:
        raise _InputReadError("market_t1.parquet") from exc
    scenarios_document = _read_json(input_dir / "scenarios.json")
    payload = {
        "config": config,
        "initial": csv_payload["initial_positions.csv"][1],
        "trades": csv_payload["trades.csv"][1],
        "metadata_rows": csv_payload["instrument_metadata.csv"][1],
        "rate_rows": csv_payload["rates.csv"][1],
        "csv_columns": {filename: columns for filename, (columns, _) in csv_payload.items()},
        "market_schemas": {"market_t0.parquet": market0_table.schema, "market_t1.parquet": market1_table.schema},
        "market0_raw": market0_table.to_pylist(),
        "market1_rows": market1_table.to_pylist(),
        "scenarios_document": scenarios_document,
        "scenarios": scenarios_document.get("scenarios", []) if isinstance(scenarios_document, dict) else [],
    }
    return payload


def _finalize_inputs(payload: dict[str, Any]) -> dict[str, Any]:
    warnings: list[dict[str, object]] = []
    payload["market0_rows"], payload["duplicate_conflicts"] = _normalize_t0(payload["market0_raw"], warnings)
    warnings.extend(_validation_warnings(payload))
    payload["warnings"] = _sort_records(warnings)
    return payload


def load_inputs(input_dir: Path) -> dict[str, Any]:
    payload = _read_inputs(input_dir)
    error = _validate_public_contract(payload)
    if error is not None:
        raise ValueError(f"invalid input contract: {error[0]}")
    return _finalize_inputs(payload)


def _validation_report(warnings=(), errors=()) -> dict[str, object]:
    warnings, errors = list(warnings), list(errors)
    status = "ERROR" if errors else ("PASS_WITH_WARNINGS" if warnings else "PASS")
    return {"schema_version": 1, "status": status, "warnings": warnings, "errors": errors}


def _error_report(code: str, file: str, row_key: str | None, field: str | None) -> dict[str, object]:
    record = {"code": code, "file": file, "row_key": row_key, "field": field,
              "message": f"{code} detected in {file}."}
    return {"validation_report.json": _validation_report(errors=[record])}


def _duplicate_key(rows: list[dict[str, Any]], field: str) -> str | None:
    seen: set[str] = set()
    for row in rows:
        value = str(row.get(field, ""))
        if value in seen:
            return value
        seen.add(value)
    return None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp_error(value: Any) -> bool:
    try:
        _parse_utc(value)
    except (TypeError, ValueError):
        return True
    return False


def _validate_public_contract(payload: dict[str, Any]) -> tuple[str, str, str | None, str | None] | None:
    """Validate only rules frozen in the Candidate-visible input contract."""
    config = payload["config"]
    if not isinstance(config, dict):
        return "SCHEMA_ERROR", "config.json", None, None
    config_fields = set(config)
    if config_fields != CONFIG_FIELDS:
        field = sorted(config_fields ^ CONFIG_FIELDS)[0]
        return "SCHEMA_ERROR", "config.json", None, field

    for filename, (required, allowed) in CSV_INPUT_COLUMNS.items():
        columns = set(payload["csv_columns"][filename])
        if not required <= columns or not columns <= allowed:
            field = sorted((required - columns) | (columns - allowed))[0]
            return "SCHEMA_ERROR", filename, None, field

    for filename, schema in payload["market_schemas"].items():
        required_types = MARKET_REQUIRED_COLUMNS
        optional_types = MARKET_OPTIONAL_COLUMNS[filename]
        allowed = set(required_types) | set(optional_types)
        names = set(schema.names)
        if not set(required_types) <= names or not names <= allowed:
            field = sorted((set(required_types) - names) | (names - allowed))[0]
            return "SCHEMA_ERROR", filename, None, field
        for field, expected_type in {**required_types, **optional_types}.items():
            if field in names and str(schema.field(field).type) != expected_type:
                return "SCHEMA_ERROR", filename, None, field

    document = payload["scenarios_document"]
    if not isinstance(document, dict) or set(document) != {"scenarios"} or not isinstance(document.get("scenarios"), list):
        return "SCHEMA_ERROR", "scenarios.json", None, None
    for scenario in payload["scenarios"]:
        if not isinstance(scenario, dict) or set(scenario) != {"scenario_id", "spot_shock_pct", "vol_shock_abs"}:
            return "SCHEMA_ERROR", "scenarios.json", None, None

    keyed_rows = (
        ("initial_positions.csv", payload["initial"], "instrument_name"),
        ("trades.csv", payload["trades"], "trade_id"),
        ("instrument_metadata.csv", payload["metadata_rows"], "instrument_name"),
        ("rates.csv", payload["rate_rows"], "instrument_name"),
        ("market_t1.parquet", payload["market1_rows"], "instrument_name"),
        ("scenarios.json", payload["scenarios"], "scenario_id"),
    )
    for filename, rows, key in keyed_rows:
        duplicate = _duplicate_key(rows, key)
        if duplicate is not None:
            return "DUPLICATE_KEY", filename, duplicate, None
    by_t0_name: dict[str, list[dict[str, Any]]] = {}
    for row in payload["market0_raw"]:
        by_t0_name.setdefault(str(row.get("instrument_name", "")), []).append(row)
    for name in sorted(by_t0_name):
        group = by_t0_name[name]
        if len(group) > 1 and not all(group[0] == item for item in group[1:]):
            return "DUPLICATE_KEY", "market_t0.parquet", name, None

    metadata_names = {str(row["instrument_name"]) for row in payload["metadata_rows"]}
    for filename, rows, key in (
        ("initial_positions.csv", payload["initial"], "instrument_name"),
        ("trades.csv", payload["trades"], "instrument_name"),
    ):
        for row in rows:
            name = str(row.get(key, ""))
            if name not in metadata_names:
                return "MISSING_INSTRUMENT", filename, name or None, key

    state_rows = {
        "rates.csv": payload["rate_rows"],
        "market_t0.parquet": payload["market0_raw"],
        "market_t1.parquet": payload["market1_rows"],
    }
    for filename, rows in state_rows.items():
        names = {str(row.get("instrument_name", "")) for row in rows}
        missing = sorted(metadata_names - names)
        if missing:
            return "CORE_STATE_MISSING", filename, missing[0], None

    if _timestamp_error(config["benchmark_t0_utc"]):
        return "INVALID_TIMESTAMP", "config.json", None, "benchmark_t0_utc"
    if _timestamp_error(config["benchmark_t1_utc"]):
        return "INVALID_TIMESTAMP", "config.json", None, "benchmark_t1_utc"
    t0, t1 = _parse_utc(config["benchmark_t0_utc"]), _parse_utc(config["benchmark_t1_utc"])
    if t1 <= t0:
        return "INVALID_TIMESTAMP", "config.json", None, "benchmark_t1_utc"

    metadata = {str(row["instrument_name"]): row for row in payload["metadata_rows"]}
    rates = {str(row["instrument_name"]): row for row in payload["rate_rows"]}
    market0 = {str(row["instrument_name"]): row for row in payload["market0_raw"]}
    market1 = {str(row["instrument_name"]): row for row in payload["market1_rows"]}

    for name in sorted(metadata):
        meta = metadata[name]
        if _timestamp_error(meta.get("expiration_timestamp")):
            return "INVALID_TIMESTAMP", "instrument_metadata.csv", name, "expiration_timestamp"
        if meta.get("option_type") not in {"call", "put"}:
            return "INVALID_OPTION_TYPE", "instrument_metadata.csv", name, "option_type"
        for field in ("strike_usd", "contract_multiplier"):
            value = _number(meta.get(field))
            if value is None or value <= 0:
                return "INVALID_CORE_VALUE", "instrument_metadata.csv", name, field
        rate = _number(rates[name].get("rate_decimal"))
        if rate is None:
            return "INVALID_CORE_VALUE", "rates.csv", name, "rate_decimal"
        for field in ("benchmark_t0_utc", "benchmark_t1_utc"):
            if _timestamp_error(rates[name].get(field)):
                return "INVALID_TIMESTAMP", "rates.csv", name, field
            if rates[name][field] != config[field]:
                return "CORE_STATE_CONFLICT", "rates.csv", name, field
        for filename, row, clock in (
            ("market_t0.parquet", market0[name], config["benchmark_t0_utc"]),
            ("market_t1.parquet", market1[name], config["benchmark_t1_utc"]),
        ):
            for field in ("valuation_timestamp", "expiration_timestamp"):
                if _timestamp_error(row.get(field)):
                    return "INVALID_TIMESTAMP", filename, name, field
            if row["valuation_timestamp"] != clock:
                return "CORE_STATE_CONFLICT", filename, name, "valuation_timestamp"
            for field in ("underlying", "option_type", "strike_usd", "expiration_timestamp", "contract_multiplier"):
                left, right = row.get(field), meta.get(field)
                if field in {"strike_usd", "contract_multiplier"}:
                    left, right = _number(left), _number(right)
                if left != right:
                    return "CORE_STATE_CONFLICT", filename, name, field
            if _number(row.get("rate_decimal")) != rate:
                return "CORE_STATE_CONFLICT", filename, name, "rate_decimal"
            for field in ("spot_usd", "strike_usd", "contract_multiplier"):
                value = _number(row.get(field))
                if value is None or value <= 0:
                    return "INVALID_CORE_VALUE", filename, name, field
            iv = _number(row.get("iv_decimal"))
            expiry = _parse_utc(row["expiration_timestamp"])
            valuation = _parse_utc(row["valuation_timestamp"])
            if iv is None or (expiry > valuation and iv <= 0):
                return "INVALID_CORE_VALUE", filename, name, "iv_decimal"

    for row in payload["initial"]:
        if _number(row.get("quantity")) is None:
            return "INVALID_CORE_VALUE", "initial_positions.csv", row.get("instrument_name"), "quantity"
    for row in payload["trades"]:
        trade_id = str(row.get("trade_id", "")) or None
        if _timestamp_error(row.get("timestamp")):
            return "INVALID_TIMESTAMP", "trades.csv", trade_id, "timestamp"
        timestamp = _parse_utc(row["timestamp"])
        if not t0 < timestamp < t1:
            return "INVALID_TIMESTAMP", "trades.csv", trade_id, "timestamp"
        for field, lower, inclusive in (
            ("quantity", 0.0, False), ("execution_price_usd", 0.0, True), ("fee_usd", 0.0, True),
        ):
            value = _number(row.get(field))
            if value is None or value < lower or (not inclusive and value == lower):
                return "INVALID_CORE_VALUE", "trades.csv", trade_id, field
        if row.get("side") not in {"BUY", "SELL"}:
            return "SCHEMA_ERROR", "trades.csv", trade_id, "side"

    for scenario in sorted(payload["scenarios"], key=lambda row: str(row.get("scenario_id", ""))):
        scenario_id = str(scenario.get("scenario_id", "")) or None
        shock, vol_shock = _number(scenario.get("spot_shock_pct")), _number(scenario.get("vol_shock_abs"))
        if scenario_id is None or shock is None or vol_shock is None:
            return "INVALID_SCENARIO", "scenarios.json", scenario_id, None
        for name, row in market1.items():
            spot = _number(row.get("spot_usd"))
            iv = _number(row.get("iv_decimal"))
            if spot is None or iv is None or spot * (1 + shock) <= 0 or (
                _parse_utc(row["expiration_timestamp"]) > t1 and iv + vol_shock <= 0
            ):
                return "INVALID_SCENARIO", "scenarios.json", scenario_id, None
    return None


def render_reference_outputs(input_dir: Path) -> dict[str, Any]:
    try:
        payload = _read_inputs(input_dir)
    except FileNotFoundError as exc:
        return _error_report("MISSING_REQUIRED_FILE", Path(exc.filename).name, None, None)
    except _InputReadError as exc:
        return _error_report("SCHEMA_ERROR", exc.filename, None, None)
    error = _validate_public_contract(payload)
    if error is not None:
        return _error_report(*error)
    payload = _finalize_inputs(payload)
    config = payload["config"]
    t0, t1 = _parse_utc(config["benchmark_t0_utc"]), _parse_utc(config["benchmark_t1_utc"])
    metadata = {row["instrument_name"]: row for row in payload["metadata_rows"]}
    rates = {row["instrument_name"]: row for row in payload["rate_rows"]}
    market0 = {row["instrument_name"]: row for row in payload["market0_rows"]}
    market1 = {row["instrument_name"]: row for row in payload["market1_rows"]}
    for name in sorted(metadata):
        for filename, row in (("market_t0.parquet", market0[name]), ("market_t1.parquet", market1[name])):
            for field in ("spot_usd", "strike_usd", "contract_multiplier"):
                value = float(row[field])
                if not math.isfinite(value) or value <= 0:
                    return _error_report("INVALID_CORE_VALUE", filename, name, field)
            iv = float(row["iv_decimal"])
            if _parse_utc(row["expiration_timestamp"]) > _parse_utc(row["valuation_timestamp"]) and (not math.isfinite(iv) or iv <= 0):
                return _error_report("INVALID_CORE_VALUE", filename, name, "iv_decimal")
    scenarios = sorted(payload["scenarios"], key=lambda row: row["scenario_id"])
    for scenario in scenarios:
        for row in market1.values():
            if row["spot_usd"] * (1 + float(scenario["spot_shock_pct"])) <= 0 or (
                _parse_utc(row["expiration_timestamp"]) > t1 and row["iv_decimal"] + float(scenario["vol_shock_abs"]) <= 0
            ):
                return _error_report("INVALID_SCENARIO", "scenarios.json", scenario["scenario_id"], None)

    initial = {name: 0.0 for name in metadata}
    initial.update({row["instrument_name"]: float(row["quantity"]) for row in payload["initial"]})
    trades = [Trade(row["instrument_name"], _parse_utc(row["timestamp"]), row["side"], float(row["quantity"]), float(row["execution_price_usd"]), float(row["fee_usd"])) for row in payload["trades"]]
    ending = reconstruct_positions(initial, trades)
    positions_rows, valuation_rows, greek_rows, pnl_rows = [], [], [], []
    position_greek_states = {"t0": [], "t1": []}
    pnl_objects, attribution_objects = [], []
    stress_inputs: dict[str, dict[str, object]] = {}
    for name in sorted(metadata):
        meta, row0, row1 = metadata[name], market0[name], market1[name]
        q0, q1, multiplier = initial[name], ending[name], float(meta["contract_multiplier"])
        expiry, rate = _parse_utc(meta["expiration_timestamp"]), float(rates[name]["rate_decimal"])
        time0, time1 = act365_time_years(t0, expiry), act365_time_years(t1, expiry)
        spot0, spot1, iv0, iv1 = map(float, (row0["spot_usd"], row1["spot_usd"], row0["iv_decimal"], row1["iv_decimal"]))
        option_type, strike = meta["option_type"], float(meta["strike_usd"])
        value0, value1 = bs_price(option_type, spot0, strike, time0, iv0, rate), bs_price(option_type, spot1, strike, time1, iv1, rate)
        unit0, unit1 = bs_greeks(option_type, spot0, strike, time0, iv0, rate), bs_greeks(option_type, spot1, strike, time1, iv1, rate)
        pos0, pos1 = scale_position_greeks(unit0, q0, multiplier), scale_position_greeks(unit1, q1, multiplier)
        position_greek_states["t0"].append(pos0); position_greek_states["t1"].append(pos1)
        positions_rows.append({"instrument_name": name, "quantity_t0": q0, "net_trade_quantity": q1 - q0, "quantity_t1": q1})
        valuation_rows.append({"instrument_name": name, "quantity_t0": q0, "quantity_t1": q1, "contract_multiplier": multiplier,
                               "unit_value_t0_usd": value0, "unit_value_t1_usd": value1,
                               "market_value_t0_usd": q0 * multiplier * value0, "market_value_t1_usd": q1 * multiplier * value1})
        for state, timestamp, quantity, unit, position in (("t0", config["benchmark_t0_utc"], q0, unit0, pos0), ("t1", config["benchmark_t1_utc"], q1, unit1, pos1)):
            greek_rows.append({"aggregation_level": "INSTRUMENT", "instrument_name": name, "valuation_state": state, "valuation_timestamp_utc": timestamp,
                               "quantity": quantity, "contract_multiplier": multiplier,
                               **{f"unit_{field}": getattr(unit, field) for field in unit.__dataclass_fields__},
                               **{f"position_{field}": getattr(position, field) for field in position.__dataclass_fields__}})
        instrument_trades = [trade for trade in trades if trade.instrument == name]
        pnl = instrument_pnl(name, q0, q1, multiplier, value0, value1, instrument_trades)
        pnl_objects.append(pnl)
        attribution = attribute_carry_pnl(q0, multiplier, unit0, spot1 - spot0, iv1 - iv0, (t1 - t0).total_seconds() / SECONDS_PER_YEAR, pnl.carry_actual_pnl)
        attribution_objects.append(attribution)
        pnl_rows.append({"aggregation_level": "INSTRUMENT", "instrument_name": name,
                         "beginning_market_value_usd": pnl.beginning_market_value, "ending_market_value_usd": pnl.ending_market_value,
                         "trade_cashflow_usd": pnl.trade_cashflows, "fees_usd": pnl.fees_usd,
                         "carry_actual_pnl_usd": pnl.carry_actual_pnl, "trade_to_t1_pnl_usd": pnl.trade_to_t1_pnl, "total_pnl_usd": pnl.total_pnl,
                         **{f"{field}_usd": getattr(attribution, field) for field in attribution.__dataclass_fields__}})
        stress_inputs[name] = {"q1": q1, "multiplier": multiplier, "option_type": option_type, "strike": strike,
                               "spot": spot1, "iv": iv1, "rate": rate, "time": time1, "base_unit": value1}

    for state in ("t0", "t1"):
        aggregate = aggregate_position_greeks(position_greek_states[state])
        greek_rows.append({"aggregation_level": "PORTFOLIO", "instrument_name": "__PORTFOLIO__", "valuation_state": state,
                           "valuation_timestamp_utc": config[f"benchmark_{state}_utc"], "quantity": "", "contract_multiplier": "",
                           **{f"unit_{field}": "" for field in aggregate.__dataclass_fields__},
                           **{f"position_{field}": getattr(aggregate, field) for field in aggregate.__dataclass_fields__}})
    portfolio = portfolio_pnl(pnl_objects)
    pnl_rows.append({"aggregation_level": "PORTFOLIO", "instrument_name": "__PORTFOLIO__",
                     "beginning_market_value_usd": portfolio.beginning_market_value, "ending_market_value_usd": portfolio.ending_market_value,
                     "trade_cashflow_usd": portfolio.trade_cashflows, "fees_usd": portfolio.fees_usd,
                     "carry_actual_pnl_usd": portfolio.carry_actual_pnl, "trade_to_t1_pnl_usd": portfolio.trade_to_t1_pnl, "total_pnl_usd": portfolio.total_pnl,
                     **{f"{field}_usd": sum(getattr(item, field) for item in attribution_objects) for field in attribution_objects[0].__dataclass_fields__}})

    stress_scenarios = []
    for scenario in scenarios:
        shock, vol_shock = float(scenario["spot_shock_pct"]), float(scenario["vol_shock_abs"])
        instruments = []
        for name in sorted(stress_inputs):
            values = stress_inputs[name]
            stressed_spot, stressed_iv = values["spot"] * (1 + shock), values["iv"] + vol_shock
            stressed_unit = bs_price(values["option_type"], stressed_spot, values["strike"], values["time"], stressed_iv, values["rate"])
            base_mv = values["q1"] * values["multiplier"] * values["base_unit"]
            stressed_mv = values["q1"] * values["multiplier"] * stressed_unit
            instruments.append({"instrument_name": name, "quantity_t1": values["q1"], "contract_multiplier": values["multiplier"],
                                "base_unit_value_usd": values["base_unit"], "stressed_spot_usd": stressed_spot, "stressed_iv_decimal": stressed_iv,
                                "stressed_unit_value_usd": stressed_unit, "base_market_value_usd": base_mv,
                                "stressed_market_value_usd": stressed_mv, "stress_pnl_usd": stressed_mv - base_mv})
        base_total = sum(row["base_market_value_usd"] for row in instruments)
        stressed_total = sum(row["stressed_market_value_usd"] for row in instruments)
        stress_scenarios.append({"scenario_id": scenario["scenario_id"], "spot_shock_pct": shock, "vol_shock_abs": vol_shock,
                                 "portfolio_base_value_usd": base_total, "portfolio_stressed_value_usd": stressed_total,
                                 "portfolio_stress_pnl_usd": stressed_total - base_total, "instruments": instruments})
    return {
        "positions_eod.csv": {"columns": CSV_COLUMNS["positions_eod.csv"], "rows": positions_rows},
        "valuation.csv": {"columns": CSV_COLUMNS["valuation.csv"], "rows": valuation_rows},
        "greeks.csv": {"columns": CSV_COLUMNS["greeks.csv"], "rows": greek_rows},
        "pnl_attribution.csv": {"columns": CSV_COLUMNS["pnl_attribution.csv"], "rows": pnl_rows},
        "stress_report.json": {"schema_version": 1, "reporting_currency": "USD", "base_valuation_timestamp_utc": config["benchmark_t1_utc"], "scenarios": stress_scenarios},
        "validation_report.json": _validation_report(payload["warnings"]),
    }


def write_outputs(output_dir: Path, outputs: dict[str, Any]) -> None:
    output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=False)
    for filename, value in outputs.items():
        path = output_dir / filename
        if filename.endswith(".csv"):
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=value["columns"], lineterminator="\n")
                writer.writeheader(); writer.writerows(value["rows"])
        else:
            path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
