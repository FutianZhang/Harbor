"""
Input loading and validation module.

Responsibilities:
- Load all input files deterministically.
- Validate schema, referential integrity, and core-state consistency.
- Generate validation_report records (warnings + errors).
- Abort on non-recoverable errors with non-zero exit, writing only validation_report.json.

Validation categories:
- Recoverable warnings: ROW_ORDER_NORMALIZED, EXACT_DUPLICATE_DROPPED, OPTIONAL_QUOTE_*,
  schema normalization / optional-observation warnings.
- Non-recoverable errors: SCHEMA_ERROR, referential integrity, authoritative core-state
  consistency, scenario/finite-output constraints.
"""

from __future__ import annotations

import csv
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

# Optional import for pyarrow optional column physical-type checks
try:
    import pyarrow as pa
except Exception:
    pa = None

# ===== Validation record types =====

@dataclass
class ValidationRecord:
    code: str
    file: str | None
    row_key: str | None
    field: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "file": self.file,
            "row_key": self.row_key,
            "field": self.field,
            "message": self.message,
        }


@dataclass
class ValidationResult:
    warnings: list[ValidationRecord] = field(default_factory=list)
    errors: list[ValidationRecord] = field(default_factory=list)

    def add_warning(self, code: str, file: str | None, row_key: str | None, field: str | None, message: str):
        self.warnings.append(ValidationRecord(code, file, row_key, field, message))

    def add_error(self, code: str, file: str | None, row_key: str | None, field: str | None, message: str):
        self.errors.append(ValidationRecord(code, file, row_key, field, message))

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def status(self) -> str:
        if self.has_errors:
            return "ERROR"
        if self.warnings:
            return "PASS_WITH_WARNINGS"
        return "PASS"

    def sorted_warnings(self) -> list[dict[str, Any]]:
        def key(r: ValidationRecord):
            return (r.code or "", r.file or "", r.row_key or "", r.field or "")
        return [r.to_dict() for r in sorted(self.warnings, key=key)]

    def sorted_errors(self) -> list[dict[str, Any]]:
        def key(r: ValidationRecord):
            return (r.code or "", r.file or "", r.row_key or "", r.field or "", r.message or "")
        return [r.to_dict() for r in sorted(self.errors, key=key)]


# ===== Helpers =====

def _is_finite(x: Any) -> bool:
    if x is None:
        return False
    if isinstance(x, (int, float)):
        try:
            return math.isfinite(float(x))
        except (TypeError, ValueError):
            return False
    return False


def _read_csv(path: str) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = [dict(r) for r in reader]
    return rows


# ===== Main loader =====

@dataclass
class InputData:
    config: dict[str, Any]
    initial_positions: dict[str, float]            # instrument_name -> q0
    metadata: dict[str, dict[str, Any]]             # instrument_name -> metadata row
    rates: dict[str, float]                          # instrument_name -> rate_decimal
    trades: list[dict[str, Any]]
    market_t0: dict[str, dict[str, Any]]            # instrument_name -> normalized t0 row
    market_t1: dict[str, dict[str, Any]]            # instrument_name -> t1 row
    scenarios: list[dict[str, Any]]
    t0_utc: str
    t1_utc: str


def load_and_validate(input_dir: str) -> tuple[InputData | None, ValidationResult]:
    vr = ValidationResult()
    input_dir = os.path.abspath(input_dir)

    # ---- config.json ----
    config_path = os.path.join(input_dir, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        vr.add_error("FILE_MISSING", "config.json", None, None, "Required input file config.json is missing")
        return None, vr
    except (json.JSONDecodeError, OSError) as e:
        vr.add_error("SCHEMA_ERROR", "config.json", None, None, f"config.json is unreadable: {e}")
        return None, vr

    config_required = ["benchmark_id", "benchmark_t0_utc", "benchmark_t1_utc", "dataset_version",
                       "generation_spec_sha256", "random_seed", "reporting_currency",
                       "source_snapshot_sha256", "source_version", "year_fraction_convention"]
    for fld in config_required:
        if fld not in config:
            vr.add_error("SCHEMA_ERROR", "config.json", None, fld, f"Required field {fld} missing")
    # additional fields
    allowed = set(config_required)
    for k in config.keys():
        if k not in allowed:
            vr.add_error("SCHEMA_ERROR", "config.json", None, k, f"Additional field {k} not allowed")
    if config.get("reporting_currency") != "USD":
        vr.add_error("SCHEMA_ERROR", "config.json", None, "reporting_currency", "reporting_currency must be USD")
    if config.get("year_fraction_convention") != "ACT/365":
        vr.add_error("SCHEMA_ERROR", "config.json", None, "year_fraction_convention", "year_fraction_convention must be ACT/365")

    if vr.has_errors:
        return None, vr

    t0_utc = config["benchmark_t0_utc"]
    t1_utc = config["benchmark_t1_utc"]

    # ---- instrument_metadata.csv ----
    meta_path = os.path.join(input_dir, "instrument_metadata.csv")
    if not os.path.exists(meta_path):
        vr.add_error("FILE_MISSING", "instrument_metadata.csv", None, None, "Required file missing")
        return None, vr
    meta_rows = _read_csv(meta_path)
    metadata: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(meta_rows):
        name = row.get("instrument_name")
        if not name:
            vr.add_error("SCHEMA_ERROR", "instrument_metadata.csv", f"row:{i}", "instrument_name", "instrument_name missing")
            continue
        if name in metadata:
            vr.add_error("DUPLICATE_KEY", "instrument_metadata.csv", name, "instrument_name", f"Duplicate instrument_name: {name}")
            return None, vr
        try:
            meta = {
                "instrument_name": name,
                "underlying": row["underlying"],
                "option_type": row["option_type"],
                "strike_usd": float(row["strike_usd"]),
                "expiration_timestamp": row["expiration_timestamp"],
                "contract_multiplier": float(row["contract_multiplier"]),
                "price_index": row.get("price_index"),
                "maturity_bucket": row.get("maturity_bucket"),
            }
        except (KeyError, ValueError) as e:
            vr.add_error("SCHEMA_ERROR", "instrument_metadata.csv", name, None, f"Invalid metadata: {e}")
            return None, vr
        if meta["underlying"] not in ("BTC", "ETH"):
            vr.add_error("SCHEMA_ERROR", "instrument_metadata.csv", name, "underlying", f"underlying must be BTC|ETH: {meta['underlying']}")
        if meta["option_type"] not in ("call", "put"):
            vr.add_error("SCHEMA_ERROR", "instrument_metadata.csv", name, "option_type", f"option_type must be call|put: {meta['option_type']}")
        if not _is_finite(meta["strike_usd"]) or meta["strike_usd"] <= 0:
            vr.add_error("SCHEMA_ERROR", "instrument_metadata.csv", name, "strike_usd", f"strike_usd must be finite > 0: {meta['strike_usd']}")
        if not _is_finite(meta["contract_multiplier"]) or meta["contract_multiplier"] <= 0:
            vr.add_error("SCHEMA_ERROR", "instrument_metadata.csv", name, "contract_multiplier", f"contract_multiplier must be finite > 0: {meta['contract_multiplier']}")
        metadata[name] = meta

    # Check sort order of metadata
    sorted_names = sorted(metadata.keys())
    if list(metadata.keys()) != sorted_names:
        vr.add_warning("ROW_ORDER_NORMALIZED", "instrument_metadata.csv", None, None,
                       "instrument_metadata.csv rows normalized to canonical sort order (instrument_name ascending)")

    if vr.has_errors:
        return None, vr

    # ---- rates.csv ----
    rates_path = os.path.join(input_dir, "rates.csv")
    if not os.path.exists(rates_path):
        vr.add_error("FILE_MISSING", "rates.csv", None, None, "Required file missing")
        return None, vr
    rate_rows = _read_csv(rates_path)
    rates: dict[str, float] = {}
    rates_order = []
    for i, row in enumerate(rate_rows):
        name = row.get("instrument_name")
        if not name:
            vr.add_error("SCHEMA_ERROR", "rates.csv", f"row:{i}", "instrument_name", "instrument_name missing")
            continue
        if name in rates:
            vr.add_error("DUPLICATE_KEY", "rates.csv", name, "instrument_name", f"Duplicate instrument_name: {name}")
            return None, vr
        try:
            rate = float(row["rate_decimal"])
        except (KeyError, ValueError) as e:
            vr.add_error("SCHEMA_ERROR", "rates.csv", name, "rate_decimal", f"Invalid rate: {e}")
            return None, vr
        if not _is_finite(rate):
            vr.add_error("SCHEMA_ERROR", "rates.csv", name, "rate_decimal", f"rate_decimal must be finite: {rate}")
            return None, vr
        # Check timestamps equal config
        if row.get("benchmark_t0_utc") != t0_utc:
            vr.add_error("SCHEMA_ERROR", "rates.csv", name, "benchmark_t0_utc", "benchmark_t0_utc must equal config.json benchmark_t0_utc")
        if row.get("benchmark_t1_utc") != t1_utc:
            vr.add_error("SCHEMA_ERROR", "rates.csv", name, "benchmark_t1_utc", "benchmark_t1_utc must equal config.json benchmark_t1_utc")
        rates[name] = rate
        rates_order.append(name)

    # referential integrity: every metadata instrument must exist in rates
    for name in metadata:
        if name not in rates:
            vr.add_error("REFERENTIAL_INTEGRITY", "rates.csv", name, None, f"instrument {name} missing from rates.csv")

    if rates_order != sorted(rates_order):
        vr.add_warning("ROW_ORDER_NORMALIZED", "rates.csv", None, None,
                       "rates.csv rows normalized to canonical sort order (instrument_name ascending)")

    if vr.has_errors:
        return None, vr

    # ---- initial_positions.csv ----
    pos_path = os.path.join(input_dir, "initial_positions.csv")
    initial_positions: dict[str, float] = {}
    if os.path.exists(pos_path):
        pos_rows = _read_csv(pos_path)
        pos_order = []
        for i, row in enumerate(pos_rows):
            name = row.get("instrument_name")
            if not name:
                vr.add_error("SCHEMA_ERROR", "initial_positions.csv", f"row:{i}", "instrument_name", "instrument_name missing")
                continue
            if name in initial_positions:
                vr.add_error("DUPLICATE_KEY", "initial_positions.csv", name, "instrument_name", f"Duplicate instrument_name: {name}")
                return None, vr
            try:
                q = float(row["quantity"])
            except (KeyError, ValueError) as e:
                vr.add_error("SCHEMA_ERROR", "initial_positions.csv", name, "quantity", f"Invalid quantity: {e}")
                return None, vr
            if not _is_finite(q):
                vr.add_error("SCHEMA_ERROR", "initial_positions.csv", name, "quantity", f"quantity must be finite: {q}")
                return None, vr
            initial_positions[name] = q
            pos_order.append(name)
        # Referential integrity
        for name in initial_positions:
            if name not in metadata:
                vr.add_error("REFERENTIAL_INTEGRITY", "initial_positions.csv", name, None, f"instrument {name} not in metadata")
        if pos_order != sorted(pos_order):
            vr.add_warning("ROW_ORDER_NORMALIZED", "initial_positions.csv", None, None,
                           "initial_positions.csv rows normalized to canonical sort order (instrument_name ascending)")
    else:
        vr.add_error("FILE_MISSING", "initial_positions.csv", None, None, "Required file missing")
        return None, vr

    if vr.has_errors:
        return None, vr

    # ---- trades.csv ----
    trades_path = os.path.join(input_dir, "trades.csv")
    trades: list[dict[str, Any]] = []
    if os.path.exists(trades_path):
        trade_rows = _read_csv(trades_path)
        seen_ids = set()
        for i, row in enumerate(trade_rows):
            tid = row.get("trade_id")
            if not tid:
                vr.add_error("SCHEMA_ERROR", "trades.csv", f"row:{i}", "trade_id", "trade_id missing")
                continue
            if tid in seen_ids:
                vr.add_error("DUPLICATE_KEY", "trades.csv", tid, "trade_id", f"Duplicate trade_id: {tid}")
                return None, vr
            seen_ids.add(tid)
            instr = row.get("instrument_name")
            if instr not in metadata:
                vr.add_error("REFERENTIAL_INTEGRITY", "trades.csv", tid, "instrument_name", f"instrument {instr} not in metadata")
                return None, vr
            ts = row.get("timestamp")
            try:
                qty = float(row["quantity"])
                px = float(row["execution_price_usd"])
                fee = float(row["fee_usd"])
            except (KeyError, ValueError) as e:
                vr.add_error("SCHEMA_ERROR", "trades.csv", tid, None, f"Invalid numeric field: {e}")
                return None, vr
            side = row.get("side")
            if side not in ("BUY", "SELL"):
                vr.add_error("SCHEMA_ERROR", "trades.csv", tid, "side", f"side must be BUY|SELL: {side}")
                return None, vr
            if not _is_finite(qty) or qty <= 0:
                vr.add_error("SCHEMA_ERROR", "trades.csv", tid, "quantity", f"quantity must be finite > 0: {qty}")
                return None, vr
            if not _is_finite(px) or px < 0:
                vr.add_error("SCHEMA_ERROR", "trades.csv", tid, "execution_price_usd", f"execution_price_usd must be finite >= 0: {px}")
                return None, vr
            if not _is_finite(fee) or fee < 0:
                vr.add_error("SCHEMA_ERROR", "trades.csv", tid, "fee_usd", f"fee_usd must be finite >= 0: {fee}")
                return None, vr
            trades.append({
                "trade_id": tid,
                "timestamp": ts,
                "instrument_name": instr,
                "side": side,
                "quantity": qty,
                "execution_price_usd": px,
                "fee_usd": fee,
            })
        # Check sort order: timestamp UTC ascending, trade_id ascending
        expected_order = sorted(trades, key=lambda t: (t["timestamp"], t["trade_id"]))
        if [t["trade_id"] for t in trades] != [t["trade_id"] for t in expected_order]:
            vr.add_warning("ROW_ORDER_NORMALIZED", "trades.csv", None, None,
                           "trades.csv rows normalized to canonical sort order (timestamp UTC ascending, trade_id ascending)")
        trades = expected_order  # always process in canonical order
        # Verify trade timestamps strictly between t0 and t1
        for t in trades:
            if not (t0_utc < t["timestamp"] < t1_utc):
                vr.add_error("SCHEMA_ERROR", "trades.csv", t["trade_id"], "timestamp", f"timestamp must be strictly between t0 and t1")
    else:
        vr.add_error("FILE_MISSING", "trades.csv", None, None, "Required file missing")
        return None, vr

    if vr.has_errors:
        return None, vr

    # ---- market_t0.parquet ----
    t0_path = os.path.join(input_dir, "market_t0.parquet")
    t1_path = os.path.join(input_dir, "market_t1.parquet")
    market_t0: dict[str, dict[str, Any]] = {}
    market_t1: dict[str, dict[str, Any]] = {}

    if not os.path.exists(t0_path):
        vr.add_error("FILE_MISSING", "market_t0.parquet", None, None, "Required file missing")
        return None, vr
    if not os.path.exists(t1_path):
        vr.add_error("FILE_MISSING", "market_t1.parquet", None, None, "Required file missing")
        return None, vr

    # Load t0 with optional duplicate-dedup and optional-quote warning logic
    market_t0 = _load_market_t0(t0_path, metadata, rates, t0_utc, vr)
    if market_t0 is None:
        return None, vr

    market_t1 = _load_market_t1(t1_path, metadata, rates, t1_utc, vr)
    if market_t1 is None:
        return None, vr

    if vr.has_errors:
        return None, vr

    # ---- scenarios.json ----
    scen_path = os.path.join(input_dir, "scenarios.json")
    if not os.path.exists(scen_path):
        vr.add_error("FILE_MISSING", "scenarios.json", None, None, "Required file missing")
        return None, vr
    try:
        with open(scen_path, "r", encoding="utf-8") as f:
            scen_obj = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        vr.add_error("SCHEMA_ERROR", "scenarios.json", None, None, f"scenarios.json unreadable: {e}")
        return None, vr
    if "scenarios" not in scen_obj:
        vr.add_error("SCHEMA_ERROR", "scenarios.json", None, "scenarios", "Required field scenarios missing")
        return None, vr
    scenarios = scen_obj["scenarios"]
    scen_ids = set()
    for i, s in enumerate(scenarios):
        sid = s.get("scenario_id")
        if not sid:
            vr.add_error("SCHEMA_ERROR", "scenarios.json", f"row:{i}", "scenario_id", "scenario_id missing")
            return None, vr
        if sid in scen_ids:
            vr.add_error("DUPLICATE_KEY", "scenarios.json", sid, "scenario_id", f"Duplicate scenario_id: {sid}")
            return None, vr
        scen_ids.add(sid)
        try:
            sp = float(s["spot_shock_pct"])
            vs = float(s["vol_shock_abs"])
        except (KeyError, ValueError, TypeError) as e:
            vr.add_error("SCHEMA_ERROR", "scenarios.json", sid, None, f"Invalid scenario numeric: {e}")
            return None, vr
        if not _is_finite(sp):
            vr.add_error("SCHEMA_ERROR", "scenarios.json", sid, "spot_shock_pct", f"spot_shock_pct must be finite: {sp}")
            return None, vr
        if not _is_finite(vs):
            vr.add_error("SCHEMA_ERROR", "scenarios.json", sid, "vol_shock_abs", f"vol_shock_abs must be finite: {vs}")
            return None, vr
    # Sort scenarios by scenario_id
    scenarios_sorted = sorted(scenarios, key=lambda s: s["scenario_id"])
    if [s["scenario_id"] for s in scenarios] != [s["scenario_id"] for s in scenarios_sorted]:
        vr.add_warning("ROW_ORDER_NORMALIZED", "scenarios.json", None, None,
                       "scenarios.json rows normalized to canonical sort order (scenario_id ascending)")

    return InputData(
        config=config,
        initial_positions=initial_positions,
        metadata=metadata,
        rates=rates,
        trades=trades,
        market_t0=market_t0,
        market_t1=market_t1,
        scenarios=scenarios_sorted,
        t0_utc=t0_utc,
        t1_utc=t1_utc,
    ), vr


# ===== Parquet loaders =====

# Optional source columns in market_t0 (physical type "double")
T0_OPTIONAL_DOUBLE_FIELDS = [
    "source_bid_price_coin", "source_ask_price_coin",
    "source_mark_price_coin", "source_mark_iv_pct",
]
T0_OPTIONAL_INT_FIELDS = ["source_orderbook_timestamp_ms"]
T0_REQUIRED_FIELDS = [
    "instrument_name", "underlying", "option_type", "valuation_timestamp",
    "spot_usd", "strike_usd", "expiration_timestamp", "iv_decimal",
    "rate_decimal", "contract_multiplier",
]
T1_OPTIONAL_FIELDS = ["spot_shock_pct", "iv_change_decimal"]
T1_REQUIRED_FIELDS = [
    "instrument_name", "underlying", "option_type", "valuation_timestamp",
    "spot_usd", "strike_usd", "expiration_timestamp", "iv_decimal",
    "rate_decimal", "contract_multiplier",
]


def _check_parquet_schema(table, expected_columns: list[str], file_label: str, vr: ValidationResult,
                         optional_fields: list[str]) -> bool:
    """Verify required columns present; report SCHEMA_ERROR for missing required or undeclared columns."""
    present = set(table.column_names)
    required = set(expected_columns)
    missing = required - present
    for fld in sorted(missing):
        vr.add_error("SCHEMA_ERROR", file_label, None, fld, f"Required column {fld} missing")
    # Undeclared columns not in required + optional
    declared = required | set(optional_fields)
    undeclared = present - declared
    for fld in sorted(undeclared):
        vr.add_error("SCHEMA_ERROR", file_label, None, fld, f"Undeclared column {fld} not allowed")
    return len(missing) == 0 and len(undeclared) == 0


def _load_market_t0(path: str, metadata: dict, rates: dict, t0_utc: str, vr: ValidationResult) -> dict[str, dict[str, Any]] | None:
    """Load market_t0.parquet, dedup exact duplicates, validate optional quotes."""
    try:
        table = pq.read_table(path)
    except Exception as e:
        vr.add_error("SCHEMA_ERROR", "market_t0.parquet", None, None, f"Parquet container unreadable: {e}")
        return None

    if not _check_parquet_schema(table, T0_REQUIRED_FIELDS, "market_t0.parquet", vr, T0_OPTIONAL_DOUBLE_FIELDS + T0_OPTIONAL_INT_FIELDS):
        return None

    # Check physical types of optional numeric columns if present
    for fld in T0_OPTIONAL_DOUBLE_FIELDS:
        if fld in table.column_names:
            try:
                col_type = table.schema.field(fld).type
                if pa is not None and not (col_type.equals(pa.float64()) or col_type.equals(pa.float32())):
                    vr.add_error("SCHEMA_ERROR", "market_t0.parquet", None, fld, f"Optional column {fld} physical type mismatch: {col_type}")
                    return None
            except Exception:
                pass
    for fld in T0_OPTIONAL_INT_FIELDS:
        if fld in table.column_names:
            try:
                col_type = table.schema.field(fld).type
                if pa is not None and not (col_type.equals(pa.int64()) or col_type.equals(pa.int32())):
                    vr.add_error("SCHEMA_ERROR", "market_t0.parquet", None, fld, f"Optional column {fld} physical type mismatch: {col_type}")
                    return None
            except Exception:
                pass

    df = table.to_pandas()
    # Build row list
    rows: list[dict[str, Any]] = df.to_dict(orient="records")

    # Deduplicate: group by instrument_name; exact duplicates (across every present column) -> keep one, warn;
    # differing same-instrument duplicates -> non-recoverable error.
    by_instr: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        name = r.get("instrument_name")
        if name is None:
            vr.add_error("SCHEMA_ERROR", "market_t0.parquet", None, "instrument_name", "instrument_name missing")
            return None
        by_instr.setdefault(name, []).append(r)

    market: dict[str, dict[str, Any]] = {}
    for name, group in by_instr.items():
        if len(group) == 1:
            market[name] = group[0]
            continue
        # Multiple rows: check exact duplicates
        first = _canon_row(group[0])
        all_same = all(_canon_row(r) == first for r in group[1:])
        if all_same:
            market[name] = group[0]
            vr.add_warning("EXACT_DUPLICATE_DROPPED", "market_t0.parquet", name, None,
                           f"Exact duplicate rows for instrument {name} deduplicated")
        else:
            vr.add_error("DUPLICATE_KEY", "market_t0.parquet", name, None, f"Differing duplicate rows for instrument {name}")
            return None

    # Check every metadata instrument exists in market_t0
    for name in metadata:
        if name not in market:
            vr.add_error("REFERENTIAL_INTEGRITY", "market_t0.parquet", name, None, f"instrument {name} missing from market_t0")

    # Validate required fields and consistency for each instrument
    for name, row in market.items():
        if name not in metadata:
            vr.add_error("REFERENTIAL_INTEGRITY", "market_t0.parquet", name, None, f"instrument {name} not in metadata")
            continue
        meta = metadata[name]
        # under/option/strike/expiry/mult must match metadata
        if str(row.get("underlying")) != str(meta["underlying"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t0.parquet", name, "underlying", f"underlying mismatch: {row.get('underlying')} vs {meta['underlying']}")
        if str(row.get("option_type")) != str(meta["option_type"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t0.parquet", name, "option_type", f"option_type mismatch: {row.get('option_type')} vs {meta['option_type']}")
        if _is_finite(row.get("strike_usd")) and float(row["strike_usd"]) != float(meta["strike_usd"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t0.parquet", name, "strike_usd", f"strike_usd mismatch: {row.get('strike_usd')} vs {meta['strike_usd']}")
        if str(row.get("expiration_timestamp")) != str(meta["expiration_timestamp"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t0.parquet", name, "expiration_timestamp", f"expiration_timestamp mismatch: {row.get('expiration_timestamp')} vs {meta['expiration_timestamp']}")
        if _is_finite(row.get("contract_multiplier")) and float(row["contract_multiplier"]) != float(meta["contract_multiplier"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t0.parquet", name, "contract_multiplier", f"contract_multiplier mismatch")
        # rate must equal rates.csv
        if name in rates and _is_finite(row.get("rate_decimal")):
            if float(row["rate_decimal"]) != float(rates[name]):
                vr.add_error("CORE_STATE_CONSISTENCY", "market_t0.parquet", name, "rate_decimal", f"rate_decimal mismatch: {row.get('rate_decimal')} vs {rates[name]}")
        # valuation_timestamp must equal config t0
        if str(row.get("valuation_timestamp")) != t0_utc:
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t0.parquet", name, "valuation_timestamp", f"valuation_timestamp must equal config t0: {row.get('valuation_timestamp')} vs {t0_utc}")
        # Required numeric checks
        spot = row.get("spot_usd")
        if not _is_finite(spot) or float(spot) <= 0:
            vr.add_error("SCHEMA_ERROR", "market_t0.parquet", name, "spot_usd", f"spot_usd must be finite > 0: {spot}")
        iv = row.get("iv_decimal")
        if not _is_finite(iv):
            vr.add_error("SCHEMA_ERROR", "market_t0.parquet", name, "iv_decimal", f"iv_decimal must be finite: {iv}")
        else:
            # iv_decimal must be > 0 before expiry; 0 or negative after expiry allowed? spec says "> 0 before expiry"
            # We'll flag <=0 as error if expiry is strictly after t0
            from .bs import time_to_expiry
            T = time_to_expiry(t0_utc, str(meta["expiration_timestamp"]))
            if T > 0 and float(iv) <= 0:
                vr.add_error("SCHEMA_ERROR", "market_t0.parquet", name, "iv_decimal", f"iv_decimal must be > 0 before expiry: {iv}")

        # Optional quote fields
        for fld in T0_OPTIONAL_DOUBLE_FIELDS:
            val = row.get(fld)
            if val is None:
                vr.add_warning("OPTIONAL_QUOTE_MISSING", "market_t0.parquet", name, fld,
                               f"Optional quote field {fld} missing for instrument {name}")
            else:
                # If non-finite (NaN, inf) -> OPTIONAL_QUOTE_IGNORED
                if not _is_finite(val):
                    vr.add_warning("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, fld,
                                   f"Optional quote field {fld} has non-finite value {val}; ignored")
                else:
                    # Semantic invalid checks
                    fval = float(val)
                    if fld == "source_bid_price_coin":
                        if fval < 0:
                            vr.add_warning("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, fld,
                                           f"source_bid_price_coin < 0: {fval}; ignored")
                        elif fval == 0:
                            vr.add_warning("OPTIONAL_QUOTE_ZERO_BID", "market_t0.parquet", name, fld,
                                           f"source_bid_price_coin == 0 for instrument {name}")
                    elif fld == "source_ask_price_coin":
                        if fval <= 0:
                            vr.add_warning("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, fld,
                                           f"source_ask_price_coin <= 0: {fval}; ignored")
                    elif fld == "source_mark_price_coin":
                        if fval <= 0:
                            vr.add_warning("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, fld,
                                           f"source_mark_price_coin <= 0: {fval}; ignored")
                    elif fld == "source_mark_iv_pct":
                        if fval <= 0:
                            vr.add_warning("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, fld,
                                           f"source_mark_iv_pct <= 0: {fval}; ignored")
        # source_orderbook_timestamp_ms
        val = row.get("source_orderbook_timestamp_ms")
        if val is not None and _is_finite(val) and float(val) < 0:
            vr.add_warning("OPTIONAL_QUOTE_IGNORED", "market_t0.parquet", name, "source_orderbook_timestamp_ms",
                           f"source_orderbook_timestamp_ms < 0: {val}; ignored")
        # Crossed quotes
        bid = row.get("source_bid_price_coin")
        ask = row.get("source_ask_price_coin")
        if _is_finite(bid) and _is_finite(ask) and float(bid) > float(ask):
            vr.add_warning("OPTIONAL_QUOTE_CROSSED", "market_t0.parquet", name, None,
                           f"Crossed quote for instrument {name}: bid {bid} > ask {ask}")

    if vr.has_errors:
        return None

    # Check sort order
    sorted_keys = sorted(market.keys())
    if list(market.keys()) != sorted_keys:
        vr.add_warning("ROW_ORDER_NORMALIZED", "market_t0.parquet", None, None,
                       "market_t0.parquet rows normalized to canonical sort order (instrument_name ascending)")

    return market


def _canon_row(row: dict[str, Any]) -> tuple:
    """Canonical representation of a row for exact-duplicate comparison."""
    items = []
    for k in sorted(row.keys()):
        v = row[k]
        # Treat NaN as equal to NaN for dedup purposes (common in parquet)
        if v != v:  # NaN check
            v = "__NAN__"
        items.append((k, v))
    return tuple(items)


def _load_market_t1(path: str, metadata: dict, rates: dict, t1_utc: str, vr: ValidationResult) -> dict[str, dict[str, Any]] | None:
    """Load market_t1.parquet. No dedup; duplicate instrument_name is non-recoverable."""
    try:
        table = pq.read_table(path)
    except Exception as e:
        vr.add_error("SCHEMA_ERROR", "market_t1.parquet", None, None, f"Parquet container unreadable: {e}")
        return None

    if not _check_parquet_schema(table, T1_REQUIRED_FIELDS, "market_t1.parquet", vr, T1_OPTIONAL_FIELDS):
        return None

    df = table.to_pandas()
    rows: list[dict[str, Any]] = df.to_dict(orient="records")
    market: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = r.get("instrument_name")
        if name is None:
            vr.add_error("SCHEMA_ERROR", "market_t1.parquet", None, "instrument_name", "instrument_name missing")
            return None
        if name in market:
            vr.add_error("DUPLICATE_KEY", "market_t1.parquet", name, None, f"Duplicate instrument_name: {name}")
            return None
        market[name] = r

    for name in metadata:
        if name not in market:
            vr.add_error("REFERENTIAL_INTEGRITY", "market_t1.parquet", name, None, f"instrument {name} missing from market_t1")

    for name, row in market.items():
        if name not in metadata:
            vr.add_error("REFERENTIAL_INTEGRITY", "market_t1.parquet", name, None, f"instrument {name} not in metadata")
            continue
        meta = metadata[name]
        if str(row.get("underlying")) != str(meta["underlying"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t1.parquet", name, "underlying", f"underlying mismatch")
        if str(row.get("option_type")) != str(meta["option_type"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t1.parquet", name, "option_type", f"option_type mismatch")
        if _is_finite(row.get("strike_usd")) and float(row["strike_usd"]) != float(meta["strike_usd"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t1.parquet", name, "strike_usd", f"strike_usd mismatch")
        if str(row.get("expiration_timestamp")) != str(meta["expiration_timestamp"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t1.parquet", name, "expiration_timestamp", f"expiration_timestamp mismatch")
        if _is_finite(row.get("contract_multiplier")) and float(row["contract_multiplier"]) != float(meta["contract_multiplier"]):
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t1.parquet", name, "contract_multiplier", f"contract_multiplier mismatch")
        if name in rates and _is_finite(row.get("rate_decimal")):
            if float(row["rate_decimal"]) != float(rates[name]):
                vr.add_error("CORE_STATE_CONSISTENCY", "market_t1.parquet", name, "rate_decimal", f"rate_decimal mismatch")
        if str(row.get("valuation_timestamp")) != t1_utc:
            vr.add_error("CORE_STATE_CONSISTENCY", "market_t1.parquet", name, "valuation_timestamp", f"valuation_timestamp must equal config t1")
        spot = row.get("spot_usd")
        if not _is_finite(spot) or float(spot) <= 0:
            vr.add_error("SCHEMA_ERROR", "market_t1.parquet", name, "spot_usd", f"spot_usd must be finite > 0: {spot}")
        iv = row.get("iv_decimal")
        if not _is_finite(iv):
            vr.add_error("SCHEMA_ERROR", "market_t1.parquet", name, "iv_decimal", f"iv_decimal must be finite: {iv}")
        else:
            from .bs import time_to_expiry
            T = time_to_expiry(t1_utc, str(meta["expiration_timestamp"]))
            if T > 0 and float(iv) <= 0:
                vr.add_error("SCHEMA_ERROR", "market_t1.parquet", name, "iv_decimal", f"iv_decimal must be > 0 before expiry: {iv}")

    if vr.has_errors:
        return None

    sorted_keys = sorted(market.keys())
    if list(market.keys()) != sorted_keys:
        vr.add_warning("ROW_ORDER_NORMALIZED", "market_t1.parquet", None, None,
                       "market_t1.parquet rows normalized to canonical sort order (instrument_name ascending)")

    return market
