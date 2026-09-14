"""Azure DevOps Server REST client (api-version 7.1). TLS verify=False."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlparse

import httpx

from opencode_manager.azure.auth import azure_basic_auth
from opencode_manager.azure.urls import identity_root, resolve_collection_url
from opencode_manager.gitlab.client import MergeRequest
from opencode_manager.review_log import get_logger, log_fail, log_ok, redact_userinfo

logger = get_logger("azure")

_request_root: ContextVar[str] = ContextVar("azure_request_root", default="")


class AzureError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _seg(value: str) -> str:
    return quote(str(value or "").strip(), safe="")


def _identity_name_values(user: dict[str, Any]) -> list[str]:
    """Display / account names from connectionData or profile/me."""
    names: list[str] = []

    def add(raw: Any) -> None:
        value = raw
        if isinstance(value, dict):
            value = value.get("$value") or value.get("value") or ""
        text = str(value or "").strip()
        if text and text not in names:
            names.append(text)

    for key in (
        "providerDisplayName",
        "displayName",
        "customDisplayName",
        "uniqueName",
        "directoryAlias",
        "mailAddress",
        "principalName",
    ):
        add(user.get(key))
    props = user.get("properties")
    if isinstance(props, dict):
        for key in ("Account", "AccountName", "DirectoryAlias", "SamAccountName", "Mail", "MailAddress"):
            add(props.get(key))
    return names


class AzureClient:
    def __init__(self, base_url: str, token: str, *, api_version: str = "7.1", timeout: float = 30.0) -> None:
        self.configured_url = (base_url or "").rstrip("/")
        self.base_url = self.configured_url
        self.token = token
        self.api_version = api_version or "7.1"
        self.timeout = timeout
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = azure_basic_auth(token)
        self._http = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout, verify=False)
        self._user_id: Optional[str] = None
        self._user: Optional[dict[str, Any]] = None
        self._user_resolved = False

    def close(self) -> None:
        self._http.close()

    def apply_collection(self, collection: str = "", web_url: str = "") -> str:
        """Permanently rebase onto /tfs/<Collection> from the webhook or PR URL."""
        url = resolve_collection_url(configured=self.base_url, collection=collection, web_url=web_url)
        if not url:
            return self.base_url
        url = url.rstrip("/")
        if url == (self.base_url or "").rstrip("/"):
            return self.base_url
        old = self.base_url
        self.base_url = url
        log_ok(logger, "azure rebase collection", previous=old or "-", collection=url)
        return self.base_url

    def _root(self) -> str:
        return (_request_root.get() or self.base_url or "").rstrip("/")

    def _abs(self, path: str) -> str:
        text = path if str(path).startswith("/") else f"/{path}"
        return f"{self._root()}{text}"

    @contextmanager
    def bind(self, collection: str = "", web_url: str = "") -> Iterator["AzureClient"]:
        """Use the collection root from the webhook/PR when AZURE_DEVOPS_URL is only the host."""
        self.apply_collection(collection, web_url)
        token = _request_root.set(self.base_url)
        try:
            yield self
        finally:
            _request_root.reset(token)

    def _git_paths(self, project: str, repo: str, extra: str = "") -> list[str]:
        extra = extra if not extra or extra.startswith("/") else f"/{extra}"
        repo_s = _seg(repo)
        paths: list[str] = []
        if str(project or "").strip():
            paths.append(f"/{_seg(project)}/_apis/git/repositories/{repo_s}{extra}")
        paths.append(f"/_apis/git/repositories/{repo_s}{extra}")
        return list(dict.fromkeys(paths))

    def _send(self, method: str, paths: list[str], **kwargs: Any) -> httpx.Response:
        last_error: Optional[BaseException] = None
        last_404: Optional[httpx.Response] = None
        for index, path in enumerate(paths):
            url = self._abs(path)
            try:
                logger.info("azure HTTP %s %s", method, redact_userinfo(url))
                response = self._http.request(method, url, **kwargs)
                if response.status_code == 404 and index < len(paths) - 1:
                    last_404 = response
                    log_ok(logger, "azure retry path", method=method, path=path, http=404)
                    continue
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response is not None and exc.response.status_code == 404 and index < len(paths) - 1:
                    last_404 = exc.response
                    continue
                body = ""
                if exc.response is not None:
                    body = (exc.response.text or "")[:300]
                log_fail(
                    logger,
                    "azure HTTP",
                    method=method,
                    url=redact_userinfo(url),
                    http=exc.response.status_code if exc.response is not None else 0,
                    body=redact_userinfo(body),
                )
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                log_fail(logger, "azure HTTP", method=method, url=redact_userinfo(url), err=exc)
                raise
        if last_404 is not None:
            last_404.raise_for_status()
        if last_error is not None:
            raise last_error
        raise RuntimeError("azure request had no paths")

    def _identity_roots(self) -> list[str]:
        configured = (self.configured_url or self.base_url or "").rstrip("/")
        roots: list[str] = []
        ident = identity_root(configured)
        if ident:
            roots.append(ident.rstrip("/"))
        parsed = urlparse(configured)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            host_root = f"{parsed.scheme}://{parsed.netloc}"
            tfs_root = f"{host_root}/tfs"
            parts = [item for item in (parsed.path or "").split("/") if item]
            if not parts:
                roots.append(host_root)
                roots.append(tfs_root)
        if configured and configured not in roots:
            roots.append(configured)
        return list(dict.fromkeys(item for item in roots if item))

    def _identity_versions(self) -> list[str]:
        preferred = (self.api_version or "7.1").strip() or "7.1"
        versions = [preferred]
        if not preferred.endswith("-preview"):
            versions.append(f"{preferred}-preview")
        for item in ("1.0", "7.1-preview", "6.0-preview", "5.0-preview", "4.1-preview", "2.0"):
            if item not in versions:
                versions.append(item)
        return versions

    def _parse_authenticated_user(self, data: Any) -> Optional[dict[str, Any]]:
        blob = data if isinstance(data, dict) else {}
        user = blob.get("authenticatedUser") if isinstance(blob.get("authenticatedUser"), dict) else None
        if user is None and blob.get("id"):
            user = blob
        if not isinstance(user, dict) or user.get("id") is None:
            return None
        uid = str(user.get("id") or "").strip()
        if not uid:
            return None
        return {"id": uid, "names": _identity_name_values(user)}

    def current_user(self) -> Optional[dict[str, Any]]:
        if self._user_resolved:
            return self._user
        self._user_resolved = True
        if not self.token:
            log_fail(logger, "azure current user", reason="AZURE_DEVOPS_PAT empty")
            return None
        last_status = 0
        last_url = ""
        last_body = ""
        paths = ["/_apis/connectionData", "/_apis/profile/profiles/me"]
        for root in self._identity_roots():
            for path in paths:
                url = f"{root}{path}"
                for version in self._identity_versions():
                    try:
                        response = self._http.get(url, params={"api-version": version})
                    except Exception as exc:  # noqa: BLE001
                        log_fail(
                            logger,
                            "azure current user try",
                            url=redact_userinfo(url),
                            api=version,
                            err=exc,
                        )
                        continue
                    body = (response.text or "")[:240]
                    if response.status_code in {200, 203}:
                        try:
                            data = response.json() if response.content else {}
                        except Exception:
                            data = {}
                        user = self._parse_authenticated_user(data)
                        if user:
                            self._user_id = user["id"]
                            self._user = user
                            log_ok(
                                logger,
                                "azure current user",
                                http=response.status_code,
                                user_id=self._user_id,
                                name=user["names"][0] if user["names"] else "-",
                                url=redact_userinfo(url),
                                api=version,
                            )
                            return self._user
                        last_status, last_url, last_body = response.status_code, url, "no authenticatedUser.id"
                        continue
                    last_status, last_url, last_body = response.status_code, url, body
                    if response.status_code not in {400, 404, 415}:
                        break
        log_fail(
            logger,
            "azure current user",
            http=last_status,
            url=redact_userinfo(last_url) or "-",
            body=redact_userinfo(last_body) or "-",
            reason="identity unknown; REVIEW_MENTION still matches uniqueName tail",
        )
        return None

    def current_user_id(self) -> Optional[str]:
        if self._user_id is not None:
            return self._user_id
        user = self.current_user()
        return str(user["id"]) if user and user.get("id") else None

    def _attach_latest_status(self, mr: MergeRequest, project: str, repo: str, pr_id: int) -> None:
        try:
            response = self._send(
                "GET",
                self._git_paths(project, repo, f"/pullRequests/{int(pr_id)}/statuses"),
                params={"api-version": self.api_version},
            )
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "azure GET statuses", project=project, repo=repo, pr=pr_id, err=exc)
            return
        data = response.json() if response.content else {}
        rows = data if isinstance(data, list) else (data.get("value") if isinstance(data, dict) else [])
        latest: dict[str, Any] = {}
        for item in rows or []:
            if isinstance(item, dict) and (item.get("state") or item.get("status")):
                latest = item
        if not latest:
            return
        mr.pipeline_status = str(latest.get("state") or latest.get("status") or "").strip()
        mr.pipeline_url = str(latest.get("targetUrl") or latest.get("target_url") or "").strip()

    def get_pull_request(self, project: str, repo: str, pr_id: int) -> MergeRequest:
        try:
            response = self._send(
                "GET",
                self._git_paths(project, repo, f"/pullRequests/{int(pr_id)}"),
                params={"api-version": self.api_version},
            )
        except httpx.HTTPError as exc:
            detail = (getattr(exc, "response", None).text or "")[:400] if getattr(exc, "response", None) else ""
            log_fail(logger, "azure GET PR", project=project, repo=repo, pr=pr_id, err=exc, body=detail)
            raise AzureError(f"fetch PR failed: {exc}") from exc
        data = response.json() if response.content else {}
        mr = _pr_to_merge_request(data)
        if not mr.pipeline_status:
            self._attach_latest_status(mr, project, repo, pr_id)
        if not mr.sha:
            _first, _second, sha = self.iteration_span(project, repo, pr_id)
            if sha:
                logger.info("azure PR %s sha empty on GET; using iteration commit %s", pr_id, sha)
                mr.sha = sha
        if not _is_git_http(mr.http_url):
            built = self.resolve_clone_url(project, repo, mr.http_url)
            logger.info(
                "azure PR %s clone url %s -> %s (collection %s)",
                pr_id,
                redact_userinfo(mr.http_url) or "-",
                redact_userinfo(built) or "-",
                self._root() or "-",
            )
            mr.http_url = built
        log_ok(
            logger,
            "azure GET PR",
            project=project,
            repo=repo,
            pr=pr_id,
            title=mr.title,
            sha=mr.sha or "-",
            source=mr.source_branch or "-",
            target=mr.target_branch or "-",
            draft=mr.draft,
            state=mr.state or "-",
            clone=redact_userinfo(mr.http_url) or "-",
        )
        return mr

    def list_reviewers(
        self,
        project: str,
        repo: str,
        pr_id: int,
        *,
        collection: str = "",
        web_url: str = "",
    ) -> list[dict[str, Any]]:
        """Live reviewer list. Used to verify a reviewer-change webhook."""
        if collection or web_url:
            self.apply_collection(collection, web_url)
        try:
            response = self._send(
                "GET",
                self._git_paths(project, repo, f"/pullRequests/{int(pr_id)}/reviewers"),
                params={"api-version": self.api_version},
            )
        except httpx.HTTPError as exc:
            detail = (getattr(exc, "response", None).text or "")[:400] if getattr(exc, "response", None) else ""
            log_fail(logger, "azure GET reviewers", project=project, repo=repo, pr=pr_id, err=exc, body=detail)
            raise AzureError(f"fetch reviewers failed: {exc}") from exc
        data = response.json() if response.content else {}
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("value") if isinstance(data.get("value"), list) else []
        else:
            rows = []
        out = [row for row in rows if isinstance(row, dict)]
        log_ok(logger, "azure GET reviewers", project=project, repo=repo, pr=pr_id, count=len(out))
        return out

    def resolve_clone_url(self, project: str, repo: str, fallback: str = "") -> str:
        if _is_git_http(fallback):
            return fallback
        try:
            response = self._send(
                "GET",
                self._git_paths(project, repo, ""),
                params={"api-version": self.api_version},
            )
            data = response.json() if response.content else {}
            log_ok(logger, "azure GET repo", project=project, repo=repo, http=response.status_code)
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "azure GET repo", project=project, repo=repo, err=exc)
            data = {}
        remote = str((data or {}).get("remoteUrl") or (data or {}).get("webUrl") or "")
        if _is_git_http(remote):
            log_ok(logger, "azure resolve clone url", project=project, repo=repo, source="remoteUrl", url=redact_userinfo(remote))
            return remote
        converted = _ssh_to_https(remote)
        if converted:
            log_ok(logger, "azure resolve clone url", project=project, repo=repo, source="ssh-to-https", url=redact_userinfo(converted))
            return converted
        proj = data.get("project") if isinstance((data or {}).get("project"), dict) else {}
        proj_name = str((proj or {}).get("name") or project or "").strip()
        repo_name = str((data or {}).get("name") or repo or "").strip()
        root = self._root()
        if root and proj_name and repo_name:
            built = f"{root}/{_seg(proj_name)}/_git/{_seg(repo_name)}"
            log_ok(logger, "azure resolve clone url", project=project, repo=repo, source="built", url=redact_userinfo(built), collection=root)
            return built
        leftover = fallback or remote
        if leftover:
            log_ok(logger, "azure resolve clone url", project=project, repo=repo, source="fallback", url=redact_userinfo(leftover))
        else:
            log_fail(logger, "azure resolve clone url", project=project, repo=repo, reason="empty")
        return leftover

    def iteration_span(self, project: str, repo: str, pr_id: int) -> tuple[int, int, str]:
        """(firstComparingIteration, secondComparingIteration, latest source sha)."""
        try:
            response = self._send(
                "GET",
                self._git_paths(project, repo, f"/pullRequests/{int(pr_id)}/iterations"),
                params={"api-version": self.api_version},
            )
        except httpx.HTTPError as exc:
            log_fail(logger, "azure GET iterations", pr=pr_id, err=exc)
            return 1, 1, ""
        data = response.json() if response.content else {}
        rows = data if isinstance(data, list) else (data.get("value") if isinstance(data, dict) else [])
        ids: list[int] = []
        sha = ""
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            try:
                ids.append(int(item.get("id")))
            except (TypeError, ValueError):
                continue
            src = item.get("sourceRefCommit") if isinstance(item.get("sourceRefCommit"), dict) else {}
            commit = str(src.get("commitId") or "")
            if commit:
                sha = commit
        if not ids:
            log_ok(logger, "azure GET iterations", pr=pr_id, first=1, second=1, sha=sha or "-", count=0)
            return 1, 1, sha
        first, second = 1, max(ids)
        log_ok(logger, "azure GET iterations", pr=pr_id, first=first, second=second, sha=sha or "-", count=len(ids))
        return first, second, sha

    def post_overview(self, project: str, repo: str, pr_id: int, body: str) -> dict[str, Any]:
        return self._post_thread(project, repo, pr_id, body, thread_context=None)

    def post_file_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        body: str,
        thread_context: dict[str, Any],
        *,
        first_iteration: int = 1,
        second_iteration: int = 1,
    ) -> dict[str, Any]:
        extra = {
            "pullRequestThreadContext": {
                "iterationContext": {
                    "firstComparingIteration": max(1, int(first_iteration or 1)),
                    "secondComparingIteration": max(1, int(second_iteration or 1)),
                }
            }
        }
        return self._post_thread(project, repo, pr_id, body, thread_context=thread_context, extra=extra)

    def _post_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        body: str,
        thread_context: Optional[dict[str, Any]],
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
            "status": 1,
            "properties": {
                "Microsoft.TeamFoundation.Discussion.SupportsMarkdown": {
                    "$type": "System.Int32",
                    "$value": 1,
                }
            },
        }
        if thread_context:
            payload["threadContext"] = thread_context
        if extra:
            payload.update(extra)
        kind = "file" if thread_context else "overview"
        file_path = (thread_context or {}).get("filePath") or "-"
        try:
            response = self._send(
                "POST",
                self._git_paths(project, repo, f"/pullRequests/{int(pr_id)}/threads"),
                params={"api-version": self.api_version},
                json=payload,
            )
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "")[:400]
            log_fail(logger, "azure POST thread", kind=kind, pr=pr_id, path=file_path, http=exc.response.status_code, err=exc, body=detail)
            raise AzureError(
                f"post thread failed: {exc} {detail}",
                status_code=exc.response.status_code,
                body=detail,
            ) from exc
        except httpx.HTTPError as exc:
            log_fail(logger, "azure POST thread", kind=kind, pr=pr_id, path=file_path, err=exc)
            raise AzureError(f"post thread failed: {exc}") from exc
        data = response.json() if response.content else {}
        log_ok(
            logger,
            "azure POST thread",
            kind=kind,
            pr=pr_id,
            path=file_path,
            http=response.status_code,
            thread=(data or {}).get("id") if isinstance(data, dict) else "-",
        )
        return data

    def list_threads(self, project: str, repo: str, pr_id: int) -> list[dict[str, Any]]:
        paths = self._git_paths(project, repo, f"/pullRequests/{int(pr_id)}/threads")
        out: list[dict[str, Any]] = []
        token = ""
        skip = 0
        for page in range(1, 21):
            params: dict[str, Any] = {"api-version": self.api_version, "$top": 100}
            if token:
                params["continuationToken"] = token
            elif skip:
                params["$skip"] = skip
            try:
                response = self._send("GET", paths, params=params)
            except httpx.HTTPError as exc:
                log_fail(logger, "azure GET threads", pr=pr_id, page=page, err=exc)
                raise AzureError(f"list threads failed: {exc}") from exc
            data = response.json() if response.content else {}
            if isinstance(data, list):
                batch = [item for item in data if isinstance(item, dict)]
            else:
                raw = data.get("value") if isinstance(data, dict) else None
                batch = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            out.extend(batch)
            nxt = (
                (response.headers.get("x-ms-continuationtoken") or response.headers.get("X-MS-ContinuationToken") or "")
                .strip()
            )
            if nxt:
                token = nxt
                skip = 0
                continue
            if len(batch) < 100:
                break
            token = ""
            skip += len(batch)
        log_ok(logger, "azure GET threads", pr=pr_id, count=len(out))
        return out

    def reply_to_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        thread_id: str,
        body: str,
        *,
        parent_comment_id: int = 0,
    ) -> dict[str, Any]:
        parent = int(parent_comment_id or 1)
        try:
            response = self._send(
                "POST",
                self._git_paths(
                    project,
                    repo,
                    f"/pullRequests/{int(pr_id)}/threads/{_seg(thread_id)}/comments",
                ),
                params={"api-version": self.api_version},
                json={"content": body, "parentCommentId": parent, "commentType": 1},
            )
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "")[:400]
            log_fail(
                logger,
                "azure POST reply",
                pr=pr_id,
                thread=thread_id,
                parent=parent,
                http=exc.response.status_code,
                err=exc,
                body=detail,
            )
            raise AzureError(
                f"reply thread failed: {exc} {detail}",
                status_code=exc.response.status_code,
                body=detail,
            ) from exc
        except httpx.HTTPError as exc:
            log_fail(logger, "azure POST reply", pr=pr_id, thread=thread_id, parent=parent, err=exc)
            raise AzureError(f"reply thread failed: {exc}") from exc
        data = response.json() if response.content else {}
        log_ok(
            logger,
            "azure POST reply",
            pr=pr_id,
            thread=thread_id,
            parent=parent,
            http=response.status_code,
            comment=(data or {}).get("id") if isinstance(data, dict) else "-",
        )
        return data

    def delete_comment(self, project: str, repo: str, pr_id: int, thread_id: str, comment_id: int) -> bool:
        try:
            response = self._send(
                "DELETE",
                self._git_paths(
                    project,
                    repo,
                    f"/pullRequests/{int(pr_id)}/threads/{_seg(thread_id)}/comments/{int(comment_id)}",
                ),
                params={"api-version": self.api_version},
            )
            if response.status_code in {200, 202, 204, 404}:
                log_ok(
                    logger,
                    "azure DELETE comment",
                    pr=pr_id,
                    thread=thread_id,
                    comment=comment_id,
                    http=response.status_code,
                )
                return True
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                log_ok(logger, "azure DELETE comment", pr=pr_id, thread=thread_id, comment=comment_id, http=404)
                return True
            log_fail(logger, "azure DELETE comment", pr=pr_id, thread=thread_id, comment=comment_id, err=exc)
            return False
        except httpx.HTTPError as exc:
            log_fail(logger, "azure DELETE comment", pr=pr_id, thread=thread_id, comment=comment_id, err=exc)
            return False
        log_ok(logger, "azure DELETE comment", pr=pr_id, thread=thread_id, comment=comment_id)
        return True


def _is_http(url: str) -> bool:
    return str(url or "").lower().startswith(("http://", "https://"))


def _is_git_http(url: str) -> bool:
    text = str(url or "")
    if not _is_http(text):
        return False
    return "/_apis/" not in text.lower()


def _ssh_to_https(url: str) -> str:
    text = str(url or "").strip()
    if text.startswith("git@"):
        host, _, path = text[4:].partition(":")
        if host and path:
            return f"https://{host}/{path.removeprefix('/')}"
    if text.startswith("ssh://"):
        rest = text[6:]
        if rest.startswith("git@"):
            rest = rest[4:]
        host, _, path = rest.partition("/")
        host = host.split("@")[-1]
        if host and path:
            return f"https://{host}/{path}"
    return ""


def _pr_to_merge_request(data: dict[str, Any]) -> MergeRequest:
    repo = data.get("repository") if isinstance(data.get("repository"), dict) else {}
    project = repo.get("project") if isinstance(repo.get("project"), dict) else {}
    source = str(data.get("sourceRefName") or "")
    target = str(data.get("targetRefName") or "")
    for prefix in ("refs/heads/",):
        if source.startswith(prefix):
            source = source[len(prefix) :]
        if target.startswith(prefix):
            target = target[len(prefix) :]
    last = data.get("lastMergeSourceCommit") if isinstance(data.get("lastMergeSourceCommit"), dict) else {}
    merge = data.get("lastMergeCommit") if isinstance(data.get("lastMergeCommit"), dict) else {}
    sha = str(last.get("commitId") or merge.get("commitId") or "")
    http_url = str(repo.get("remoteUrl") or "")
    if not _is_git_http(http_url):
        http_url = ""
    links = data.get("_links") if isinstance(data.get("_links"), dict) else {}
    web = links.get("web") if isinstance(links.get("web"), dict) else {}
    author = data.get("createdBy") if isinstance(data.get("createdBy"), dict) else {}
    return MergeRequest(
        project_id=0,
        iid=int(data.get("pullRequestId") or 0),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        author=str(author.get("uniqueName") or author.get("displayName") or ""),
        source_branch=source,
        target_branch=target,
        sha=sha,
        base_sha="",
        start_sha="",
        web_url=str(web.get("href") or data.get("url") or ""),
        http_url=http_url,
        draft=bool(data.get("isDraft")),
        state=str(data.get("status") or ""),
        labels=_pr_labels(data.get("labels")),
    )


def _pr_labels(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            if item.get("active") is False:
                continue
            name = str(item.get("name") or item.get("title") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= 20:
            break
    return out
