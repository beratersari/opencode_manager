from opencode_manager.git.auth import isolated_git_env
from opencode_manager.git.clone import clone_identity
from opencode_manager.git.detect import classify_host


def test_classify_gitlab_default():
    assert classify_host("https://gitlab.example/g/r.git") == "gitlab"


def test_classify_azure():
    assert classify_host("https://dev.azure.com/org/proj/_git/repo") == "tfs"


def test_classify_tfs_path():
    assert classify_host("https://tfs.corp.local/tfs/DefaultCollection/proj/_git/repo") == "tfs"


def test_identity_stable_and_unique():
    a = clone_identity("A-1", "https://h/r.git", "dev")
    b = clone_identity("A-1", "https://h/r.git", "dev")
    c = clone_identity("A-2", "https://h/r.git", "dev")
    assert a == b
    assert a != c


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
