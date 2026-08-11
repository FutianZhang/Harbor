"""Synthetic option position reconstruction in contract quantities."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping


@dataclass(frozen=True)
class Trade:
    instrument: str
    timestamp: datetime
    side: str
    quantity: float
    execution_price_usd: float
    fee_usd: float

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime):
            raise TypeError("trade timestamp must be a datetime")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timedelta(0):
            raise ValueError("trade timestamp must be timezone-aware UTC")

    @property
    def signed_quantity(self) -> float:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("trade side must be BUY or SELL")
        if not math.isfinite(self.quantity) or self.quantity <= 0.0:
            raise ValueError("trade quantity must be finite and positive")
        if not math.isfinite(self.execution_price_usd) or self.execution_price_usd < 0.0:
            raise ValueError("execution_price_usd must be finite and nonnegative")
        if not math.isfinite(self.fee_usd) or self.fee_usd < 0.0:
            raise ValueError("fee_usd must be finite and nonnegative")
        return self.quantity if self.side == "BUY" else -self.quantity


def reconstruct_positions(initial: Mapping[str, float], trades: Iterable[Trade]) -> dict[str, float]:
    """Return q1 = q0 + signed trades for every instrument."""
    ending = {instrument: float(quantity) for instrument, quantity in initial.items()}
    for trade in trades:
        ending[trade.instrument] = ending.get(trade.instrument, 0.0) + trade.signed_quantity
    return ending
