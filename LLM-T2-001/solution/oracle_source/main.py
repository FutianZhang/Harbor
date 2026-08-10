"""独立的 Candidate-like Golden 可执行路径。

该模块只读取公开 input directory，并通过公开 output contract 写结果。它复用已验证的
``golden`` 金融 primitives，但不依赖 grader、reference oracle 或 expected outputs。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq

from .accounting import PnLResult, instrument_pnl, portfolio_pnl
from .attribution import AttributionResult, attribute_carry_pnl
from .greeks import PositionGreeks, UnitGreeks, aggregate_position_greeks, bs_greeks, scale_position_greeks
from .positions import Trade, reconstruct_positions
from .pricing import SECONDS_PER_YEAR, act365_time_years, bs_price


INPUT_FILES = (
    "config.json", "initial_positions.csv", "trades.csv", "instrument_metadata.csv",
    "rates.csv", "market_t0.parquet", "market_t1.parquet", "scenarios.json",
)

CONFIG_FIELDS = {
    "benchmark_id", "benchmark_t0_utc", "benchmark_t1_utc", "dataset_version",
    "generation_spec_sha256", "random_seed", "reporting_currency", "source_snapshot_sha256",
    "source_version", "year_fraction_convention",
}

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

MARKET_REQUIRED_TYPES = {
    "instrument_name": "string", "underlying": "string", "option_type": "string",
    "valuation_timestamp": "string", "spot_usd": "double", "strike_usd": "double",
    "expiration_timestamp": "string", "iv_decimal": "double", "rate_decimal": "double",
    "contract_multiplier": "double",
}
MARKET_OPTIONAL_TYPES = {
    "market_t0.parquet": {
        "source_bid_price_coin": "double", "source_ask_price_coin": "double",
        "source_mark_price_coin": "double", "source_mark_iv_pct": "double",
        "source_orderbook_timestamp_ms": "int64",
    },
    "market_t1.parquet": {"spot_shock_pct": "double", "iv_change_decimal": "double"},
}

CSV_OUTPUT_COLUMNS = {
    "positions_eod.csv": ["instrument_name", "quantity_t0", "net_trade_quantity", "quantity_t1"],
    "valuation.csv": ["instrument_name", "quantity_t0", "quantity_t1", "contract_multiplier", "unit_value_t0_usd", "unit_value_t1_usd", "market_value_t0_usd", "market_value_t1_usd"],
    "greeks.csv": ["aggregation_level", "instrument_name", "valuation_state", "valuation_timestamp_utc", "quantity", "contract_multiplier", "unit_delta", "unit_gamma", "unit_vega_decimal", "unit_vega_1vol", "unit_theta_year", "unit_theta_day", "unit_vanna", "unit_volga", "position_delta", "position_gamma", "position_vega_decimal", "position_vega_1vol", "position_theta_year", "position_theta_day", "position_vanna", "position_volga"],
    "pnl_attribution.csv": ["aggregation_level", "instrument_name", "beginning_market_value_usd", "ending_market_value_usd", "trade_cashflow_usd", "fees_usd", "carry_actual_pnl_usd", "trade_to_t1_pnl_usd", "total_pnl_usd", "delta_pnl_usd", "gamma_pnl_usd", "vega_pnl_usd", "theta_pnl_usd", "vanna_pnl_usd", "volga_pnl_usd", "explained_pnl_usd", "residual_pnl_usd"],
}


@dataclass(frozen=True)
class ContractError(Exception):
    code: str
    filename: str
    row_key: str | None = None
    field: str | None = None


@dataclass
class Inputs:
    config: dict[str, Any]
    initial: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    metadata: list[dict[str, Any]]
    rates: list[dict[str, Any]]
    market0: list[dict[str, Any]]
    market1: list[dict[str, Any]]
    scenarios: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC RFC3339 timestamp must end in Z")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return list(reader.fieldnames or ()), list(reader)
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ContractError("SCHEMA_ERROR", path.name) from exc


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except Exception as exc:
        raise ContractError("SCHEMA_ERROR", path.name) from exc


def _record(code: str, filename: str, row_key: str | None = None,
            field: str | None = None) -> dict[str, Any]:
    location = row_key or filename
    return {"code": code, "file": filename, "row_key": row_key, "field": field,
            "message": f"{code} at {location}."}


def _record_sort_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple("" if row[key] is None else str(row[key]) for key in ("code", "file", "row_key", "field"))


def _require_unique(rows: Iterable[dict[str, Any]], key: str, filename: str) -> None:
    seen: set[str] = set()
    for row in rows:
        value = str(row.get(key, ""))
        if value in seen:
            raise ContractError("DUPLICATE_KEY", filename, value)
        seen.add(value)


def _read_inputs(input_dir: Path) -> Inputs:
    for filename in INPUT_FILES:
        if not (input_dir / filename).is_file():
            raise ContractError("MISSING_REQUIRED_FILE", filename)

    config = _json(input_dir / "config.json")
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        differing = sorted((set(config) if isinstance(config, dict) else set()) ^ CONFIG_FIELDS)
        raise ContractError("SCHEMA_ERROR", "config.json", field=differing[0] if differing else None)

    csv_rows: dict[str, list[dict[str, Any]]] = {}
    for filename, (required, allowed) in CSV_INPUT_COLUMNS.items():
        columns, rows = _csv(input_dir / filename)
        names = set(columns)
        if not required <= names or not names <= allowed:
            difference = sorted((required - names) | (names - allowed))
            raise ContractError("SCHEMA_ERROR", filename, field=difference[0])
        csv_rows[filename] = rows

    market_tables = {}
    for filename in ("market_t0.parquet", "market_t1.parquet"):
        try:
            table = pq.read_table(input_dir / filename)
        except Exception as exc:
            raise ContractError("SCHEMA_ERROR", filename) from exc
        expected = {**MARKET_REQUIRED_TYPES, **MARKET_OPTIONAL_TYPES[filename]}
        names = set(table.schema.names)
        if not set(MARKET_REQUIRED_TYPES) <= names or not names <= set(expected):
            difference = sorted((set(MARKET_REQUIRED_TYPES) - names) | (names - set(expected)))
            raise ContractError("SCHEMA_ERROR", filename, field=difference[0])
        for field, expected_type in expected.items():
            if field in names and str(table.schema.field(field).type) != expected_type:
                raise ContractError("SCHEMA_ERROR", filename, field=field)
        market_tables[filename] = table.to_pylist()

    scenario_document = _json(input_dir / "scenarios.json")
    if not isinstance(scenario_document, dict) or set(scenario_document) != {"scenarios"} or not isinstance(scenario_document.get("scenarios"), list):
        raise ContractError("SCHEMA_ERROR", "scenarios.json")
    scenarios = scenario_document["scenarios"]
    if any(not isinstance(row, dict) or set(row) != {"scenario_id", "spot_shock_pct", "vol_shock_abs"} for row in scenarios):
        raise ContractError("SCHEMA_ERROR", "scenarios.json")

    payload = Inputs(
        config=config,
        initial=csv_rows["initial_positions.csv"],
        trades=csv_rows["trades.csv"],
        metadata=csv_rows["instrument_metadata.csv"],
        rates=csv_rows["rates.csv"],
        market0=market_tables["market_t0.parquet"],
        market1=market_tables["market_t1.parquet"],
        scenarios=scenarios,
        warnings=[],
    )
    _validate_and_normalize(payload)
    return payload


def _validate_and_normalize(inputs: Inputs) -> None:
    keyed = (
        (inputs.initial, "instrument_name", "initial_positions.csv"),
        (inputs.trades, "trade_id", "trades.csv"),
        (inputs.metadata, "instrument_name", "instrument_metadata.csv"),
        (inputs.rates, "instrument_name", "rates.csv"),
        (inputs.market1, "instrument_name", "market_t1.parquet"),
        (inputs.scenarios, "scenario_id", "scenarios.json"),
    )
    for rows, key, filename in keyed:
        _require_unique(rows, key, filename)

    physical_market0 = list(inputs.market0)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in physical_market0:
        groups.setdefault(str(row.get("instrument_name", "")), []).append(row)
    normalized = []
    for name in sorted(groups):
        group = groups[name]
        if len(group) > 1:
            if any(group[0] != duplicate for duplicate in group[1:]):
                raise ContractError("DUPLICATE_KEY", "market_t0.parquet", name)
            inputs.warnings.append(_record("EXACT_DUPLICATE_DROPPED", "market_t0.parquet", name))
        normalized.append(group[0])
    inputs.market0 = normalized

    sortable = (
        (inputs.initial, lambda row: row["instrument_name"], "initial_positions.csv"),
        (inputs.trades, lambda row: (_utc(row["timestamp"]), row["trade_id"]), "trades.csv"),
        (inputs.metadata, lambda row: row["instrument_name"], "instrument_metadata.csv"),
        (inputs.rates, lambda row: row["instrument_name"], "rates.csv"),
        (physical_market0, lambda row: row["instrument_name"], "market_t0.parquet"),
        (inputs.market1, lambda row: row["instrument_name"], "market_t1.parquet"),
        (inputs.scenarios, lambda row: row["scenario_id"], "scenarios.json"),
    )
    for rows, key, filename in sortable:
        try:
            observed = [key(row) for row in rows]
        except (KeyError, TypeError, ValueError):
            continue
        if observed != sorted(observed):
            inputs.warnings.append(_record("ROW_ORDER_NORMALIZED", filename))

    metadata_names = {str(row["instrument_name"]) for row in inputs.metadata}
    for rows, filename in ((inputs.initial, "initial_positions.csv"), (inputs.trades, "trades.csv")):
        for row in rows:
            name = str(row.get("instrument_name", ""))
            if name not in metadata_names:
                raise ContractError("MISSING_INSTRUMENT", filename, name or None, "instrument_name")

    for rows, filename in ((inputs.rates, "rates.csv"), (inputs.market0, "market_t0.parquet"), (inputs.market1, "market_t1.parquet")):
        missing = sorted(metadata_names - {str(row.get("instrument_name", "")) for row in rows})
        if missing:
            raise ContractError("CORE_STATE_MISSING", filename, missing[0])

    try:
        t0 = _utc(inputs.config["benchmark_t0_utc"])
    except (TypeError, ValueError):
        raise ContractError("INVALID_TIMESTAMP", "config.json", field="benchmark_t0_utc")
    try:
        t1 = _utc(inputs.config["benchmark_t1_utc"])
    except (TypeError, ValueError):
        raise ContractError("INVALID_TIMESTAMP", "config.json", field="benchmark_t1_utc")
    if t1 <= t0:
        raise ContractError("INVALID_TIMESTAMP", "config.json", field="benchmark_t1_utc")

    metadata = {str(row["instrument_name"]): row for row in inputs.metadata}
    rates = {str(row["instrument_name"]): row for row in inputs.rates}
    market0 = {str(row["instrument_name"]): row for row in inputs.market0}
    market1 = {str(row["instrument_name"]): row for row in inputs.market1}

    for name in sorted(metadata):
        meta = metadata[name]
        try:
            _utc(meta.get("expiration_timestamp"))
        except (TypeError, ValueError):
            raise ContractError("INVALID_TIMESTAMP", "instrument_metadata.csv", name, "expiration_timestamp")
        if meta.get("option_type") not in {"call", "put"}:
            raise ContractError("INVALID_OPTION_TYPE", "instrument_metadata.csv", name, "option_type")
        for field in ("strike_usd", "contract_multiplier"):
            value = _finite(meta.get(field))
            if value is None or value <= 0:
                raise ContractError("INVALID_CORE_VALUE", "instrument_metadata.csv", name, field)
        rate = _finite(rates[name].get("rate_decimal"))
        if rate is None:
            raise ContractError("INVALID_CORE_VALUE", "rates.csv", name, "rate_decimal")
        for field in ("benchmark_t0_utc", "benchmark_t1_utc"):
            try:
                _utc(rates[name].get(field))
            except (TypeError, ValueError):
                raise ContractError("INVALID_TIMESTAMP", "rates.csv", name, field)
            if rates[name][field] != inputs.config[field]:
                raise ContractError("CORE_STATE_CONFLICT", "rates.csv", name, field)

        for row, filename, expected_clock in (
            (market0[name], "market_t0.parquet", inputs.config["benchmark_t0_utc"]),
            (market1[name], "market_t1.parquet", inputs.config["benchmark_t1_utc"]),
        ):
            for field in ("valuation_timestamp", "expiration_timestamp"):
                try:
                    _utc(row.get(field))
                except (TypeError, ValueError):
                    raise ContractError("INVALID_TIMESTAMP", filename, name, field)
            if row["valuation_timestamp"] != expected_clock:
                raise ContractError("CORE_STATE_CONFLICT", filename, name, "valuation_timestamp")
            for field in ("underlying", "option_type", "strike_usd", "expiration_timestamp", "contract_multiplier"):
                left, right = row.get(field), meta.get(field)
                if field in {"strike_usd", "contract_multiplier"}:
                    left, right = _finite(left), _finite(right)
                if left != right:
                    raise ContractError("CORE_STATE_CONFLICT", filename, name, field)
            if _finite(row.get("rate_decimal")) != rate:
                raise ContractError("CORE_STATE_CONFLICT", filename, name, "rate_decimal")
            for field in ("spot_usd", "strike_usd", "contract_multiplier"):
                value = _finite(row.get(field))
                if value is None or value <= 0:
                    raise ContractError("INVALID_CORE_VALUE", filename, name, field)
            iv = _finite(row.get("iv_decimal"))
            if iv is None or (_utc(row["expiration_timestamp"]) > _utc(row["valuation_timestamp"]) and iv <= 0):
                raise ContractError("INVALID_CORE_VALUE", filename, name, "iv_decimal")

    for row in inputs.initial:
        if _finite(row.get("quantity")) is None:
            raise ContractError("INVALID_CORE_VALUE", "initial_positions.csv", str(row.get("instrument_name", "")) or None, "quantity")
    for row in inputs.trades:
        trade_id = str(row.get("trade_id", "")) or None
        try:
            timestamp = _utc(row.get("timestamp"))
        except (TypeError, ValueError):
            raise ContractError("INVALID_TIMESTAMP", "trades.csv", trade_id, "timestamp")
        if not t0 < timestamp < t1:
            raise ContractError("INVALID_TIMESTAMP", "trades.csv", trade_id, "timestamp")
        for field, allow_zero in (("quantity", False), ("execution_price_usd", True), ("fee_usd", True)):
            value = _finite(row.get(field))
            if value is None or value < 0 or (not allow_zero and value == 0):
                raise ContractError("INVALID_CORE_VALUE", "trades.csv", trade_id, field)
        if row.get("side") not in {"BUY", "SELL"}:
            raise ContractError("SCHEMA_ERROR", "trades.csv", trade_id, "side")

    for scenario in sorted(inputs.scenarios, key=lambda row: str(row.get("scenario_id", ""))):
        scenario_id = str(scenario.get("scenario_id", "")) or None
        shock, vol_shock = _finite(scenario.get("spot_shock_pct")), _finite(scenario.get("vol_shock_abs"))
        if scenario_id is None or shock is None or vol_shock is None:
            raise ContractError("INVALID_SCENARIO", "scenarios.json", scenario_id)
        for row in market1.values():
            spot, iv = _finite(row.get("spot_usd")), _finite(row.get("iv_decimal"))
            if spot is None or iv is None or spot * (1 + shock) <= 0 or (_utc(row["expiration_timestamp"]) > t1 and iv + vol_shock <= 0):
                raise ContractError("INVALID_SCENARIO", "scenarios.json", scenario_id)

    optional_quotes = ("source_bid_price_coin", "source_ask_price_coin", "source_mark_price_coin", "source_mark_iv_pct")
    for row in inputs.market0:
        name = str(row["instrument_name"])
        for field in (*optional_quotes, "source_orderbook_timestamp_ms"):
            value = row.get(field)
            if value is None:
                if field in optional_quotes:
                    inputs.warnings.append(_record("OPTIONAL_QUOTE_MISSING", "market_t0.parquet", name, field))
                continue
            number = float(value)
            invalid = not math.isfinite(number)
            if field == "source_bid_price_coin":
                invalid = invalid or number < 0
                if math.isfinite(number) and number == 0:
                    inputs.warnings.append(_record("OPTIONAL_QUOTE_ZERO_BID", "market_t0.parquet", name, field))
            elif field in {"source_ask_price_coin", "source_mark_price_coin", "source_mark_iv_pct"}:
                invalid = invalid or number <= 0
            else:
                invalid = invalid or number < 0
            if invalid:
                inputs.warnings.append(_record("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, field))
        bid, ask = row.get("source_bid_price_coin"), row.get("source_ask_price_coin")
        if bid is not None and ask is not None and math.isfinite(float(bid)) and math.isfinite(float(ask)) and float(bid) > float(ask):
            inputs.warnings.append(_record("OPTIONAL_QUOTE_CROSSED", "market_t0.parquet", name))

    inputs.initial.sort(key=lambda row: row["instrument_name"])
    inputs.trades.sort(key=lambda row: (_utc(row["timestamp"]), row["trade_id"]))
    inputs.metadata.sort(key=lambda row: row["instrument_name"])
    inputs.rates.sort(key=lambda row: row["instrument_name"])
    inputs.market1.sort(key=lambda row: row["instrument_name"])
    inputs.scenarios.sort(key=lambda row: row["scenario_id"])
    inputs.warnings.sort(key=_record_sort_key)


def _validation_report(*, warnings: Iterable[dict[str, Any]] = (),
                       errors: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    warning_rows, error_rows = list(warnings), list(errors)
    status = "ERROR" if error_rows else ("PASS_WITH_WARNINGS" if warning_rows else "PASS")
    return {"errors": error_rows, "schema_version": 1, "status": status, "warnings": warning_rows}


def _greek_row(name: str, state: str, timestamp: str, quantity: float, multiplier: float,
               unit: UnitGreeks, position: PositionGreeks) -> dict[str, Any]:
    row = {
        "aggregation_level": "INSTRUMENT", "instrument_name": name,
        "valuation_state": state, "valuation_timestamp_utc": timestamp,
        "quantity": quantity, "contract_multiplier": multiplier,
    }
    row.update({f"unit_{field}": getattr(unit, field) for field in unit.__dataclass_fields__})
    row.update({f"position_{field}": getattr(position, field) for field in position.__dataclass_fields__})
    return row


def _pnl_row(level: str, name: str, pnl: PnLResult,
             attribution: AttributionResult) -> dict[str, Any]:
    return {
        "aggregation_level": level, "instrument_name": name,
        "beginning_market_value_usd": pnl.beginning_market_value,
        "ending_market_value_usd": pnl.ending_market_value,
        "trade_cashflow_usd": pnl.trade_cashflows, "fees_usd": pnl.fees_usd,
        "carry_actual_pnl_usd": pnl.carry_actual_pnl,
        "trade_to_t1_pnl_usd": pnl.trade_to_t1_pnl, "total_pnl_usd": pnl.total_pnl,
        **{f"{field}_usd": getattr(attribution, field) for field in attribution.__dataclass_fields__},
    }


def _sum_attribution(rows: list[AttributionResult]) -> AttributionResult:
    return AttributionResult(*(sum(getattr(row, field) for row in rows) for field in AttributionResult.__dataclass_fields__))


def _build_outputs(inputs: Inputs) -> dict[str, Any]:
    config = inputs.config
    t0, t1 = _utc(config["benchmark_t0_utc"]), _utc(config["benchmark_t1_utc"])
    metadata = {row["instrument_name"]: row for row in inputs.metadata}
    rates = {row["instrument_name"]: row for row in inputs.rates}
    market0 = {row["instrument_name"]: row for row in inputs.market0}
    market1 = {row["instrument_name"]: row for row in inputs.market1}
    initial = {name: 0.0 for name in metadata}
    initial.update({row["instrument_name"]: float(row["quantity"]) for row in inputs.initial})
    trades = [
        Trade(row["instrument_name"], _utc(row["timestamp"]), row["side"], float(row["quantity"]),
              float(row["execution_price_usd"]), float(row["fee_usd"]))
        for row in inputs.trades
    ]
    ending = reconstruct_positions(initial, trades)

    positions_rows: list[dict[str, Any]] = []
    valuation_rows: list[dict[str, Any]] = []
    greek_rows: list[dict[str, Any]] = []
    pnl_rows: list[dict[str, Any]] = []
    greek_states: dict[str, list[PositionGreeks]] = {"t0": [], "t1": []}
    pnl_values: list[PnLResult] = []
    attribution_values: list[AttributionResult] = []
    stress_state: dict[str, dict[str, Any]] = {}

    for name in sorted(metadata):
        meta, row0, row1 = metadata[name], market0[name], market1[name]
        q0, q1, multiplier = initial[name], ending[name], float(meta["contract_multiplier"])
        expiry = _utc(meta["expiration_timestamp"])
        time0, time1 = act365_time_years(t0, expiry), act365_time_years(t1, expiry)
        rate = float(rates[name]["rate_decimal"])
        spot0, spot1 = float(row0["spot_usd"]), float(row1["spot_usd"])
        iv0, iv1 = float(row0["iv_decimal"]), float(row1["iv_decimal"])
        option_type, strike = meta["option_type"], float(meta["strike_usd"])
        value0 = bs_price(option_type, spot0, strike, time0, iv0, rate)
        value1 = bs_price(option_type, spot1, strike, time1, iv1, rate)
        unit0 = bs_greeks(option_type, spot0, strike, time0, iv0, rate)
        unit1 = bs_greeks(option_type, spot1, strike, time1, iv1, rate)
        position0 = scale_position_greeks(unit0, q0, multiplier)
        position1 = scale_position_greeks(unit1, q1, multiplier)
        greek_states["t0"].append(position0)
        greek_states["t1"].append(position1)

        positions_rows.append({"instrument_name": name, "quantity_t0": q0,
                               "net_trade_quantity": q1 - q0, "quantity_t1": q1})
        valuation_rows.append({
            "instrument_name": name, "quantity_t0": q0, "quantity_t1": q1,
            "contract_multiplier": multiplier, "unit_value_t0_usd": value0,
            "unit_value_t1_usd": value1, "market_value_t0_usd": q0 * multiplier * value0,
            "market_value_t1_usd": q1 * multiplier * value1,
        })
        greek_rows.extend((
            _greek_row(name, "t0", config["benchmark_t0_utc"], q0, multiplier, unit0, position0),
            _greek_row(name, "t1", config["benchmark_t1_utc"], q1, multiplier, unit1, position1),
        ))

        instrument_trades = [trade for trade in trades if trade.instrument == name]
        pnl = instrument_pnl(name, q0, q1, multiplier, value0, value1, instrument_trades)
        attribution = attribute_carry_pnl(
            q0, multiplier, unit0, spot1 - spot0, iv1 - iv0,
            (t1 - t0).total_seconds() / SECONDS_PER_YEAR, pnl.carry_actual_pnl,
        )
        pnl_values.append(pnl)
        attribution_values.append(attribution)
        pnl_rows.append(_pnl_row("INSTRUMENT", name, pnl, attribution))
        stress_state[name] = {
            "q1": q1, "multiplier": multiplier, "option_type": option_type,
            "strike": strike, "spot": spot1, "iv": iv1, "rate": rate,
            "time": time1, "base_unit": value1,
        }

    for state in ("t0", "t1"):
        aggregate = aggregate_position_greeks(greek_states[state])
        row = {
            "aggregation_level": "PORTFOLIO", "instrument_name": "__PORTFOLIO__",
            "valuation_state": state, "valuation_timestamp_utc": config[f"benchmark_{state}_utc"],
            "quantity": "", "contract_multiplier": "",
        }
        row.update({f"unit_{field}": "" for field in aggregate.__dataclass_fields__})
        row.update({f"position_{field}": getattr(aggregate, field) for field in aggregate.__dataclass_fields__})
        greek_rows.append(row)

    pnl_rows.append(_pnl_row(
        "PORTFOLIO", "__PORTFOLIO__", portfolio_pnl(pnl_values), _sum_attribution(attribution_values)
    ))

    stress_scenarios = []
    for scenario in inputs.scenarios:
        shock, vol_shock = float(scenario["spot_shock_pct"]), float(scenario["vol_shock_abs"])
        instrument_rows = []
        for name in sorted(stress_state):
            state = stress_state[name]
            stressed_spot = state["spot"] * (1 + shock)
            stressed_iv = state["iv"] + vol_shock
            stressed_unit = bs_price(state["option_type"], stressed_spot, state["strike"], state["time"], stressed_iv, state["rate"])
            base_market = state["q1"] * state["multiplier"] * state["base_unit"]
            stressed_market = state["q1"] * state["multiplier"] * stressed_unit
            instrument_rows.append({
                "base_market_value_usd": base_market, "base_unit_value_usd": state["base_unit"],
                "contract_multiplier": state["multiplier"], "instrument_name": name,
                "quantity_t1": state["q1"], "stress_pnl_usd": stressed_market - base_market,
                "stressed_iv_decimal": stressed_iv, "stressed_market_value_usd": stressed_market,
                "stressed_spot_usd": stressed_spot, "stressed_unit_value_usd": stressed_unit,
            })
        base_total = sum(row["base_market_value_usd"] for row in instrument_rows)
        stressed_total = sum(row["stressed_market_value_usd"] for row in instrument_rows)
        stress_scenarios.append({
            "instruments": instrument_rows, "portfolio_base_value_usd": base_total,
            "portfolio_stress_pnl_usd": stressed_total - base_total,
            "portfolio_stressed_value_usd": stressed_total, "scenario_id": scenario["scenario_id"],
            "spot_shock_pct": shock, "vol_shock_abs": vol_shock,
        })

    return {
        "positions_eod.csv": positions_rows,
        "valuation.csv": valuation_rows,
        "greeks.csv": greek_rows,
        "pnl_attribution.csv": pnl_rows,
        "stress_report.json": {
            "base_valuation_timestamp_utc": config["benchmark_t1_utc"],
            "reporting_currency": "USD", "scenarios": stress_scenarios, "schema_version": 1,
        },
        "validation_report.json": _validation_report(warnings=inputs.warnings),
    }


def _write_outputs(output_dir: Path, outputs: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    for filename, value in outputs.items():
        path = output_dir / filename
        if filename.endswith(".csv"):
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_OUTPUT_COLUMNS[filename], lineterminator="\n")
                writer.writeheader()
                writer.writerows(value)
        else:
            path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def run_candidate(input_dir: Path, output_dir: Path) -> int:
    """执行一次 Candidate-like run，并返回应由 harness 观察的 exit code。"""
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    try:
        inputs = _read_inputs(input_dir)
        outputs = _build_outputs(inputs)
    except ContractError as exc:
        outputs = {"validation_report.json": _validation_report(errors=[
            _record(exc.code, exc.filename, exc.row_key, exc.field)
        ])}
        _write_outputs(output_dir, outputs)
        return 2
    _write_outputs(output_dir, outputs)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    return run_candidate(Path(args.input_dir), Path(args.output_dir))


if __name__ == "__main__":
    raise SystemExit(main())
