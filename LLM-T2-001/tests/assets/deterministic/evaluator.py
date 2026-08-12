"""Thread-safe single-evaluation facade used by all eight RewardKit criteria."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .mapping import ATOMIC_ITEMS, programmatic_values


@dataclass(frozen=True)
class EvaluationResult:
    internal_score: int
    failed_items: tuple[str, ...]
    passed_items: tuple[str, ...]
    programmatic: Mapping[str, float]
    variant_count: int
    repeats_per_variant: int
    candidate_process_count: int
    runtime_seconds: float
    infrastructure_error: bool
    error_kind: str | None
    evidence: Mapping[str, Any]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EvaluationResult":
        infrastructure_error = bool(payload.get("infrastructure_error", False))
        failed = tuple(sorted(
            ATOMIC_ITEMS if infrastructure_error else (str(item) for item in payload.get("failed_items", ()))
        ))
        passed = tuple(sorted(set(ATOMIC_ITEMS) - set(failed)))
        evidence = MappingProxyType(dict(payload.get("evidence", {})))
        return cls(
            internal_score=int(payload.get("internal_score", 0)),
            failed_items=failed,
            passed_items=passed,
            programmatic=MappingProxyType(programmatic_values(failed)),
            variant_count=int(payload.get("variant_count", 0)),
            repeats_per_variant=int(payload.get("repeats_per_variant", 0)),
            candidate_process_count=int(payload.get("candidate_process_count", 0)),
            runtime_seconds=float(payload.get("runtime_seconds", 0.0)),
            infrastructure_error=infrastructure_error,
            error_kind=payload.get("error_kind"),
            evidence=evidence,
        )

    @classmethod
    def perfect(cls, *, runtime_seconds: float = 0.0) -> "EvaluationResult":
        return cls.from_payload({
            "internal_score": 100,
            "failed_items": [],
            "variant_count": 17,
            "repeats_per_variant": 2,
            "candidate_process_count": 34,
            "runtime_seconds": runtime_seconds,
            "infrastructure_error": False,
            "evidence": {},
        })


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, EvaluationResult] = {}
_EVALUATION_COUNT = 0


def _failed_result(error_kind: str) -> EvaluationResult:
    return EvaluationResult.from_payload({
        "internal_score": 0,
        "failed_items": ATOMIC_ITEMS,
        "infrastructure_error": True,
        "error_kind": error_kind,
        "evidence": {"candidate_failure": False, "verifier_error": True},
    })


_DEP_PROBE = "import numpy, pandas, scipy, pyarrow, pyarrow.parquet"
_RUNTIME_PACKAGES = (
    "numpy==2.2.6",
    "pandas==2.2.3",
    "scipy==1.15.3",
    "pyarrow==20.0.0",
)


def _ensure_evaluator_dependencies(evaluator_python: str) -> None:
    """Ensure trusted-grader deps exist in the worker interpreter via pip."""
    probe = subprocess.run(
        [evaluator_python, "-c", _DEP_PROBE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )
    if probe.returncode == 0:
        return

    command = [
        evaluator_python,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--disable-pip-version-check",
        *_RUNTIME_PACKAGES,
    ]

    installed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=1200,
    )
    if installed.returncode != 0:
        raise RuntimeError(
            "failed to bootstrap evaluator deps "
            f"(pip exit {installed.returncode}): {(installed.stderr or installed.stdout or '')[-500:]}"
        )
    confirm = subprocess.run(
        [evaluator_python, "-c", _DEP_PROBE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )
    if confirm.returncode != 0:
        raise RuntimeError(
            "evaluator deps still missing after bootstrap: "
            f"{(confirm.stderr or '')[:500]}"
        )


def _run_worker(workspace: Path) -> EvaluationResult:
    worker = Path(__file__).with_name("worker.py")
    evaluator_python = os.environ.get("V4_HYBRID_EVALUATOR_PYTHON", sys.executable)
    _ensure_evaluator_dependencies(evaluator_python)
    completed = subprocess.run(
        [evaluator_python, str(worker), str(workspace.resolve())],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=3600,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if completed.returncode != 0:
        raise RuntimeError(f"trusted evaluator worker exited {completed.returncode}")
    payload = json.loads(completed.stdout)
    return EvaluationResult.from_payload(payload)


def evaluate_candidate_once(workspace: Path) -> EvaluationResult:
    """Evaluate one workspace once per RewardKit process, including concurrent calls."""
    global _EVALUATION_COUNT
    key = str(Path(workspace).resolve())
    with _CACHE_LOCK:
        if key not in _CACHE:
            _EVALUATION_COUNT += 1
            try:
                _CACHE[key] = _run_worker(Path(workspace))
            except Exception as exc:  # verifier faults must not crash RewardKit
                _CACHE[key] = _failed_result(type(exc).__name__)
        return _CACHE[key]


def evaluation_count() -> int:
    with _CACHE_LOCK:
        return _EVALUATION_COUNT


def reset_cache_for_testing() -> None:
    global _EVALUATION_COUNT
    with _CACHE_LOCK:
        _CACHE.clear()
        _EVALUATION_COUNT = 0
