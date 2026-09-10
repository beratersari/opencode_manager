from __future__ import annotations

from pathlib import Path

import pytest

from opencode_manager.workspace.identity import IdentityError, clone_path_for, mr_key


def test_mr_key_and_path(tmp_path: Path):
    key = mr_key(42, 7)
    assert key == "42-7"
    dest = clone_path_for(tmp_path, key)
    assert dest.parent == tmp_path.resolve() or dest.parent == tmp_path
    assert dest.name == "42-7"
    assert tmp_path.resolve() in dest.resolve().parents


def test_rejects_unsafe():
    with pytest.raises(IdentityError):
        clone_path_for(Path("/tmp/work"), "../etc")
    with pytest.raises(IdentityError):
        clone_path_for(Path("/tmp/work"), "42/7")
