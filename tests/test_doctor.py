"""Host readiness diagnostics."""

from __future__ import annotations

import pytest

from tsinghua_city import cli


@pytest.mark.parametrize("release,is_wsl", [
    ("23.6.0", False),
    ("10.0.26100", False),
    ("6.6.87.2-microsoft-standard-WSL2", True),
])
def test_doctor_reports_missing_real_runtime(monkeypatch, capsys, tmp_path, release, is_wsl):
    monkeypatch.setenv("CITY_DATABASE_URL", f"sqlite:///{tmp_path / 'doctor.db'}")
    monkeypatch.setenv("CITY_ADMIN_TOKEN", "doctor-token")
    monkeypatch.setenv("CITY_RUNTIME_LOG_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CITY_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("CITY_MANIFEST_DIR", str(tmp_path / "manifests"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli.platform, "release", lambda: release)

    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "[ok] database" in output
    assert "[fail] docker CLI" in output
    assert "[fail] claude CLI" in output
    assert "trace" in output or "traces" in output
    assert ("enable Docker Desktop WSL integration" in output) is is_wsl
