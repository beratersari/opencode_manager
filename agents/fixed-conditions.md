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
| After health, wait for the directory instance before `POST /session` | `test_instance_wait_runs_before_session_create`, `test_wait_directory_retries_until_session_list_returns` |
| Directory instance timeout is `serve-dead` (not create-fail) | `test_instance_wait_timeout_is_serve_dead` |

## Clone path

| Condition | Test |
|---|---|
| Folder is `{work_dir}/{jira_id}` only | `test_clone_path_is_ticket_only` |
| Repo and branch are not in the folder name | `test_clone_path_ignores_repo_and_branch` |
| `.` / `..` / slashes are inbound 400 (no shared folder) | `test_unsafe_jira_id_is_400`, `test_clone_path_rejects_dot_slash_and_collision` |
| OpenCode `busy` is not compact; `time.compacting` is | `test_opencode_busy_is_not_compacting_without_time_field` |
| Compact-as-busy does not hang the attempt | `test_inner_loop_does_not_hang_while_session_time_compacting` |
| Hang does not fire after an assistant this turn | `test_hang_does_not_fire_after_assistant_this_turn` |
| Job JSON Access Denied is retried, not a worker crash | `tests/test_job_store_save.py` |
| Hang does not fire if list_messages fails after an assistant | `test_hang_does_not_fire_when_list_fails_after_assistant` |
| Queue JSON replace retries Access Denied | `test_queue_survives_replace_access_denied` |
| Atomic tmp write retries; total lock still raises | `test_atomic_retries_tmp_write`, `test_atomic_raises_when_replace_and_inplace_fail` |
| Hang still fires if never answered and list_messages is down | `test_hang_still_fires_when_never_answered_and_list_fails` |
| close_serve abort/stop/save explosions do not escape | `test_close_serve_explosions_do_not_escape` |
| Running-row save failure still accepts the job | `test_submit_survives_running_row_save_failure` |

## Branch existence and origin

| Condition | Test |
|---|---|
| `ls-remote` `develop` is not a hit for `develop-old` / `dev` | `test_ls_remote_ref_is_exact_heads_name`, `test_ls_remote_prefix_output_is_not_a_hit` |
| Origin scrub keeps `host:port` | `test_public_git_url_keeps_nondefault_port` |
| Windows uses stored GCM/wincred or a GCM popup | `test_windows_uses_stored_creds_or_gcm_popup` |
| Linux still disables `credential.helper` | `test_linux_still_disables_credential_helper` |
| Git child PID is tracked on the job while the process is live | `test_run_git_tracks_and_untracks_pid` |
| Direct clone leaves a clean stored origin | `test_clone_repo_leaves_clean_origin` |
| OSM clones only; never checks out `source_branch` | `tests/test_clone_no_checkout.py` |
| Origin scrub keeps `host:port` after userinfo | `test_direct_clone_scrubs_stored_userinfo_and_keeps_port` |

## Kill / boot / shutdown

| Condition | Test |
|---|---|
| Windows `rd` uses `\\?\\` | `test_win_extended_path_and_rd_cmd` |
| Reap kills a process whose argv mentions the clone | `test_reap_path_kills_argv_match` |
| Windows process JSON empty/invalid does not raise | `test_parse_windows_process_json_empty_and_invalid` |
| Windows cwd walk is only for clone-tool images | `test_windows_cwd_candidate_is_clone_tools_only` |
| Job-end skips the process scan when the clone was never created | `test_stop_job_holders_skips_scan_when_clone_missing` |
| n8n `source_branch=-1` is inbound 400 | `test_source_branch_dash_one_is_missing` |
| System images never get a Windows cwd PEB read | `test_iter_windows_processes_skips_cwd_for_system_images` |
| Holder-stop error still deletes the clone | `test_job_end_deletes_clone_if_stop_holders_raises` |
| `stop_job_holders` continues after `reap_path` fails | `test_stop_job_holders_survives_reap_error` |
| Job-end / boot / kill explosions leave the API up | `tests/test_manager_stays_up.py` |
| Many bad/good inbound and worker failures leave the API up | `tests/test_stay_up_matrix.py` |
| Extensive good/bad HTTP + worker crash-input matrix | `tests/test_crash_inputs.py` |
| Boot kills leftover recorded `serve_pid` | `test_boot_kills_recorded_serve_pid` |
| Shutdown kills `extra_pids`, sends one `500`, then `POST /jobs` is `503` | `test_shutdown_kills_extra_pids_and_rejects_submit` |
| Missing dequeued store row does not stall the queue | `test_on_done_skips_missing_queue_row` |

## Callback HTTP

| Condition | Test |
|---|---|
| Callback HTTP `200` is delivered (no second POST) | `test_callback_200_stops_without_retry` |
| Wait `404` then `200` retries the same envelope | `test_callback_404_then_200_retries` |
| Permanent `400` / `405` do not retry | `test_callback_400_does_not_retry`, `test_callback_405_does_not_retry` |
| `503` then `200` still retries | `test_callback_503_then_200_retries` |

## Model inventory

| Condition | Test |
|---|---|
| Unknown `model` fails the job before any user POST | `test_unknown_model_fails_job_before_prompt` |
| Readable empty inventory fails the job before any user POST | `test_empty_model_inventory_fails_job_before_prompt` |
| OpenCode model-not-found is job 500, not attempt timeout | `test_prompt_async_unknown_model_fails_job_immediately` |

## Idle assess

| Condition | Test |
|---|---|
| Last-turn `finish=length` (max tokens) is incomplete, not success | `test_assess_length_finish_is_incomplete` |
| Unknown unfinished finish is incomplete, not a fall-through success | `test_assess_unknown_unfinished_finish_is_incomplete` |

## Compact loop

| Condition | Test |
|---|---|
| Compact-loop (~8) ignores markers already in the session | `test_historical_compact_markers_do_not_trigger_compact_loop_nudge` |
| Eight **new** compact markers this wait still nudge | `test_eight_new_compact_markers_this_wait_trigger_nudge` |

## Dashboard list

| Condition | Test |
|---|---|
| Error/Completed filters paginate the filtered set, not page-then-filter | `test_jobs_filter_paginates_filtered_set` |
| Queue search honors `jira_id` | `test_queue_jira_filter` |
| Finished-job `/chat` does not mix later turns from a shared `ses_*` | `test_chat_api_does_not_mix_later_session_turns` |
| Live `/chat` does not GET OpenCode `/session/-1/message` | `test_live_chat_does_not_call_opencode_with_dash_one_session`, `test_client_skips_http_for_unusable_session`, `test_inbound_dash_one_session_is_treated_as_none` |

## Logs

| Condition | Test |
|---|---|
| Azure `https://PAT@host/…` is not written to `app.log` | `test_azure_username_pat_url_is_redacted_in_app_log` |
| `oauth2:PAT@` and `:PAT@` userinfo are redacted in `app.log` | `test_gitlab_oauth2_and_colon_userinfo_redacted_in_app_log` |

## Job-end kill + delete (every terminal path)

Fake (mocked git / OpenCode) and real (OS children + `file://` git) matrices
live in `tests/test_job_end_paths_fake.py` and
`tests/test_job_end_paths_real.py`. They lock: success / 404 / 500 / 504 /
crash / shutdown all kill this job’s holders and delete the clone; mid-retry
keeps the clone.

Run:

```
pytest tests/test_fixed_conditions.py tests/test_cleanup_and_serve_boot.py tests/test_git.py tests/test_git_branch_and_origin.py tests/test_cleanup_pipeline.py tests/test_manager_stays_up.py tests/test_shutdown_and_boot_reap.py tests/test_dashboard_filters.py tests/test_job_end_paths_fake.py tests/test_job_end_paths_real.py tests/test_gitlab_pat_origin_scrub_e2e.py tests/test_job_chat_isolation_e2e.py tests/test_inbound_log_redact_e2e.py
```
