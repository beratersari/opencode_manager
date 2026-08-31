"""Static check: n8nflow.json $() expressions must name real nodes."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "n8nflow.json"
_NODE_REF = re.compile(r"\$\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _flow() -> dict:
    return json.loads(FLOW.read_text(encoding="utf-8"))


def test_n8nflow_is_json_with_nodes_and_connections() -> None:
    data = _flow()
    assert data.get("nodes")
    assert data.get("connections")


def test_every_expression_node_ref_exists() -> None:
    data = _flow()
    names = {str(n["name"]) for n in data["nodes"]}
    missing: list[str] = []
    for node in data["nodes"]:
        blob = json.dumps(node, ensure_ascii=False)
        for match in _NODE_REF.finditer(blob):
            target = match.group(1)
            if target not in names:
                missing.append(f"{node['name']} -> {target}")
    assert missing == [], "n8n $() refs a node that is not on the canvas:\n" + "\n".join(missing)


def test_connections_only_name_existing_nodes() -> None:
    data = _flow()
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
    assert bad == [], "n8n connections name missing nodes:\n" + "\n".join(bad)


def test_job_path_polls_osm_jobs_not_callback() -> None:
    data = _flow()
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


def test_session_delete_branch_targets_osm_sessions() -> None:
    data = _flow()
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
