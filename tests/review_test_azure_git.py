import base64
import os
import subprocess
from pathlib import Path

from opencode_manager.azure.auth import azure_basic_auth
from opencode_manager.azure.client import _is_git_http, _is_http, _ssh_to_https
from opencode_manager.workspace.gitops import inject_token, isolated_git_env


def test_azure_token_uses_pat_user_not_oauth2():
    url = "https://ado.example/tfs/DefaultCollection/App/_git/app"
    got = inject_token(url, "secret-pat", scheme="azure")
    assert "pat:secret-pat@" in got
    assert "oauth2:" not in got
    gitlab = inject_token(url, "secret-pat")
    assert "oauth2:secret-pat@" in gitlab
    assert inject_token(url, "") == url


def test_azure_basic_auth_is_not_empty_username():
    header = azure_basic_auth("secret-pat")
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("ascii")
    assert decoded == "pat:secret-pat"
    assert not decoded.startswith(":")


def test_inject_token_encodes_pat_special_chars():
    url = "https://tfs02.company.com.tr/tfs/ExampleCollection/Example%20Projeleri/_git/ProjectX"
    got = inject_token(url, "ab+c/d=", scheme="azure")
    assert "pat:ab%2Bc%2Fd%3D@" in got


def test_azure_git_env_sends_basic_header_and_askpass(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    env = isolated_git_env("secret-pat", auth_scheme="azure")
    assert env["CREASY_AZURE_GIT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    decoded = base64.b64decode(env["GIT_CONFIG_VALUE_0"].split(" ", 2)[2]).decode("ascii")
    assert decoded == "pat:secret-pat"
    assert env["GIT_ASKPASS"] != "echo"
    assert Path(env["GIT_ASKPASS"]).is_file()
    gitlab = isolated_git_env("secret-pat")
    assert gitlab["GIT_ASKPASS"] == "echo"
    assert "GIT_CONFIG_KEY_0" not in gitlab


def test_azure_askpass_prints_pat_user_then_token(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    env = isolated_git_env("secret-pat", auth_scheme="azure")
    path = Path(env["GIT_ASKPASS"])
    if os.name == "nt":
        user = subprocess.run(["cmd", "/c", str(path), "Username for 'https://tfs'"], capture_output=True, text=True, env=env)
        password = subprocess.run(["cmd", "/c", str(path), "Password for 'https://tfs'"], capture_output=True, text=True, env=env)
    else:
        user = subprocess.run(["sh", str(path), "Username for 'https://tfs'"], capture_output=True, text=True, env=env)
        password = subprocess.run(["sh", str(path), "Password for 'https://tfs'"], capture_output=True, text=True, env=env)
    assert user.returncode == 0
    assert password.returncode == 0
    assert user.stdout.strip() == "pat"
    assert password.stdout.strip() == "secret-pat"


def test_api_url_is_not_a_git_clone_url():
    assert _is_http("https://tfs02.company.com.tr/tfs/ExampleCollection/_apis/git/repositories/x")
    assert not _is_git_http("https://tfs02.company.com.tr/tfs/ExampleCollection/_apis/git/repositories/x")
    assert _is_git_http("https://tfs02.company.com.tr/tfs/ExampleCollection/Example%20Projeleri/_git/ProjectX")


def test_ssh_remote_converts_to_https():
    assert _is_http("https://ado.example/col/App/_git/app")
    assert not _is_http("ssh://ado.example/col/App/_git/app")
    assert _ssh_to_https("git@ado.example:tfs/DefaultCollection/App/_git/app") == (
        "https://ado.example/tfs/DefaultCollection/App/_git/app"
    )
    assert _ssh_to_https("ssh://git@ado.example/tfs/DefaultCollection/App/_git/app") == (
        "https://ado.example/tfs/DefaultCollection/App/_git/app"
    )