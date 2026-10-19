"""Host readiness diagnostics."""

from __future__ import annotations

from tsinghua_city import cli


def test_doctor_reports_missing_real_runtime(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CITY_DATABASE_URL", f"sqlite:///{tmp_path / 'doctor.db'}")
    monkeypatch.setenv("CITY_ADMIN_TOKEN", "doctor-token")
    monkeypatch.setenv("CITY_RUNTIME_LOG_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CITY_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("CITY_MANIFEST_DIR", str(tmp_path / "manifests"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "[ok] database" in output
    assert "[fail] docker CLI" in output
    assert "[fail] claude CLI" in output
    assert "trace" in output or "traces" in output
