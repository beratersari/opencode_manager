"""Two jobs must not share a serve port; health must be this child."""

from __future__ import annotations

import pytest

from opencode_manager.opencode import serve as serve_mod
from opencode_manager.opencode.serve import reserve_port, release_port, wait_health


def test_reserve_port_skips_a_port_already_held(monkeypatch) -> None:
    seq = iter([41000, 41000, 41001])
    monkeypatch.setattr(serve_mod, "free_port", lambda: next(seq))
    first = reserve_port()
    second = reserve_port()
    try:
        assert first == 41000
        assert second == 41001
        assert first != second
    finally:
        release_port(first)
        release_port(second)


def test_two_threads_never_reserve_the_same_port() -> None:
    import threading

    got: list[int] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        port = reserve_port()
        got.append(port)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert len(got) == 8
        assert len(set(got)) == 8
    finally:
        for port in got:
            release_port(port)


def test_wait_health_rejects_dead_child_even_if_http_200(monkeypatch) -> None:
    class Dead:
        def poll(self) -> int:
            return 1

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"healthy": True}

    class FakeClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ARG002
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def get(self, *a, **k):  # noqa: ANN002, ARG002
            return FakeResp()

    monkeypatch.setattr(serve_mod.httpx, "Client", FakeClient)
    with pytest.raises(RuntimeError, match="exited"):
        wait_health("http://127.0.0.1:9", "/tmp", timeout=2.0, proc=Dead())


def test_wait_health_ignores_200_from_other_listener(monkeypatch) -> None:
    class Live:
        pid = 4242

        def poll(self):
            return None

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"healthy": True}

    class FakeClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ARG002
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def get(self, *a, **k):  # noqa: ANN002, ARG002
            return FakeResp()

    monkeypatch.setattr(serve_mod.httpx, "Client", FakeClient)
    monkeypatch.setattr(serve_mod, "pids_listening_on", lambda port: [9999])
    with pytest.raises(TimeoutError):
        wait_health("http://127.0.0.1:9", "/tmp", timeout=0.5, proc=Live(), port=9)


def test_wait_health_accepts_live_child(monkeypatch) -> None:
    class Live:
        def poll(self):
            return None

    class FakeResp:
        status_code = 200

        def json(self) -> dict:
            return {"healthy": True}

    class FakeClient:
        def __init__(self, *a, **k):  # noqa: ANN002, ARG002
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            return False

        def get(self, *a, **k):  # noqa: ANN002, ARG002
            return FakeResp()

    monkeypatch.setattr(serve_mod.httpx, "Client", FakeClient)
    monkeypatch.setattr(serve_mod, "pids_listening_on", lambda port: [77])

    class LivePid:
        pid = 77

        def poll(self):
            return None

    assert wait_health("http://127.0.0.1:9", "/tmp", timeout=2.0, proc=LivePid()) == {
        "healthy": True
    }
