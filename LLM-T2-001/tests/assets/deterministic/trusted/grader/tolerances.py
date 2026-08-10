"""Explicit, metric-specific float comparison policy."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Tolerance:
    abs_tol: float
    rel_tol: float


TOLERANCES = {
    "quantity": Tolerance(1e-12, 1e-12),
    "unit_price": Tolerance(1e-9, 2e-10),
    "market_value": Tolerance(1e-7, 2e-10),
    "unit_greek": Tolerance(1e-9, 2e-8),
    "position_greek": Tolerance(1e-7, 2e-8),
    "pnl": Tolerance(1e-7, 2e-9),
    "rate_vol_shock": Tolerance(1e-12, 1e-12),
    "spot": Tolerance(1e-9, 2e-10),
}

NUMERIC_FIELD_KINDS = {
    "quantity_t0": "quantity", "quantity_t1": "quantity", "quantity": "quantity",
    "net_trade_quantity": "quantity", "contract_multiplier": "quantity",
    "unit_value_t0_usd": "unit_price", "unit_value_t1_usd": "unit_price",
    "base_unit_value_usd": "unit_price", "stressed_unit_value_usd": "unit_price",
    "market_value_t0_usd": "market_value", "market_value_t1_usd": "market_value",
    "beginning_market_value_usd": "market_value", "ending_market_value_usd": "market_value",
    "base_market_value_usd": "market_value", "stressed_market_value_usd": "market_value",
    "portfolio_base_value_usd": "market_value", "portfolio_stressed_value_usd": "market_value",
    "unit_delta": "unit_greek", "unit_gamma": "unit_greek",
    "unit_vega_decimal": "unit_greek", "unit_vega_1vol": "unit_greek",
    "unit_theta_year": "unit_greek", "unit_theta_day": "unit_greek",
    "unit_vanna": "unit_greek", "unit_volga": "unit_greek",
    "position_delta": "position_greek", "position_gamma": "position_greek",
    "position_vega_decimal": "position_greek", "position_vega_1vol": "position_greek",
    "position_theta_year": "position_greek", "position_theta_day": "position_greek",
    "position_vanna": "position_greek", "position_volga": "position_greek",
    "trade_cashflow_usd": "pnl", "fees_usd": "pnl", "carry_actual_pnl_usd": "pnl",
    "trade_to_t1_pnl_usd": "pnl", "total_pnl_usd": "pnl", "delta_pnl_usd": "pnl",
    "gamma_pnl_usd": "pnl", "vega_pnl_usd": "pnl", "theta_pnl_usd": "pnl",
    "vanna_pnl_usd": "pnl", "volga_pnl_usd": "pnl", "explained_pnl_usd": "pnl",
    "residual_pnl_usd": "pnl", "stress_pnl_usd": "pnl", "portfolio_stress_pnl_usd": "pnl",
    "spot_shock_pct": "rate_vol_shock", "vol_shock_abs": "rate_vol_shock",
    "stressed_iv_decimal": "rate_vol_shock", "stressed_spot_usd": "spot",
}


def tolerance_for(field: str) -> Tolerance:
    try:
        return TOLERANCES[NUMERIC_FIELD_KINDS[field]]
    except KeyError as exc:
        raise KeyError(f"no explicit tolerance for numeric field {field!r}") from exc


def parse_finite_numeric(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric output")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("numeric output must be finite")
    return parsed


def close_numeric(left: object, right: object, tolerance: Tolerance) -> bool:
    try:
        return math.isclose(
            parse_finite_numeric(left), parse_finite_numeric(right),
            abs_tol=tolerance.abs_tol, rel_tol=tolerance.rel_tol,
        )
    except (TypeError, ValueError, OverflowError):
        return False
