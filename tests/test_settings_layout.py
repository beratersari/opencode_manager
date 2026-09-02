from pathlib import Path

import pytest

from opencode_manager.settings import Settings, load_settings


def test_yaml_data_dir_derives_all_paths(tmp_path: Path) -> None:
    yaml = tmp_path / "settings.yaml"
    root = tmp_path / "osm"
    yaml.write_text(f"data_dir: {root}\nlisten_port: 8099\n", encoding="utf-8")
    s = load_settings(yaml)
    assert s.data_dir == root
    assert s.work_dir == root / ".temp"
    assert s.job_log_dir == root / "logs"
    assert s.job_store_dir == root / "jobs"
    assert s.queue_path == root / "queue.json"
    assert s.serve_dir == root / ".serve"
    assert s.app_log_path == root / "logs" / "app.log"
    assert s.listen_port == 8099


def test_explicit_work_dir_keeps_serve_next_to_clones(tmp_path: Path) -> None:
    s = Settings(work_dir=tmp_path / "work", job_log_dir=tmp_path / "joblogs")
    assert s.work_dir == tmp_path / "work"
    assert s.serve_dir == tmp_path / "work" / ".serve"
    assert s.app_log_path == tmp_path / "joblogs" / "app.log"


def test_ensure_dirs_permission_error_mentions_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(data_dir=tmp_path / "blocked")

    def boom(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(PermissionError, match="settings.local.yaml"):
        s.ensure_dirs()
