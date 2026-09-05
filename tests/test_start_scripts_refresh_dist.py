"""Start scripts rebuild web/dist when local Vite exists."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_start_scripts_refresh_web_dist_before_serve() -> None:
    lib = (ROOT / "scripts" / "osm-lib.sh").read_text(encoding="utf-8")
    assert "osm_refresh_web_dist" in lib
    assert 'web/node_modules/.bin/vite' in lib
    backend = (ROOT / "scripts" / "start-backend.sh").read_text(encoding="utf-8")
    assert "/var/lib/osm" in backend
    assert "wrapper-exit.log" in backend
    frontend = (ROOT / "scripts" / "start-frontend.sh").read_text(encoding="utf-8")
    assert "osm_refresh_web_dist" in backend
    assert "osm_refresh_web_dist" in frontend
    win_be = (ROOT / "scripts" / "start-backend.bat").read_text(encoding="utf-8")
    win_fe = (ROOT / "scripts" / "start-frontend.bat").read_text(encoding="utf-8")
    assert "vite.cmd" in win_be
    assert "vite.cmd" in win_fe
    assert "run-backend.bat" in win_be
    assert 'cmd /v:on /c' not in win_be
    runner = (ROOT / "scripts" / "run-backend.bat").read_text(encoding="utf-8")
    assert "opencode_manager.app" in runner
    assert "wrapper-exit.log" in runner
    assert r"C:\osm\logs\wrapper-exit.log" in runner
