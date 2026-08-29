# Fixed conditions

These were real defects. They are closed. Do not re-open them as
review findings. The pytest names below must keep failing if the
bug returns.

`agents.md` cannot be a directory next to `AGENTS.md` on a
case-insensitive disk (macOS). This folder is `agents/`.

## Clone cleanup

| Condition | Test |
|---|---|
| `GitError` after dest is created still hard-deletes the clone | `test_git_error_after_partial_clone_deletes_dest` |
| Missing remote branch (404) still deletes leftover dest | `test_missing_branch_404_deletes_leftover_dest` |
| `JobFailed` from OpenCode still deletes the clone | `test_opencode_jobfailed_still_deletes_clone` |
| Unexpected OpenCode exception still deletes the clone | `test_opencode_unexpected_exception_still_deletes_clone` |
| Leftover dest is gone before `git clone` | `test_leftover_clone_gone_before_git_clone` |
| If leftover cannot be deleted, do not clone (500) | `test_no_clone_if_leftover_cannot_be_removed` |
| `subprocess.TimeoutExpired` from git becomes `GitError` | `test_subprocess_timeout_on_git_becomes_giterror` |
| `subprocess.TimeoutExpired` from ls-remote becomes `GitError` | `test_subprocess_timeout_on_ls_remote_becomes_giterror` |
| Git timeout after dest is created still deletes dest | `test_git_timeout_from_ls_remote_deletes_dest` |

## Serve boot

| Condition | Test |
|---|---|
| Health timeout is an outer attempt (`retry_count` used) | `test_serve_health_timeout_uses_all_retry_attempts` |
| Health `TimeoutError` does not escape `run_opencode_job` | `test_serve_boot_timeout_does_not_escape_as_timeouterror` |
| `{pid, port}` recorded in `on_spawn` before health wait | `test_serve_pid_recorded_on_spawn_before_health_fails` |
| `on_spawn` runs before `wait_health` | `test_start_serve_on_spawn_runs_before_wait_health` |
| Failed health wait kills the child process | `test_start_serve_kills_child_when_health_times_out` |

## Clone path

| Condition | Test |
|---|---|
| Folder is `{work_dir}/{jira_id}` only | `test_clone_path_is_ticket_only` |
| Repo and branch are not in the folder name | `test_clone_path_ignores_repo_and_branch` |

## Branch existence and origin

| Condition | Test |
|---|---|
| `ls-remote` `develop` is not a hit for `develop-old` / `dev` | `test_ls_remote_ref_is_exact_heads_name`, `test_ls_remote_prefix_output_is_not_a_hit` |
| Origin scrub keeps `host:port` | `test_public_git_url_keeps_nondefault_port` |
| Git child PID is tracked on the job while the process is live | `test_run_git_tracks_and_untracks_pid` |

## Kill / boot / shutdown

| Condition | Test |
|---|---|
| Windows `rd` uses `\\?\\` | `test_win_extended_path_and_rd_cmd` |
| Reap kills a process whose argv mentions the clone | `test_reap_path_kills_argv_match` |
| Boot kills leftover recorded `serve_pid` | `test_boot_kills_recorded_serve_pid` |
| Shutdown kills `extra_pids`, sends one `500`, then `POST /jobs` is `503` | `test_shutdown_kills_extra_pids_and_rejects_submit` |
| Missing dequeued store row does not stall the queue | `test_on_done_skips_missing_queue_row` |

## Dashboard list

| Condition | Test |
|---|---|
| Error/Completed filters paginate the filtered set, not page-then-filter | `test_jobs_filter_paginates_filtered_set` |
| Queue search honors `jira_id` | `test_queue_jira_filter` |

## Job-end kill + delete (every terminal path)

Fake (mocked git / OpenCode) and real (OS children + `file://` git) matrices
live in `tests/test_job_end_paths_fake.py` and
`tests/test_job_end_paths_real.py`. They lock: success / 404 / 500 / 504 /
crash / shutdown all kill this job’s holders and delete the clone; mid-retry
keeps the clone.

Run:

```
pytest tests/test_fixed_conditions.py tests/test_cleanup_and_serve_boot.py tests/test_git.py tests/test_git_branch_and_origin.py tests/test_cleanup_pipeline.py tests/test_shutdown_and_boot_reap.py tests/test_dashboard_filters.py tests/test_job_end_paths_fake.py tests/test_job_end_paths_real.py
```
