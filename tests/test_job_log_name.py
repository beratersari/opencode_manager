from opencode_manager.log import clip, fmt_cmd, job_log_filename, read_job_log_lines


def test_job_log_filename_format() -> None:
    name = job_log_filename("PROJ-1", "job_abc123", "2026-08-29T16:48:17.335Z")
    assert name == "PROJ-1_job_abc123_20260829_164817.log"


def test_job_log_filename_sanitizes() -> None:
    name = job_log_filename("PROJ/1", "job_x", "2026-01-02T03:04:05Z")
    assert name == "PROJ_1_job_x_20260102_030405.log"
    assert "/" not in name


def test_fmt_cmd_redacts_userinfo() -> None:
    line = fmt_cmd(["git", "ls-remote", "https://oauth2:supersecret@gitlab.example/r.git"])
    assert "supersecret" not in line
    assert "***" in line
    assert "ls-remote" in line


def test_clip_redacts_and_truncates() -> None:
    assert "secret" not in clip("https://user:secret@host/r")
    long = "x" * 200
    assert clip(long, 20).endswith("chars)")


def test_read_job_log_lines_limit_zero_returns_all(tmp_path) -> None:
    name = "T_job_x_20260102_030405.log"
    path = tmp_path / name
    path.write_text("\n".join(f"line {i}" for i in range(5)) + "\n", encoding="utf-8")
    all_lines = read_job_log_lines(tmp_path, "T", "job_x", log_file=name, limit=0)
    last_two = read_job_log_lines(tmp_path, "T", "job_x", log_file=name, limit=2)
    assert [row["message"] for row in all_lines] == [f"line {i}" for i in range(5)]
    assert [row["message"] for row in last_two] == ["line 3", "line 4"]
