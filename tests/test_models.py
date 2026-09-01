from opencode_manager.models import (
    callback_host_allowed,
    parse_model,
    validate_request_fields,
    validate_session_delete_fields,
)


def _ok(**overrides):
    body = {
        "repo_url": "https://gitlab.example/group/repo.git",
        "source_branch": "develop",
        "prompt": "do the thing",
        "model": "opencode/hy3-free",
        "agent_mode": "orchestrator",
        "timeout_in_seconds": 60,
        "retry_count": 0,
        "jira_id": "PROJ-1",
        "callback_url": "http://127.0.0.1:9/cb",
    }
    body.update(overrides)
    return body


def test_missing_field_is_400():
    body = _ok()
    del body["prompt"]
    assert validate_request_fields(body).startswith("missing")


def test_ssh_rejected():
    assert "SSH" in validate_request_fields(_ok(repo_url="git@gitlab.example:x/y.git"))


def test_bad_model():
    assert "model" in (validate_request_fields(_ok(model="nopath")) or "")


def test_unknown_agent():
    assert "agent_mode" in (validate_request_fields(_ok(agent_mode="codex")) or "")
    assert "agent_mode" in (validate_request_fields(_ok(agent_mode="Plan")) or "")
    assert "agent_mode" in (validate_request_fields(_ok(agent_mode="build")) or "")
    assert "agent_mode" in (validate_request_fields(_ok(agent_mode="plan")) or "")


def test_only_planner_and_orchestrator_are_accepted():
    from opencode_manager.models import JobRequest, normalize_agent_mode

    assert normalize_agent_mode("planner") == "planner"
    assert normalize_agent_mode("orchestrator") == "orchestrator"
    assert normalize_agent_mode("Plan") is None
    assert normalize_agent_mode("build") is None
    assert validate_request_fields(_ok(agent_mode="planner")) is None
    assert validate_request_fields(_ok(agent_mode="orchestrator")) is None
    assert JobRequest.model_validate(_ok(agent_mode="planner")).agent_mode == "planner"
    assert JobRequest.model_validate(_ok(agent_mode="orchestrator")).agent_mode == "orchestrator"
    body = _ok()
    del body["agent_mode"]
    body["working_mode"] = "Plan"
    assert validate_request_fields(body) == "missing required field: agent_mode"


def test_agent_type_field_is_accepted():
    from opencode_manager.models import JobRequest

    body = _ok()
    del body["agent_mode"]
    body["agent_type"] = "planner"
    assert validate_request_fields(body) is None
    req = JobRequest.model_validate({**body, "agent_mode": "planner"})
    assert req.agent_mode == "planner"


def test_bad_callback():
    assert "callback_url" in (validate_request_fields(_ok(callback_url="ftp://x")) or "")


def test_missing_or_empty_callback_is_ok():
    body = _ok()
    del body["callback_url"]
    assert validate_request_fields(body) is None
    assert validate_request_fields(_ok(callback_url="")) is None
    assert validate_request_fields(_ok(callback_url="   ")) is None


def test_n8n_wait_url_is_valid():
    assert (
        validate_request_fields(
            _ok(callback_url="https://n8n.example.com/webhook-waiting/abc-123")
        )
        is None
    )
    assert (
        validate_request_fields(_ok(callback_url="http://192.168.1.10:5678/webhook-waiting/x"))
        is None
    )


def test_callback_host_allowed_star_accepts_all():
    url = "https://n8n.example.com/webhook-waiting/abc"
    assert callback_host_allowed(url, [])
    assert callback_host_allowed(url, ["*"])
    assert callback_host_allowed(url, ["all"])
    assert callback_host_allowed(url, ["*", "n8n.example.com"])
    assert callback_host_allowed("http://127.0.0.1:8090/callback", ["*"])


def test_callback_host_allowed_list_is_ssrf():
    url = "https://n8n.example.com/webhook-waiting/abc"
    assert callback_host_allowed(url, ["n8n.example.com"])
    assert not callback_host_allowed(url, ["other.example"])
    assert callback_host_allowed(url, ["*.example.com"])
    assert callback_host_allowed("https://wait.n8n.cloud/x", ["*.n8n.cloud"])
    assert not callback_host_allowed("https://evil.com/x", ["*.n8n.cloud"])


def test_valid_body():
    assert validate_request_fields(_ok()) is None


def test_source_branch_dash_one_is_missing():
    from opencode_manager.models import JobRequest

    assert validate_request_fields(_ok(source_branch="-1"))
    assert validate_request_fields(_ok(source_branch="  -1  "))
    try:
        JobRequest.model_validate(_ok(source_branch="-1"))
    except Exception as exc:
        assert "source_branch" in str(exc)
    else:
        raise AssertionError("source_branch=-1 must be rejected")


def test_unsafe_jira_id_is_400():
    for jira_id in (".", "..", "PROJ/99", "../etc", "a b"):
        assert validate_request_fields(_ok(jira_id=jira_id)), jira_id


def test_extra_pat_field_is_ignored_on_validate():
    assert validate_request_fields(_ok()) is None
    assert validate_request_fields(_ok(PAT="leftover")) is None


def test_parse_model():
    assert parse_model("opencode/hy3-free") == ("opencode", "hy3-free")


def test_session_delete_fields():
    assert validate_session_delete_fields({"jira_id": "PROJ-1", "session_id": "ses_abc"}) is None
    assert "jira_id" in (validate_session_delete_fields({}) or "")
    assert "session_id" in (validate_session_delete_fields({"jira_id": "PROJ-1"}) or "")
    assert validate_session_delete_fields({"jira_id": "PROJ-1", "session_id": "-1"})
    assert validate_session_delete_fields({"jira_id": "../x", "session_id": "ses_abc"})


def test_inbound_dash_one_session_is_treated_as_none():
    from opencode_manager.models import JobRecord, JobRequest, usable_session_id

    assert usable_session_id("-1") is None
    assert usable_session_id("  -1  ") is None
    assert usable_session_id("") is None
    assert usable_session_id("0") is None
    assert usable_session_id("null") is None
    assert usable_session_id("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") is None
    assert usable_session_id("ses_abc") == "ses_abc"
    req = JobRequest.model_validate(_ok(session_id="-1"))
    assert req.session_id is None
    stored = JobRecord(job_id="job_x", jira_id="ARI-259", session_id="-1")
    assert stored.session_id == ""


def test_retry_count_zero_is_coerced_by_request():
    from opencode_manager.models import JobRequest

    req = JobRequest.model_validate(_ok(retry_count=0))
    assert req.retry_count == 1


def test_job_request_ignores_leftover_pat_field():
    from opencode_manager.models import JobRequest

    req = JobRequest.model_validate(_ok(PAT="leftover-should-drop"))
    assert "PAT" not in req.model_dump()
