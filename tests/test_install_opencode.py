"""Offline OpenCode vendor copy."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "install_opencode.py"
    spec = importlib.util.spec_from_file_location("install_opencode", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_wipes_old_and_copies_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    old_home = home / ".opencode"
    old_home.mkdir(parents=True)
    (old_home / "bin").mkdir()
    (old_home / "bin" / "opencode").write_bytes(b"OLD")
    (old_home / "stale.txt").write_text("leftover", encoding="utf-8")

    root = tmp_path / "pkg"
    src = root / "vendor" / "bin"
    src.mkdir(parents=True)
    binary = src / "opencode"
    binary.write_bytes(b"#!/bin/sh\necho ok\n")

    inst = _load()
    monkeypatch.setattr(inst, "home", lambda: home)
    monkeypatch.setattr(inst, "prepend_user_path", lambda _d: None)

    target = inst.install(root)
    assert target == old_home / "bin" / "opencode"
    assert target.read_bytes().startswith(b"#!/bin/sh")
    assert not (old_home / "stale.txt").exists()
    cfg = (old_home / "opencode.json").read_text(encoding="utf-8")
    assert '"plugin": []' in cfg
    assert '"autoupdate": false' in cfg


def test_install_missing_vendor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inst = _load()
    monkeypatch.setattr(inst, "home", lambda: tmp_path / "home")
    with pytest.raises(FileNotFoundError):
        inst.install(tmp_path)


def test_existing_locations_detects_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".opencode").mkdir(parents=True)
    inst = _load()
    monkeypatch.setattr(inst, "home", lambda: home)
    found = inst.existing_locations()
    assert found == [home / ".opencode"]
