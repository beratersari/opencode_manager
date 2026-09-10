from __future__ import annotations

import re
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class IdentityError(ValueError):
    pass


def mr_key(project_id: int, mr_iid: int) -> str:
    key = f"{int(project_id)}-{int(mr_iid)}"
    if not _SAFE.match(key):
        raise IdentityError(f"unsafe mr_key {key!r}")
    return key


def parse_mr_key(key: str) -> tuple[int, int]:
    project_s, _, iid_s = (key or "").partition("-")
    return int(project_s), int(iid_s)


def clone_path_for(work_dir: Path, key: str) -> Path:
    if not _SAFE.match(key or ""):
        raise IdentityError(f"unsafe mr_key {key!r}")
    dest = Path(work_dir) / key
    root = Path(work_dir).resolve()
    resolved = dest.resolve()
    if resolved == root or root not in resolved.parents:
        raise IdentityError(f"clone path {resolved} is not under {root}")
    return dest
