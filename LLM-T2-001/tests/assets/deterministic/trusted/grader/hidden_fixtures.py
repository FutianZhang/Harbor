"""Deterministic, non-public Phase 04B input fixture generation."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


INPUT_FILES = (
    "config.json", "initial_positions.csv", "trades.csv", "instrument_metadata.csv",
    "rates.csv", "market_t0.parquet", "market_t1.parquet", "scenarios.json",
)

REQUIRED_VARIANTS = (
    "cross_asset_nonlinear", "portfolio_reversal", "validation_edges",
    "fatal_invalid_scenario", "fatal_invalid_core", "fatal_optional_dtype", "fatal_duplicate",
    "fatal_missing_file", "fatal_extra_field", "fatal_missing_column", "fatal_missing_core_state",
    "fatal_missing_instrument", "fatal_core_conflict", "fatal_invalid_iv",
    "fatal_invalid_option_type", "fatal_invalid_strike", "fatal_invalid_timestamp",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _market_schema(t0: bool, optional_dtype_mismatch: bool = False) -> pa.Schema:
    fields = [
        pa.field("instrument_name", pa.string()), pa.field("underlying", pa.string()),
        pa.field("option_type", pa.string()), pa.field("valuation_timestamp", pa.string()),
        pa.field("spot_usd", pa.float64()), pa.field("strike_usd", pa.float64()),
        pa.field("expiration_timestamp", pa.string()), pa.field("iv_decimal", pa.float64()),
        pa.field("rate_decimal", pa.float64()), pa.field("contract_multiplier", pa.float64()),
    ]
    if t0:
        fields.extend([
            pa.field("source_bid_price_coin", pa.int64() if optional_dtype_mismatch else pa.float64()), pa.field("source_ask_price_coin", pa.float64()),
            pa.field("source_mark_price_coin", pa.float64()), pa.field("source_mark_iv_pct", pa.float64()),
            pa.field("source_orderbook_timestamp_ms", pa.int64()),
        ])
    else:
        fields.extend([pa.field("spot_shock_pct", pa.float64()), pa.field("iv_change_decimal", pa.float64())])
    return pa.schema(fields)


def _variant_payload(name: str, seed: int) -> dict[str, Any]:
    t0 = "2026-02-17T09:17:23.125000Z"
    t1 = "2026-02-18T15:41:09.875000Z"
    instruments = [
        ("BTC-X-73500-C", "BTC", "call", 73500.0, "2026-03-07T08:00:00Z", 0.17, 71240.0, 76890.0, 0.61, 0.56, -0.004),
        ("BTC-X-81000-P", "BTC", "put", 81000.0, "2026-08-28T08:00:00Z", 1.7, 71240.0, 76890.0, 0.52, 0.52, 0.0),
        ("ETH-X-2140-C", "ETH", "call", 2140.0, "2026-02-18T15:41:09.875000Z", 3.25, 2317.0, 2251.0, 0.78, 0.72, 0.012),
        ("ETH-X-2600-P", "ETH", "put", 2600.0, "2026-02-19T03:11:09.875000Z", 0.43, 2317.0, 2317.0, 0.83, 0.89, -0.011),
        ("ETH-X-2251-C", "ETH", "call", 2251.0, "2026-05-01T08:00:00Z", 2.1, 2317.0, 2251.0, 0.67, 0.78, 0.0),
        ("ETH-X-2251-P", "ETH", "put", 2251.0, "2026-05-01T08:00:00Z", 0.37, 2317.0, 2251.0, 0.67, 0.78, 0.0),
    ]
    if name == "portfolio_reversal":
        instruments[0] = ("BTC-R-64000-P", "BTC", "put", 64000.0, "2027-01-15T08:00:00Z", 0.29, 70200.0, 65500.0, 0.44, 0.63, -0.015)
    elif name == "validation_edges":
        instruments[1] = ("BTC-V-90000-C", "BTC", "call", 90000.0, "2026-04-24T08:00:00Z", 0.91, 69000.0, 73100.0, 0.58, 0.60, -0.002)

    config = {
        "benchmark_id": f"hidden-{name}-{seed}", "benchmark_t0_utc": t0, "benchmark_t1_utc": t1,
        "dataset_version": "hidden-v1", "generation_spec_sha256": hashlib.sha256(f"spec-{seed}".encode()).hexdigest(),
        "random_seed": seed, "reporting_currency": "USD",
        "source_snapshot_sha256": hashlib.sha256(f"source-{name}-{seed}".encode()).hexdigest(),
        "source_version": "phase04b-offline", "year_fraction_convention": "ACT/365",
    }
    metadata, rates, market0, market1 = [], [], [], []
    for index, (instrument, underlying, option_type, strike, expiry, multiplier, spot0, spot1, iv0, iv1, rate) in enumerate(instruments):
        metadata.append({"instrument_name": instrument, "underlying": underlying, "option_type": option_type,
                         "strike_usd": strike, "expiration_timestamp": expiry, "contract_multiplier": multiplier,
                         "price_index": f"{underlying.lower()}_usd", "maturity_bucket": f"H{index}"})
        rates.append({"instrument_name": instrument, "rate_decimal": rate,
                      "benchmark_t0_utc": t0, "benchmark_t1_utc": t1})
        core = {"instrument_name": instrument, "underlying": underlying, "option_type": option_type,
                "spot_usd": spot0, "strike_usd": strike, "expiration_timestamp": expiry,
                "iv_decimal": iv0, "rate_decimal": rate, "contract_multiplier": multiplier}
        market0.append({**core, "valuation_timestamp": t0, "source_bid_price_coin": 0.012 + index / 1000,
                        "source_ask_price_coin": 0.014 + index / 1000, "source_mark_price_coin": 0.013 + index / 1000,
                        "source_mark_iv_pct": iv0 * 100, "source_orderbook_timestamp_ms": 1771320000000 + index})
        market1.append({**core, "valuation_timestamp": t1, "spot_usd": spot1, "iv_decimal": iv1,
                        "spot_shock_pct": spot1 / spot0 - 1.0, "iv_change_decimal": iv1 - iv0})

    initial = [
        {"instrument_name": instruments[0][0], "quantity": 4.5},
        {"instrument_name": instruments[1][0], "quantity": -2.25},
    ]
    if len(instruments) > 2:
        initial.extend([
            {"instrument_name": instruments[2][0], "quantity": 1.1},
            {"instrument_name": instruments[3][0], "quantity": -0.9},
            {"instrument_name": instruments[4][0], "quantity": 1.25},
            {"instrument_name": instruments[-1][0], "quantity": 0.75},
        ])
    trades = [
        {"trade_id": "z-reverse", "timestamp": "2026-02-18T12:00:00.250000Z", "instrument_name": instruments[0][0], "side": "SELL", "quantity": 7.0, "execution_price_usd": 4321.25, "fee_usd": 7.75},
        {"trade_id": "a-partial", "timestamp": "2026-02-17T13:02:01.500000Z", "instrument_name": instruments[1][0], "side": "BUY", "quantity": 1.0, "execution_price_usd": 8200.75, "fee_usd": 13.95},
        {"trade_id": "m-add", "timestamp": "2026-02-18T02:44:59.125000Z", "instrument_name": instruments[-1][0], "side": "BUY", "quantity": 0.5, "execution_price_usd": 190.125, "fee_usd": 0.05},
    ]
    if len(instruments) > 2:
        trades.extend([
            {"trade_id": "b-multi", "timestamp": "2026-02-17T18:30:00.125000Z", "instrument_name": instruments[0][0], "side": "BUY", "quantity": 1.0, "execution_price_usd": 5100.5, "fee_usd": 1.3},
            {"trade_id": "c-full-close", "timestamp": "2026-02-18T05:15:00.625000Z", "instrument_name": instruments[4][0], "side": "SELL", "quantity": 1.25, "execution_price_usd": 245.75, "fee_usd": 0.95},
        ])
    scenarios = [
        {"scenario_id": "combo-curved", "spot_shock_pct": 0.19, "vol_shock_abs": 0.23},
        {"scenario_id": "down-vol-down", "spot_shock_pct": -0.27, "vol_shock_abs": -0.18},
        {"scenario_id": "spot-up", "spot_shock_pct": 0.11, "vol_shock_abs": 0.0},
    ]
    metadata.sort(key=lambda row: row["instrument_name"]); rates.sort(key=lambda row: row["instrument_name"])
    market0.sort(key=lambda row: row["instrument_name"]); market1.sort(key=lambda row: row["instrument_name"])
    initial.sort(key=lambda row: row["instrument_name"])
    trades.sort(key=lambda row: (row["timestamp"], row["trade_id"])); scenarios.sort(key=lambda row: row["scenario_id"])
    if name == "validation_edges":
        market0[0]["source_bid_price_coin"] = 0.0
        market0[1]["source_bid_price_coin"] = 0.2
        market0[1]["source_ask_price_coin"] = 0.1
        market0[2]["source_mark_iv_pct"] = float("nan")
        market0[2]["source_bid_price_coin"] = None
        market0[3]["source_mark_price_coin"] = None
        market0[3]["source_ask_price_coin"] = None
        market0[4]["source_orderbook_timestamp_ms"] = -5
        market0[5]["source_ask_price_coin"] = -0.01
        market0.append(dict(market0[0]))
        metadata.reverse(); rates.reverse(); market0.reverse(); market1.reverse(); trades.reverse(); scenarios.reverse()
    if name == "fatal_invalid_scenario":
        scenarios = [{"scenario_id": "invalid-spot", "spot_shock_pct": -1.01, "vol_shock_abs": 0.0}]
    elif name == "fatal_invalid_core":
        market0[0]["spot_usd"] = -1.0
    elif name == "fatal_optional_dtype":
        for row in market0:
            row["source_bid_price_coin"] = 1
    elif name == "fatal_duplicate":
        conflicting = dict(market0[0]); conflicting["source_ask_price_coin"] += 0.001; market0.insert(1, conflicting)
    elif name == "fatal_extra_field":
        config["undeclared_probe"] = "must be rejected"
    elif name == "fatal_missing_core_state":
        market1.pop(0)
    elif name == "fatal_missing_instrument":
        initial[0]["instrument_name"] = "AAA-UNKNOWN-INSTRUMENT"
    elif name == "fatal_core_conflict":
        market1[0]["contract_multiplier"] *= 2.0
    elif name == "fatal_invalid_iv":
        market1[0]["iv_decimal"] = 0.0
    elif name == "fatal_invalid_option_type":
        metadata[0]["option_type"] = "straddle"
        market0[0]["option_type"] = "straddle"
        market1[0]["option_type"] = "straddle"
    elif name == "fatal_invalid_strike":
        metadata[0]["strike_usd"] = 0.0
        market0[0]["strike_usd"] = 0.0
        market1[0]["strike_usd"] = 0.0
    elif name == "fatal_invalid_timestamp":
        trades[0]["timestamp"] = t0
        trades.sort(key=lambda row: (row["timestamp"], row["trade_id"]))
    return {"config": config, "metadata": metadata, "rates": rates, "market0": market0, "market1": market1,
            "initial": initial, "trades": trades, "scenarios": scenarios}


def _write_variant(path: Path, payload: dict[str, Any], *, optional_dtype_mismatch: bool = False,
                   skip_file: str | None = None, market1_missing_column: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=False)
    _write_json(path / "config.json", payload["config"])
    _write_csv(path / "initial_positions.csv", ["instrument_name", "quantity"], payload["initial"])
    _write_csv(path / "trades.csv", ["trade_id", "timestamp", "instrument_name", "side", "quantity", "execution_price_usd", "fee_usd"], payload["trades"])
    _write_csv(path / "instrument_metadata.csv", ["instrument_name", "underlying", "option_type", "strike_usd", "expiration_timestamp", "contract_multiplier", "price_index", "maturity_bucket"], payload["metadata"])
    if skip_file != "rates.csv":
        _write_csv(path / "rates.csv", ["instrument_name", "rate_decimal", "benchmark_t0_utc", "benchmark_t1_utc"], payload["rates"])
    pq.write_table(pa.Table.from_pylist(payload["market0"], schema=_market_schema(True, optional_dtype_mismatch)), path / "market_t0.parquet", compression="zstd")
    market1_schema = _market_schema(False)
    market1_rows = payload["market1"]
    if market1_missing_column is not None:
        market1_schema = pa.schema([field for field in market1_schema if field.name != market1_missing_column])
        market1_rows = [{key: value for key, value in row.items() if key != market1_missing_column} for row in market1_rows]
    pq.write_table(pa.Table.from_pylist(market1_rows, schema=market1_schema), path / "market_t1.parquet", compression="zstd")
    _write_json(path / "scenarios.json", {"scenarios": payload["scenarios"]})


def _semantic_fingerprint(payload: dict[str, Any]) -> str:
    normalized = json.dumps(payload, sort_keys=True, allow_nan=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode()).hexdigest()


def generate_hidden_suite(root: Path, seed: int = 20260809) -> dict[str, Any]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    definitions = (
        ("cross_asset_nonlinear", {"call_put", "btc_eth", "itm_atm_otm", "subday_exact_time", "exact_expiry", "zero_negative_rates", "all_greeks", "nonlinear_attribution", "full_repricing_stress", "actual_pnl"}),
        ("portfolio_reversal", {"position_reconstruction", "actual_pnl", "all_greeks", "nonlinear_attribution", "full_repricing_stress"}),
        ("validation_edges", {"recoverable_validation", "position_reconstruction"}),
        ("fatal_invalid_scenario", {"fatal_validation", "full_repricing_stress"}),
        ("fatal_invalid_core", {"fatal_validation", "invalid_valuation_inputs"}),
        ("fatal_optional_dtype", {"fatal_validation", "optional_dtype_boundary"}),
        ("fatal_duplicate", {"fatal_validation", "duplicate_policy"}),
        ("fatal_missing_file", {"fatal_validation", "missing_required_file"}),
        ("fatal_extra_field", {"fatal_validation", "undeclared_field"}),
        ("fatal_missing_column", {"fatal_validation", "missing_required_column"}),
        ("fatal_missing_core_state", {"fatal_validation", "missing_core_state"}),
        ("fatal_missing_instrument", {"fatal_validation", "missing_instrument_reference"}),
        ("fatal_core_conflict", {"fatal_validation", "core_state_conflict"}),
        ("fatal_invalid_iv", {"fatal_validation", "invalid_iv"}),
        ("fatal_invalid_option_type", {"fatal_validation", "invalid_option_type"}),
        ("fatal_invalid_strike", {"fatal_validation", "invalid_strike"}),
        ("fatal_invalid_timestamp", {"fatal_validation", "invalid_timestamp"}),
    )
    if tuple(name for name, _ in definitions) != REQUIRED_VARIANTS:
        raise RuntimeError("hidden suite definitions must match REQUIRED_VARIANTS")
    variants = []
    for offset, (name, coverage) in enumerate(definitions):
        payload = _variant_payload(name, seed + offset)
        _write_variant(root / name, payload, optional_dtype_mismatch=name == "fatal_optional_dtype",
                       skip_file="rates.csv" if name == "fatal_missing_file" else None,
                       market1_missing_column="strike_usd" if name == "fatal_missing_column" else None)
        variants.append({"name": name, "seed": seed + offset, "coverage": sorted(coverage),
                         "semantic_fingerprint": _semantic_fingerprint(payload),
                         "file_fingerprint": suite_fingerprint(root / name)})
    manifest = {"schema_version": 1, "base_seed": seed, "variants": variants}
    _write_json(root / "manifest.json", manifest)
    return manifest


def suite_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file():
            relative = path.relative_to(root).as_posix().encode()
            payload = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big")); digest.update(relative)
            digest.update(len(payload).to_bytes(8, "big")); digest.update(payload)
    return digest.hexdigest()
