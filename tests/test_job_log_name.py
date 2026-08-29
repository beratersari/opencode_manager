from opencode_manager.log import job_log_filename


def test_job_log_filename_format() -> None:
    name = job_log_filename("PROJ-1", "job_abc123", "2026-08-29T16:48:17.335Z")
    assert name == "PROJ-1_job_abc123_20260829_164817.log"


def test_job_log_filename_sanitizes() -> None:
    name = job_log_filename("PROJ/1", "job_x", "2026-01-02T03:04:05Z")
    assert name == "PROJ_1_job_x_20260102_030405.log"
    assert "/" not in name
