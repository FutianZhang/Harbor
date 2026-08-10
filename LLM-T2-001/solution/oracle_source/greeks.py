"""Analytic Black-Scholes unit Greeks for USD value per unit underlying.

Vega is exposed for a decimal-volatility change and for one volatility point;
theta is exposed per calendar year and per calendar day. Vanna and volga use
decimal volatility. Position scaling is deliberately outside this unit engine.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .pricing import d1_d2, normal_cdf, normal_pdf, validate_bs_inputs


@dataclass(frozen=True)
class UnitGreeks:
    delta: float
    gamma: float
    vega_decimal: float
    vega_1vol: float
    theta_year: float
    theta_day: float
    vanna: float
    volga: float


@dataclass(frozen=True)
class PositionGreeks:
    """Greeks scaled once by contract quantity and contract multiplier."""

    delta: float
    gamma: float
    vega_decimal: float
    vega_1vol: float
    theta_year: float
    theta_day: float
    vanna: float
    volga: float


ZERO_GREEKS = UnitGreeks(
    delta=0.0,
    gamma=0.0,
    vega_decimal=0.0,
    vega_1vol=0.0,
    theta_year=0.0,
    theta_day=0.0,
    vanna=0.0,
    volga=0.0,
)


ZERO_POSITION_GREEKS = PositionGreeks(
    delta=0.0,
    gamma=0.0,
    vega_decimal=0.0,
    vega_1vol=0.0,
    theta_year=0.0,
    theta_day=0.0,
    vanna=0.0,
    volga=0.0,
)


def scale_position_greeks(
    unit_greeks: UnitGreeks,
    quantity: float,
    contract_multiplier: float,
) -> PositionGreeks:
    """Apply ``quantity * contract_multiplier`` exactly once to unit Greeks."""
    quantity = float(quantity)
    contract_multiplier = float(contract_multiplier)
    if not math.isfinite(quantity):
        raise ValueError("quantity must be finite")
    if not math.isfinite(contract_multiplier) or contract_multiplier <= 0.0:
        raise ValueError("contract_multiplier must be finite and positive")
    factor = quantity * contract_multiplier
    return PositionGreeks(
        **{
            field: factor * getattr(unit_greeks, field)
            for field in PositionGreeks.__dataclass_fields__
        }
    )


def aggregate_position_greeks(rows: Iterable[PositionGreeks]) -> PositionGreeks:
    """Aggregate already-scaled position Greeks without additional scaling."""
    values = list(rows)
    if not values:
        return ZERO_POSITION_GREEKS
    return PositionGreeks(
        **{
            field: sum(getattr(row, field) for row in values)
            for field in PositionGreeks.__dataclass_fields__
        }
    )


def bs_greeks(
    option_type: str,
    spot: float,
    strike: float,
    time_years: float,
    volatility_decimal: float,
    rate_decimal: float,
    dividend_yield_decimal: float = 0.0,
) -> UnitGreeks:
    """Return analytic unit Greeks using the benchmark's locked units."""
    option_type, spot, strike, time_years, volatility_decimal, rate_decimal, dividend_yield_decimal = (
        validate_bs_inputs(
            option_type,
            spot,
            strike,
            time_years,
            volatility_decimal,
            rate_decimal,
            dividend_yield_decimal,
        )
    )
    if time_years <= 0.0:
        return ZERO_GREEKS

    sqrt_time = math.sqrt(time_years)
    d1, d2 = d1_d2(spot, strike, time_years, volatility_decimal, rate_decimal, dividend_yield_decimal)
    pdf_d1 = normal_pdf(d1)
    discounted_spot_factor = math.exp(-dividend_yield_decimal * time_years)
    discounted_strike = strike * math.exp(-rate_decimal * time_years)

    if option_type == "call":
        delta = discounted_spot_factor * normal_cdf(d1)
        theta_year = (
            -spot * discounted_spot_factor * pdf_d1 * volatility_decimal / (2.0 * sqrt_time)
            - rate_decimal * discounted_strike * normal_cdf(d2)
            + dividend_yield_decimal * spot * discounted_spot_factor * normal_cdf(d1)
        )
    else:
        delta = discounted_spot_factor * (normal_cdf(d1) - 1.0)
        theta_year = (
            -spot * discounted_spot_factor * pdf_d1 * volatility_decimal / (2.0 * sqrt_time)
            + rate_decimal * discounted_strike * normal_cdf(-d2)
            - dividend_yield_decimal * spot * discounted_spot_factor * normal_cdf(-d1)
        )

    gamma = discounted_spot_factor * pdf_d1 / (spot * volatility_decimal * sqrt_time)
    vega_decimal = spot * discounted_spot_factor * pdf_d1 * sqrt_time
    vanna = -discounted_spot_factor * pdf_d1 * d2 / volatility_decimal
    volga = vega_decimal * d1 * d2 / volatility_decimal
    return UnitGreeks(
        delta=delta,
        gamma=gamma,
        vega_decimal=vega_decimal,
        vega_1vol=vega_decimal * 0.01,
        theta_year=theta_year,
        theta_day=theta_year / 365.0,
        vanna=vanna,
        volga=volga,
    )
