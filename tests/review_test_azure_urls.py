from __future__ import annotations

from opencode_manager.azure.urls import (
    identity_root,
    looks_like_azure_resource,
    normalize_collection_url,
    resolve_collection_url,
)


def test_looks_like_collection_root_without_git() -> None:
    assert looks_like_azure_resource("https://tfs02.company.com.tr/tfs/ExampleCollection/")
    assert not looks_like_azure_resource("https://tfs02.company.com.tr/tfs")
    assert not looks_like_azure_resource("https://tfs02.company.com.tr")


def test_identity_root_keeps_tfs_app_and_strips_collection() -> None:
    assert identity_root("https://tfs02.company.com.tr/tfs") == "https://tfs02.company.com.tr/tfs"
    assert (
        identity_root("https://tfs02.company.com.tr/tfs/ExampleCollection")
        == "https://tfs02.company.com.tr/tfs"
    )
    assert identity_root("https://tfs02.company.com.tr") == "https://tfs02.company.com.tr"
    assert identity_root("https://dev.azure.com/contoso") == "https://dev.azure.com/contoso"
    assert identity_root("https://dev.azure.com/contoso/proj") == "https://dev.azure.com/contoso"


def test_normalize_server_pr_web_url() -> None:
    url = (
        "https://tfs02.company.com.tr/tfs/ExampleCollection/"
        "Example%20Projeleri/_git/ProjectX/pullrequest/26509"
    )
    assert normalize_collection_url(url) == "https://tfs02.company.com.tr/tfs/ExampleCollection"


def test_normalize_api_and_remote_urls() -> None:
    api = (
        "https://tfs02.company.com.tr/tfs/ExampleCollection/"
        "_apis/git/repositories/240c25cd-5cbf-4485-b91f-6d58bd8e9c68/pullRequests/26509"
    )
    remote = "https://tfs02.company.com.tr/tfs/ExampleCollection/Example Projeleri/_git/ProjectX"
    assert normalize_collection_url(api) == "https://tfs02.company.com.tr/tfs/ExampleCollection"
    assert normalize_collection_url(remote) == "https://tfs02.company.com.tr/tfs/ExampleCollection"


def test_resolve_fills_collection_when_env_is_only_the_host() -> None:
    web = (
        "https://tfs02.company.com.tr/tfs/ExampleCollection/"
        "Example%20Projeleri/_git/ProjectX/pullrequest/26509"
    )
    got = resolve_collection_url(
        configured="https://tfs02.company.com.tr",
        collection="",
        web_url=web,
    )
    assert got == "https://tfs02.company.com.tr/tfs/ExampleCollection"


def test_resolve_from_tfs_collection_url_without_git() -> None:
    got = resolve_collection_url(
        configured="https://tfs02.company.com.tr",
        collection="",
        web_url="https://tfs02.company.com.tr/tfs/ExampleCollection/",
    )
    assert got == "https://tfs02.company.com.tr/tfs/ExampleCollection"


def test_resolve_prefers_hook_collection() -> None:
    got = resolve_collection_url(
        configured="https://tfs02.company.com.tr",
        collection="https://tfs02.company.com.tr/tfs/ExampleCollection/",
        web_url="",
    )
    assert got == "https://tfs02.company.com.tr/tfs/ExampleCollection"
