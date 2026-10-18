#!/usr/bin/env python3
"""Fail if the git index contains secrets or experiment run artifacts.

Intended to be run before every commit/push:

    python scripts/check_hygiene.py

It inspects the files git would publish (git ls-files), not the whole working
tree, so ignored .env / runs/ content is allowed to stay local.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

FORBIDDEN_PARTS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".research",
    "runs",
}
FORBIDDEN_SUFFIXES = {
    ".pyc",
    ".log",
    ".jsonl",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"ANTHROPIC_AUTH_TOKEN\s*=\s*(?!sk-REPLACE_ME|sk-你的-key)\S{16,}"),
]
ALLOWED_ENV_FILE = ".env.example"


def repo_root() -> Path:
    output = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True)
    return Path(output.strip())


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return [item.decode("utf-8", "replace") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    try:
        root = repo_root()
        files = tracked_files(root)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"cannot list tracked files (run inside the git repo): {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    for rel in files:
        path = root / rel
        name = path.name
        if name == ".env" or (name.startswith(".env.") and name != ALLOWED_ENV_FILE):
            problems.append(f"tracked env file: {rel}")
        if FORBIDDEN_PARTS & set(path.parts):
            problems.append(f"tracked local/run artifact: {rel}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"tracked log/data/credential file: {rel}")
        if path.exists() and path.stat().st_size <= 2 * 1024 * 1024:
            blob = path.read_bytes()
            if b"\0" in blob:
                continue
            text = blob.decode("utf-8", "replace")
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    problems.append(f"possible secret pattern in {rel}")
                    break

    if problems:
        print("repo hygiene check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nOnly placeholders belong in .env.example; real keys and runs/ stay local.",
            file=sys.stderr,
        )
        return 1

    print(f"repo hygiene check passed ({len(files)} tracked files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
