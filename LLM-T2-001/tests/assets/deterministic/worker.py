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

    Parquet bytes can differ across pyarrow wheel builds even at the same package
    version, so the frozen EXPECTED hash is advisory; within a run we pin the
    fingerprint that generation actually produced.
    """
    with tempfile.TemporaryDirectory(prefix="v4-suite-") as directory:
        suite = Path(directory) / "suite"
        generate_hidden_suite(suite)
        actual = suite_fingerprint(suite)
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
    """Ensure workspace/solution exists (oracle deploy, /solution, or bundled fallback)."""
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
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("production worker requires Linux root verifier")
    trusted_core = _verify_trusted_payload()
    candidate_source = _snapshot_candidate_source(workspace)
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
    captured_outputs, exit_codes, timeouts, boundary_failures = _candidate_outputs(candidate_source, variants)

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
    return {
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


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    try:
        payload = evaluate(Path(argv[1]))
    except Exception as exc:
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
