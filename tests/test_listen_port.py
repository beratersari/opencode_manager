"""Exe startup may kill a leftover listener on listen_port."""

from __future__ import annotations

from opencode_manager.cleanup.port import (
    free_listen_port,
    local_port,
    parse_netstat_listening_pids,
    parse_ss_listening_pids,
    port_is_busy,
)


def test_local_port_parses_ipv4_and_ipv6() -> None:
    assert local_port("0.0.0.0:4096") == 4096
    assert local_port("127.0.0.1:5173") == 5173
    assert local_port("[::]:4096") == 4096
    assert local_port("[::1]:4096") == 4096
    assert local_port("0.0.0.0:40960") == 40960
    assert local_port("bad") is None


def test_parse_netstat_listening_pids_ignores_other_ports() -> None:
    output = """
  TCP    0.0.0.0:4096           0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:40960        0.0.0.0:0              LISTENING       99
  TCP    [::]:4096              [::]:0                 LISTENING       4242
  TCP    127.0.0.1:4096         127.0.0.1:50000        ESTABLISHED     77
  UDP    0.0.0.0:4096           *:*                                    88
"""
    assert parse_netstat_listening_pids(output, 4096) == [4242]


def test_parse_ss_listening_pids() -> None:
    output = (
        'LISTEN 0 2048 0.0.0.0:4096 0.0.0.0:* users:(("osm",pid=4242,fd=7))\n'
        "LISTEN 0 2048 127.0.0.1:5173 0.0.0.0:* users:((\"python\",pid=99,fd=8))\n"
    )
    assert parse_ss_listening_pids(output, 4096) == [4242]


def test_free_listen_port_no_op_when_free(monkeypatch) -> None:
    monkeypatch.setattr("opencode_manager.cleanup.port.port_is_busy", lambda *_a, **_k: False)
    called = {"n": 0}
    monkeypatch.setattr(
        "opencode_manager.cleanup.port.kill_pid",
        lambda *_a, **_k: called.__setitem__("n", called["n"] + 1),
    )
    assert free_listen_port("127.0.0.1", 4096) == []
    assert called["n"] == 0


def test_free_listen_port_kills_holder_via_may_kill(monkeypatch) -> None:
    busy = {"n": 1}

    def is_busy(*_a, **_k) -> bool:
        return busy["n"] > 0

    killed: list[int] = []

    def fake_kill(pid):  # noqa: ANN001
        killed.append(int(pid))
        busy["n"] = 0

    monkeypatch.setattr("opencode_manager.cleanup.port.port_is_busy", is_busy)
    monkeypatch.setattr("opencode_manager.cleanup.port.pids_listening_on", lambda *_a, **_k: [4, 4242, 8])
    monkeypatch.setattr("opencode_manager.cleanup.port.may_kill", lambda pid: int(pid) == 4242)
    monkeypatch.setattr("opencode_manager.cleanup.port.kill_pid", fake_kill)
    assert free_listen_port("0.0.0.0", 4096, wait_seconds=0.2) == [4242]
    assert killed == [4242]


def test_port_is_busy_false_on_ephemeral() -> None:
    assert port_is_busy("127.0.0.1", 0) is False
