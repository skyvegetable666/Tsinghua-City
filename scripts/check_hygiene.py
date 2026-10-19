"""Fail if tracked files contain obvious secrets or local run artifacts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(
    r"(^|/)(\.env$|runs/|.*\.db$|.*\.sqlite3?$|.*\.log$|.*\.jsonl$|"
    r".*\.pem$|.*\.key$|__pycache__/|\.venv/)"
)
SECRETS = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"
)


def tracked() -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "--cached", "--others",
                                      "--exclude-standard", "-z"], cwd=ROOT)
    return [item for item in output.decode("utf-8").split("\0") if item]


def main() -> int:
    problems: list[str] = []
    for relative in tracked():
        if FORBIDDEN.search(relative):
            problems.append(f"forbidden tracked path: {relative}")
            continue
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if SECRETS.search(text):
            problems.append(f"possible secret in tracked file: {relative}")
    if problems:
        print("repo hygiene check failed:")
        for problem in problems:
            print("  - " + problem)
        return 1
    print(f"repo hygiene check passed ({len(tracked())} non-ignored source files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
