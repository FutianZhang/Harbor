"""Thread-safe single-evaluation facade used by all eight RewardKit criteria."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .mapping import ATOMIC_ITEMS, programmatic_values


# #region agent log
def _agent_dbg(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    payload = {
        "sessionId": "d11355",
        "runId": "post-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
    for target in (
        Path("/logs/verifier/debug-d11355.log"),
        Path("/app/debug-d11355.log"),
        Path("debug-d11355.log"),
    ):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            pass
# #endregion


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


_RUNTIME_REQUIREMENTS = Path(__file__).with_name("runtime_requirements.txt")
_WHEELHOUSE = Path("/opt/task-wheels")
_DEP_PROBE = "import numpy, pandas, scipy, pyarrow, pyarrow.parquet"
# Embedded pins so bootstrap works even if sidecar requirements file was not uploaded with /tests.
_RUNTIME_PACKAGES = (
    "numpy==2.2.6",
    "pandas==2.2.3",
    "scipy==1.15.3",
    "pyarrow==20.0.0",
)


def _ensure_evaluator_dependencies(evaluator_python: str) -> None:
    """Make sure the worker interpreter can import trusted-grader deps.

    Harbor images sometimes have rewardkit but lack the scientific stack, and may
    omit /opt/task-wheels when the task Dockerfile was not the image actually used.
    Prefer offline wheelhouse; otherwise pip-install the embedded pins (needs PyPI
    on the verifier allowlist).
    """
    probe = subprocess.run(
        [evaluator_python, "-c", _DEP_PROBE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )
    # #region agent log
    site_probe = subprocess.run(
        [
            evaluator_python,
            "-c",
            "import sys,importlib.util,os; "
            "print(sys.executable); "
            "print(sys.version); "
            "print('|'.join(sys.path)); "
            "print(importlib.util.find_spec('pyarrow')); "
            "print(os.path.isdir('/opt/task-wheels')); "
            "print(os.path.isdir('/usr/local/lib/python3.12/site-packages/pyarrow'))",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=60,
    )
    _agent_dbg(
        "F,G,H",
        "evaluator.py:_ensure_evaluator_dependencies:probe",
        "dependency probe before worker",
        {
            "probe_returncode": probe.returncode,
            "probe_stderr": (probe.stderr or "")[:800],
            "site_stdout": (site_probe.stdout or "")[:1200],
            "site_stderr": (site_probe.stderr or "")[:400],
            "evaluator_python": evaluator_python,
            "requirements_exists": _RUNTIME_REQUIREMENTS.is_file(),
            "wheelhouse_exists": _WHEELHOUSE.is_dir(),
            "embedded_packages": list(_RUNTIME_PACKAGES),
        },
    )
    # #endregion
    if probe.returncode == 0:
        return

    command = [
        evaluator_python,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--disable-pip-version-check",
    ]
    used_wheelhouse = _WHEELHOUSE.is_dir() and any(_WHEELHOUSE.iterdir())
    if used_wheelhouse:
        command.extend(("--no-index", "--find-links", str(_WHEELHOUSE)))
    if _RUNTIME_REQUIREMENTS.is_file():
        command.extend(("-r", str(_RUNTIME_REQUIREMENTS)))
    else:
        command.extend(_RUNTIME_PACKAGES)

    installed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=1200,
    )
    # #region agent log
    _agent_dbg(
        "F",
        "evaluator.py:_ensure_evaluator_dependencies:install",
        "bootstrap pip install finished",
        {
            "returncode": installed.returncode,
            "command": command,
            "stdout_tail": (installed.stdout or "")[-1000:],
            "stderr_tail": (installed.stderr or "")[-1000:],
            "used_wheelhouse": used_wheelhouse,
            "used_requirements_file": _RUNTIME_REQUIREMENTS.is_file(),
        },
    )
    # #endregion
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
    # #region agent log
    _agent_dbg(
        "F",
        "evaluator.py:_ensure_evaluator_dependencies:confirm",
        "dependency probe after bootstrap",
        {
            "confirm_returncode": confirm.returncode,
            "confirm_stderr": (confirm.stderr or "")[:500],
        },
    )
    # #endregion
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
    # #region agent log
    _agent_dbg(
        "D",
        "evaluator.py:_run_worker:complete",
        "trusted worker subprocess finished",
        {
            "returncode": completed.returncode,
            "stdout_len": len(completed.stdout or ""),
            "stderr_head": (completed.stderr or "")[:2000],
            "stdout_head": (completed.stdout or "")[:500],
            "evaluator_python": evaluator_python,
            "worker": str(worker),
            "workspace": str(workspace.resolve()),
        },
    )
    # #endregion
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
                # #region agent log
                _agent_dbg(
                    "D",
                    "evaluator.py:evaluate_candidate_once:exception",
                    "evaluate_candidate_once caught exception; caching failed result",
                    {"error_kind": type(exc).__name__, "error_message": str(exc), "workspace": key},
                )
                # #endregion
                _CACHE[key] = _failed_result(type(exc).__name__)
            else:
                result = _CACHE[key]
                # #region agent log
                _agent_dbg(
                    "A,C",
                    "evaluator.py:evaluate_candidate_once:cached",
                    "evaluation result cached",
                    {
                        "infrastructure_error": result.infrastructure_error,
                        "error_kind": result.error_kind,
                        "internal_score": result.internal_score,
                        "failed_items": list(result.failed_items),
                        "programmatic": dict(result.programmatic),
                        "evidence_keys": sorted(result.evidence.keys()),
                        "error_message": result.evidence.get("error_message"),
                    },
                )
                # #endregion
        return _CACHE[key]


def evaluation_count() -> int:
    with _CACHE_LOCK:
        return _EVALUATION_COUNT


def reset_cache_for_testing() -> None:
    global _EVALUATION_COUNT
    with _CACHE_LOCK:
        _CACHE.clear()
        _EVALUATION_COUNT = 0
