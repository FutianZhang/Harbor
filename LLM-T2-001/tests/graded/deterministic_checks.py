"""Exactly eight RewardKit 0.1.7 programmatic criteria for Phase 3C."""
from __future__ import annotations

import sys
from pathlib import Path

import rewardkit as rk


ASSETS = Path(__file__).resolve().parents[1] / "assets"
if str(ASSETS) not in sys.path:
    sys.path.insert(0, str(ASSETS))

from deterministic.evaluator import evaluate_candidate_once  # noqa: E402


def _value(workspace: Path, programmatic_id: str) -> float:
    try:
        return float(evaluate_candidate_once(workspace).programmatic.get(programmatic_id, 0.0))
    except Exception:
        return 0.0


@rk.criterion(description="Position reconstruction: POS-1 through POS-3")
def position_reconstruction(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


@rk.criterion(description="Pricing and exact time: PRC-1 through PRC-3")
def pricing_and_time(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


@rk.criterion(description="Greeks: GRK-1 through GRK-5")
def greeks(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


@rk.criterion(description="Actual PnL and accounting: PNL-1 through PNL-3")
def actual_pnl_and_accounting(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


@rk.criterion(description="PnL attribution: ATT-1 through ATT-4")
def pnl_attribution(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


@rk.criterion(description="Stress full repricing: STR-1 through STR-3")
def stress_repricing(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


@rk.criterion(description="Validation: VAL-1 through VAL-4")
def validation(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


@rk.criterion(description="Output and repeat determinism: OUT-1 through OUT-5")
def output_and_determinism(workspace: Path, programmatic_id: str) -> float:
    return _value(workspace, programmatic_id)


rk.position_reconstruction("P01", weight=10.0, name="P01")
rk.pricing_and_time("P02", weight=10.0, name="P02")
rk.greeks("P03", weight=10.0, name="P03")
rk.actual_pnl_and_accounting("P04", weight=10.0, name="P04")
rk.pnl_attribution("P05", weight=10.0, name="P05")
rk.stress_repricing("P06", weight=10.0, name="P06")
rk.validation("P07", weight=10.0, name="P07")
rk.output_and_determinism("P08", weight=10.0, name="P08")
