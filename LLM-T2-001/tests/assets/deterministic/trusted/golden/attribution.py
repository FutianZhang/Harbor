"""Carry-position Greek PnL attribution in USD."""
from __future__ import annotations

from dataclasses import dataclass

from .greeks import UnitGreeks


@dataclass(frozen=True)
class AttributionResult:
    delta_pnl: float
    gamma_pnl: float
    vega_pnl: float
    theta_pnl: float
    vanna_pnl: float
    volga_pnl: float
    explained_pnl: float
    residual_pnl: float


def attribute_carry_pnl(
    starting_quantity: float,
    contract_multiplier: float,
    unit_greeks_t0: UnitGreeks,
    spot_change_usd: float,
    volatility_change_decimal: float,
    elapsed_years: float,
    carry_actual_pnl: float,
) -> AttributionResult:
    """Attribute q0 carry PnL with t0 unit Greeks scaled exactly once."""
    scale = starting_quantity * contract_multiplier
    delta = scale * unit_greeks_t0.delta * spot_change_usd
    gamma = 0.5 * scale * unit_greeks_t0.gamma * spot_change_usd**2
    vega = scale * unit_greeks_t0.vega_decimal * volatility_change_decimal
    theta = scale * unit_greeks_t0.theta_year * elapsed_years
    vanna = scale * unit_greeks_t0.vanna * spot_change_usd * volatility_change_decimal
    volga = 0.5 * scale * unit_greeks_t0.volga * volatility_change_decimal**2
    explained = delta + gamma + vega + theta + vanna + volga
    return AttributionResult(delta, gamma, vega, theta, vanna, volga, explained, carry_actual_pnl - explained)
