"""Black-Scholes unit pricing in USD per one unit of BTC or ETH.

Volatility and continuously compounded rates are decimals. ``time_years`` is
an ACT/365 year fraction computed from exact UTC timestamps.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
SQRT_TWO = math.sqrt(2.0)


def normal_cdf(x: float) -> float:
    """Return the standard-normal CDF without negative-tail cancellation."""
    return 0.5 * math.erfc(-x / SQRT_TWO)


def normal_pdf(x: float) -> float:
    """Return the standard-normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _require_finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def validate_bs_inputs(
    option_type: str,
    spot: float,
    strike: float,
    time_years: float,
    volatility_decimal: float,
    rate_decimal: float,
    dividend_yield_decimal: float,
) -> tuple[str, float, float, float, float, float, float]:
    """Validate and normalize the locked Black-Scholes inputs."""
    if option_type not in {"call", "put"}:
        raise ValueError("option_type must be 'call' or 'put'")
    spot = _require_finite("spot", spot)
    strike = _require_finite("strike", strike)
    time_years = _require_finite("time_years", time_years)
    volatility_decimal = _require_finite("volatility_decimal", volatility_decimal)
    rate_decimal = _require_finite("rate_decimal", rate_decimal)
    dividend_yield_decimal = _require_finite("dividend_yield_decimal", dividend_yield_decimal)
    if spot <= 0.0:
        raise ValueError("spot must be positive")
    if strike <= 0.0:
        raise ValueError("strike must be positive")
    if time_years > 0.0 and volatility_decimal <= 0.0:
        raise ValueError("volatility_decimal must be positive when time_years > 0")
    return option_type, spot, strike, time_years, volatility_decimal, rate_decimal, dividend_yield_decimal


def d1_d2(
    spot: float,
    strike: float,
    time_years: float,
    volatility_decimal: float,
    rate_decimal: float,
    dividend_yield_decimal: float = 0.0,
) -> tuple[float, float]:
    """Return Black-Scholes d1 and d2 for a live option."""
    sqrt_time = math.sqrt(time_years)
    d1 = (
        math.log(spot / strike)
        + (rate_decimal - dividend_yield_decimal + 0.5 * volatility_decimal**2) * time_years
    ) / (volatility_decimal * sqrt_time)
    return d1, d1 - volatility_decimal * sqrt_time


def bs_price(
    option_type: str,
    spot: float,
    strike: float,
    time_years: float,
    volatility_decimal: float,
    rate_decimal: float,
    dividend_yield_decimal: float = 0.0,
) -> float:
    """Return unit option value in USD per one unit of underlying."""
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
        return max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)

    d1, d2 = d1_d2(spot, strike, time_years, volatility_decimal, rate_decimal, dividend_yield_decimal)
    discounted_spot = spot * math.exp(-dividend_yield_decimal * time_years)
    discounted_strike = strike * math.exp(-rate_decimal * time_years)
    if option_type == "call":
        return discounted_spot * normal_cdf(d1) - discounted_strike * normal_cdf(d2)
    return discounted_strike * normal_cdf(-d2) - discounted_spot * normal_cdf(-d1)


def act365_time_years(valuation_time: datetime, expiry_time: datetime) -> float:
    """Return exact UTC ACT/365 time, floored at zero."""
    if not isinstance(valuation_time, datetime) or not isinstance(expiry_time, datetime):
        raise TypeError("valuation_time and expiry_time must be datetimes")
    for name, timestamp in (("valuation_time", valuation_time), ("expiry_time", expiry_time)):
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be timezone-aware UTC")
    return max((expiry_time - valuation_time).total_seconds(), 0.0) / SECONDS_PER_YEAR
