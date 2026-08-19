"""
Black-Scholes pricing and Greeks for European options.

Deterministic, offline-only implementation. No network, no wall clock.
All computations use float64 precision.

Conventions (per README / schema):
- S, K, V  -> USD per unit of underlying
- r        -> continuously compounded annual rate (decimal)
- q        -> 0  (q_div = 0, no dividend yield)
- sigma    -> decimal implied volatility (0.50 means 50% IV)
- T        -> year fraction using ACT/365 exact-second

When T <= 0: Call = max(S-K,0), Put = max(K-S,0); all unit Greeks = 0.
"""

from __future__ import annotations

import math

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf_prime(x: float) -> float:
    """First derivative of standard normal pdf."""
    return -x * _norm_pdf(x)


def year_fraction_act365(t0_utc: str, t1_utc: str) -> float:
    """
    ACT/365 exact-second fraction between two UTC RFC-3339 timestamps.

    Parses the timestamp strings deterministically (no timezone-naive datetime).
    """
    from datetime import datetime, timezone

    def _parse(ts: str) -> datetime:
        ts = ts.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    a = _parse(t0_utc)
    b = _parse(t1_utc)
    delta_seconds = (b - a).total_seconds()
    # ACT/365 exact-second: convert seconds to years via 365 days * 86400 seconds/day
    return delta_seconds / (365.0 * 86400.0)


def time_to_expiry(valuation_utc: str, expiration_utc: str) -> float:
    """
    ACT/365 exact-second year fraction from valuation timestamp to expiration.
    Never negative (returns 0 when expiry is at or before valuation).
    """
    t = year_fraction_act365(valuation_utc, expiration_utc)
    if t < 0.0:
        t = 0.0
    return t


def bs_price_delta(
    is_call: bool, S: float, K: float, sigma: float, T: float, r: float, q: float = 0.0
) -> tuple[float, float]:
    """Return (price, delta) for the Black-Scholes model."""
    if T <= 0.0:
        if is_call:
            price = max(S - K, 0.0)
            delta = 1.0 if S > K else (0.0 if S < K else 0.0)
        else:
            price = max(K - S, 0.0)
            delta = -1.0 if K > S else (0.0 if K < S else 0.0)
        # At expiry delta convention: intrinsic delta (some use 0). We use intrinsic delta.
        return price, delta

    # Avoid singularity when sigma=0: option value is deterministic forward payoff
    if sigma <= 0.0:
        disc = math.exp(-r * T)
        fwd = S * math.exp((r - q) * T)
        if is_call:
            price = disc * max(fwd - K, 0.0)
            delta = math.exp(-q * T) * (1.0 if fwd > K else 0.0)
        else:
            price = disc * max(K - fwd, 0.0)
            delta = -math.exp(-q * T) * (1.0 if K > fwd else 0.0)
        return price, delta

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    disc = math.exp(-r * T)
    eqt = math.exp(-q * T)
    nd1 = _norm_cdf(d1)
    nd2 = _norm_cdf(d2)
    nnd1 = _norm_cdf(-d1)
    nnd2 = _norm_cdf(-d2)

    if is_call:
        price = S * eqt * nd1 - K * disc * nd2
        delta = eqt * nd1
    else:
        price = K * disc * nnd2 - S * eqt * nnd1
        delta = -eqt * nnd1

    return price, delta


def bs_greeks(
    is_call: bool,
    S: float,
    K: float,
    sigma: float,
    T: float,
    r: float,
    q: float = 0.0,
) -> dict[str, float]:
    """
    Compute unit Black-Scholes Greeks.

    Returns dict with keys:
        price, delta, gamma, vega_decimal, vega_1vol,
        theta_year, theta_day, vanna, volga
    """
    # T <= 0 branch
    if T <= 0.0:
        if is_call:
            price = max(S - K, 0.0)
        else:
            price = max(K - S, 0.0)
        return {
            "price": price,
            "delta": 0.0,
            "gamma": 0.0,
            "vega_decimal": 0.0,
            "vega_1vol": 0.0,
            "theta_year": 0.0,
            "theta_day": 0.0,
            "vanna": 0.0,
            "volga": 0.0,
        }

    if sigma <= 0.0:
        # Degenerate (zero vol): price and delta computed, but second-order Greeks vanish
        price, delta = bs_price_delta(is_call, S, K, sigma, T, r, q)
        return {
            "price": price,
            "delta": delta,
            "gamma": 0.0,
            "vega_decimal": 0.0,
            "vega_1vol": 0.0,
            "theta_year": 0.0,
            "theta_day": 0.0,
            "vanna": 0.0,
            "volga": 0.0,
        }

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    disc = math.exp(-r * T)
    eqt = math.exp(-q * T)
    nd1 = _norm_cdf(d1)
    nd2 = _norm_cdf(d2)
    nnd1 = _norm_cdf(-d1)
    nnd2 = _norm_cdf(-d2)
    pdf_d1 = _norm_pdf(d1)
    pdf_d2 = _norm_pdf(d2)

    # Price
    if is_call:
        price = S * eqt * nd1 - K * disc * nd2
    else:
        price = K * disc * nnd2 - S * eqt * nnd1

    # Delta (spot USD change per 1 USD spot move, per unit underlying)
    if is_call:
        delta = eqt * nd1
    else:
        delta = -eqt * nd1

    # Gamma (delta change per 1 USD spot move, per unit underlying)
    gamma = eqt * pdf_d1 / (S * sigma * sqrtT)

    # Vega (per decimal volatility 1.00, per unit underlying)
    vega_decimal = S * eqt * pdf_d1 * sqrtT
    vega_1vol = 0.01 * vega_decimal

    # Theta (annualized; calendar-time forward)
    # theta_call = -(S * eqt * pdf_d1 * sigma)/(2*T) - r*K*disc*nd2 + q*S*eqt*nd1
    # theta_put  = -(S * eqt * pdf_d1 * sigma)/(2*T) + r*K*disc*nnd2 - q*S*eqt*nnd1
    common = -(S * eqt * pdf_d1 * sigma) / (2.0 * T)
    if is_call:
        theta_year = common - r * K * disc * nd2 + q * S * eqt * nd1
    else:
        theta_year = common + r * K * disc * nnd2 - q * S * eqt * nnd1
    theta_day = theta_year / 365.0

    # Vanna = d(delta)/d(sigma = -(eqt * pdf_d1 * d2) / sigma  (also = -eqt * pdf_d1 * d2 / sigma)
    # Standard closed form: vanna = -eqt * pdf_d1 * d2 / sigma
    # But the common "second derivative d2V/dS dsigma" form equals -eqt * (pdf_d1 / (S * sigma * sqrtT)) * d2
    # We use the d2V/dS dsigma form which is the direct second derivative definition.
    # d2V/dS dsigma = -eqt * (pdf_d1 / (S * sigma * sqrtT)) * d2
    vanna = -eqt * (pdf_d1 / (S * sigma * sqrtT)) * d2

    # Volga = d2V/dsigma2
    # volga = S * eqt * pdf_d1 * sqrtT * (d1 * d2 / sigma)
    volga = S * eqt * pdf_d1 * sqrtT * (d1 * d2 / sigma)

    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "vega_decimal": vega_decimal,
        "vega_1vol": vega_1vol,
        "theta_year": theta_year,
        "theta_day": theta_day,
        "vanna": vanna,
        "volga": volga,
    }


def bs_price(is_call: bool, S: float, K: float, sigma: float, T: float, r: float, q: float = 0.0) -> float:
    """Black-Scholes price only."""
    if T <= 0.0:
        return max((S - K), 0.0) if is_call else max((K - S), 0.0)
    if sigma <= 0.0:
        disc = math.exp(-r * T)
        fwd = S * math.exp((r - q) * T)
        if is_call:
            return disc * max(fwd - K, 0.0)
        return disc * max(K - fwd, 0.0)
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if is_call:
        return S * math.exp(-q * T) * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)
