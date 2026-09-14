from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from opencode_manager.azure.client import AzureClient, AzureError
from opencode_manager.azure.threads import azure_thread_context, parse_azure_threads
from opencode_manager.review_config import ReviewConfig
from opencode_manager.gitlab.client import GitLabClient, GitLabError, MergeRequest
from opencode_manager.models import JobRecord, PromptRow, utc_now
from opencode_manager.cleanup.end import stop_job_holders
from opencode_manager.diag import log_diag, merge_job_diag
from opencode_manager.review_log import get_logger, log_fail, log_ok, redact_userinfo
from opencode_manager.opencode.serve import ServeHandle, serve_log_path, start_serve, stop_serve
from opencode_manager.review_session import (
    OpenCodeClient,
    OpenCodeError,
    snapshot_chat,
    turn_assistant_text,
)
from opencode_manager.dashboard.store import JobStore
from opencode_manager.review.comment_range import format_code_comment_prompt
from opencode_manager.review.mention import plain_comment
from opencode_manager.review.findings import Finding, split_findings
from opencode_manager.review.format import format_cancelled, format_failure, format_success, format_usage
from opencode_manager.review.position import build_position_variants, format_discussion
from opencode_manager.review.similarity import should_skip_similar_reply
from opencode_manager.review.threads import match_creasy_thread, parse_creasy_threads
from opencode_manager.review.prompt import (
    build_ask_prompt,
    build_review_prompt,
    build_thread_review_prompt,
    hang_resume_prompt,
)
from opencode_manager.workspace.diffmap import parse_unified_diff
from opencode_manager.workspace.gitops import (
    GitError,
    clone_is_usable,
    clone_repo,
    delete_clone,
    diff_stat,
    fetch_and_checkout,
    resolve_merge_base,
    unified_diff,
)
from opencode_manager.workspace.identity import clone_path_for
from opencode_manager.workspace.store import WorkspaceRecord, WorkspaceStore

logger = get_logger("worker")


@dataclass
class RunResult:
    text: str = ""
    session_id: str = ""
    error: str = ""
    timeout: bool = False
    cancelled: bool = False
    clone_path: str = ""
    merge_base: str = ""
    sha: str = ""
    diff_stat: str = ""
    changed_paths: list[str] | None = None
    chat_snapshot: list | None = None
    serve_pid: Optional[int] = None
    serve_port: Optional[int] = None
    posted: bool = False
    base_sha: str = ""
    start_sha: str = ""
    findings_posted: int = 0


def discussion_sha_attempts(result: RunResult) -> list[tuple[str, str]]:
    """GitLab discussion SHAs. Live merge-base first so line numbers match the diff.

    GitLab ``diff_refs`` can be empty, or still hold the previous version
    for a moment after a rebase. The review itself is always
    ``git diff <merge-base>...HEAD``. Positions must use that same left
    side first. GitLab's stored pair is only a fallback.
    """
    merge = (result.merge_base or "").strip()
    gitlab_base = (result.base_sha or "").strip()
    gitlab_start = (result.start_sha or gitlab_base or "").strip()
    pairs: list[tuple[str, str]] = []
    if merge:
        pairs.append((merge, merge))
    gitlab = (gitlab_base, gitlab_start or gitlab_base)
    if gitlab[0] and gitlab not in pairs:
        pairs.append(gitlab)
    return pairs


class JobRunner(Protocol):
    def run(self, job: JobRecord, should_stop: Callable[[], bool]) -> RunResult: ...


class OpenCodeRunner:
    def __init__(
        self,
        config: ReviewConfig,
        workspaces: WorkspaceStore,
        gitlab: Optional[GitLabClient] = None,
        store: Optional[JobStore] = None,
        azure: Optional[AzureClient] = None,
    ) -> None:
        self.config = config
        self.workspaces = workspaces
        self.gitlab = gitlab or GitLabClient(config.gitlab_url, config.gitlab_token)
        self.azure = azure
        self.store = store

    def _is_azure(self, job: JobRecord) -> bool:
        return (job.provider or "gitlab") == "azure"

    def _bind_azure(self, job: JobRecord) -> Any:
        azure = self.azure
        if not self._is_azure(job) or azure is None:
            return nullcontext()
        bind = getattr(azure, "bind", None)
        if not callable(bind):
            return nullcontext()
        return bind(getattr(job, "azure_collection", "") or "", job.web_url or "")

    def _remember_title(self, job: JobRecord, title: str) -> None:
        text = (title or "").strip()
        if not text or job.mr_title == text:
            return
        job.mr_title = text
        if self.store is None:
            return
        try:
            self.store.save(job)
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "persist mr_title", job=job.job_id, err=exc)

    def _append_job_log(self, job: JobRecord, line: str) -> None:
        if not job.log_file:
            return
        path = self.config.log_dir / job.log_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line.rstrip() + "\n")
        except OSError as exc:
            log_fail(logger, "job log write", job=job.job_id, err=exc)

    def _note_diag(self, job: JobRecord, stage: str, **fields: Any) -> None:
        blob = merge_job_diag(
            job,
            stage=stage,
            provider=job.provider or "gitlab",
            trigger=job.trigger,
            **fields,
        )
        log_diag("job", stage, job=job.job_id, mr=job.mr_key, **{k: v for k, v in blob.items() if k not in {"stage"}})
        self._persist_job(job, f"diag {stage}")

    def _persist_job(self, job: JobRecord, what: str) -> None:
        if self.store is None:
            return
        try:
            self.store.save(job)
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "persist job", what=what, job=job.job_id, err=exc)

    def _record_spawn(self, job: JobRecord, handle: ServeHandle) -> None:
        job.serve_pid = handle.pid
        job.serve_port = handle.port
        job.serve_base_url = handle.base_url
        self._persist_job(job, "serve pid")
        log_ok(logger, "serve started", pid=handle.pid, port=handle.port)
        self._append_job_log(job, f"serve started pid={handle.pid} port={handle.port}")

    def _track_git_pid(self, job: JobRecord, pid: int) -> None:
        job.extra_pids = [pid] if pid else []
        self._persist_job(job, "extra pids")

    def _git_kw(self, job: JobRecord, should_stop: Callable[[], bool]) -> dict:
        return {"should_stop": should_stop, "on_pid": lambda pid: self._track_git_pid(job, pid)}

    def run(self, job: JobRecord, should_stop: Callable[[], bool]) -> RunResult:
        if job.trigger == "reset":
            log_ok(logger, "obsolete trigger skipped", job=job.job_id, trigger=job.trigger)
            return RunResult()
        if job.trigger == "usage":
            return self._run_usage(job, should_stop)
        result = RunResult()
        handle: Optional[ServeHandle] = None
        client: Optional[OpenCodeClient] = None
        azure_cm = self._bind_azure(job)
        azure_cm.__enter__()
        try:
            if should_stop():
                result.cancelled = True
                log_ok(logger, "job cancelled", job=job.job_id, stage="start")
                return result
            prior = self.workspaces.get(job.mr_key)
            previous_sha = prior.last_sha if prior else ""
            azure = self.azure if self._is_azure(job) else None
            self._note_diag(
                job,
                "start",
                azure_project=job.azure_project or "",
                azure_repo=job.azure_repo or "",
                collection=getattr(azure, "base_url", "") or job.azure_collection or "",
                web_url=job.web_url or "",
                token_set=bool(self.config.azure_token if self._is_azure(job) else self.config.gitlab_token),
                token_chars=len((self.config.azure_token if self._is_azure(job) else self.config.gitlab_token) or ""),
            )
            mr = self._load_change(job)
            log_ok(
                logger,
                "load change",
                provider=job.provider or "gitlab",
                title=mr.title or "-",
                sha=mr.sha or "-",
                source=mr.source_branch or "-",
                target=mr.target_branch or "-",
            )
            self._remember_title(job, mr.title)
            self._note_diag(
                job,
                "load",
                title=mr.title or "",
                sha=mr.sha or "",
                source=mr.source_branch or "",
                target=mr.target_branch or "",
                clone_url=mr.http_url or "",
            )
            workspace = self._ensure_workspace(job, mr, should_stop)
            log_ok(logger, "workspace ready", path=workspace.clone_path, sha=workspace.last_sha or mr.sha or "-")
            result.clone_path = workspace.clone_path
            self._note_diag(job, "workspace", clone_path=workspace.clone_path, sha=workspace.last_sha or mr.sha or "")
            result.sha = workspace.last_sha or mr.sha
            result.base_sha = mr.base_sha
            result.start_sha = mr.start_sha or mr.base_sha
            clone = Path(workspace.clone_path)
            git_kw = self._git_kw(job, should_stop)
            merge_base = resolve_merge_base(
                clone,
                target_branch=mr.target_branch,
                preferred_base=mr.base_sha,
                timeout=min(60.0, self.config.git_timeout),
                **git_kw,
            )
            index = diff_stat(
                clone,
                merge_base,
                timeout=min(60.0, self.config.git_timeout),
                **git_kw,
            )
            result.merge_base = merge_base
            result.diff_stat = index.stat
            result.changed_paths = list(index.paths)
            job.clone_path = workspace.clone_path
            self._persist_job(job, "clone path")
            log_ok(logger, "diff stat", mr=job.mr_key, paths=len(index.paths), merge_base=merge_base)
            logger.info("diff stat for %s:\n%s", job.mr_key, index.stat)

            created_new = False
            self._ensure_parent_comment(job)
            prompt = self._prompt(
                job, mr, index, workspace, created_new=False, previous_sha=previous_sha
            )
            job.prompt = prompt
            self._persist_job(job, "prompt")
            handle = start_serve(
                bin_name=self.config.opencode_bin,
                cwd=clone,
                log_path=serve_log_path(self.config.serve_dir, job.job_id),
                timeout=self.config.serve_health_timeout,
                should_stop=should_stop,
                on_spawn=lambda spawned: self._record_spawn(job, spawned),
            )
            result.serve_pid = handle.pid
            result.serve_port = handle.port
            self._note_diag(job, "serve", serve_pid=handle.pid, serve_port=handle.port)
            client = OpenCodeClient(handle.base_url, str(clone))
            client.wait_directory(
                timeout=float(self.config.opencode_timeout),
                should_stop=should_stop,
            )
            session_id, created_new = client.resume_or_create(
                workspace.session_id or None,
                title=f"amir-mini {job.mr_key}",
            )
            try:
                client.list_messages(session_id)
            except OpenCodeError as exc:
                if exc.status_code == 400:
                    log_fail(logger, "opencode session unreadable", session=session_id, http=400)
                    session_id = client.create_session(title=f"creasy {job.mr_key}")
                    created_new = True
                else:
                    raise
            result.session_id = session_id
            job.session_id = session_id
            self._persist_job(job, "session id")
            log_ok(logger, "opencode session", session=session_id, created_new=created_new)
            if created_new and job.trigger == "ask":
                prompt = self._prompt(
                    job, mr, index, workspace, created_new=True, previous_sha=previous_sha
                )
                job.prompt = prompt
                self._persist_job(job, "ask prompt")
            last_error = ""
            text = ""
            original_posted = False
            for attempt in range(1, self.config.opencode_retry_count + 1):
                if should_stop():
                    result.cancelled = True
                    return result
                try:
                    if attempt > 1:
                        if not session_id.startswith("ses_"):
                            raise OpenCodeError("resume rejected; will not open a blank session")
                        got = client.get_session(session_id)
                        if got.status_code != 200:
                            raise OpenCodeError(f"resume rejected: HTTP {got.status_code}")
                    turn = prompt if not original_posted else hang_resume_prompt()
                    prompt_id = "ORIGINAL" if not original_posted else "HANG_RESUME"
                    client.post_message(
                        session_id,
                        turn,
                        model=job.model or self.config.opencode_model,
                        agent=self.config.opencode_agent,
                    )
                    job.prompts.append(PromptRow(id=prompt_id, text=turn, posted_at=utc_now()))
                    if prompt_id == "ORIGINAL":
                        job.original_posted = True
                    self._persist_job(job, "posted prompt")
                    original_posted = True
                    text = client.wait_idle(
                        session_id,
                        timeout=self.config.opencode_timeout,
                        hang_timeout=self.config.hang_timeout,
                        should_stop=should_stop,
                    )
                    last_error = ""
                    break
                except OpenCodeError as exc:
                    last_error = str(exc)
                    result.timeout = exc.timeout
                    log_fail(logger, "opencode turn", attempt=attempt, session=session_id, err=exc)
                    self._append_job_log(job, f"attempt {attempt} ended: {exc}")
                    if should_stop() or attempt >= self.config.opencode_retry_count:
                        break
            if should_stop():
                result.cancelled = True
                log_ok(logger, "job cancelled", job=job.job_id, stage="opencode")
                return result
            try:
                messages = client.list_messages(session_id)
                result.chat_snapshot = snapshot_chat(messages, session_id)
            except Exception:
                messages = []
            if last_error:
                result.error = last_error
                result.text = ""
                workspace.session_id = session_id
                workspace.last_job_id = job.job_id
                self.workspaces.save(workspace)
                log_fail(logger, "opencode turn exhausted", session=session_id, err=last_error)
                self._post_note(job, result)
                return result
            result.text = turn_assistant_text(messages, prefer_review=job.trigger != "ask") or text
            markdown, findings = split_findings(result.text)
            result.text = markdown
            workspace.session_id = session_id
            workspace.last_job_id = job.job_id
            self.workspaces.save(workspace)
            log_ok(logger, "opencode turn", session=session_id, chars=len(result.text), findings=len(findings))
            self._post_note(job, result, findings=findings)
            return result
        except GitError as exc:
            if should_stop() or str(exc) == "cancelled":
                result.cancelled = True
                log_ok(logger, "job cancelled", job=job.job_id, stage="git")
            else:
                result.error = f"git failed: {redact_userinfo(str(exc))}"
                self._note_diag(job, "git_fail", error=result.error, error_class=type(exc).__name__)
                log_fail(logger, "git pipeline", job=job.job_id, err=result.error)
                self._post_note(job, result)
            return result
        except AzureError as exc:
            if should_stop():
                result.cancelled = True
                log_ok(logger, "job cancelled", job=job.job_id, stage="azure")
            else:
                result.error = f"azure failed: {redact_userinfo(str(exc))}"
                self._note_diag(
                    job,
                    "azure_fail",
                    error=result.error,
                    error_class=type(exc).__name__,
                    http=getattr(exc, "status_code", 0) or "",
                )
                log_fail(logger, "azure pipeline", job=job.job_id, err=result.error)
                self._post_note(job, result)
            return result
        except Exception as exc:  # noqa: BLE001
            if should_stop():
                result.cancelled = True
                log_ok(logger, "job cancelled", job=job.job_id, stage="pipeline")
                return result
            logger.exception("worker failed job=%s", job.job_id)
            result.error = f"pipeline failed: {redact_userinfo(str(exc))}"
            self._note_diag(job, "pipeline_fail", error=result.error, error_class=type(exc).__name__)
            log_fail(logger, "pipeline", job=job.job_id, err=result.error)
            self._post_note(job, result)
            return result
        finally:
            try:
                azure_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("azure unbind failed job=%s", job.job_id)
            if result.cancelled:
                self._post_note(job, result)
            if client is not None and result.session_id:
                try:
                    client.abort(result.session_id)
                except Exception:
                    pass
                client.close()
            if handle is not None:
                job.serve_pid = handle.pid
                job.serve_port = handle.port
                result.serve_pid = handle.pid
                result.serve_port = handle.port
            clone = Path(result.clone_path) if result.clone_path else None
            try:
                stop_job_holders(job, clone)
            except Exception:  # noqa: BLE001
                logger.exception("job-end stop_job_holders failed job=%s", job.job_id)
            stop_serve(handle)
            # Keep the clone. OSM deletes here; Creasy waits for MR close/merge.

    def _run_usage(self, job: JobRecord, should_stop: Callable[[], bool]) -> RunResult:
        result = RunResult()
        azure_cm = self._bind_azure(job)
        azure_cm.__enter__()
        try:
            if should_stop():
                result.cancelled = True
                log_ok(logger, "job cancelled", job=job.job_id, stage="usage")
                return result
            body = format_usage(job)
            via = self._post_result_body(job, body)
            result.posted = True
            result.text = body
            log_ok(
                logger,
                "post usage",
                provider=job.provider or "gitlab",
                job=job.job_id,
                via=via,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            result.error = f"post usage failed: {redact_userinfo(str(exc))}"
            log_fail(logger, "post usage", provider=job.provider or "gitlab", job=job.job_id, err=exc)
            return result
        finally:
            try:
                azure_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("azure unbind failed job=%s", job.job_id)

    def _load_change(self, job: JobRecord) -> MergeRequest:
        if self._is_azure(job):
            if self.azure is None:
                raise AzureError("azure not configured")
            if not job.azure_project or not job.azure_repo:
                raise AzureError("azure job is missing project or repo id")
            apply = getattr(self.azure, "apply_collection", None)
            if callable(apply):
                apply(getattr(job, "azure_collection", "") or "", job.web_url or "")
            return self.azure.get_pull_request(job.azure_project, job.azure_repo, job.mr_iid)
        return self.gitlab.get_merge_request(job.project_id, job.mr_iid)

    def _prompt(
        self,
        job: JobRecord,
        mr: MergeRequest,
        index,
        workspace: WorkspaceRecord,
        *,
        created_new: bool,
        previous_sha: str = "",
    ) -> str:
        if job.trigger == "ask":
            current = workspace.last_sha or mr.sha
            sha_changed = bool(previous_sha and current and previous_sha != current)
            question = format_code_comment_prompt(
                job.comment_text,
                path=getattr(job, "comment_path", "") or "",
                side=getattr(job, "comment_side", "") or "",
                start_line=int(getattr(job, "comment_start_line", 0) or 0),
                end_line=int(getattr(job, "comment_end_line", 0) or 0),
            )
            return build_ask_prompt(
                question,
                mr=mr,
                index=index,
                sha_changed=sha_changed,
                previous_sha=previous_sha,
                include_context=created_new or not workspace.session_id,
                parent_text=getattr(job, "parent_comment_text", "") or "",
            )
        extra = format_code_comment_prompt(
            job.comment_text,
            path=getattr(job, "comment_path", "") or "",
            side=getattr(job, "comment_side", "") or "",
            start_line=int(getattr(job, "comment_start_line", 0) or 0),
            end_line=int(getattr(job, "comment_end_line", 0) or 0),
        )
        if str(getattr(job, "discussion_id", "") or "").strip():
            return build_thread_review_prompt(
                mr,
                index,
                user_text=extra or (job.comment_text or ""),
                parent_text=getattr(job, "parent_comment_text", "") or "",
                path=getattr(job, "comment_path", "") or "",
                start_line=int(getattr(job, "comment_start_line", 0) or 0),
                end_line=int(getattr(job, "comment_end_line", 0) or 0),
            )
        return build_review_prompt(mr, index, extra_notes=extra)

    def _ensure_workspace(
        self,
        job: JobRecord,
        mr: MergeRequest,
        should_stop: Callable[[], bool],
    ) -> WorkspaceRecord:
        if should_stop():
            raise GitError("cancelled")
        dest = clone_path_for(self.config.work_dir, job.mr_key)
        record = self.workspaces.get(job.mr_key) or WorkspaceRecord(
            mr_key=job.mr_key,
            project_id=job.project_id,
            mr_iid=job.mr_iid,
        )
        if self._is_azure(job):
            http_url = mr.http_url or record.http_url
            if self.azure is not None and (not http_url or not str(http_url).lower().startswith("http")):
                http_url = self.azure.resolve_clone_url(job.azure_project, job.azure_repo, http_url)
            token = self.config.azure_token
            auth_scheme = "azure"
            logger.info(
                "azure clone using %s auth=pat token_chars=%s",
                http_url or "-",
                len(token or ""),
            )
        else:
            http_url = mr.http_url or self.gitlab.resolve_http_url(job.project_id, record.http_url)
            token = self.config.gitlab_token
            auth_scheme = "gitlab"
        if not http_url:
            raise GitError("no http repo url for project")
        git_kw = self._git_kw(job, should_stop)
        if not clone_is_usable(dest):
            if dest.exists():
                log_ok(logger, "re-clone workspace", path=dest, reason="unusable")
                delete_clone(dest)
            clone_repo(
                http_url,
                dest,
                token,
                timeout=self.config.git_timeout,
                auth_scheme=auth_scheme,
                **git_kw,
            )
        try:
            sha = fetch_and_checkout(
                dest,
                source_branch=mr.source_branch,
                target_branch=mr.target_branch,
                sha=mr.sha,
                token=token,
                timeout=self.config.git_timeout,
                auth_scheme=auth_scheme,
                **git_kw,
            )
        except GitError:
            if should_stop() or clone_is_usable(dest):
                raise
            log_ok(logger, "re-clone workspace", path=dest, reason="corrupt after fetch")
            if dest.exists():
                delete_clone(dest)
            clone_repo(
                http_url,
                dest,
                token,
                timeout=self.config.git_timeout,
                auth_scheme=auth_scheme,
                **git_kw,
            )
            sha = fetch_and_checkout(
                dest,
                source_branch=mr.source_branch,
                target_branch=mr.target_branch,
                sha=mr.sha,
                token=token,
                timeout=self.config.git_timeout,
                auth_scheme=auth_scheme,
                **git_kw,
            )
        record.clone_path = str(dest)
        record.source_branch = mr.source_branch
        record.target_branch = mr.target_branch
        record.last_sha = sha
        record.http_url = http_url
        record.web_url = mr.web_url
        record.last_job_id = job.job_id
        return self.workspaces.save(record)

    def _post_result_body(self, job: JobRecord, body: str) -> str:
        """Reply on the request thread when we have one; else post the overview."""
        discussion_id = str(getattr(job, "discussion_id", "") or "").strip()
        if self._is_azure(job):
            if self.azure is None:
                raise AzureError("azure not configured")
            if discussion_id:
                reply = getattr(self.azure, "reply_to_thread", None)
                if callable(reply):
                    try:
                        reply(
                            job.azure_project,
                            job.azure_repo,
                            job.mr_iid,
                            discussion_id,
                            body,
                            parent_comment_id=int(getattr(job, "parent_comment_id", 0) or 0),
                        )
                        return "thread"
                    except Exception as exc:  # noqa: BLE001
                        log_fail(
                            logger,
                            "reply request thread",
                            provider="azure",
                            job=job.job_id,
                            discussion=discussion_id,
                            err=exc,
                        )
            self.azure.post_overview(job.azure_project, job.azure_repo, job.mr_iid, body)
            return "overview"
        if discussion_id:
            reply = getattr(self.gitlab, "reply_to_discussion", None)
            if callable(reply):
                try:
                    reply(job.project_id, job.mr_iid, discussion_id, body)
                    return "thread"
                except Exception as exc:  # noqa: BLE001
                    log_fail(
                        logger,
                        "reply request thread",
                        provider="gitlab",
                        job=job.job_id,
                        discussion=discussion_id,
                        err=exc,
                    )
        self.gitlab.post_note(job.project_id, job.mr_iid, body)
        return "overview"

    def _post_note(
        self,
        job: JobRecord,
        result: RunResult,
        *,
        findings: Optional[list[Finding]] = None,
    ) -> None:
        if result.posted:
            return
        shadow = job.model_copy(update={
            "text": result.text,
            "error_message": result.error or None,
            "model": job.model or self.config.opencode_model,
        })
        if result.cancelled:
            body = format_cancelled(shadow)
        elif result.error and not result.text:
            body = format_failure(shadow)
        else:
            body = format_success(shadow)
        try:
            via = self._post_result_body(job, body)
            result.posted = True
            log_ok(
                logger,
                "post overview",
                provider=job.provider or "gitlab",
                job=job.job_id,
                mr=job.mr_iid,
                via=via,
                cancelled=result.cancelled,
                error=bool(result.error),
            )
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "post overview", provider=job.provider or "gitlab", job=job.job_id, mr=job.mr_iid, err=exc)
            if not result.error:
                result.error = f"post note failed: {exc}"
            return
        if result.cancelled or (result.error and not result.text):
            return
        if findings and job.trigger != "ask":
            self._post_discussions(job, result, findings)
        elif findings and job.trigger == "ask":
            log_ok(logger, "ask skips finding threads", job=job.job_id, findings=len(findings))
        self._submit_gitlab_review(job)

    def _ensure_parent_comment(self, job: JobRecord) -> None:
        if (getattr(job, "parent_comment_text", "") or "").strip():
            return
        if not str(getattr(job, "discussion_id", "") or "").strip() and not int(
            getattr(job, "parent_comment_id", 0) or 0
        ):
            return
        try:
            text = self._load_prior_thread_text(job)
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "load prior thread text", job=job.job_id, err=exc)
            return
        if text:
            job.parent_comment_text = text
            self._persist_job(job, "parent comment")
            log_ok(logger, "prior thread text", job=job.job_id, chars=len(text))

    def _same_thread_id(self, raw: Any, wanted: str) -> bool:
        left = str(raw or "").strip()
        right = str(wanted or "").strip()
        if not left or not right:
            return False
        if left == right:
            return True
        try:
            return int(left) == int(right)
        except ValueError:
            return False

    def _comment_plain(self, comment: dict) -> str:
        return plain_comment(str(comment.get("content") or comment.get("body") or ""))

    def _prior_from_comments(self, comments: list[dict], *, current_id: int, current_text: str) -> str:
        current_text = (current_text or "").strip()
        current = None
        if current_id:
            for comment in comments:
                if int(comment.get("id") or 0) == current_id:
                    current = comment
                    break
        if current is None and current_text:
            for comment in reversed(comments):
                body = self._comment_plain(comment)
                if body and current_text in body:
                    current = comment
                    break
        if current is not None:
            parent_id = int(
                current.get("parentCommentId")
                or current.get("parent_comment_id")
                or current.get("parentId")
                or 0
            )
            if parent_id:
                for comment in comments:
                    if int(comment.get("id") or 0) == parent_id:
                        return self._comment_plain(comment)
        skip_ids = {int(current.get("id") or 0)} if current is not None else set()
        if current_id:
            skip_ids.add(current_id)
        prior = [comment for comment in comments if int(comment.get("id") or 0) not in skip_ids]
        if current_text:
            prior = [comment for comment in prior if current_text not in self._comment_plain(comment)]
        if prior:
            return self._comment_plain(prior[-1])
        return ""

    def _load_prior_thread_text(self, job: JobRecord) -> str:
        discussion_id = str(getattr(job, "discussion_id", "") or "").strip()
        current_id = int(getattr(job, "parent_comment_id", 0) or 0)
        current_text = str(getattr(job, "comment_text", "") or "")
        if self._is_azure(job):
            if self.azure is None:
                return ""
            threads = self.azure.list_threads(job.azure_project, job.azure_repo, job.mr_iid)
            for thread in threads:
                comments = [c for c in (thread.get("comments") or []) if isinstance(c, dict)]
                if discussion_id and not self._same_thread_id(thread.get("id"), discussion_id):
                    if not current_id or not any(int(c.get("id") or 0) == current_id for c in comments):
                        continue
                elif not discussion_id:
                    if not current_id or not any(int(c.get("id") or 0) == current_id for c in comments):
                        continue
                text = self._prior_from_comments(comments, current_id=current_id, current_text=current_text)
                if text:
                    return text
            return ""
        if not discussion_id:
            return ""
        discussions = self.gitlab.list_discussions(job.project_id, job.mr_iid)
        for disc in discussions:
            if not self._same_thread_id(disc.get("id"), discussion_id):
                continue
            notes = [n for n in (disc.get("notes") or []) if isinstance(n, dict)]
            return self._prior_from_comments(notes, current_id=current_id, current_text=current_text)
        return ""

    def _submit_gitlab_review(self, job: JobRecord) -> None:
        if self._is_azure(job):
            return
        if (job.trigger or "") in {"ask", "usage"}:
            return
        submit = getattr(self.gitlab, "submit_review", None)
        if not callable(submit):
            return
        try:
            ok = submit(job.project_id, job.mr_iid)
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "submit review", job=job.job_id, mr=job.mr_iid, err=exc)
            return
        if ok:
            log_ok(logger, "submit review", job=job.job_id, mr=job.mr_iid)
        else:
            log_fail(logger, "submit review", job=job.job_id, mr=job.mr_iid, reason="gitlab returned false")

    def _post_discussions(
        self,
        job: JobRecord,
        result: RunResult,
        findings: list[Finding],
    ) -> None:
        azure_job = self._is_azure(job)
        poster = getattr(self.gitlab, "post_discussion", None)
        if not azure_job and not callable(poster):
            return
        first_iter, second_iter = 1, 1
        if azure_job and self.azure is not None:
            first_iter, second_iter, _sha = self.azure.iteration_span(
                job.azure_project, job.azure_repo, job.mr_iid
            )
            logger.info(
                "azure file threads will use iterations %s..%s",
                first_iter,
                second_iter,
            )
        clone = Path(result.clone_path) if result.clone_path else None
        if clone is None or not result.merge_base:
            log_fail(logger, "post discussions", job=job.job_id, reason="no clone or merge-base")
            return
        try:
            diffmap = parse_unified_diff(
                unified_diff(clone, result.merge_base, timeout=min(60.0, self.config.git_timeout))
            )
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "post discussions", job=job.job_id, reason="diff failed", err=exc)
            return
        existing = self._existing_creasy_threads(job)
        used: set[str] = set()
        posted = 0
        replies = 0
        skipped = 0
        for finding in findings:
            if azure_job:
                context = azure_thread_context(finding, diffmap)
                if not context:
                    logger.warning(
                        "skip finding job=%s path=%s lines=%s-%s: no Azure position",
                        job.job_id,
                        finding.path,
                        finding.start_line,
                        finding.end_line,
                    )
                    continue
                variants = [context]
            else:
                variants = []
                seen: list[dict] = []
                for base_sha, start_sha in discussion_sha_attempts(result):
                    for item in build_position_variants(
                        finding,
                        diffmap,
                        base_sha=base_sha,
                        start_sha=start_sha,
                        head_sha=result.sha,
                    ):
                        if item in seen:
                            continue
                        seen.append(item)
                        variants.append(item)
                if not variants:
                    logger.warning(
                        "skip finding job=%s path=%s lines=%s-%s: no GitLab position",
                        job.job_id,
                        finding.path,
                        finding.start_line,
                        finding.end_line,
                    )
                    continue
            body = format_discussion(finding)
            matched = match_creasy_thread(finding, existing, used)
            if matched and should_skip_similar_reply(body, matched.last_body):
                used.add(matched.discussion_id)
                skipped += 1
                logger.info(
                    "skip similar reply job=%s discussion=%s path=%s",
                    job.job_id,
                    matched.discussion_id,
                    finding.path,
                )
                continue
            if matched and self._reply_finding(
                job,
                matched.discussion_id,
                body,
                parent_comment_id=matched.root_comment_id,
            ):
                used.add(matched.discussion_id)
                posted += 1
                replies += 1
                continue
            last_error = ""
            for position in variants:
                try:
                    if azure_job:
                        assert self.azure is not None
                        self.azure.post_file_thread(
                            job.azure_project,
                            job.azure_repo,
                            job.mr_iid,
                            body,
                            position,
                            first_iteration=first_iter,
                            second_iteration=second_iter,
                        )
                    else:
                        poster(job.project_id, job.mr_iid, body, position)
                    posted += 1
                    last_error = ""
                    break
                except (GitLabError, AzureError) as exc:
                    last_error = str(exc)
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    break
            if last_error:
                log_fail(
                    logger,
                    "post discussion",
                    job=job.job_id,
                    path=finding.path,
                    line=finding.start_line,
                    err=last_error,
                )
        result.findings_posted = posted
        if posted or skipped:
            log_ok(
                logger,
                "post discussions",
                posted=posted,
                replies=replies,
                skipped_similar=skipped,
            )
            self._append_job_log(
                job,
                f"posted {posted} diff thread(s) replies={replies} skipped_similar={skipped}",
            )

    def _existing_creasy_threads(self, job: JobRecord):
        if self._is_azure(job):
            if self.azure is None:
                return []
            try:
                return parse_azure_threads(
                    self.azure.list_threads(job.azure_project, job.azure_repo, job.mr_iid)
                )
            except Exception as exc:  # noqa: BLE001
                log_fail(logger, "list Azure threads", job=job.job_id, err=exc)
                return []
        lister = getattr(self.gitlab, "list_discussions", None)
        if not callable(lister):
            return []
        try:
            return parse_creasy_threads(lister(job.project_id, job.mr_iid))
        except Exception as exc:  # noqa: BLE001
            log_fail(logger, "list GitLab discussions", job=job.job_id, err=exc)
            return []

    def _reply_finding(
        self,
        job: JobRecord,
        discussion_id: str,
        body: str,
        *,
        parent_comment_id: int = 0,
    ) -> bool:
        if self._is_azure(job):
            if self.azure is None:
                return False
            try:
                self.azure.reply_to_thread(
                    job.azure_project,
                    job.azure_repo,
                    job.mr_iid,
                    discussion_id,
                    body,
                    parent_comment_id=parent_comment_id or 0,
                )
                log_ok(logger, "reply discussion", provider="azure", job=job.job_id, discussion=discussion_id)
                return True
            except Exception as exc:  # noqa: BLE001
                log_fail(
                    logger,
                    "reply discussion",
                    provider="azure",
                    job=job.job_id,
                    discussion=discussion_id,
                    parent=parent_comment_id,
                    err=exc,
                )
                return False
        replier = getattr(self.gitlab, "reply_to_discussion", None)
        if not callable(replier):
            return False
        try:
            replier(job.project_id, job.mr_iid, discussion_id, body)
            log_ok(logger, "reply discussion", provider="gitlab", job=job.job_id, discussion=discussion_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log_fail(
                logger,
                "reply discussion",
                provider="gitlab",
                job=job.job_id,
                discussion=discussion_id,
                err=exc,
            )
            return False
