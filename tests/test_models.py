from opencode_manager.models import parse_model, validate_request_fields


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


def test_valid_body():
    assert validate_request_fields(_ok()) is None


def test_parse_model():
    assert parse_model("opencode/hy3-free") == ("opencode", "hy3-free")


def test_retry_count_zero_is_coerced_by_request():
    from opencode_manager.models import JobRequest

    req = JobRequest.model_validate(_ok(retry_count=0))
    assert req.retry_count == 1
