import type { ChatMessage, ChatPart, JobItem, LogLine, PromptRow, ReportContext, ReportLogBlob } from '../api/types'

export const REPORT_NOTE_MIN = 20
export const REPORT_JOB_MAX = 10

export function reportNoteReady(note: string): boolean {
  return note.trim().length >= REPORT_NOTE_MIN
}

export type JobReportBundle = {
  job: JobItem
  prompts?: PromptRow[]
  messages?: ChatMessage[]
  sessionIds?: string[]
  logs?: LogLine[]
  serveLog?: string
  serveLogMissing?: boolean
  error?: string
}

export type JobReportInput = {
  kind?: 'job' | 'general' | 'jobs'
  job?: JobItem | null
  prompts?: PromptRow[]
  messages?: ChatMessage[]
  sessionIds?: string[]
  logs?: LogLine[]
  serveLog?: string
  serveLogMissing?: boolean
  bundles?: JobReportBundle[]
  context?: ReportContext | null
  contextError?: string | null
  recentJobs?: JobItem[]
  note: string
  exportedAt: string
}

function safeName(value: string): string {
  const cleaned = (value || '').replace(/[^\w.-]+/g, '_')
  return cleaned.replace(/^[._]+|[._]+$/g, '') || 'file'
}

function stampFromIso(exportedAt: string): string {
  const compact = (exportedAt || '').replace(/[-:]/g, '').replace('T', '-').replace(/\..*$/, '')
  return compact.slice(0, 15) || 'export'
}

export function reportZipName(
  job?: Pick<JobItem, 'jira_id' | 'job_id'> | null,
  exportedAt?: string,
  jobs?: Array<Pick<JobItem, 'jira_id' | 'job_id'>> | null,
): string {
  const stamp = stampFromIso(exportedAt || new Date().toISOString())
  const many = (jobs || []).filter((row) => row && row.job_id)
  if (many.length > 1) {
    return `osm-report-multi-${many.length}-${stamp}.zip`
  }
  if (many.length === 1) {
    job = many[0]
  }
  if (!job) return `osm-report-general-${stamp}.zip`
  const ticket = safeName(job.jira_id || 'ticket')
  const id = safeName(job.job_id || 'job')
  return `osm-report-${ticket}-${id}-${stamp}.zip`
}

export function jobReportFolder(job: Pick<JobItem, 'jira_id' | 'job_id'>): string {
  return `jobs/${safeName(job.jira_id || 'ticket')}_${safeName(job.job_id || 'job')}`
}

function redactSecrets(text: string): string {
  let out = text || ''
  out = out.replace(/(:\/\/)([^@\s]+):([^@\s]+)@/g, '$1***:***@')
  out = out.replace(/(:\/\/):([^@\s]+)@/g, '$1:***@')
  out = out.replace(/(:\/\/)([^@\s]+)@/g, '$1***@')
  out = out.replace(/([?&](?:private_token|access_token|token|api[_-]?key)=)([^&\s"']+)/gi, '$1***')
  out = out.replace(/\b((?:OPENAI|ANTHROPIC|OPENROUTER)?[_-]?API[_-]?KEY\s*[=:]\s*)(\S+)/gi, '$1***')
  out = out.replace(/\bsk-(?:ant-|or-v1-|live-)?[A-Za-z0-9_-]{8,}/gi, 'sk-***')
  return out
}

function jsonFile(payload: unknown): string {
  return `${redactSecrets(JSON.stringify(payload, null, 2))}\n`
}

function logText(blob: ReportLogBlob | undefined, missingNote: string): string {
  if (!blob || blob.missing) return missingNote.endsWith('\n') ? missingNote : `${missingNote}\n`
  const text = blob.text || ''
  if (!text) return ''
  return text.endsWith('\n') ? text : `${text}\n`
}

function jobAsRecord(job: JobItem): Record<string, unknown> {
  return job as unknown as Record<string, unknown>
}

function jobParameters(job: JobItem) {
  const extra = jobAsRecord(job)
  return {
    job_id: job.job_id,
    jira_id: job.jira_id,
    status: job.status,
    live: job.live,
    job_kind: job.job_kind || extra.job_kind || '',
    provider: job.provider || extra.provider || '',
    trigger: job.trigger || extra.trigger || '',
    explicit: extra.explicit ?? null,
    source: job.source || extra.source || '',
    mr_title: job.mr_title || extra.mr_title || '',
    mr_key: job.mr_key || extra.mr_key || '',
    project_id: extra.project_id ?? null,
    mr_iid: extra.mr_iid ?? null,
    web_url: job.web_url || extra.web_url || '',
    agent_mode: job.agent_mode || extra.agent || '',
    agent: extra.agent || '',
    model: job.model || '',
    session_id: job.session_id || '',
    session_bound: extra.session_bound ?? null,
    log_file: extra.log_file || '',
    repo_url: job.repo_url || '',
    source_branch: job.source_branch || '',
    target_branch: extra.target_branch || '',
    clone_path: job.clone_path || '',
    serve_pid: job.serve_pid ?? extra.serve_pid ?? null,
    serve_port: job.serve_port ?? extra.serve_port ?? null,
    serve_base_url: extra.serve_base_url || '',
    timeout_in_seconds: job.timeout_in_seconds ?? null,
    retry_count: job.retry_count ?? null,
    attempt: job.attempt ?? null,
    started_at: job.started_at || null,
    completed_at: job.completed_at || null,
    accepted_at: job.accepted_at || null,
    updated_at: extra.updated_at || null,
    error_message: job.error_message || extra.error_message || null,
    error_class: extra.error_class || '',
    callback_status_code: job.callback_status_code ?? null,
    original_posted: job.original_posted ?? extra.original_posted ?? false,
    sha: extra.sha || '',
    base_sha: extra.base_sha || '',
    merge_base: extra.merge_base || '',
    azure_project: extra.azure_project || '',
    azure_repo: extra.azure_repo || '',
    azure_collection: extra.azure_collection || '',
    discussion_id: extra.discussion_id || '',
    parent_comment_id: extra.parent_comment_id ?? null,
    comment_path: extra.comment_path || '',
    comment_side: extra.comment_side || '',
    comment_start_line: extra.comment_start_line ?? null,
    comment_end_line: extra.comment_end_line ?? null,
    comment_text: extra.comment_text || job.comment_text || '',
    parent_comment_text: extra.parent_comment_text || job.parent_comment_text || '',
    diff_stat: extra.diff_stat || '',
    changed_paths: extra.changed_paths || [],
    result_chars: (job.text || '').length,
    retry_attempts: job.attempts || extra.attempts || [],
  }
}

function looksLikeProblem(line: string): boolean {
  return /fail|error|timed out|timeout|traceback|exception|readtimeout|serve-dead|hang|pipeline failed/i.test(
    line,
  )
}

function lastProblemLines(text: string, limit = 25): string[] {
  const rows = (text || '')
    .split(/\r?\n/)
    .map((line) => line.trimEnd())
    .filter((line) => line && looksLikeProblem(line))
  return rows.slice(-limit)
}

function selectedBundles(input: JobReportInput): JobReportBundle[] {
  if (input.bundles && input.bundles.length) return input.bundles
  if (input.job) {
    return [
      {
        job: input.job,
        prompts: input.prompts,
        messages: input.messages,
        sessionIds: input.sessionIds,
        logs: input.logs,
        serveLog: input.serveLog,
        serveLogMissing: input.serveLogMissing,
      },
    ]
  }
  return []
}

export function buildIssueSummary(input: JobReportInput): string {
  const bundles = selectedBundles(input)
  const job = bundles.length === 1 ? bundles[0].job : input.job
  const extra = job ? jobAsRecord(job) : {}
  const ctx = input.context
  const settings = (ctx?.settings || {}) as Record<string, unknown>
  const runtime = (ctx?.runtime || {}) as Record<string, unknown>
  const manager = (ctx?.manager || {}) as Record<string, unknown>
  const jobsSummary = ctx?.jobs_summary
  const logs = (input.logs || []).map((line) => line.message).join('\n')
  const serve = input.serveLog || ''
  const appLog = ctx?.app_log?.text || ''
  const crashLog = ctx?.crash_log?.text || ''
  const wrapperLog = ctx?.wrapper_exit_log?.text || ''
  const chatStats = chatStatsFrom(input.messages || [])
  const lines: string[] = [
    'START HERE — aMIR-mini issue summary',
    '',
    `kind: ${bundles.length > 1 ? 'jobs' : job ? 'job' : 'general'}`,
    `selected_jobs: ${bundles.length}`,
    `exported_at: ${input.exportedAt}`,
    `app: ${ctx?.meta?.app_name || 'aMIR-mini'} ${ctx?.meta?.version || runtime.osm_version || ''}`,
    `server_time: ${ctx?.meta?.server_time || ctx?.server_time || '-'}`,
    `platform: ${runtime.platform || runtime.system || '(unknown)'} ${runtime.machine || ''}`,
    `python: ${clipText(String(runtime.python || ''), 120)}`,
    `pid: ${runtime.pid ?? '-'}  cwd: ${runtime.cwd || '-'}  frozen: ${runtime.frozen ?? '-'}`,
    `git: ${JSON.stringify((runtime.cli_versions as Record<string, unknown> | undefined)?.git || runtime.which || '')}`,
    `opencode: ${JSON.stringify((runtime.cli_versions as Record<string, unknown> | undefined)?.opencode || '')}`,
    `which.git: ${(runtime.which as Record<string, unknown> | undefined)?.git || '-'}`,
    `which.opencode: ${(runtime.which as Record<string, unknown> | undefined)?.opencode || '-'}`,
    '',
    'Manager',
    `  ready: ${manager.ready ?? '-'}  stopping: ${manager.stopping ?? '-'}`,
    `  n8n: running=${ctx?.live?.n8n_running ?? manager.n8n_running ?? '-'} queued=${ctx?.live?.n8n_queued ?? manager.n8n_queued ?? '-'}`,
    `  review: running=${ctx?.live?.review_running ?? manager.review_running ?? '-'} queued=${ctx?.live?.review_queued ?? manager.review_queued ?? '-'}`,
    `  live totals: running=${ctx?.live?.running ?? '-'} queued=${ctx?.live?.queued ?? '-'}`,
    '',
  ]
  if (bundles.length > 1) {
    lines.push(`Selected jobs (${bundles.length})`)
    for (const [index, bundle] of bundles.entries()) {
      const row = bundle.job
      const rowExtra = jobAsRecord(row)
      lines.push(
        `  ${index + 1}. ${row.job_id}  ${row.jira_id}`,
        `     status=${row.status} live=${row.live} kind=${row.job_kind || 'ticket'} trigger=${row.trigger || '-'}`,
        `     error=${row.error_message || rowExtra.error_message || '(none)'}`,
        `     model=${row.model || '-'} session=${row.session_id || '-'}`,
        `     folder=${jobReportFolder(row)}/`,
        `     fetch_error=${bundle.error || '-'}`,
      )
    }
    lines.push('')
  } else if (job) {
    lines.push(
      'Job',
      `  job_id: ${job.job_id}`,
      `  ticket / mr: ${job.jira_id}`,
      `  kind: ${job.job_kind || 'ticket'}  provider: ${job.provider || extra.provider || '-'}  trigger: ${job.trigger || '-'}`,
      `  status: ${job.status}  live: ${job.live}  callback_status: ${job.callback_status_code ?? '-'}`,
      `  title: ${(job.mr_title || '').trim() || '-'}`,
      `  source: ${job.source || extra.source || '-'}`,
      `  error: ${job.error_message || extra.error_message || '(none)'}`,
      `  model: ${job.model || '-'}  agent: ${job.agent_mode || extra.agent || '-'}`,
      `  session: ${job.session_id || '-'}  session_bound: ${extra.session_bound ?? '-'}`,
      `  original_posted: ${job.original_posted ?? extra.original_posted ?? '-'}`,
      `  attempt: ${job.attempt ?? '-'} / ${job.retry_count ?? '-'}`,
      `  serve: pid=${job.serve_pid ?? extra.serve_pid ?? '-'} port=${job.serve_port ?? extra.serve_port ?? '-'} url=${extra.serve_base_url || '-'}`,
      `  timeout_in_seconds: ${job.timeout_in_seconds ?? '-'}`,
      `  repo: ${job.repo_url || '-'}`,
      `  branches: ${job.source_branch || '-'} -> ${extra.target_branch || '-'}`,
      `  sha: ${extra.sha || '-'}  base_sha: ${extra.base_sha || '-'}  merge_base: ${extra.merge_base || '-'}`,
      `  clone_path: ${job.clone_path || '-'}`,
      `  log_file: ${extra.log_file || '-'}`,
      `  accepted: ${job.accepted_at || '-'}`,
      `  started: ${job.started_at || '-'}  completed: ${job.completed_at || '-'}  updated: ${extra.updated_at || '-'}`,
      `  prompts_posted: ${(input.prompts || []).map((p) => p.id).join(', ') || '(none)'} (${(input.prompts || []).length})`,
      `  chat_messages: ${(input.messages || []).length}  sessions: ${(input.sessionIds || []).join(', ') || '-'}`,
      `  chat_roles: ${fmtCountMap(chatStats.roles)}`,
      `  chat_parts: ${fmtCountMap(chatStats.partTypes)}  tools: ${fmtCountMap(chatStats.tools)}`,
      `  result_chars: ${(job.text || '').length}`,
      `  job_log_lines: ${(input.logs || []).length}  serve_log_chars: ${serve.length}  serve_missing: ${input.serveLogMissing ?? '-'}`,
      '',
    )
    if (extra.comment_text || extra.parent_comment_text || extra.discussion_id) {
      lines.push(
        'Review comment',
        `  discussion_id: ${extra.discussion_id || '-'}  parent_comment_id: ${extra.parent_comment_id ?? '-'}`,
        `  path: ${extra.comment_path || '-'}  side: ${extra.comment_side || '-'}  lines: ${extra.comment_start_line ?? '-'}..${extra.comment_end_line ?? '-'}`,
        `  comment_chars: ${String(extra.comment_text || '').length}  parent_chars: ${String(extra.parent_comment_text || '').length}`,
        '',
      )
    }
    const attempts = job.attempts || []
    if (attempts.length) {
      lines.push('Attempts')
      for (const row of attempts) {
        lines.push(
          `  #${row.number} ${row.kind || '-'} prompt=${row.prompt_id || '-'} session=${row.session_id || '-'} ended=${row.ended_at || '-'} err=${row.error || '-'}`,
        )
      }
      lines.push('')
    }
  } else {
    lines.push(
      'History snapshot',
      `  jobs_total: ${jobsSummary?.total ?? (input.recentJobs || []).length}`,
      `  by_status: ${fmtCountMap(jobsSummary?.by_status || countBy(input.recentJobs || [], (j) => j.status))}`,
      `  by_kind: ${fmtCountMap(jobsSummary?.by_kind || countBy(input.recentJobs || [], (j) => j.job_kind || 'ticket'))}`,
      `  live_rows: ${(jobsSummary?.live || []).length}`,
      `  n8n_queue_items: ${(ctx?.queue?.items || []).length}`,
      `  review_queue_items: ${(ctx?.review_queue?.items || []).length}`,
      `  app_log: missing=${ctx?.app_log?.missing ?? '-'} truncated=${ctx?.app_log?.truncated ?? '-'} chars=${appLog.length}`,
      `  crash_log: missing=${ctx?.crash_log?.missing ?? '-'} chars=${crashLog.length}`,
      `  wrapper_exit_log: missing=${ctx?.wrapper_exit_log?.missing ?? '-'} chars=${wrapperLog.length}`,
      `  opencode_cli_logs: ${(ctx?.opencode_logs || []).length}`,
      `  service_logs: ${(ctx?.service_logs || []).length}`,
      `  serve_logs_on_disk: ${(ctx?.serve_logs_present || []).length}`,
      '',
    )
    const liveRows = jobsSummary?.live || []
    if (liveRows.length) {
      lines.push('Live jobs')
      for (const row of liveRows.slice(0, 20)) {
        lines.push(
          `  ${row.job_id || '-'} ${row.jira_id || '-'} ${row.status || '-'} ${row.job_kind || ''} ${row.model || ''}`,
        )
      }
      lines.push('')
    }
  }
  lines.push(
    'Timeouts / capacity (settings)',
    `  hang_timeout_seconds: ${settings.hang_timeout_seconds ?? '-'}`,
    `  git_clone_timeout_seconds: ${settings.git_clone_timeout_seconds ?? '-'}`,
    `  review_timeout_seconds: ${settings.review_timeout_seconds ?? '-'}`,
    `  review_retry_count: ${settings.review_retry_count ?? '-'}`,
    `  review_agent: ${settings.review_agent ?? '-'}  review_model: ${settings.review_model ?? '-'}`,
    `  max_concurrent_jobs: ${settings.max_concurrent_jobs ?? '-'}`,
    `  max_concurrent_reviews: ${settings.max_concurrent_reviews ?? '-'}`,
    `  callback_timeout_seconds: ${settings.callback_timeout_seconds ?? '-'}  callback_retry_count: ${settings.callback_retry_count ?? '-'}`,
    `  listen: ${settings.listen_host ?? '-'}:${settings.listen_port ?? '-'}`,
    `  data_dir: ${settings.data_dir ?? '-'}`,
    `  log_level: ${settings.log_level ?? '-'}  opencode_bin: ${settings.opencode_bin ?? '-'}`,
    '',
    'Where to look next',
  )
  if (bundles.length > 1) {
    lines.push(
      '  1. This file (each selected job + last FAIL lines)',
      '  2. jobs/<ticket>_<job_id>/system.log          OSM timeline',
      '  3. jobs/<ticket>_<job_id>/opencode-serve.log  that serve stdout',
      '  4. jobs/<ticket>_<job_id>/chat.md             model/tool output',
      '  5. system/app.log                             other jobs around the same time',
      '  6. system/wrapper-exit.log                    if the backend vanished',
    )
  } else if (job) {
    lines.push(
      '  1. This file (status + error + last FAIL lines)',
      '  2. job/system.log          OSM timeline for this job_id',
      '  3. job/opencode-serve.log  that serve stdout',
      '  4. job/prompts/            what OSM POSTed',
      '  5. job/chat.md             model/tool output',
      '  6. job/timeline.txt        extracted job-log events',
      '  7. job/app-log-excerpt.txt this job_id lines from process app.log',
      '  8. system/app.log          other jobs around the same time',
      '  9. system/wrapper-exit.log if the backend vanished',
    )
  } else {
    lines.push(
      '  1. This file (live jobs + last FAIL lines from app.log)',
      '  2. system/app.log          whole-process OSM log (may be truncated)',
      '  3. system/crash.log        uncaught / abrupt exit',
      '  4. system/wrapper-exit.log start-script / service exit codes',
      '  5. system/manager.json     ready / stopping / slot counts',
      '  6. system/layout.json      data_dir / logs / jobs / serve inventory',
      '  7. jobs/recent.json        last dashboard-visible history rows',
      '  8. queue.json              n8n FIFO  +  review-queue.json',
      '  9. system/opencode-logs/   machine-wide OpenCode CLI logs (not one job)',
    )
  }
  lines.push('')
  const allJobLogs = bundles
    .map((bundle) => (bundle.logs || []).map((line) => line.message).join('\n'))
    .join('\n')
  const allServe = bundles.map((bundle) => bundle.serveLog || '').join('\n')
  const problems = [
    ...lastProblemLines(allJobLogs || logs, 40),
    ...lastProblemLines(allServe || serve, 40),
    ...lastProblemLines(appLog, job || bundles.length ? 20 : 50),
    ...lastProblemLines(crashLog, 20),
    ...lastProblemLines(wrapperLog, 10),
  ]
  const unique = [...new Set(problems)].slice(-50)
  lines.push('Last FAIL / ERROR / timeout lines')
  if (!unique.length) {
    lines.push('  (none matched in job log / serve log / app.log)')
  } else {
    for (const line of unique) {
      lines.push(`  ${line}`)
    }
  }
  lines.push('')
  return `${lines.join('\n')}\n`
}

function clipText(text: string, limit: number): string {
  const raw = (text || '').replace(/\s+/g, ' ').trim()
  if (raw.length <= limit) return raw
  return `${raw.slice(0, limit)}…`
}

function fmtCountMap(map?: Record<string, number>): string {
  const entries = Object.entries(map || {}).filter(([, n]) => n)
  if (!entries.length) return '(none)'
  return entries.map(([k, n]) => `${k}=${n}`).join(' ')
}

function countBy<T>(rows: T[], key: (row: T) => string): Record<string, number> {
  const out: Record<string, number> = {}
  for (const row of rows) {
    const k = key(row) || 'unknown'
    out[k] = (out[k] || 0) + 1
  }
  return out
}

export function chatStatsFrom(messages: ChatMessage[]): {
  roles: Record<string, number>
  partTypes: Record<string, number>
  tools: Record<string, number>
  finishes: Record<string, number>
} {
  const roles: Record<string, number> = {}
  const partTypes: Record<string, number> = {}
  const tools: Record<string, number> = {}
  const finishes: Record<string, number> = {}
  for (const msg of messages) {
    const role = msg.role || 'unknown'
    roles[role] = (roles[role] || 0) + 1
    const finish = String(msg.finish || '')
    if (finish) finishes[finish] = (finishes[finish] || 0) + 1
    for (const part of msg.parts || []) {
      const ptype = part.type || (part.tool ? 'tool' : 'text')
      partTypes[ptype] = (partTypes[ptype] || 0) + 1
      if (part.tool) tools[part.tool] = (tools[part.tool] || 0) + 1
    }
  }
  return { roles, partTypes, tools, finishes }
}

export function chatMarkdown(jobId: string, messages: ChatMessage[]): string {
  const sessions = [...new Set(messages.map((m) => m.session_id).filter(Boolean))]
  const stats = chatStatsFrom(messages)
  const lines = [
    `# Chat for ${jobId || 'job'}`,
    '',
    `Sessions: ${sessions.join(', ') || '(none)'}`,
    `Messages: ${messages.length}  roles: ${fmtCountMap(stats.roles)}  parts: ${fmtCountMap(stats.partTypes)}`,
    `Tools: ${fmtCountMap(stats.tools)}  finishes: ${fmtCountMap(stats.finishes)}`,
    '',
  ]
  for (const msg of messages) {
    const when = msg.created_at == null ? '' : String(msg.created_at)
    lines.push(`## ${msg.role || 'unknown'} ${msg.id || ''} ${when}`.trimEnd())
    if (msg.session_id) lines.push(`session: ${msg.session_id}`)
    if (msg.finish) lines.push(`finish: ${msg.finish}`)
    for (const part of msg.parts || []) {
      lines.push(...partMarkdown(part))
    }
    lines.push('')
  }
  return `${lines.join('\n')}\n`
}

function partMarkdown(part: ChatPart): string[] {
  const ptype = part.type || ''
  if (ptype === 'tool' || part.tool) {
    const out: string[] = [
      `- tool \`${part.tool || ''}\` type=${ptype || 'tool'} status=${part.status || ''} id=${part.id || ''}`.trimEnd(),
    ]
    if (part.input && Object.keys(part.input).length) {
      let inp = redactSecrets(JSON.stringify(part.input, null, 2))
      if (inp.length > 16_000) inp = `${inp.slice(0, 16_000)}\n…[truncated]…`
      out.push('input:', '```json', inp, '```')
    }
    if (part.output) {
      let chunk = String(part.output)
      if (chunk.length > 32_000) chunk = `${chunk.slice(0, 32_000)}\n…[truncated]…`
      out.push('output:', '```', chunk, '```')
    }
    if (part.text) {
      let text = String(part.text)
      if (text.length > 16_000) text = `${text.slice(0, 16_000)}\n…[truncated]…`
      out.push(text)
    }
    return out
  }
  if (ptype === 'text' || ptype === 'reasoning' || ptype === 'compaction' || part.text) {
    let text = String(part.text || '')
    if (text.length > 100_000) text = `${text.slice(0, 100_000)}\n…[truncated]…`
    if (ptype && ptype !== 'text') return [`_${ptype}_`, text]
    return [text]
  }
  const leftover = redactSecrets(JSON.stringify(part))
  return leftover.length > 4000 ? [leftover.slice(0, 4000) + '\n…[truncated]…'] : [leftover]
}

function gitExplanation(job: JobItem): string {
  const extra = jobAsRecord(job)
  const kind = job.job_kind || extra.job_kind || 'ticket'
  const reviewKeeps = kind === 'review'
  return [
    reviewKeeps
      ? 'Review jobs keep the clone under workspaces/{mr_key} until the MR/PR is closed, merged, or abandoned.'
      : 'No live git snapshot is available for this ticket job.',
    '',
    reviewKeeps
      ? 'Job-end kills this serve and keeps the tree so /ask can resume ses_* on the same path.'
      : 'aMIR-mini always deletes the clone when the ticket job ends (success or fail).',
    reviewKeeps ? '' : 'The next job for the same ticket re-clones to the same path.',
    reviewKeeps ? '' : 'Chat vs disk drift is expected after delete.',
    '',
    `job_kind: ${kind}`,
    `clone_path: ${job.clone_path || '(none recorded)'}`,
    `repo_url: ${job.repo_url || '(none)'}`,
    `source_branch: ${job.source_branch || '(none)'}`,
    `target_branch: ${extra.target_branch || '(none)'}`,
    `sha: ${extra.sha || '(none)'}`,
    `base_sha: ${extra.base_sha || '(none)'}`,
    `merge_base: ${extra.merge_base || '(none)'}`,
    `diff_stat_chars: ${String(extra.diff_stat || '').length}`,
    `changed_paths: ${Array.isArray(extra.changed_paths) ? extra.changed_paths.length : 0}`,
    `status: ${job.status}`,
    `started_at: ${job.started_at || '?'}`,
    `completed_at: ${job.completed_at || '?'}`,
    '',
    'Do not treat a missing working tree as a product-repo problem.',
    '',
  ]
    .filter((line, i, all) => line !== '' || (i > 0 && all[i - 1] !== ''))
    .join('\n')
}

function serveLogText(serveLog: string | undefined, missing: boolean | undefined): string {
  if (missing) {
    return '(no serve log for this job — serve never started or the file was removed)\n'
  }
  if (!serveLog) return ''
  return serveLog.endsWith('\n') ? serveLog : `${serveLog}\n`
}

function processFiles(input: JobReportInput): Record<string, string> {
  const ctx = input.context
  const files: Record<string, string> = {
    'meta.json': jsonFile({
      kind: input.job ? 'job' : 'general',
      created_at: input.exportedAt,
      app: ctx?.meta || null,
      job_id: input.job?.job_id || null,
      jira_id: input.job?.jira_id || null,
      server_time: ctx?.server_time || ctx?.meta?.server_time || null,
    }),
    'runtime.json': jsonFile(ctx?.runtime || { error: input.contextError || 'report-context not loaded' }),
    'settings.json': jsonFile(ctx?.settings || { error: input.contextError || 'report-context not loaded' }),
    'queue.json': jsonFile(ctx?.queue || { items: [], queued_count: 0 }),
    'review-queue.json': jsonFile(ctx?.review_queue || { items: [], queued_count: 0 }),
    'system/manager.json': jsonFile(ctx?.manager || { error: input.contextError || 'report-context not loaded' }),
    'system/layout.json': jsonFile(ctx?.layout || { error: input.contextError || 'report-context not loaded' }),
    'system/live.json': jsonFile(ctx?.live || {}),
    'system/app.log': logText(ctx?.app_log, '(no app.log — process log was never created or was removed)'),
    'system/crash.log': logText(ctx?.crash_log, '(no crash.log)'),
    'system/wrapper-exit.log': logText(
      ctx?.wrapper_exit_log,
      '(no wrapper-exit.log — start script has not recorded a backend exit)',
    ),
  }
  if (input.contextError) {
    files['CONTEXT_ERROR.txt'] = `${input.contextError}\n`
  }
  for (const blob of ctx?.opencode_logs || []) {
    const name = safeName(blob.name || 'opencode.log')
    files[`system/opencode-logs/${name}`] = logText(blob, '(empty)')
  }
  for (const blob of ctx?.service_logs || []) {
    const name = safeName(blob.name || 'service.log')
    files[`system/service-logs/${name}`] = logText(blob, '(empty)')
  }
  if (ctx?.serve_logs_present || ctx?.serve_logs) {
    files['system/serve-logs-present.json'] = jsonFile({
      files: ctx.serve_logs_present || [],
      details: ctx.serve_logs || [],
    })
  }
  if (ctx?.log_files_present) {
    files['system/log-files-present.json'] = jsonFile({ files: ctx.log_files_present })
  }
  if (ctx?.jobs_summary) {
    files['jobs/summary.json'] = jsonFile(ctx.jobs_summary)
  }
  return files
}

function readme(kind: 'job' | 'general' | 'jobs', job?: JobItem | null, bundles?: JobReportBundle[]): string {
  const lines = [
    'aMIR-mini issue report',
    '',
    `Kind: ${kind}`,
    '',
    'SUMMARY.txt                   START HERE — status, error, last FAIL lines',
    'NOTE.txt                      Reporter note (not stored on the server)',
    'README.txt                    This file',
    'meta.json                     App version and report metadata',
    'runtime.json                  Host, Python, git/opencode versions, live counts',
    'settings.json                 Safe settings (no secrets, no callback_url)',
    'queue.json                    n8n queued tickets (public fields only)',
    'review-queue.json             GitLab/Azure review FIFO (mr_key + job_id)',
    'jobs/summary.json             History counts + live + recent public rows',
    'jobs/recent.json              Dashboard-visible recent jobs (no chat/prompts)',
    'system/manager.json           ready / stopping / n8n vs review slot counts',
    'system/layout.json            data_dir / logs / jobs / serve inventory (names + sizes)',
    'system/live.json              Combined live running/queued counts',
    'system/app.log                Process app.log (redacted, may be truncated)',
    'system/crash.log              Uncaught / abrupt-exit log',
    'system/wrapper-exit.log       start-backend wrapper exit codes (if any)',
    'system/opencode-logs/         Recent OpenCode CLI logs from this machine',
    'system/service-logs/          WinSW / systemd service stdout-stderr (if present)',
    'system/serve-logs-present.json  Names + sizes of per-job serve logs still on disk',
    'system/log-files-present.json Names of *.log files under the job log dir',
  ]
  if (kind === 'jobs' && bundles && bundles.length > 1) {
    lines.push('', `Selected jobs: ${bundles.length}`, '')
    for (const bundle of bundles) {
      lines.push(`  ${jobReportFolder(bundle.job)}/   ${bundle.job.job_id}  ${bundle.job.jira_id}`)
    }
    lines.push(
      '',
      'Each jobs/<ticket>_<job_id>/ folder has the same files as a single-job',
      'report: record, parameters, prompts, chat, system.log, opencode-serve.log.',
    )
  } else if (kind === 'job' && job) {
    lines.push(
      '',
      `Selected job: ${job.job_id}`,
      `Ticket: ${job.jira_id}`,
      '',
      'job/error.txt               Job error_message if the job failed',
      'job/diagnostics.json        Stage snapshot (clone/serve/fail)',
      'job/record.json             Dashboard job record (no callback_url)',
      'job/parameters.json         Fields needed to reproduce the run',
      'job/retry_attempts.json     Outer-retry bookkeeping',
      'job/prompts.json            User messages aMIR-mini POSTed',
      'job/prompts/                Same prompts as individual text files',
      'job/chat.json               Transcript snapshot or live serve copy',
      'job/chat.md                 Same transcript, readable',
      'job/chat-stats.json         Role / tool / finish counts',
      'job/result.txt              Last assistant text (the job product)',
      'job/system.log              aMIR-mini per-job manager log',
      'job/log-stats.json          Line counts for the job log and serve log',
      'job/timeline.txt            Extracted events from the job log',
      'job/app-log-excerpt.txt     app.log lines that mention this job_id / jira_id',
      'job/opencode-serve.log      stdout/stderr from this job\'s opencode serve',
      'job/git.txt                 Clone path / repo — no live git scan',
    )
  }
  lines.push('')
  return `${lines.join('\n')}\n`
}

export function buildJobReportFiles(input: JobReportInput): Record<string, string> {
  const bundles = selectedBundles(input)
  const job = bundles.length === 1 ? bundles[0].job : null
  const multi = bundles.length > 1
  const kind: 'job' | 'general' | 'jobs' = multi ? 'jobs' : job ? 'job' : input.kind || 'general'
  const note = input.note.trim()
  const noteHead = multi
    ? [`kind: jobs`, `jobs: ${bundles.map((b) => `${b.job.jira_id}/${b.job.job_id}`).join(', ')}`]
    : job
      ? [`jira_id: ${job.jira_id}`, `job_id: ${job.job_id}`]
      : ['kind: general']
  const files: Record<string, string> = {
    'SUMMARY.txt': buildIssueSummary({ ...input, bundles, job }),
    'NOTE.txt': [...noteHead, `exported_at: ${input.exportedAt}`, '', note, '']
      .filter((line, i, all) => line !== '' || i === 0 || all[i - 1] !== '')
      .join('\n'),
    'README.txt': readme(kind, job, bundles),
    ...processFiles(input),
    ...recentJobFiles(input),
  }

  if (multi) {
    files['jobs/selected.json'] = jsonFile(
      bundles.map((bundle) => ({
        job_id: bundle.job.job_id,
        jira_id: bundle.job.jira_id,
        status: bundle.job.status,
        folder: jobReportFolder(bundle.job),
        error: bundle.error || null,
      })),
    )
    for (const bundle of bundles) {
      Object.assign(
        files,
        jobBundleFiles(bundle, input, `${jobReportFolder(bundle.job)}/`, { flatAliases: false }),
      )
    }
    return files
  }

  if (job) {
    Object.assign(files, jobBundleFiles(bundles[0], input, 'job/', { flatAliases: true }))
  }

  return files
}

function jobBundleFiles(
  bundle: JobReportBundle,
  input: JobReportInput,
  prefix: string,
  opts: { flatAliases: boolean },
): Record<string, string> {
  const job = bundle.job
  const prompts = bundle.prompts || []
  const messages = bundle.messages || []
  const logs = bundle.logs || []
  const jobLog = logs.length ? `${logs.map((line) => line.message).join('\n')}\n` : ''
  const extra = jobAsRecord(job)
  const files: Record<string, string> = {}
  if (bundle.error) {
    files[`${prefix}FETCH_ERROR.txt`] = `${bundle.error}\n`
  }
  if (job.error_message) {
    files[`${prefix}error.txt`] = `${job.error_message}\n`
  }
  if (extra.diagnostics && typeof extra.diagnostics === 'object') {
    files[`${prefix}diagnostics.json`] = jsonFile(extra.diagnostics)
  }
  files[`${prefix}record.json`] = jsonFile({ exported_at: input.exportedAt, job })
  files[`${prefix}parameters.json`] = jsonFile(jobParameters(job))
  files[`${prefix}retry_attempts.json`] = jsonFile(job.attempts || [])
  files[`${prefix}prompts.json`] = jsonFile({ prompts })
  files[`${prefix}chat.json`] = jsonFile({
    job_id: job.job_id,
    session_ids: bundle.sessionIds || [],
    messages,
  })
  files[`${prefix}chat.md`] = redactSecrets(chatMarkdown(job.job_id, messages))
  files[`${prefix}chat-stats.json`] = jsonFile(chatStatsFrom(messages))
  files[`${prefix}result.txt`] = job.text ? (job.text.endsWith('\n') ? job.text : `${job.text}\n`) : ''
  files[`${prefix}system.log`] = jobLog
  files[`${prefix}log-stats.json`] = jsonFile({
    job_log_lines: logs.length,
    job_log_chars: jobLog.length,
    serve_log_chars: (bundle.serveLog || '').length,
    serve_log_lines: (bundle.serveLog || '').split(/\r?\n/).filter(Boolean).length,
    serve_log_missing: Boolean(bundle.serveLogMissing),
    app_log_excerpt_lines: appLogExcerpt(input.context?.app_log?.text || '', job)
      .split(/\r?\n/)
      .filter(Boolean).length,
  })
  files[`${prefix}timeline.txt`] = jobTimeline(logs)
  files[`${prefix}app-log-excerpt.txt`] = appLogExcerpt(input.context?.app_log?.text || '', job)
  files[`${prefix}opencode-serve.log`] = serveLogText(bundle.serveLog, bundle.serveLogMissing)
  files[`${prefix}git.txt`] = gitExplanation(job)
  if (extra.diff_stat) {
    files[`${prefix}diff-stat.txt`] = `${String(extra.diff_stat)}\n`
  }
  if (Array.isArray(extra.changed_paths) && extra.changed_paths.length) {
    files[`${prefix}changed-paths.json`] = jsonFile(extra.changed_paths)
  }
  if (extra.comment_text) {
    files[`${prefix}comment.txt`] = `${redactSecrets(String(extra.comment_text))}\n`
  }
  if (extra.parent_comment_text) {
    files[`${prefix}parent-comment.txt`] = `${redactSecrets(String(extra.parent_comment_text))}\n`
  }
  for (const prompt of prompts) {
    files[`${prefix}prompts/${safeName(prompt.id)}.txt`] = [
      `id: ${prompt.id}`,
      `posted_at: ${prompt.posted_at}`,
      '',
      redactSecrets(prompt.text || ''),
      '',
    ].join('\n')
  }
  if (opts.flatAliases) {
    files['job.json'] = files[`${prefix}record.json`]
    files['prompts.json'] = files[`${prefix}prompts.json`]
    files['chat.json'] = files[`${prefix}chat.json`]
    files['logs.txt'] = jobLog
    files['opencode-serve.log'] = files[`${prefix}opencode-serve.log`]
  }
  return files
}

export function buildGeneralReportFiles(input: Omit<JobReportInput, 'job'>): Record<string, string> {
  return buildJobReportFiles({ ...input, job: null, kind: 'general' })
}

function recentJobFiles(input: JobReportInput): Record<string, string> {
  const fromCtx = input.context?.jobs_summary?.recent || []
  const fromFetch = (input.recentJobs || []).map((job) => {
    const extra = jobAsRecord(job)
    return {
      job_id: job.job_id,
      jira_id: job.jira_id,
      status: job.status,
      live: job.live,
      job_kind: job.job_kind || extra.job_kind || 'ticket',
      provider: job.provider || '',
      trigger: job.trigger || '',
      model: job.model || '',
      agent_mode: job.agent_mode || '',
      session_id: job.session_id || '',
      error_message: job.error_message || '',
      callback_status_code: job.callback_status_code ?? null,
      accepted_at: job.accepted_at || '',
      started_at: job.started_at || '',
      completed_at: job.completed_at || '',
      result_chars: (job.text || '').length,
    }
  })
  const recent = fromFetch.length ? fromFetch : fromCtx
  if (!recent.length && !input.context?.jobs_summary) return {}
  const lines = ['Recent jobs (newest first)', '']
  for (const row of recent) {
    const rec = row as Record<string, unknown>
    lines.push(
      `${rec.job_id || '-'}  ${rec.jira_id || '-'}  ${rec.status || '-'}  ${rec.job_kind || ''}  ${rec.model || ''}  err=${rec.error_message || '-'}`,
    )
  }
  lines.push('')
  return {
    'jobs/recent.json': jsonFile(recent),
    'jobs/recent.txt': `${lines.join('\n')}\n`,
  }
}

function jobTimeline(logs: LogLine[]): string {
  if (!logs.length) return '(no job log lines)\n'
  const interesting =
    /start|accept|dispatch|clone|serve|session|POST|prompt|hang|timeout|retry|attempt|idle|compact|callback|delete|fail|error|health|queue|shutdown|boot/i
  const rows = logs
    .map((line) => line.message)
    .filter((message) => interesting.test(message))
  const picked = rows.length ? rows : logs.map((line) => line.message)
  const body = picked.join('\n')
  return body.endsWith('\n') ? body : `${body}\n`
}

function appLogExcerpt(appLog: string, job: JobItem): string {
  if (!appLog) return '(app.log not loaded)\n'
  const needles = [job.job_id, job.jira_id, job.session_id || ''].filter((item) => item && item !== '-1')
  const hits = appLog.split(/\r?\n/).filter((line) => needles.some((n) => n && line.includes(n)))
  if (!hits.length) return '(no app.log lines mention this job_id / jira_id / session_id)\n'
  const text = hits.join('\n')
  return text.endsWith('\n') ? text : `${text}\n`
}

