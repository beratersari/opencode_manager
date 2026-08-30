from opencode_manager.git.auth import isolated_git_env
from opencode_manager.git.clone import clone_identity
from opencode_manager.git.detect import classify_host


def test_classify_gitlab_default():
    assert classify_host("https://gitlab.example/g/r.git") == "gitlab"


def test_classify_azure():
    assert classify_host("https://dev.azure.com/org/proj/_git/repo") == "tfs"


def test_classify_tfs_path():
    assert classify_host("https://tfs.corp.local/tfs/DefaultCollection/proj/_git/repo") == "tfs"


def test_identity_is_ticket_only():
    assert clone_identity("A-1") == "A-1"
    assert clone_identity("A-1") == clone_identity("A-1")
    assert clone_identity("A-2") == "A-2"
    assert clone_identity("A-1") != clone_identity("A-2")
    assert clone_identity("PROJ/99") == "PROJ_99"


def test_pat_not_on_argv_helper():
    env = isolated_git_env("https://gitlab.example/g/r.git", "SUPERSECRET")
    # PAT is in env config values, never something we put on argv here.
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "credential.helper" in " ".join(
        env[k] for k in env if k.startswith("GIT_CONFIG_KEY_")
    )
    joined_keys = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_KEY_"))
    assert "insteadOf" in joined_keys or "extraHeader" in joined_keys
    argvish = ["git", "clone", "https://gitlab.example/g/r.git"]
    assert "SUPERSECRET" not in " ".join(argvish)


def test_empty_pat_is_anonymous_https():
    env = isolated_git_env("https://github.com/example/public.git", "")
    values = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_VALUE_"))
    keys = " ".join(env[k] for k in env if k.startswith("GIT_CONFIG_KEY_"))
    assert "Authorization" not in values
    assert "insteadOf" not in keys
    assert "credential.helper" in keys
