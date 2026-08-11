"""Bubblewrap boundary for each production hidden Candidate execution."""
from __future__ import annotations

import os
import pwd
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class SandboxConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateRun:
    exit_code: int
    timed_out: bool


def sandbox_available() -> bool:
    """True when bubblewrap + agent uid 1000 are present (task Dockerfile image)."""
    if shutil.which("bwrap") is None:
        return False
    try:
        account = pwd.getpwnam("agent")
    except KeyError:
        return False
    return account.pw_uid == 1000


def prepare_agent_output_parent(path: Path) -> None:
    if sandbox_available():
        account = pwd.getpwnam("agent")
        if account.pw_uid != 1000:
            raise SandboxConfigurationError("agent UID must be 1000")
        path.mkdir(parents=False, exist_ok=False)
        os.chown(path, account.pw_uid, account.pw_gid)
        path.chmod(0o700)
        return
    # Harbor images that ignore the task Dockerfile still need a writable output parent.
    path.mkdir(parents=False, exist_ok=False)
    path.chmod(0o700)


def make_tree_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _bubblewrap_prefix(workspace: Path, input_dir: Path, output_parent: Path) -> list[str]:
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        raise SandboxConfigurationError("bubblewrap is unavailable")
    command = [
        bubblewrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ]
    for system_root in (Path("/usr"), Path("/lib"), Path("/lib64")):
        if system_root.exists():
            command.extend(("--ro-bind", str(system_root), str(system_root)))
    command.extend((
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--ro-bind", str(workspace), "/app",
        "--ro-bind", str(input_dir), "/input",
        "--bind", str(output_parent), "/sandbox",
        "--chdir", "/app",
        "--uid", "1000",
        "--gid", "1000",
        "--cap-drop", "ALL",
        "--setenv", "HOME", "/tmp",
        "--setenv", "PATH", "/usr/local/bin:/usr/bin",
        "--setenv", "PYTHONHASHSEED", "0",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PYTHONPATH", "/app",
        "--setenv", "TZ", "UTC",
    ))
    return command


def sandbox_preflight(workspace: Path, input_dir: Path, output_parent: Path) -> None:
    """Prove UID, mount, network, and namespace isolation before hidden runs."""
    if not sandbox_available():
        raise SandboxConfigurationError("sandbox prerequisites unavailable")
    probe = (
        "import os,pathlib,socket; "
        "assert os.getuid()==1000; "
        "assert not pathlib.Path('/tests').exists(); "
        "assert not pathlib.Path('/solution').exists(); "
        "s=socket.socket(); s.settimeout(0.2); "
        "r=s.connect_ex(('198.51.100.1', 9)); assert r != 0"
    )
    completed = subprocess.run(
        _bubblewrap_prefix(workspace, input_dir, output_parent)
        + [sys.executable, "-c", probe],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
        start_new_session=True,
    )
    if completed.returncode != 0:
        raise SandboxConfigurationError(
            f"bubblewrap preflight failed with exit {completed.returncode}"
        )


def _run_candidate_direct(
    workspace: Path,
    input_dir: Path,
    output_dir: Path,
    *,
    timeout: int = 60,
) -> CandidateRun:
    """Fallback runner when the Harbor image lacks bubblewrap/agent (no task Dockerfile)."""
    # Candidate contract uses mkdir(exist_ok=False); only the parent may exist (same as bwrap).
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    env = {
        **os.environ,
        "HOME": "/tmp",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(workspace),
        "TZ": "UTC",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "solution.main",
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
        ],
        cwd=str(workspace),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        return CandidateRun(exit_code=process.wait(timeout=timeout), timed_out=False)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return CandidateRun(exit_code=124, timed_out=True)


def run_candidate_cli(
    workspace: Path,
    input_dir: Path,
    output_dir: Path,
    *,
    timeout: int = 60,
) -> CandidateRun:
    """Run one Candidate; prefer bubblewrap, else direct subprocess on incomplete images."""
    workspace = Path(workspace).resolve()
    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise SandboxConfigurationError("production verifier must run as root on Linux")
    if not workspace.is_dir() or workspace.is_symlink():
        return CandidateRun(exit_code=127, timed_out=False)

    if not sandbox_available():
        return _run_candidate_direct(workspace, input_dir, output_dir, timeout=timeout)

    command = _bubblewrap_prefix(workspace, input_dir, output_dir.parent) + [
        sys.executable,
        "-m",
        "solution.main",
        "--input-dir",
        "/input",
        "--output-dir",
        "/sandbox/output",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return CandidateRun(exit_code=process.wait(timeout=timeout), timed_out=False)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return CandidateRun(exit_code=124, timed_out=True)
