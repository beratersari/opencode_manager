"""Static check: n8n-callback.json and n8n-poller.json $() refs are real nodes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_NODE_REF = re.compile(r"\$\(\s*['\"]([^'\"]+)['\"]\s*\)")
FLOWS = (
    ROOT / "n8n-callback.json",
    ROOT / "n8n-poller.json",
)


def _flow(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.name)
def test_n8n_flow_is_json_with_nodes_and_connections(path: Path) -> None:
    data = _flow(path)
    assert data.get("nodes")
    assert data.get("connections")


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.name)
def test_every_expression_node_ref_exists(path: Path) -> None:
    data = _flow(path)
    names = {str(n["name"]) for n in data["nodes"]}
    missing: list[str] = []
    for node in data["nodes"]:
        blob = json.dumps(node, ensure_ascii=False)
        for match in _NODE_REF.finditer(blob):
            target = match.group(1)
            if target not in names:
                missing.append(f"{node['name']} -> {target}")
    assert missing == [], f"{path.name} $() refs a node that is not on the canvas:\n" + "\n".join(
        missing
    )


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.name)
def test_connections_only_name_existing_nodes(path: Path) -> None:
    data = _flow(path)
    names = {str(n["name"]) for n in data["nodes"]}
    bad: list[str] = []
    for src, ports in data["connections"].items():
        if src not in names:
            bad.append(f"source {src}")
        for lanes in ports.values():
            for lane in lanes:
                for edge in lane:
                    dest = edge.get("node")
                    if dest not in names:
                        bad.append(f"{src} -> {dest}")
    assert bad == [], f"{path.name} connections name missing nodes:\n" + "\n".join(bad)


def test_callback_flow_uses_wait_webhook() -> None:
    data = _flow(ROOT / "n8n-callback.json")
    assert data.get("name") == "n8n-callback"
    names = {str(n["name"]) for n in data["nodes"]}
    assert "waitForOsmCallback" in names
    assert "waitPollInterval" not in names
    build = next(n for n in data["nodes"] if n["name"] == "buildOsmRequest")
    assert "callback_url" in str(build["parameters"].get("jsCode") or "")
    wait = next(n for n in data["nodes"] if n["name"] == "waitForOsmCallback")
    assert wait["parameters"].get("resume") == "webhook"


def test_poller_flow_polls_osm_jobs_not_callback() -> None:
    data = _flow(ROOT / "n8n-poller.json")
    assert data.get("name") == "n8n-poller"
    names = {str(n["name"]) for n in data["nodes"]}
    assert {"waitPollInterval", "pollJobStatus", "normalizePoll", "stillInProgress"} <= names
    assert "waitForOsmCallback" not in names
    build = next(n for n in data["nodes"] if n["name"] == "buildOsmRequest")
    code = str(build["parameters"].get("jsCode") or "")
    assert "callback_url" not in code
    poll = next(n for n in data["nodes"] if n["name"] == "pollJobStatus")
    url = str(poll["parameters"].get("url") or "")
    assert "/jobs/" in url
    assert poll["parameters"].get("method") == "GET"
    wait = next(n for n in data["nodes"] if n["name"] == "waitPollInterval")
    assert wait["parameters"].get("resume") == "timeInterval"
    loop = data["connections"]["stillInProgress"]["main"]
    assert loop[0][0]["node"] == "waitPollInterval"
    assert loop[1][0]["node"] == "isCallback200"
    still = next(n for n in data["nodes"] if n["name"] == "stillInProgress")
    expr = str(still["parameters"]["conditions"]["conditions"][0]["leftValue"])
    assert "poll_max_seconds" in expr
    assert "live" in expr


@pytest.mark.parametrize("path", FLOWS, ids=lambda p: p.name)
def test_session_delete_branch_targets_osm_sessions(path: Path) -> None:
    data = _flow(path)
    names = {str(n["name"]) for n in data["nodes"]}
    assert {"isSessionDeleted1", "deleteSession1", "parseDeleteReturnInfo1"} <= names
    delete = next(n for n in data["nodes"] if n["name"] == "deleteSession1")
    url = str(delete["parameters"].get("url") or "")
    assert "/sessions" in url
    assert "/session/{{" not in url
    assert delete["parameters"].get("method") == "DELETE"
    lanes = data["connections"]["isSessionDeleted1"]["main"]
    assert lanes[0][0]["node"] == "deleteSession1"
    assert lanes[1][0]["node"] == "parseAllNeedInfo1"
    parse = next(n for n in data["nodes"] if n["name"] == "parseDeleteReturnInfo1")
    js = str(parse["parameters"].get("jsCode") or "")
    assert "ok ? 'deleted' : 'updated'" in js
    assert "is_success]: ok" in js
