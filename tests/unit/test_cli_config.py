from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES
from torus import __version__, cli
from torus.cli import main
from torus.config import paths, torus_home
from torus.core.store.migrate import latest_version


def test_torus_home_from_env(tmp_path: Path) -> None:
    assert torus_home({"TORUS_HOME": str(tmp_path / "x")}) == (tmp_path / "x").resolve()


def test_torus_home_defaults_to_user_dir() -> None:
    assert torus_home({}) == (Path.home() / ".torus").resolve()


def test_torus_home_ignores_blank_env() -> None:
    assert torus_home({"TORUS_HOME": "   "}) == (Path.home() / ".torus").resolve()


def test_init_creates_home_dirs_and_db(
    torus_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init"]) == 0
    for sub in ("sources", "reference", "logs"):
        assert (torus_home / sub).is_dir()
    assert (torus_home / "torus.db").is_file()
    assert f"schema v{latest_version()}" in capsys.readouterr().out


def test_init_is_idempotent(torus_home: Path) -> None:
    assert main(["init"]) == 0
    assert main(["init"]) == 0


def test_status_without_db_fails(torus_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 1
    assert "torus init" in capsys.readouterr().err


def test_status_json(torus_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["init"])
    capsys.readouterr()
    assert main(["status", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["schema_version"] == latest_version()
    assert info["counts"]["claims"] == 0
    assert info["queue"] == {}
    assert Path(info["home"]) == paths().home


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_module_entrypoint_runs_in_subprocess(torus_home: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "torus", "init"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (torus_home / "torus.db").is_file()


def _home_with_reference(torus_home: Path) -> None:
    main(["init"])
    shutil.copy(
        FIXTURES / "sec_company_tickers.json", torus_home / "reference" / "sec_company_tickers.json"
    )


def test_resolve_command(torus_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _home_with_reference(torus_home)
    capsys.readouterr()
    assert main(["resolve", "TSLA"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["status"] == "reference"
    assert info["canonical_key"] == "cik:0001318605"
    assert main(["resolve", "TSLA", "--create"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "resolved"
    assert main(["resolve", "XYZQ"]) == 1


def test_detect_command(torus_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _home_with_reference(torus_home)
    main(["resolve", "NVDA", "--create"])
    capsys.readouterr()
    assert main(["detect", "Compare $NVDA with AMZN", "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert [d["surface"] for d in info["detections"]] == ["$NVDA", "AMZN"]
    assert info["detections"][0]["entity_id"] is not None


def test_refresh_reference_needs_contact(
    torus_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TORUS_SEC_CONTACT", raising=False)
    assert main(["init", "--refresh-reference"]) == 2
    assert "--contact" in capsys.readouterr().err


def test_refresh_reference_downloads(
    torus_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Path, str]] = []

    def fake_fetch(dest: Path, contact: str) -> object:
        calls.append((dest, contact))
        shutil.copy(FIXTURES / "sec_company_tickers.json", dest)
        from torus.core.reference import ReferenceData

        return ReferenceData.load(dest)

    monkeypatch.setattr(cli, "fetch_sec_tickers", fake_fetch)
    assert main(["init", "--refresh-reference", "--contact", "me@example.com"]) == 0
    assert calls == [(torus_home / "reference" / "sec_company_tickers.json", "me@example.com")]
    assert "SEC companies" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("env", "mode"),
    [
        ({}, "balanced"),
        ({"TORUS_CAPTURE_MODE": "live"}, "live"),
        ({"CLAUDE_PLUGIN_OPTION_CAPTURE_MODE": "Curator"}, "curator"),
        ({"TORUS_CAPTURE_MODE": "bogus", "CLAUDE_PLUGIN_OPTION_CAPTURE_MODE": "live"}, "live"),
        ({"TORUS_CAPTURE_MODE": ""}, "balanced"),
    ],
)
def test_capture_mode_from_env(env: dict[str, str], mode: str) -> None:
    from torus.config import capture_mode

    assert capture_mode(env).value == mode


def test_graph_explore_doctor_export_import(
    torus_home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home_with_reference(torus_home)
    capsys.readouterr()
    assert main(["graph", "packs"]) == 0
    assert "semis" in capsys.readouterr().out
    assert main(["graph", "seed", "nope"]) == 2
    assert main(["graph", "seed", "semis"]) == 0
    assert "new links" in capsys.readouterr().out
    assert main(["graph", "paths", "ASML", "AMD"]) == 0
    assert "-SUPPLIES->" in capsys.readouterr().out
    assert main(["graph", "neighbors", "NVDA"]) == 0
    assert "starter:semis@1" in capsys.readouterr().out
    assert main(["graph", "paths", "ASML", "XYZQ"]) == 1

    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    out = tmp_path / "explorer.html"
    assert main(["explore", "NVDA", "--hops", "1", "--out", str(out)]) == 0
    assert out.exists() and opened == [out.resolve().as_uri()]
    assert main(["explore", "--no-open"]) == 0
    assert (torus_home / "explorer.html").exists() and len(opened) == 1

    capsys.readouterr()
    assert main(["doctor"]) == 0
    assert "schema" in capsys.readouterr().out

    dump = tmp_path / "dump.jsonl"
    assert main(["export", "--out", str(dump)]) == 0
    assert main(["import", str(dump)]) == 1  # this home is not empty
    monkeypatch.setenv("TORUS_HOME", str(tmp_path / "fresh"))
    assert main(["import", str(dump)]) == 0
    capsys.readouterr()
    assert main(["graph", "paths", "ASML", "AMD"]) == 0
