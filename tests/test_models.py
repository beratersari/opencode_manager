from opencode_manager.models import (
    callback_host_allowed,
    parse_model,
    validate_request_fields,
    validate_session_delete_fields,
)


def _ok(**overrides):
    body = {
        "repo_url": "https://gitlab.example/group/repo.git",
        "PAT": "secret-token",
        "source_branch": "develop",
        "prompt": "do the thing",
        "model": "opencode/hy3-free",
        "agent_mode": "build",
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


def test_unsafe_jira_id_is_400():
    for jira_id in (".", "..", "PROJ/99", "../etc", "a b"):
        assert validate_request_fields(_ok(jira_id=jira_id)), jira_id


def test_missing_or_empty_pat_is_ok():
    body = _ok()
    del body["PAT"]
    assert validate_request_fields(body) is None
    assert validate_request_fields(_ok(PAT="")) is None
    assert validate_request_fields(_ok(PAT="   ")) is None


def test_parse_model():
    assert parse_model("opencode/hy3-free") == ("opencode", "hy3-free")


def test_session_delete_fields():
    assert validate_session_delete_fields({"jira_id": "PROJ-1", "session_id": "ses_abc"}) is None
    assert "jira_id" in (validate_session_delete_fields({}) or "")
    assert "session_id" in (validate_session_delete_fields({"jira_id": "PROJ-1"}) or "")
    assert validate_session_delete_fields({"jira_id": "PROJ-1", "session_id": "-1"})
    assert validate_session_delete_fields({"jira_id": "../x", "session_id": "ses_abc"})


def test_retry_count_zero_is_coerced_by_request():
    from opencode_manager.models import JobRequest

    req = JobRequest.model_validate(_ok(retry_count=0))
    assert req.retry_count == 1


def test_job_request_allows_omitted_pat():
    from opencode_manager.models import JobRequest

    body = _ok()
    del body["PAT"]
    req = JobRequest.model_validate(body)
    assert req.PAT == ""
