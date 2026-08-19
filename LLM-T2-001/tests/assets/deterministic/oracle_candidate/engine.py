"""
Engine: compute positions, valuation, greeks, pnl attribution, stress.

Implements the financial semantics defined in the README and output_schema:
- EOD position rebuild
- Per-instrument valuation (t0/t1)
- Greeks (unit + position scaled; instrument + portfolio aggregate)
- PnL attribution using t0 position Greeks
- Full-repricing stress using EOD position q1 and t1 base market
"""

from __future__ import annotations

import math
from typing import Any

from .bs import bs_greeks, bs_price, time_to_expiry


def compute_positions(data) -> dict[str, dict[str, float]]:
    """
    Return dict instrument_name -> {quantity_t0, net_trade_quantity, quantity_t1}.
    Instruments = union of metadata, initial_positions, trades.
    """
    result: dict[str, dict[str, float]] = {}
    # Start from metadata
    for name in sorted(data.metadata.keys()):
        q0 = float(data.initial_positions.get(name, 0.0))
        result[name] = {
            "quantity_t0": q0,
            "net_trade_quantity": 0.0,
            "quantity_t1": q0,
        }
    # Apply trades
    for t in data.trades:
        name = t["instrument_name"]
        if name not in result:
            # Shouldn't happen (validated), but include for safety
            result[name] = {"quantity_t0": 0.0, "net_trade_quantity": 0.0, "quantity_t1": 0.0}
        signed = float(t["quantity"]) if t["side"] == "BUY" else -float(t["quantity"])
        result[name]["net_trade_quantity"] += signed
        result[name]["quantity_t1"] += signed
    return result


def compute_valuation(data, positions: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """
    Return dict instrument_name -> valuation fields.
    """
    result: dict[str, dict[str, float]] = {}
    for name in sorted(data.metadata.keys()):
        meta = data.metadata[name]
        m = float(meta["contract_multiplier"])
        is_call = (meta["option_type"] == "call")
        K = float(meta["strike_usd"])
        expiry = str(meta["expiration_timestamp"])

        t0_row = data.market_t0[name]
        t1_row = data.market_t1[name]
        S0 = float(t0_row["spot_usd"])
        S1 = float(t1_row["spot_usd"])
        sigma0 = float(t0_row["iv_decimal"])
        sigma1 = float(t1_row["iv_decimal"])
        r = float(data.rates[name])
        q = 0.0

        T0 = time_to_expiry(data.t0_utc, expiry)
        T1 = time_to_expiry(data.t1_utc, expiry)

        v0 = bs_price(is_call, S0, K, sigma0, T0, r, q)
        v1 = bs_price(is_call, S1, K, sigma1, T1, r, q)

        q0 = positions[name]["quantity_t0"]
        q1 = positions[name]["quantity_t1"]

        result[name] = {
            "quantity_t0": q0,
            "quantity_t1": q1,
            "contract_multiplier": m,
            "unit_value_t0_usd": v0,
            "unit_value_t1_usd": v1,
            "market_value_t0_usd": q0 * m * v0,
            "market_value_t1_usd": q1 * m * v1,
            # Also store t0/t1 market state for greeks/pnl/stress use
            "S0": S0,
            "S1": S1,
            "sigma0": sigma0,
            "sigma1": sigma1,
            "r": r,
            "T0": T0,
            "T1": T1,
            "is_call": is_call,
            "K": K,
            "expiry": expiry,
        }
    return result


def compute_greeks(data, positions: dict[str, dict[str, float]], valuation: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """
    Build greeks rows: two INSTRUMENT rows per metadata instrument (t0, t1),
    plus two PORTFOLIO aggregate rows (t0, t1).
    """
    rows: list[dict[str, Any]] = []
    # Instrument rows sorted by instrument_name ascending; for each, t0 then t1
    for name in sorted(data.metadata.keys()):
        meta = data.metadata[name]
        m = float(meta["contract_multiplier"])
        is_call = (meta["option_type"] == "call")
        K = float(meta["strike_usd"])
        expiry = str(meta["expiration_timestamp"])

        t0_row = data.market_t0[name]
        t1_row = data.market_t1[name]
        S0 = float(t0_row["spot_usd"])
        S1 = float(t1_row["spot_usd"])
        sigma0 = float(t0_row["iv_decimal"])
        sigma1 = float(t1_row["iv_decimal"])
        r = float(data.rates[name])
        q = 0.0

        T0 = time_to_expiry(data.t0_utc, expiry)
        T1 = time_to_expiry(data.t1_utc, expiry)

        q0 = positions[name]["quantity_t0"]
        q1 = positions[name]["quantity_t1"]

        for state, S, sigma, T, qty, ts in [
            ("t0", S0, sigma0, T0, q0, data.t0_utc),
            ("t1", S1, sigma1, T1, q1, data.t1_utc),
        ]:
            g = bs_greeks(is_call, S, K, sigma, T, r, q)
            rows.append({
                "aggregation_level": "INSTRUMENT",
                "instrument_name": name,
                "valuation_state": state,
                "valuation_timestamp_utc": ts,
                "quantity": qty,
                "contract_multiplier": m,
                "unit_delta": g["delta"],
                "unit_gamma": g["gamma"],
                "unit_vega_decimal": g["vega_decimal"],
                "unit_vega_1vol": g["vega_1vol"],
                "unit_theta_year": g["theta_year"],
                "unit_theta_day": g["theta_day"],
                "unit_vanna": g["vanna"],
                "unit_volga": g["volga"],
                "position_delta": qty * m * g["delta"],
                "position_gamma": qty * m * g["gamma"],
                "position_vega_decimal": qty * m * g["vega_decimal"],
                "position_vega_1vol": qty * m * g["vega_1vol"],
                "position_theta_year": qty * m * g["theta_year"],
                "position_theta_day": qty * m * g["theta_day"],
                "position_vanna": qty * m * g["vanna"],
                "position_volga": qty * m * g["volga"],
            })

    # Portfolio aggregate rows
    for state, ts in [("t0", data.t0_utc), ("t1", data.t1_utc)]:
        agg = {
            "aggregation_level": "PORTFOLIO",
            "instrument_name": "__PORTFOLIO__",
            "valuation_state": state,
            "valuation_timestamp_utc": ts,
            "quantity": None,
            "contract_multiplier": None,
            "unit_delta": None,
            "unit_gamma": None,
            "unit_vega_decimal": None,
            "unit_vega_1vol": None,
            "unit_theta_year": None,
            "unit_theta_day": None,
            "unit_vanna": None,
            "unit_volga": None,
            "position_delta": 0.0,
            "position_gamma": 0.0,
            "position_vega_decimal": 0.0,
            "position_vega_1vol": 0.0,
            "position_theta_year": 0.0,
            "position_theta_day": 0.0,
            "position_vanna": 0.0,
            "position_volga": 0.0,
        }
        for r in rows:
            if r["aggregation_level"] == "INSTRUMENT" and r["valuation_state"] == state:
                for key in ["position_delta", "position_gamma", "position_vega_decimal",
                            "position_vega_1vol", "position_theta_year", "position_theta_day",
                            "position_vanna", "position_volga"]:
                    agg[key] += float(r[key])
        rows.append(agg)

    return rows


def compute_pnl(data, positions: dict[str, dict[str, float]], valuation: dict[str, dict[str, float]], greeks_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Build PnL attribution rows: one INSTRUMENT row per instrument + one PORTFOLIO row.
    """
    # Index t0 INSTRUMENT greeks by instrument_name for attribution
    t0_greeks: dict[str, dict[str, Any]] = {}
    for r in greeks_rows:
        if r["aggregation_level"] == "INSTRUMENT" and r["valuation_state"] == "t0":
            t0_greeks[r["instrument_name"]] = r

    # Trades grouped by instrument
    trades_by_instr: dict[str, list[dict[str, Any]]] = {}
    for t in data.trades:
        trades_by_instr.setdefault(t["instrument_name"], []).append(t)

    from .bs import year_fraction_act365
    elapsed_years = year_fraction_act365(data.t0_utc, data.t1_utc)

    rows: list[dict[str, Any]] = []
    for name in sorted(data.metadata.keys()):
        meta = data.metadata[name]
        m = float(meta["contract_multiplier"])
        v = valuation[name]
        q0 = v["quantity_t0"]
        q1 = v["quantity_t1"]
        V0 = v["unit_value_t0_usd"]
        V1 = v["unit_value_t1_usd"]
        MV0 = v["market_value_t0_usd"]
        MV1 = v["market_value_t1_usd"]
        S0 = v["S0"]
        S1 = v["S1"]
        sigma0 = v["sigma0"]
        sigma1 = v["sigma1"]

        dS = S1 - S0
        dSigma = sigma1 - sigma0

        # Trades
        trade_cashflow = 0.0
        fees = 0.0
        trade_to_t1_pnl = 0.0
        for t in trades_by_instr.get(name, []):
            dq = float(t["quantity"]) if t["side"] == "BUY" else -float(t["quantity"])
            Pexec = float(t["execution_price_usd"])
            trade_cashflow += -dq * m * Pexec
            fees += float(t["fee_usd"])
            trade_to_t1_pnl += dq * m * (V1 - Pexec)

        carry_actual_pnl = q0 * m * (V1 - V0)
        total_pnl = MV1 - MV0 + trade_cashflow - fees

        # Attribution uses t0 position Greeks (q0 carry attribution)
        g = t0_greeks.get(name, {})
        pos_delta = float(g.get("position_delta", 0.0))
        pos_gamma = float(g.get("position_gamma", 0.0))
        pos_vega_dec = float(g.get("position_vega_decimal", 0.0))
        pos_theta_year = float(g.get("position_theta_year", 0.0))
        pos_vanna = float(g.get("position_vanna", 0.0))
        pos_volga = float(g.get("position_volga", 0.0))

        delta_pnl = pos_delta * dS
        gamma_pnl = 0.5 * pos_gamma * dS * dS
        vega_pnl = pos_vega_dec * dSigma
        theta_pnl = pos_theta_year * elapsed_years
        vanna_pnl = pos_vanna * dS * dSigma
        volga_pnl = 0.5 * pos_volga * dSigma * dSigma
        explained_pnl = delta_pnl + gamma_pnl + vega_pnl + theta_pnl + vanna_pnl + volga_pnl
        residual_pnl = carry_actual_pnl - explained_pnl

        rows.append({
            "aggregation_level": "INSTRUMENT",
            "instrument_name": name,
            "beginning_market_value_usd": MV0,
            "ending_market_value_usd": MV1,
            "trade_cashflow_usd": trade_cashflow,
            "fees_usd": fees,
            "carry_actual_pnl_usd": carry_actual_pnl,
            "trade_to_t1_pnl_usd": trade_to_t1_pnl,
            "total_pnl_usd": total_pnl,
            "delta_pnl_usd": delta_pnl,
            "gamma_pnl_usd": gamma_pnl,
            "vega_pnl_usd": vega_pnl,
            "theta_pnl_usd": theta_pnl,
            "vanna_pnl_usd": vanna_pnl,
            "volga_pnl_usd": volga_pnl,
            "explained_pnl_usd": explained_pnl,
            "residual_pnl_usd": residual_pnl,
        })

    # Portfolio row
    agg = {
        "aggregation_level": "PORTFOLIO",
        "instrument_name": "__PORTFOLIO__",
        "beginning_market_value_usd": 0.0,
        "ending_market_value_usd": 0.0,
        "trade_cashflow_usd": 0.0,
        "fees_usd": 0.0,
        "carry_actual_pnl_usd": 0.0,
        "trade_to_t1_pnl_usd": 0.0,
        "total_pnl_usd": 0.0,
        "delta_pnl_usd": 0.0,
        "gamma_pnl_usd": 0.0,
        "vega_pnl_usd": 0.0,
        "theta_pnl_usd": 0.0,
        "vanna_pnl_usd": 0.0,
        "volga_pnl_usd": 0.0,
        "explained_pnl_usd": 0.0,
        "residual_pnl_usd": 0.0,
    }
    for r in rows:
        for key in agg.keys():
            if key in ("aggregation_level", "instrument_name"):
                continue
            agg[key] += float(r[key])
    rows.append(agg)

    return rows


def compute_stress(data, positions: dict[str, dict[str, float]], valuation: dict[str, dict[str, float]]) -> dict[str, Any]:
    """
    Build stress report: full repricing per scenario using q1 and t1 base market.
    """
    scenarios_out: list[dict[str, Any]] = []
    for s in data.scenarios:
        sid = s["scenario_id"]
        spot_shock = float(s["spot_shock_pct"])
        vol_shock = float(s["vol_shock_abs"])

        portfolio_base_value = 0.0
        portfolio_stressed_value = 0.0
        instruments: list[dict[str, Any]] = []

        for name in sorted(data.metadata.keys()):
            meta = data.metadata[name]
            m = float(meta["contract_multiplier"])
            is_call = (meta["option_type"] == "call")
            K = float(meta["strike_usd"])
            expiry = str(meta["expiration_timestamp"])
            t1_row = data.market_t1[name]
            S1 = float(t1_row["spot_usd"])
            sigma1 = float(t1_row["iv_decimal"])
            r = float(data.rates[name])
            q = 0.0
            T1 = time_to_expiry(data.t1_utc, expiry)

            # Base value
            base_v = valuation[name]["unit_value_t1_usd"]
            base_mv = valuation[name]["market_value_t1_usd"]
            portfolio_base_value += base_mv

            # Stressed spot/iv
            S_stress = S1 * (1.0 + spot_shock)
            sigma_stress = sigma1 + vol_shock

            # Validate finite and valid
            if not (math.isfinite(S_stress) and S_stress > 0):
                raise InvalidScenarioError(f"Invalid stressed spot for scenario {sid}, instrument {name}: {S_stress}")
            # stressed_iv_decimal: > 0 before expiry (schema says finite > 0 before expiry)
            if T1 > 0 and not (math.isfinite(sigma_stress) and sigma_stress > 0):
                raise InvalidScenarioError(f"Invalid stressed iv for scenario {sid}, instrument {name}: {sigma_stress}")
            if not math.isfinite(sigma_stress):
                raise InvalidScenarioError(f"Invalid stressed iv for scenario {sid}, instrument {name}: {sigma_stress}")

            # If expired at t1, T1=0 -> intrinsic; sigma can be anything finite
            stressed_v = bs_price(is_call, S_stress, K, sigma_stress, T1, r, q)
            q1 = positions[name]["quantity_t1"]
            stressed_mv = q1 * m * stressed_v
            portfolio_stressed_value += stressed_mv

            instruments.append({
                "instrument_name": name,
                "quantity_t1": q1,
                "contract_multiplier": m,
                "base_unit_value_usd": base_v,
                "stressed_spot_usd": S_stress,
                "stressed_iv_decimal": sigma_stress,
                "stressed_unit_value_usd": stressed_v,
                "base_market_value_usd": base_mv,
                "stressed_market_value_usd": stressed_mv,
                "stress_pnl_usd": stressed_mv - base_mv,
            })

        scenarios_out.append({
            "scenario_id": sid,
            "spot_shock_pct": spot_shock,
            "vol_shock_abs": vol_shock,
            "portfolio_base_value_usd": portfolio_base_value,
            "portfolio_stressed_value_usd": portfolio_stressed_value,
            "portfolio_stress_pnl_usd": portfolio_stressed_value - portfolio_base_value,
            "instruments": instruments,
        })

    return {
        "schema_version": 1,
        "reporting_currency": "USD",
        "base_valuation_timestamp_utc": data.t1_utc,
        "scenarios": scenarios_out,
    }


class InvalidScenarioError(Exception):
    """Raised when a scenario produces non-finite or invalid stressed spot/IV."""
    pass
