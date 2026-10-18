"""Thin wrapper around docker compose, so the CLI can build and run the world."""

from __future__ import annotations

import subprocess
from pathlib import Path


class DockerError(RuntimeError):
    pass


def _compose(
    root: Path,
    args: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
    capture: bool = False,
    compose_file: Path | None = None,
) -> subprocess.CompletedProcess:
    command = ["docker", "compose"]
    if compose_file is not None:
        command.extend(["-f", str(compose_file)])
    command.extend(args)
    try:
        process = subprocess.run(
            command,
            cwd=str(root),
            env=env,
            text=True,
            capture_output=capture,
            timeout=1800,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerError(
            "docker CLI not found on PATH; install Docker Engine + compose plugin "
            "(Linux) or Docker Desktop (Windows/macOS)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"docker compose timed out: {' '.join(args)}") from exc
    if check and process.returncode != 0:
        detail = ((process.stderr or "") + "\n" + (process.stdout or "")).strip()
        raise DockerError(
            f"docker compose {' '.join(args)} failed (exit {process.returncode}):\n{detail[-3000:]}"
        )
    return process


def compose_version() -> str | None:
    try:
        process = subprocess.run(
            ["docker", "compose", "version"],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if process.returncode != 0:
        return None
    return (process.stdout or process.stderr).strip()


def daemon_ok() -> tuple[bool, str]:
    try:
        process = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if process.returncode != 0:
        return False, (process.stderr or "docker daemon not reachable").strip()
    return True, (process.stdout or "").strip()


def build(root: Path, env: dict[str, str], *, compose_file: Path | None = None) -> None:
    _compose(root, ["build"], env=env, compose_file=compose_file)


def up(root: Path, env: dict[str, str], *, compose_file: Path | None = None) -> None:
    _compose(root, ["up", "-d", "--remove-orphans"], env=env, compose_file=compose_file)


def down(
    root: Path,
    env: dict[str, str],
    *,
    check: bool = False,
    compose_file: Path | None = None,
) -> subprocess.CompletedProcess:
    return _compose(
        root,
        ["down", "--remove-orphans"],
        env=env,
        check=check,
        compose_file=compose_file,
    )


def ps(root: Path, env: dict[str, str], *, compose_file: Path | None = None) -> str:
    process = _compose(root, ["ps"], env=env, check=False, capture=True, compose_file=compose_file)
    return (process.stdout or "") + (process.stderr or "")


def container_logs(
    root: Path,
    env: dict[str, str],
    service: str,
    tail: int = 100,
    *,
    compose_file: Path | None = None,
) -> str:
    process = _compose(
        root,
        ["logs", "--no-color", "--tail", str(tail), service],
        env=env,
        check=False,
        capture=True,
        compose_file=compose_file,
    )
    return (process.stdout or "") + (process.stderr or "")


def is_available() -> bool:
    return compose_version() is not None
