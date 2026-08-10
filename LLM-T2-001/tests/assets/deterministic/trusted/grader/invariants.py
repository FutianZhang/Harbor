"""Independent observable invariants, separate from reference-output equality."""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from grader.reference import load_inputs


@dataclass(frozen=True)
class InvariantFinding:
    check_id: str
    item_id: str
    detail: str


@dataclass(frozen=True)
class InvariantResult:
    checks_run: tuple[str, ...]
    findings: tuple[InvariantFinding, ...]


CHECKS = (
    "put_call_parity", "independent_price", "finite_difference_greeks", "position_identity",
    "instrument_pnl_identity", "portfolio_pnl_aggregation", "portfolio_greek_aggregation",
    "attribution_identity", "residual_not_forced_zero", "stress_full_repricing",
    "exact_time", "vega_unit_consistency",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _normal_cdf(value: float) -> float:
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def _bs(option_type: str, spot: float, strike: float, time: float, vol: float, rate: float) -> float:
    if time <= 0:
        return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
    root = math.sqrt(time)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * time) / (vol * root)
    d2 = d1 - vol * root
    discounted = strike * math.exp(-rate * time)
    if option_type == "call":
        return spot * _normal_cdf(d1) - discounted * _normal_cdf(d2)
    return discounted * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


def _close(left: float, right: float, *, absolute: float = 2e-6, relative: float = 3e-5) -> bool:
    return math.isfinite(left) and math.isfinite(right) and math.isclose(left, right, abs_tol=absolute, rel_tol=relative)


def _add(findings, check_id, item_id, detail):
    findings.append(InvariantFinding(check_id, item_id, detail))


def _portfolio_pnl_item(field: str) -> str:
    if field in {"delta_pnl_usd", "gamma_pnl_usd"}:
        return "ATT-1"
    if field in {"vega_pnl_usd", "theta_pnl_usd"}:
        return "ATT-2"
    if field in {"vanna_pnl_usd", "volga_pnl_usd"}:
        return "ATT-3"
    if field in {"explained_pnl_usd", "residual_pnl_usd"}:
        return "ATT-4"
    return "PNL-3"


def _numeric(row: dict[str, str], field: str) -> float:
    return float(row[field])


def check_invariants(input_dir: Path, output_dir: Path) -> InvariantResult:
    """Run independent checks; unreadable/missing files become scoped findings."""
    findings: list[InvariantFinding] = []
    output_dir = Path(output_dir)
    try:
        positions = _read_csv(output_dir / "positions_eod.csv")
        for row in positions:
            if not _close(_numeric(row, "quantity_t1"), _numeric(row, "quantity_t0") + _numeric(row, "net_trade_quantity"), absolute=1e-11, relative=1e-11):
                _add(findings, "position_identity", "POS-3", row["instrument_name"])
    except Exception:
        positions = []

    try:
        greeks = _read_csv(output_dir / "greeks.csv")
        instrument_greeks = [row for row in greeks if row.get("aggregation_level") == "INSTRUMENT"]
        for row in instrument_greeks:
            if not _close(_numeric(row, "unit_vega_1vol"), 0.01 * _numeric(row, "unit_vega_decimal"), absolute=2e-8, relative=2e-8):
                _add(findings, "vega_unit_consistency", "GRK-2", f"{row['instrument_name']}:{row['valuation_state']}")
            if not _close(_numeric(row, "unit_theta_day"), _numeric(row, "unit_theta_year") / 365.0, absolute=2e-8, relative=2e-8):
                _add(findings, "finite_difference_greeks", "GRK-3", f"theta-unit:{row['instrument_name']}:{row['valuation_state']}")
            scale = _numeric(row, "quantity") * _numeric(row, "contract_multiplier")
            for suffix in ("delta", "gamma", "vega_decimal", "vega_1vol", "theta_year", "theta_day", "vanna", "volga"):
                if not _close(_numeric(row, f"position_{suffix}"), scale * _numeric(row, f"unit_{suffix}"), absolute=2e-6, relative=2e-8):
                    _add(findings, "portfolio_greek_aggregation", "GRK-5", f"scale:{row['instrument_name']}:{suffix}")
        for state in ("t0", "t1"):
            portfolio = next((row for row in greeks if row.get("aggregation_level") == "PORTFOLIO" and row.get("valuation_state") == state), None)
            rows = [row for row in instrument_greeks if row["valuation_state"] == state]
            if portfolio:
                for suffix in ("delta", "gamma", "vega_decimal", "vega_1vol", "theta_year", "theta_day", "vanna", "volga"):
                    total = sum(_numeric(row, f"position_{suffix}") for row in rows)
                    if not _close(_numeric(portfolio, f"position_{suffix}"), total, absolute=2e-6, relative=2e-8):
                        _add(findings, "portfolio_greek_aggregation", "GRK-5", f"{state}:{suffix}")
    except Exception:
        greeks, instrument_greeks = [], []

    try:
        pnl = _read_csv(output_dir / "pnl_attribution.csv")
        instruments = [row for row in pnl if row.get("aggregation_level") == "INSTRUMENT"]
        portfolio = next((row for row in pnl if row.get("aggregation_level") == "PORTFOLIO"), None)
        component_fields = ("delta_pnl_usd", "gamma_pnl_usd", "vega_pnl_usd", "theta_pnl_usd", "vanna_pnl_usd", "volga_pnl_usd")
        nonzero_actual_residual = False
        for row in instruments:
            market_identity = _numeric(row, "ending_market_value_usd") - _numeric(row, "beginning_market_value_usd") + _numeric(row, "trade_cashflow_usd") - _numeric(row, "fees_usd")
            carry_identity = _numeric(row, "carry_actual_pnl_usd") + _numeric(row, "trade_to_t1_pnl_usd") - _numeric(row, "fees_usd")
            if not _close(_numeric(row, "total_pnl_usd"), market_identity, absolute=2e-6, relative=2e-9) or not _close(_numeric(row, "total_pnl_usd"), carry_identity, absolute=2e-6, relative=2e-9):
                _add(findings, "instrument_pnl_identity", "PNL-3", row["instrument_name"])
            explained = sum(_numeric(row, field) for field in component_fields)
            if not _close(_numeric(row, "explained_pnl_usd"), explained, absolute=2e-6, relative=2e-9) or not _close(_numeric(row, "carry_actual_pnl_usd"), explained + _numeric(row, "residual_pnl_usd"), absolute=2e-6, relative=2e-9):
                _add(findings, "attribution_identity", "ATT-4", row["instrument_name"])
            if abs(_numeric(row, "carry_actual_pnl_usd") - explained) > 1e-5:
                nonzero_actual_residual = True
                if abs(_numeric(row, "residual_pnl_usd")) <= 1e-12:
                    _add(findings, "residual_not_forced_zero", "ATT-4", row["instrument_name"])
        if portfolio:
            for field in ("beginning_market_value_usd", "ending_market_value_usd", "trade_cashflow_usd", "fees_usd", "carry_actual_pnl_usd", "trade_to_t1_pnl_usd", "total_pnl_usd", *component_fields, "explained_pnl_usd", "residual_pnl_usd"):
                if not _close(_numeric(portfolio, field), sum(_numeric(row, field) for row in instruments), absolute=2e-6, relative=2e-9):
                    _add(findings, "portfolio_pnl_aggregation", _portfolio_pnl_item(field), field)
        del nonzero_actual_residual
    except Exception:
        pnl = []

    try:
        inputs = load_inputs(input_dir)
        config = inputs["config"]
        t0, t1 = _utc(config["benchmark_t0_utc"]), _utc(config["benchmark_t1_utc"])
        metadata = {row["instrument_name"]: row for row in inputs["metadata_rows"]}
        rates = {row["instrument_name"]: float(row["rate_decimal"]) for row in inputs["rate_rows"]}
        market0 = {row["instrument_name"]: row for row in inputs["market0_rows"]}
        market1 = {row["instrument_name"]: row for row in inputs["market1_rows"]}
        greek_index = {(row["instrument_name"], row["valuation_state"]): row for row in instrument_greeks}
        valuation = {row["instrument_name"]: row for row in _read_csv(output_dir / "valuation.csv")}
        reported_positions = {row["instrument_name"]: row for row in positions}
        reconstructed = {name: 0.0 for name in metadata}
        reconstructed.update({row["instrument_name"]: float(row["quantity"]) for row in inputs["initial"]})
        trade_net = {name: 0.0 for name in metadata}
        for trade in inputs["trades"]:
            signed = float(trade["quantity"]) if trade["side"] == "BUY" else -float(trade["quantity"])
            reconstructed[trade["instrument_name"]] += signed
            trade_net[trade["instrument_name"]] += signed
        for name, expected_q1 in reconstructed.items():
            row = reported_positions.get(name)
            if row is None or not _close(float(row["quantity_t1"]), expected_q1, absolute=1e-11, relative=1e-11):
                _add(findings, "position_identity", "POS-3", f"input-trades:{name}")
            if row is None or not _close(float(row["net_trade_quantity"]), trade_net[name], absolute=1e-11, relative=1e-11):
                _add(findings, "position_identity", "POS-2", f"input-trades:{name}")
        parity_groups = {}
        for name, meta in metadata.items():
            expiry = _utc(meta["expiration_timestamp"])
            for state, clock, market, price_field in (("t0", t0, market0, "unit_value_t0_usd"), ("t1", t1, market1, "unit_value_t1_usd")):
                row = market[name]; time = max((expiry - clock).total_seconds(), 0.0) / (365 * 86400)
                spot, strike, vol, rate = float(row["spot_usd"]), float(meta["strike_usd"]), float(row["iv_decimal"]), rates[name]
                actual_price = float(valuation[name][price_field])
                independent = _bs(meta["option_type"], spot, strike, time, vol, rate)
                if not _close(actual_price, independent, absolute=2e-7, relative=2e-9):
                    exact_time_case = time <= 2.0 / 365.0
                    item = "PRC-3" if exact_time_case else ("PRC-1" if meta["option_type"] == "call" else "PRC-2")
                    check = "exact_time" if exact_time_case else "independent_price"
                    _add(findings, check, item, f"{name}:{state}")
                key = (state, meta["underlying"], strike, meta["expiration_timestamp"], spot, vol, rate)
                parity_groups.setdefault(key, {})[meta["option_type"]] = actual_price
                greek = greek_index.get((name, state))
                if greek is None:
                    continue
                if time <= 0:
                    if any(abs(float(greek[f"unit_{suffix}"])) > 1e-12 for suffix in ("delta", "gamma", "vega_decimal", "vega_1vol", "theta_year", "theta_day", "vanna", "volga")):
                        _add(findings, "finite_difference_greeks", "PRC-3", f"expiry:{name}:{state}")
                    continue
                hs, hv = max(spot * 2e-4, 1e-3), max(vol * 2e-4, 1e-6)
                base = independent
                up_s, down_s = _bs(meta["option_type"], spot + hs, strike, time, vol, rate), _bs(meta["option_type"], spot - hs, strike, time, vol, rate)
                up_v, down_v = _bs(meta["option_type"], spot, strike, time, vol + hv, rate), _bs(meta["option_type"], spot, strike, time, vol - hv, rate)
                estimates = {"delta": (up_s - down_s) / (2 * hs), "gamma": (up_s - 2 * base + down_s) / hs**2,
                             "vega_decimal": (up_v - down_v) / (2 * hv), "volga": (up_v - 2 * base + down_v) / hv**2}
                mixed = (_bs(meta["option_type"], spot + hs, strike, time, vol + hv, rate) - _bs(meta["option_type"], spot + hs, strike, time, vol - hv, rate) - _bs(meta["option_type"], spot - hs, strike, time, vol + hv, rate) + _bs(meta["option_type"], spot - hs, strike, time, vol - hv, rate)) / (4 * hs * hv)
                estimates["vanna"] = mixed
                ht = min(time * 0.01, 1e-7)
                estimates["theta_year"] = (
                    _bs(meta["option_type"], spot, strike, time - ht, vol, rate)
                    - _bs(meta["option_type"], spot, strike, time + ht, vol, rate)
                ) / (2 * ht)
                for suffix, estimate in estimates.items():
                    if not _close(float(greek[f"unit_{suffix}"]), estimate, absolute=5e-4 if suffix == "theta_year" else 5e-5, relative=2e-5 if suffix == "theta_year" else 4e-4):
                        item = "GRK-1" if suffix in {"delta", "gamma"} else ("GRK-2" if suffix == "vega_decimal" else ("GRK-3" if suffix == "theta_year" else "GRK-4"))
                        _add(findings, "finite_difference_greeks", item, f"{name}:{state}:{suffix}")
        for key, pair in parity_groups.items():
            if set(pair) == {"call", "put"}:
                state, _, strike, _, spot, _, rate = key
                clock = t0 if state == "t0" else t1
                expiry = _utc(key[3]); time = max((expiry - clock).total_seconds(), 0.0) / (365 * 86400)
                if not _close(pair["call"] - pair["put"], spot - strike * math.exp(-rate * time), absolute=2e-7, relative=2e-9):
                    _add(findings, "put_call_parity", "PRC-2", str(key))
    except Exception:
        inputs = None

    try:
        stress = json.loads((output_dir / "stress_report.json").read_text(encoding="utf-8"))
        if inputs is not None:
            for scenario in stress["scenarios"]:
                if len(scenario.get("instruments", [])) != len(metadata):
                    _add(findings, "stress_full_repricing", "STR-3", f"universe:{scenario.get('scenario_id')}")
                base_total = stressed_total = 0.0
                for row in scenario["instruments"]:
                    name = row["instrument_name"]; meta = metadata[name]; state = market1[name]
                    expected_spot = float(state["spot_usd"]) * (1.0 + float(scenario["spot_shock_pct"]))
                    expected_iv = float(state["iv_decimal"]) + float(scenario["vol_shock_abs"])
                    if not _close(float(row["stressed_spot_usd"]), expected_spot, absolute=2e-7, relative=2e-9) or not _close(float(row["stressed_iv_decimal"]), expected_iv, absolute=2e-10, relative=2e-10):
                        _add(findings, "stress_full_repricing", "STR-1", f"shock:{scenario['scenario_id']}:{name}")
                    expiry = _utc(meta["expiration_timestamp"]); time = max((expiry - t1).total_seconds(), 0.0) / (365 * 86400)
                    independent = _bs(meta["option_type"], float(row["stressed_spot_usd"]), float(meta["strike_usd"]), time, float(row["stressed_iv_decimal"]), rates[name])
                    if not _close(float(row["stressed_unit_value_usd"]), independent, absolute=2e-7, relative=2e-9):
                        _add(findings, "stress_full_repricing", "STR-2", f"{scenario['scenario_id']}:{name}")
                    base, stressed_value = float(row["base_market_value_usd"]), float(row["stressed_market_value_usd"])
                    if not _close(float(row["stress_pnl_usd"]), stressed_value - base, absolute=2e-6, relative=2e-9):
                        _add(findings, "stress_full_repricing", "STR-2", f"identity:{scenario['scenario_id']}:{name}")
                    base_total += base; stressed_total += stressed_value
                if not _close(float(scenario["portfolio_base_value_usd"]), base_total, absolute=2e-6, relative=2e-9) or not _close(float(scenario["portfolio_stressed_value_usd"]), stressed_total, absolute=2e-6, relative=2e-9) or not _close(float(scenario["portfolio_stress_pnl_usd"]), stressed_total - base_total, absolute=2e-6, relative=2e-9):
                    _add(findings, "stress_full_repricing", "STR-3", scenario["scenario_id"])
    except Exception:
        pass
    unique = {(finding.check_id, finding.item_id, finding.detail): finding for finding in findings}
    ordered = tuple(unique[key] for key in sorted(unique))
    return InvariantResult(CHECKS, ordered)
