"""Fingerprint the exact packaged sources, including uncommitted edits."""

import hashlib
import subprocess
from pathlib import Path


def source_hash(root: Path) -> str:
    files = [p for p in (root / "pyproject.toml", root / "README.md") if p.is_file()]
    files += [
        p
        for p in (root / "tsinghua_city").rglob("*")
        if p.is_file() and p.suffix in {".py", ".html"} and "__pycache__" not in p.parts
    ]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def verify_image(image: str, root: Path) -> str:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            image,
            "-m",
            "tsinghua_city.source_fingerprint",
            "/opt/city",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    expected = source_hash(root)
    if result.returncode or result.stdout.strip() != expected:
        raise ValueError(
            f"Docker image {image!r} must be built from the current working source. "
            f"Run: docker build -f deploy/agent.Dockerfile -t {image} . "
            "Then set CITY_DOCKER_IMAGE to that image and retry."
        )
    return expected


if __name__ == "__main__":
    import sys

    print(source_hash(Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()))
