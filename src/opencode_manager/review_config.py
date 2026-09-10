"""Review-path settings. Tokens live here, never on POST /jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opencode_manager.settings import Settings


@dataclass
class ReviewConfig:
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    webhook_secret: str = ""
    opencode_model: str = "opencode/big-pickle"
    opencode_timeout: int = 1800
    opencode_retry_count: int = 2
    opencode_agent: str = "code-reviewer"
    opencode_bin: str = "opencode"
    max_concurrent_jobs: int = 2
    data_dir: Path = Path(".")
    skip_draft_mrs: bool = True
    log_level: str = "INFO"
    git_timeout: int = 600
    serve_health_timeout: int = 60
    hang_timeout: int = 300
    azure_url: str = ""
    azure_token: str = ""
    azure_api_version: str = "7.1"
    azure_webhook_user: str = ""
    azure_webhook_password: str = ""
    review_mention: str = ""
    work_dir: Path = Path(".")
    job_dir: Path = Path(".")
    log_dir: Path = Path(".")
    serve_dir: Path = Path(".")
    opencode_model_env: str = ""
    opencode_timeout_env: int = 0
    opencode_agent_env: str = ""

    @property
    def azure_enabled(self) -> bool:
        return bool(self.azure_url and self.azure_token)

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.work_dir, self.job_dir, self.log_dir, self.serve_dir):
            path.mkdir(parents=True, exist_ok=True)


# Copied Creasy modules import Config.
Config = ReviewConfig


def review_config_from_settings(settings: Settings) -> ReviewConfig:
    data_dir = Path(settings.data_dir)
    cfg = ReviewConfig(
        gitlab_url=(settings.gitlab_url or "https://gitlab.com").rstrip("/"),
        gitlab_token=(settings.gitlab_token or "").strip(),
        webhook_secret=(settings.webhook_secret or "").strip(),
        opencode_model=(settings.review_model or "opencode/big-pickle").strip() or "opencode/big-pickle",
        opencode_timeout=max(1, int(settings.review_timeout_seconds)),
        opencode_retry_count=max(1, int(settings.review_retry_count)),
        opencode_agent=(settings.review_agent or "code-reviewer").strip() or "code-reviewer",
        opencode_bin=(settings.opencode_bin or "opencode").strip() or "opencode",
        max_concurrent_jobs=max(1, int(settings.max_concurrent_reviews)),
        data_dir=data_dir,
        skip_draft_mrs=bool(settings.skip_draft_mrs),
        log_level=settings.log_level,
        git_timeout=max(30, int(settings.git_clone_timeout_seconds)),
        serve_health_timeout=max(5, int(settings.review_serve_health_timeout)),
        hang_timeout=max(30, int(settings.hang_timeout_seconds)),
        azure_url=(settings.azure_url or "").strip().rstrip("/"),
        azure_token=(settings.azure_token or "").strip(),
        azure_api_version=(settings.azure_api_version or "7.1").strip() or "7.1",
        azure_webhook_user=(settings.azure_webhook_user or "").strip(),
        azure_webhook_password=(settings.azure_webhook_password or "").strip(),
        review_mention=(settings.review_mention or "").strip(),
        work_dir=data_dir / "workspaces",
        job_dir=Path(settings.job_store_dir or (data_dir / "jobs")),
        log_dir=Path(settings.job_log_dir or (data_dir / "logs")),
        serve_dir=Path(settings.serve_dir or (data_dir / ".serve")),
        opencode_model_env=(settings.review_model or "opencode/big-pickle").strip(),
        opencode_timeout_env=max(1, int(settings.review_timeout_seconds)),
        opencode_agent_env=(settings.review_agent or "code-reviewer").strip() or "code-reviewer",
    )
    cfg.ensure_dirs()
    return cfg
