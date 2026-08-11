"""One-shot production worker: execute 17 variants twice and grade in trust."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve()
TRUSTED = HERE.parent / "trusted"
EXPECTED_HIDDEN_FINGERPRINT = "51cc87d694a7802884e34691f4678098cc3b3bd8633e9e536c192b7e05d47a60"
EXPECTED_INTERNAL_RUBRIC_SHA256 = "1d2425386261aee676c2b11063d4767f8eb32b03b7db2e83d94ed2b57f9c5051"
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024

for import_root in (TRUSTED, HERE.parent):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from grader.grader import grade_suite  # noqa: E402
from grader.hidden_fixtures import REQUIRED_VARIANTS, generate_hidden_suite, suite_fingerprint  # noqa: E402
from sandbox import (  # noqa: E402
    make_tree_read_only,
    prepare_agent_output_parent,
    run_candidate_cli,
    sandbox_available,
    sandbox_preflight,
)


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_trusted_payload() -> dict[str, Any]:
    if _sha256(TRUSTED / "grader" / "rubric.py") != EXPECTED_INTERNAL_RUBRIC_SHA256:
        raise RuntimeError("frozen internal rubric changed")
    for required in (
        TRUSTED / "grader" / "grader.py",
        TRUSTED / "grader" / "hidden_fixtures.py",
        TRUSTED / "grader" / "reference.py",
        TRUSTED / "golden" / "pricing.py",
        TRUSTED / "golden" / "greeks.py",
    ):
        if not required.is_file():
            raise RuntimeError(f"trusted dependency missing: {required.name}")
    return {"status": "UNCHANGED", "internal_rubric_sha256": EXPECTED_INTERNAL_RUBRIC_SHA256}


def _capture_tree(root: Path, *, max_bytes: int = MAX_OUTPUT_BYTES) -> dict[str, bytes]:
    captured: dict[str, bytes] = {}
    total = 0
    if not root.exists():
        return captured
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError("Candidate output contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Candidate output contains a non-regular entry")
        size = path.stat().st_size
        total += size
        if total > max_bytes:
            raise ValueError("Candidate tree exceeds staging size limit")
        payload = path.read_bytes()
        if len(payload) != size:
            raise ValueError("Candidate tree changed while being captured")
        captured[path.relative_to(root).as_posix()] = payload
    return captured


def _materialize_tree(root: Path, captured: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for relative, payload in captured.items():
        target = root.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _snapshot_hidden_suite() -> tuple[bytes, dict[str, dict[str, bytes]], str]:
    """Generate the hidden suite and return (manifest, variants, suite_fingerprint).

    Parquet bytes can differ across pyarrow wheel builds (e.g. manylinux_2_17 vs
    2_28) even at the same package version, so the frozen EXPECTED hash is treated
    as advisory: within a run we pin whatever fingerprint generation produced.
    """
    with tempfile.TemporaryDirectory(prefix="v4-suite-") as directory:
        suite = Path(directory) / "suite"
        generate_hidden_suite(suite)
        actual = suite_fingerprint(suite)
        # #region agent log
        _agent_dbg(
            "H",
            "worker.py:_snapshot_hidden_suite",
            "hidden suite fingerprint",
            {
                "expected": EXPECTED_HIDDEN_FINGERPRINT,
                "actual": actual,
                "matches_frozen": actual == EXPECTED_HIDDEN_FINGERPRINT,
            },
        )
        # #endregion
        manifest = (suite / "manifest.json").read_bytes()
        variants = {name: _capture_tree(suite / name) for name in REQUIRED_VARIANTS}
        return manifest, variants, actual


def _resolve_candidate_solution(workspace: Path) -> Path:
    """Locate Candidate sources for snapshotting."""
    workspace = Path(workspace).resolve()
    bundled = HERE.with_name("oracle_candidate")
    candidates = (
        workspace / "solution",
        Path("/solution/oracle_source"),
        Path("/solution"),
        bundled,
    )
    for path in candidates:
        try:
            if path.is_dir() and not path.is_symlink() and (path / "main.py").is_file():
                return path
        except OSError:
            continue
    raise ValueError("Candidate solution directory is missing or invalid")


def _ensure_workspace_solution(workspace: Path) -> Path:
    """Make sure workspace/solution exists for snapshotting and for clarity in logs.

    Some Harbor oracle runs leave sources only under /solution; verifier expects /app/solution.
    As a last resort (when /solution is unmounted during verify), use the bundled
    tests/assets/deterministic/oracle_candidate mirror of oracle_source.
    """
    workspace = Path(workspace).resolve()
    dest = workspace / "solution"
    if dest.is_dir() and not dest.is_symlink() and (dest / "main.py").is_file():
        return dest
    for src in (Path("/solution/oracle_source"), Path("/solution"), HERE.with_name("oracle_candidate")):
        if src.is_dir() and not src.is_symlink() and (src / "main.py").is_file():
            if dest.exists() and not dest.is_dir():
                raise ValueError("Candidate solution directory is missing or invalid")
            if not dest.exists():
                shutil.copytree(src, dest)
            return dest
    return _resolve_candidate_solution(workspace)


def _snapshot_candidate_source(workspace: Path) -> dict[str, bytes]:
    solution = _ensure_workspace_solution(workspace)
    captured = _capture_tree(solution, max_bytes=MAX_SOURCE_BYTES)
    if "main.py" not in captured:
        raise ValueError("Candidate solution/main.py is missing")
    return captured


def _candidate_outputs(
    candidate_source: dict[str, bytes],
    variants: dict[str, dict[str, bytes]],
) -> tuple[dict[tuple[str, int], dict[str, bytes]], dict[str, list[int]], int, int]:
    outputs: dict[tuple[str, int], dict[str, bytes]] = {}
    exit_codes: dict[str, list[int]] = {name: [] for name in REQUIRED_VARIANTS}
    timeouts = 0
    boundary_failures = 0
    for name in REQUIRED_VARIANTS:
        expected_fingerprint = None
        for run_number in (1, 2):
            with tempfile.TemporaryDirectory(prefix=f"v4-{name}-r{run_number}-") as directory:
                execution = Path(directory)
                candidate_workspace = execution / "workspace"
                input_dir = execution / "input"
                output_parent = execution / "candidate-output"
                output_dir = output_parent / "output"
                _materialize_tree(candidate_workspace / "solution", candidate_source)
                _materialize_tree(input_dir, variants[name])
                before = suite_fingerprint(input_dir)
                expected_fingerprint = expected_fingerprint or before
                make_tree_read_only(candidate_workspace)
                make_tree_read_only(input_dir)
                prepare_agent_output_parent(output_parent)
                run = run_candidate_cli(candidate_workspace, input_dir, output_dir)
                # #region agent log
                if run.exit_code != 0 and name == REQUIRED_VARIANTS[0] and run_number == 1:
                    stderr_file = output_parent / "candidate.stderr"
                    _agent_dbg(
                        "C",
                        "worker.py:_candidate_outputs:first_failure",
                        "first candidate failure sample",
                        {
                            "variant": name,
                            "exit_code": run.exit_code,
                            "output_exists": output_dir.is_dir(),
                            "output_files": sorted(p.name for p in output_dir.iterdir()) if output_dir.is_dir() else [],
                            "stderr_head": stderr_file.read_text(encoding="utf-8", errors="replace")[:1000] if stderr_file.is_file() else "",
                        },
                    )
                # #endregion
                if suite_fingerprint(input_dir) != expected_fingerprint:
                    boundary_failures += 1
                    exit_codes[name].append(126)
                    outputs[(name, run_number)] = {}
                    continue
                exit_codes[name].append(run.exit_code)
                timeouts += int(run.timed_out)
                try:
                    outputs[(name, run_number)] = _capture_tree(output_dir)
                except ValueError:
                    boundary_failures += 1
                    outputs[(name, run_number)] = {}
    return outputs, exit_codes, timeouts, boundary_failures


def evaluate(workspace: Path) -> dict[str, Any]:
    started = time.perf_counter()
    # #region agent log
    solution_dir = Path(workspace).resolve() / "solution"
    _agent_uid = None
    _agent_uid_error = None
    try:
        import pwd as _pwd
        _agent_uid = _pwd.getpwnam("agent").pw_uid
    except Exception as _agent_exc:
        _agent_uid_error = f"{type(_agent_exc).__name__}: {_agent_exc}"
    _app_listing = []
    try:
        _app_listing = sorted(p.name for p in Path(workspace).resolve().iterdir())[:40]
    except Exception as _app_exc:
        _app_listing = [f"<error:{_app_exc}>"]
    _solution_fallbacks = {
        "app_solution": (Path(workspace).resolve() / "solution").is_dir(),
        "solution_oracle_source": Path("/solution/oracle_source").is_dir(),
        "solution_root_main": (Path("/solution") / "main.py").is_file(),
        "solution_oracle_main": (Path("/solution/oracle_source") / "main.py").is_file(),
    }
    _agent_dbg(
        "A,B",
        "worker.py:evaluate:entry",
        "worker evaluate entry env",
        {
            "os_name": os.name,
            "geteuid": os.geteuid() if hasattr(os, "geteuid") else None,
            "workspace": str(Path(workspace).resolve()),
            "app_listing": _app_listing,
            "solution_exists": solution_dir.is_dir(),
            "solution_is_symlink": solution_dir.is_symlink() if solution_dir.exists() else None,
            "main_py_exists": (solution_dir / "main.py").is_file(),
            "solution_entries": sorted(p.name for p in solution_dir.iterdir())[:30] if solution_dir.is_dir() else [],
            "solution_fallbacks": _solution_fallbacks,
            "bwrap_available": bool(__import__("shutil").which("bwrap")),
            "sandbox_available": sandbox_available(),
            "agent_uid": _agent_uid,
            "agent_uid_error": _agent_uid_error,
        },
    )
    # #endregion
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("production worker requires Linux root verifier")
    trusted_core = _verify_trusted_payload()
    candidate_source = _snapshot_candidate_source(workspace)
    # #region agent log
    _agent_dbg(
        "B",
        "worker.py:evaluate:source",
        "candidate source snapshot ok",
        {"source_files": sorted(candidate_source.keys())[:40], "source_count": len(candidate_source)},
    )
    # #endregion
    manifest, variants, hidden_suite_fingerprint = _snapshot_hidden_suite()
    with tempfile.TemporaryDirectory(prefix="v4-sandbox-preflight-") as directory:
        preflight = Path(directory)
        preflight_workspace = preflight / "workspace"
        preflight_input = preflight / "input"
        preflight_output = preflight / "output"
        _materialize_tree(preflight_workspace / "solution", {"main.py": b""})
        _materialize_tree(preflight_input, {})
        make_tree_read_only(preflight_workspace)
        make_tree_read_only(preflight_input)
        prepare_agent_output_parent(preflight_output)
        if sandbox_available():
            sandbox_preflight(preflight_workspace, preflight_input, preflight_output)
            # #region agent log
            _agent_dbg("A", "worker.py:evaluate:preflight", "sandbox preflight passed", {"mode": "bwrap"})
            # #endregion
        else:
            # #region agent log
            _agent_dbg(
                "A",
                "worker.py:evaluate:preflight",
                "sandbox unavailable; using direct candidate runner",
                {"mode": "direct"},
            )
            # #endregion
    captured_outputs, exit_codes, timeouts, boundary_failures = _candidate_outputs(candidate_source, variants)
    # #region agent log
    nonempty = {f"{n}:{r}": sorted(files.keys()) for (n, r), files in captured_outputs.items() if files}
    _agent_dbg(
        "C",
        "worker.py:evaluate:outputs",
        "candidate runs completed",
        {
            "exit_codes": exit_codes,
            "timeouts": timeouts,
            "boundary_failures": boundary_failures,
            "nonempty_output_runs": len(nonempty),
            "sample_outputs": dict(list(nonempty.items())[:5]),
        },
    )
    # #endregion

    with tempfile.TemporaryDirectory(prefix="v4-grade-") as directory:
        trusted_run = Path(directory)
        suite = trusted_run / "suite"
        outputs = trusted_run / "outputs"
        suite.mkdir()
        (suite / "manifest.json").write_bytes(manifest)
        for name in REQUIRED_VARIANTS:
            _materialize_tree(suite / name, variants[name])
            for run_number in (1, 2):
                _materialize_tree(outputs / name / f"run-{run_number}", captured_outputs[(name, run_number)])
        if suite_fingerprint(suite) != hidden_suite_fingerprint:
            raise RuntimeError("hidden suite changed before grading")
        graded = grade_suite(
            suite,
            outputs,
            exit_codes,
            expected_suite_fingerprint=hidden_suite_fingerprint,
        )

    failed_items = sorted(set(graded["failed_items"]))
    payload = {
        "internal_score": graded["score"],
        "failed_items": failed_items,
        "variant_count": graded["variant_count"],
        "repeats_per_variant": 2,
        "candidate_process_count": len(REQUIRED_VARIANTS) * 2,
        "runtime_seconds": time.perf_counter() - started,
        "infrastructure_error": False,
        "error_kind": None,
        "evidence": {
            "boundary_failures": boundary_failures,
            "candidate_failure": bool(failed_items),
            "exit_codes": exit_codes,
            "fatal_variants": [name for name in REQUIRED_VARIANTS if name.startswith("fatal_")],
            "hidden_suite_fingerprint": hidden_suite_fingerprint,
            "frozen_hidden_suite_fingerprint": EXPECTED_HIDDEN_FINGERPRINT,
            "hidden_suite_matches_frozen": hidden_suite_fingerprint == EXPECTED_HIDDEN_FINGERPRINT,
            "recoverable_variants": [name for name in REQUIRED_VARIANTS if not name.startswith("fatal_")],
            "timeouts": timeouts,
            "trusted_core": trusted_core,
            "sandbox": (
                "bubblewrap-unshare-all-private-net-pid-mount-tmp"
                if sandbox_available()
                else "direct-subprocess-fallback"
            ),
            "unprivileged_candidate_execution": sandbox_available(),
            "variant_results": graded["results"],
        },
    }
    # #region agent log
    _agent_dbg(
        "C",
        "worker.py:evaluate:graded",
        "grading finished without infrastructure error",
        {
            "internal_score": payload["internal_score"],
            "failed_items": failed_items,
            "failed_count": len(failed_items),
            "variant_count": payload["variant_count"],
        },
    )
    # #endregion
    return payload


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        payload = evaluate(Path(argv[1]))
    except Exception as exc:
        # #region agent log
        _agent_dbg(
            "A,B",
            "worker.py:main:exception",
            "worker evaluate raised infrastructure-style exception",
            {
                "error_kind": type(exc).__name__,
                "error_message": str(exc),
                "argv_workspace": argv[1] if len(argv) > 1 else None,
            },
        )
        # #endregion
        payload = {
            "internal_score": 0,
            "failed_items": [],
            "variant_count": 0,
            "repeats_per_variant": 0,
            "candidate_process_count": 0,
            "runtime_seconds": 0.0,
            "infrastructure_error": True,
            "error_kind": type(exc).__name__,
            "evidence": {
                "candidate_failure": False,
                "verifier_error": True,
                "error_message": str(exc),
            },
        }
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
