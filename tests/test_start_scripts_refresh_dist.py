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


def test_start_scripts_probe_unauthenticated_auth_not_meta() -> None:
    """Shipped overlay makes GET /api/meta 401. Wait loops must use /api/auth."""
    files = [
        ROOT / "scripts" / "start.sh",
        ROOT / "scripts" / "start-backend.bat",
        ROOT / "scripts" / "start-frontend.sh",
        ROOT / "scripts" / "start-frontend.bat",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "/api/auth" in text, path.name
        assert "urlopen" not in text or "/api/auth" in text
        if "Invoke-WebRequest" in text or "curl -sf" in text:
            assert "/api/auth" in text
            # Health wait must not require 2xx from /api/meta.
            wait = text
            if "Waiting" in text:
                wait = text.split("Waiting", 1)[-1]
            elif "Checking backend" in text:
                wait = text.split("Checking backend", 1)[-1]
            assert "/api/meta" not in wait.split("echo", 1)[0]
