"""GitLab REST v4 client.

Uses ``PRIVATE-TOKEN`` and ``verify=False`` (INTENTIONAL: on-prem / TLS
intercept; no custom-CA path yet).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import httpx

from opencode_manager.review_log import get_logger, log_fail, log_ok

logger = get_logger("gitlab")

# After GitLab's rebase API, GET can report rebase_in_progress=false while
# sha / diff_refs are still the pre-rebase commits for one poll.
REBASE_SETTLE_TIMEOUT = 15.0
REBASE_SETTLE_INTERVAL = 0.25


def _http_detail(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code, (exc.response.text or "")[:400]
    return 0, str(exc)


@dataclass
class MergeRequest:
    project_id: int
    iid: int
    title: str
    description: str
    author: str
    source_branch: str
    target_branch: str
    sha: str
    base_sha: str
    start_sha: str
    web_url: str
    http_url: str
    draft: bool
    state: str
    labels: list[str] = field(default_factory=list)
    pipeline_status: str = ""
    pipeline_url: str = ""


def _parse_labels(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("title") or item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in out:
            out.append(name)
        if len(out) >= 20:
            break
    return out


class GitLabError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class GitLabClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
        self._http = httpx.Client(
            base_url=f"{self.base_url}/api/v4",
            headers={"PRIVATE-TOKEN": token} if token else {},
            timeout=timeout,
            verify=False,
        )
        self._user_id: Optional[int] = None
        self._user: Optional[dict[str, Any]] = None
        self._user_resolved = False

    def close(self) -> None:
        self._http.close()

    def current_user(self) -> Optional[dict[str, Any]]:
        # Same rule as Azure: one lookup per process. A miss is not retried,
        # so a flaky /user cannot hold later webhook acks. REVIEW_MENTION
        # still matches after the first miss.
        if self._user_resolved:
            return self._user
        self._user_resolved = True
        if not self.token:
            return None
        try:
            response = self._http.get("/user")
            response.raise_for_status()
            data = response.json() if response.content else {}
            if not isinstance(data, dict) or data.get("id") is None:
                log_fail(logger, "gitlab current user", reason="no id")
                return None
            username = str(data.get("username") or "").strip()
            name = str(data.get("name") or "").strip()
            names = [item for item in (username, name) if item]
            self._user_id = int(data["id"])
            self._user = {
                "id": self._user_id,
                "username": username,
                "name": name,
                "names": names,
            }
            log_ok(
                logger,
                "gitlab current user",
                http=response.status_code,
                user_id=self._user_id,
                username=username or "-",
            )
            return self._user
        except Exception as exc:  # noqa: BLE001
            status, detail = _http_detail(exc)
            log_fail(logger, "gitlab current user", http=status, err=exc, body=detail)
            return None

    def current_user_id(self) -> Optional[int]:
        if self._user_id is not None:
            return self._user_id
        user = self.current_user()
        return int(user["id"]) if user and user.get("id") is not None else None

    def get_merge_request(self, project_id: int, mr_iid: int) -> MergeRequest:
        path = f"/projects/{project_id}/merge_requests/{mr_iid}"
        params = {"include_rebase_in_progress": "true"}
        deadline = time.time() + max(0.0, float(REBASE_SETTLE_TIMEOUT))
        sha_while_rebasing = ""
        data: dict[str, Any] = {}
        while True:
            try:
                response = self._http.get(path, params=params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                status, detail = _http_detail(exc)
                log_fail(logger, "gitlab GET MR", project=project_id, mr=mr_iid, http=status, err=exc, body=detail)
                raise GitLabError(f"fetch MR failed: {exc}") from exc
            data = response.json() if response.content else {}
            if not isinstance(data, dict):
                data = {}
            sha = str(data.get("sha") or (data.get("diff_refs") or {}).get("head_sha") or "")
            rebasing = bool(data.get("rebase_in_progress"))
            if rebasing:
                sha_while_rebasing = sha or sha_while_rebasing
                if time.time() >= deadline:
                    log_fail(logger, "gitlab GET MR", project=project_id, mr=mr_iid, reason="rebase still running")
                    break
                time.sleep(max(0.01, float(REBASE_SETTLE_INTERVAL)))
                continue
            if sha_while_rebasing and sha == sha_while_rebasing:
                if time.time() >= deadline:
                    log_fail(
                        logger,
                        "gitlab GET MR",
                        project=project_id,
                        mr=mr_iid,
                        reason="rebase done but sha unchanged",
                    )
                    break
                time.sleep(max(0.01, float(REBASE_SETTLE_INTERVAL)))
                continue
            break
        refs = data.get("diff_refs") or {}
        source = data.get("source") or {}
        last = data.get("sha") or (data.get("diff_refs") or {}).get("head_sha") or ""
        http_url = (
            source.get("http_url_to_repo")
            or source.get("git_http_url")
            or (data.get("project") or {}).get("http_url_to_repo")
            or ""
        )
        pipe = data.get("head_pipeline") or data.get("pipeline") or {}
        if not isinstance(pipe, dict):
            pipe = {}
        mr = MergeRequest(
            project_id=int(data.get("target_project_id") or project_id),
            iid=int(data["iid"]),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            author=str((data.get("author") or {}).get("username") or ""),
            source_branch=str(data.get("source_branch") or ""),
            target_branch=str(data.get("target_branch") or ""),
            sha=str(last or ""),
            base_sha=str(refs.get("base_sha") or ""),
            start_sha=str(refs.get("start_sha") or ""),
            web_url=str(data.get("web_url") or ""),
            http_url=str(http_url),
            draft=bool(data.get("draft") or data.get("work_in_progress")),
            state=str(data.get("state") or ""),
            labels=_parse_labels(data.get("labels")),
            pipeline_status=str(pipe.get("status") or "").strip(),
            pipeline_url=str(pipe.get("web_url") or "").strip(),
        )
        if not mr.pipeline_status:
            self._attach_latest_pipeline(mr)
        log_ok(
            logger,
            "gitlab GET MR",
            project=mr.project_id,
            mr=mr.iid,
            title=mr.title,
            sha=mr.sha or "-",
            source=mr.source_branch or "-",
            target=mr.target_branch or "-",
            draft=mr.draft,
            state=mr.state or "-",
            pipeline=mr.pipeline_status or "-",
        )
        return mr

    def _attach_latest_pipeline(self, mr: MergeRequest) -> None:
        path = f"/projects/{mr.project_id}/merge_requests/{mr.iid}/pipelines"
        try:
            response = self._http.get(path, params={"per_page": 1})
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            status, detail = _http_detail(exc)
            log_fail(logger, "gitlab GET pipelines", project=mr.project_id, mr=mr.iid, http=status, err=exc, body=detail)
            return
        batch = response.json() if response.content else []
        if not isinstance(batch, list) or not batch or not isinstance(batch[0], dict):
            log_ok(logger, "gitlab GET pipelines", project=mr.project_id, mr=mr.iid, count=0)
            return
        mr.pipeline_status = str(batch[0].get("status") or "").strip()
        mr.pipeline_url = str(batch[0].get("web_url") or "").strip()
        log_ok(logger, "gitlab GET pipelines", project=mr.project_id, mr=mr.iid, status=mr.pipeline_status or "-")

    def post_note(self, project_id: int, mr_iid: int, body: str) -> dict[str, Any]:
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/notes"
        try:
            response = self._http.post(path, json={"body": body})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status, detail = _http_detail(exc)
            log_fail(logger, "gitlab post note", project=project_id, mr=mr_iid, http=status, err=exc, body=detail)
            raise GitLabError(f"post note failed: {exc}") from exc
        data = response.json() if response.content else {}
        log_ok(
            logger,
            "gitlab post note",
            project=project_id,
            mr=mr_iid,
            http=response.status_code,
            note_id=(data or {}).get("id") if isinstance(data, dict) else "-",
        )
        return data

    def post_discussion(
        self,
        project_id: int,
        mr_iid: int,
        body: str,
        position: dict[str, Any],
    ) -> dict[str, Any]:
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/discussions"
        file_path = (position or {}).get("new_path") or (position or {}).get("old_path") or "-"
        try:
            response = self._http.post(path, json={"body": body, "position": position})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "")[:400]
            log_fail(
                logger,
                "gitlab post discussion",
                project=project_id,
                mr=mr_iid,
                path=file_path,
                http=exc.response.status_code,
                err=exc,
                body=detail,
            )
            raise GitLabError(
                f"post discussion failed: {exc} {detail}",
                status_code=exc.response.status_code,
                body=detail,
            ) from exc
        except httpx.HTTPError as exc:
            log_fail(logger, "gitlab post discussion", project=project_id, mr=mr_iid, path=file_path, err=exc)
            raise GitLabError(f"post discussion failed: {exc}") from exc
        data = response.json() if response.content else {}
        log_ok(
            logger,
            "gitlab post discussion",
            project=project_id,
            mr=mr_iid,
            path=file_path,
            http=response.status_code,
            discussion=(data or {}).get("id") if isinstance(data, dict) else "-",
        )
        return data

    def list_discussions(self, project_id: int, mr_iid: int) -> list[dict[str, Any]]:
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/discussions"
        return self._paginate(path, "list discussions failed")

    def list_notes(self, project_id: int, mr_iid: int) -> list[dict[str, Any]]:
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/notes"
        return self._paginate(path, "list notes failed")

    def _paginate(self, path: str, err: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            try:
                response = self._http.get(path, params={"per_page": 100, "page": page})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                status, detail = _http_detail(exc)
                log_fail(logger, "gitlab paginate", op=err, path=path, page=page, http=status, err=exc, body=detail)
                raise GitLabError(f"{err}: {exc}") from exc
            batch = response.json() if response.content else []
            if not isinstance(batch, list) or not batch:
                break
            out.extend(item for item in batch if isinstance(item, dict))
            nxt = (response.headers.get("X-Next-Page") or "").strip()
            if not nxt:
                break
            try:
                page = int(nxt)
            except ValueError:
                break
        log_ok(logger, "gitlab paginate", op=err.replace(" failed", ""), path=path, count=len(out))
        return out

    def delete_note(self, project_id: int, mr_iid: int, note_id: int) -> bool:
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/notes/{int(note_id)}"
        return self._delete(path, f"delete note {note_id} failed")

    def delete_discussion_note(
        self,
        project_id: int,
        mr_iid: int,
        discussion_id: str,
        note_id: int,
    ) -> bool:
        disc = quote(str(discussion_id), safe="")
        path = (
            f"/projects/{project_id}/merge_requests/{mr_iid}"
            f"/discussions/{disc}/notes/{int(note_id)}"
        )
        return self._delete(path, f"delete discussion note {note_id} failed")

    def _delete(self, path: str, err: str) -> bool:
        try:
            response = self._http.delete(path)
            if response.status_code in {200, 202, 204, 404}:
                log_ok(logger, "gitlab delete", op=err.replace(" failed", ""), path=path, http=response.status_code)
                return True
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status, detail = _http_detail(exc)
            log_fail(logger, "gitlab delete", op=err, path=path, http=status, err=exc, body=detail)
            return False
        log_ok(logger, "gitlab delete", op=err.replace(" failed", ""), path=path)
        return True

    def submit_review(self, project_id: int, mr_iid: int) -> bool:
        """Mark this reviewer as reviewed so GitLab shows Re-request."""
        if self._publish_reviewed(project_id, mr_iid) and self._reviewer_is_reviewed(project_id, mr_iid):
            return True
        return self._submit_review_quick_action(project_id, mr_iid)

    def _publish_reviewed(self, project_id: int, mr_iid: int) -> bool:
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/draft_notes/bulk_publish"
        try:
            response = self._http.post(path, json={"reviewer_state": "reviewed"})
            if response.status_code in {200, 204}:
                log_ok(
                    logger,
                    "gitlab submit review",
                    via="bulk_publish",
                    project=project_id,
                    mr=mr_iid,
                    http=response.status_code,
                )
                return True
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            status, detail = _http_detail(exc)
            log_fail(
                logger,
                "gitlab submit review",
                via="bulk_publish",
                project=project_id,
                mr=mr_iid,
                http=status,
                err=exc,
                body=detail,
            )
            return False
        return False

    def _reviewer_is_reviewed(self, project_id: int, mr_iid: int) -> bool:
        user_id = self.current_user_id()
        if user_id is None:
            return False
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/reviewers"
        try:
            response = self._http.get(path)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            status, detail = _http_detail(exc)
            log_fail(
                logger,
                "gitlab list reviewers",
                project=project_id,
                mr=mr_iid,
                http=status,
                err=exc,
                body=detail,
            )
            return False
        batch = response.json() if response.content else []
        if not isinstance(batch, list):
            return False
        done = {"reviewed", "requested_changes", "approved"}
        for item in batch:
            if not isinstance(item, dict):
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else item
            try:
                uid = int((user or {}).get("id"))
            except (TypeError, ValueError):
                continue
            if uid != int(user_id):
                continue
            state = str(item.get("state") or "").strip().lower()
            return state in done
        return False

    def _submit_review_quick_action(self, project_id: int, mr_iid: int) -> bool:
        try:
            self.post_note(project_id, mr_iid, "/submit_review")
        except Exception as exc:  # noqa: BLE001
            log_fail(
                logger,
                "gitlab submit review",
                via="quick_action",
                project=project_id,
                mr=mr_iid,
                err=exc,
            )
            return False
        log_ok(logger, "gitlab submit review", via="quick_action", project=project_id, mr=mr_iid)
        return True

    def reply_to_discussion(
        self,
        project_id: int,
        mr_iid: int,
        discussion_id: str,
        body: str,
    ) -> dict[str, Any]:
        disc = quote(str(discussion_id), safe="")
        path = f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{disc}/notes"
        try:
            response = self._http.post(path, json={"body": body})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "")[:400]
            log_fail(
                logger,
                "gitlab reply discussion",
                project=project_id,
                mr=mr_iid,
                discussion=discussion_id,
                http=exc.response.status_code,
                err=exc,
                body=detail,
            )
            raise GitLabError(
                f"reply discussion failed: {exc} {detail}",
                status_code=exc.response.status_code,
                body=detail,
            ) from exc
        except httpx.HTTPError as exc:
            log_fail(logger, "gitlab reply discussion", project=project_id, mr=mr_iid, discussion=discussion_id, err=exc)
            raise GitLabError(f"reply discussion failed: {exc}") from exc
        data = response.json() if response.content else {}
        log_ok(
            logger,
            "gitlab reply discussion",
            project=project_id,
            mr=mr_iid,
            discussion=discussion_id,
            http=response.status_code,
            note_id=(data or {}).get("id") if isinstance(data, dict) else "-",
        )
        return data

    def resolve_http_url(self, project_id: int, fallback: str = "") -> str:
        if fallback:
            log_ok(logger, "gitlab resolve clone url", project=project_id, source="fallback")
            return fallback
        try:
            response = self._http.get(f"/projects/{quote(str(project_id), safe='')}")
            response.raise_for_status()
            data = response.json()
            url = str(data.get("http_url_to_repo") or "")
            log_ok(logger, "gitlab resolve clone url", project=project_id, http=response.status_code, url=url or "-")
            return url
        except Exception as exc:  # noqa: BLE001
            status, detail = _http_detail(exc)
            log_fail(logger, "gitlab resolve clone url", project=project_id, http=status, err=exc, body=detail)
            return ""
