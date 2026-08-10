"""USD option market-value and interval-PnL accounting identities."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .positions import Trade


@dataclass(frozen=True)
class PnLResult:
    beginning_market_value: float
    ending_market_value: float
    trade_cashflows: float
    fees_usd: float
    carry_actual_pnl: float
    trade_to_t1_pnl: float
    total_pnl: float

    @property
    def market_value_identity(self) -> float:
        return self.ending_market_value - self.beginning_market_value + self.trade_cashflows - self.fees_usd

    @property
    def carry_trade_identity(self) -> float:
        return self.carry_actual_pnl + self.trade_to_t1_pnl - self.fees_usd


def instrument_pnl(
    instrument: str,
    starting_quantity: float,
    ending_quantity: float,
    contract_multiplier: float,
    value_t0_usd: float,
    value_t1_usd: float,
    trades: Iterable[Trade],
) -> PnLResult:
    """Compute both algebraically equivalent instrument PnL forms."""
    if not math.isfinite(contract_multiplier) or contract_multiplier <= 0.0:
        raise ValueError("contract_multiplier must be finite and positive")
    instrument_trades = list(trades)
    if any(trade.instrument != instrument for trade in instrument_trades):
        raise ValueError("all trades must match the instrument")
    signed = [trade.signed_quantity for trade in instrument_trades]
    reconstructed = starting_quantity + sum(signed)
    if not math.isclose(reconstructed, ending_quantity, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("ending_quantity does not match starting quantity plus trades")

    beginning = starting_quantity * contract_multiplier * value_t0_usd
    ending = ending_quantity * contract_multiplier * value_t1_usd
    cashflows = sum(-dq * contract_multiplier * trade.execution_price_usd for dq, trade in zip(signed, instrument_trades))
    fees = sum(trade.fee_usd for trade in instrument_trades)
    carry = starting_quantity * contract_multiplier * (value_t1_usd - value_t0_usd)
    trade_to_t1 = sum(
        dq * contract_multiplier * (value_t1_usd - trade.execution_price_usd)
        for dq, trade in zip(signed, instrument_trades)
    )
    total = carry + trade_to_t1 - fees
    return PnLResult(beginning, ending, cashflows, fees, carry, trade_to_t1, total)


def portfolio_pnl(rows: Iterable[PnLResult]) -> PnLResult:
    """Aggregate instrument accounting fields without re-scaling."""
    values = list(rows)
    return PnLResult(*(sum(getattr(row, field) for row in values) for field in PnLResult.__dataclass_fields__))
