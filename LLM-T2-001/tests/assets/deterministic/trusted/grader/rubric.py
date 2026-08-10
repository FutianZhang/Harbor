"""Observable-behavior rubric and public-contract traceability."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RubricItem:
    item_id: str
    category: str
    points: int
    title: str
    public_rule: str
    hidden_case: str
    observable_output: str


def _item(item_id: str, category: str, points: int, title: str, section: str,
          hidden_case: str, output: str) -> RubricItem:
    return RubricItem(item_id, category, points, title, f"task/README.md#{section}", hidden_case, output)


RUBRIC_ITEMS: tuple[RubricItem, ...] = (
    _item("VAL-1", "input_validation", 2, "Required schema and error boundary", "11-validation-contract", "missing/extra/wrong physical type", "validation status, code and output-file set"),
    _item("VAL-2", "input_validation", 2, "Recoverable optional observations", "65-market-t0parquet-and-market-t1parquet", "missing/nonfinite/semantic-invalid optional observations", "warning code/cardinality/location"),
    _item("VAL-3", "input_validation", 2, "Duplicate policy", "65-market-t0parquet-and-market-t1parquet", "exact and conflicting same-instrument duplicates", "warning or fatal category"),
    _item("VAL-4", "input_validation", 2, "Order normalization", "111-recoverable", "independently shuffled input containers", "warning code/cardinality/location/order"),
    _item("POS-1", "positions", 4, "Starting and no-trade positions", "7-position-reconstruction", "omitted zero and no-trade instruments", "positions_eod quantities"),
    _item("POS-2", "positions", 4, "Signed trades", "7-position-reconstruction", "BUY/SELL add, close and reversal", "net_trade_quantity"),
    _item("POS-3", "positions", 4, "Reconstruction identity", "7-position-reconstruction", "multiple shuffled trades", "quantity_t1 identity"),
    _item("PRC-1", "pricing", 4, "Black-Scholes calls", "4-black-scholes-pricing", "BTC/ETH ITM/ATM/OTM calls", "unit_value_t0/t1"),
    _item("PRC-2", "pricing", 4, "Black-Scholes puts and rates", "4-black-scholes-pricing", "puts with zero and negative rates", "unit_value_t0/t1 and parity"),
    _item("PRC-3", "pricing", 4, "Exact ACT/365 and expiry", "3-volatility-and-time", "sub-day timestamps and exact expiry", "valuation and zero-expiry boundary"),
    _item("GRK-1", "greeks", 3, "Delta and Gamma", "5-greeks-definitions", "call/put tails and nonlinear spots", "unit delta and gamma"),
    _item("GRK-2", "greeks", 3, "Vega units", "3-volatility-and-time", "decimal-vol and one-vol-point mutations", "unit vega_decimal / unit vega_1vol and their relation"),
    _item("GRK-3", "greeks", 3, "Theta units and sign", "5-greeks-definitions", "sub-day maturity and theta sign mutation", "unit theta_year / unit theta_day"),
    _item("GRK-4", "greeks", 3, "Vanna and Volga", "5-greeks-definitions", "mixed spot/vol and curved vol cases", "unit vanna and volga"),
    _item("GRK-5", "greeks", 3, "Position scaling and aggregation", "5-greeks-definitions", "non-unit multipliers and long/short positions", "position Greeks and portfolio Greek aggregation"),
    _item("PNL-1", "actual_pnl", 5, "Market value scaling", "8-valuation-and-actual-pnl", "non-unit multiplier at both states", "instrument market values"),
    _item("PNL-2", "actual_pnl", 4, "Execution cashflow and fees", "8-valuation-and-actual-pnl", "BUY/SELL executions with trade-total fees", "cashflow, fee and trade-to-t1 PnL"),
    _item("PNL-3", "actual_pnl", 4, "PnL identities and aggregation", "8-valuation-and-actual-pnl", "mixed instruments and zero positions", "instrument identities and portfolio sums"),
    _item("ATT-1", "attribution", 5, "Delta/Gamma attribution", "9-carry-position-greek-pnl-attribution", "spot-only and nonlinear spot moves", "delta/gamma components"),
    _item("ATT-2", "attribution", 5, "Vega/Theta attribution", "9-carry-position-greek-pnl-attribution", "vol-only and exact elapsed-time moves", "vega/theta components"),
    _item("ATT-3", "attribution", 5, "Vanna/Volga attribution", "9-carry-position-greek-pnl-attribution", "combined spot-vol finite moves", "vanna/volga components"),
    _item("ATT-4", "attribution", 5, "Residual identity", "9-carry-position-greek-pnl-attribution", "meaningful Taylor residual", "explained plus residual equals carry actual"),
    _item("STR-1", "stress", 5, "Spot/vol scenario application", "10-full-repricing-stress", "up/down and combined shocks", "stressed spot and IV"),
    _item("STR-2", "stress", 5, "Full repricing and scaling", "10-full-repricing-stress", "nonlinear long/short positions unlike Greek approximation", "instrument stress values/PnL"),
    _item("STR-3", "stress", 5, "Stress universe, aggregation and invalid state", "10-full-repricing-stress", "zero positions, both underlyings, invalid shocked state", "instrument universe, portfolio totals or INVALID_SCENARIO"),
    _item("OUT-1", "output_determinism", 1, "Required files", "1-task-interface", "success and fatal runs", "exact required output-file set"),
    _item("OUT-2", "output_determinism", 1, "CSV schema", "12-output-serialization-and-ordering", "all success outputs", "exact columns and no index"),
    _item("OUT-3", "output_determinism", 1, "JSON schema", "12-output-serialization-and-ordering", "nested stress/validation reports", "exact fields and strict JSON"),
    _item("OUT-4", "output_determinism", 1, "Row granularity and ordering", "12-output-serialization-and-ordering", "multiple instruments/scenarios", "row sets and canonical order"),
    _item("OUT-5", "output_determinism", 1, "Repeat determinism", "1-task-interface", "same logical inputs twice", "semantically identical outputs and warnings"),
)


def traceability_rows() -> list[dict[str, object]]:
    return sorted((asdict(item) for item in RUBRIC_ITEMS), key=lambda row: str(row["item_id"]))
