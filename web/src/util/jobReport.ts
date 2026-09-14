import type { ChatMessage, ChatPart, JobItem, LogLine, PromptRow, ReportContext, ReportLogBlob } from '../api/types'

export const REPORT_NOTE_MIN = 20

export function reportNoteReady(note: string): boolean {
  return note.trim().length >= REPORT_NOTE_MIN
}

export type JobReportInput = {
  kind?: 'job' | 'general'
  job?: JobItem | null
  prompts?: PromptRow[]
  messages?: ChatMessage[]
  logs?: LogLine[]
  serveLog?: string
  serveLogMissing?: boolean
  context?: ReportContext | null
  contextError?: string | null
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

export function reportZipName(job?: Pick<JobItem, 'jira_id' | 'job_id'> | null, exportedAt?: string): string {
  const stamp = stampFromIso(exportedAt || new Date().toISOString())
  if (!job) return `osm-report-general-${stamp}.zip`
  const ticket = safeName(job.jira_id || 'ticket')
  const id = safeName(job.job_id || 'job')
  return `osm-report-${ticket}-${id}-${stamp}.zip`
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
    job_kind: job.job_kind || '',
    provider: job.provider || '',
    trigger: job.trigger || '',
    source: job.source || '',
    mr_title: job.mr_title || '',
    mr_key: job.mr_key || '',
    web_url: job.web_url || '',
    agent_mode: job.agent_mode || '',
    model: job.model || '',
    session_id: job.session_id || '',
    repo_url: job.repo_url || '',
    source_branch: job.source_branch || '',
    target_branch: extra.target_branch || '',
    clone_path: job.clone_path || '',
    serve_pid: job.serve_pid ?? null,
    serve_port: job.serve_port ?? null,
    timeout_in_seconds: job.timeout_in_seconds ?? null,
    retry_count: job.retry_count ?? null,
    attempt: job.attempt ?? null,
    started_at: job.started_at || null,
    completed_at: job.completed_at || null,
    accepted_at: job.accepted_at || null,
    error_message: job.error_message || null,
    callback_status_code: job.callback_status_code ?? null,
    original_posted: job.original_posted ?? false,
    sha: extra.sha || '',
    merge_base: extra.merge_base || '',
    retry_attempts: job.attempts || [],
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

export function buildIssueSummary(input: JobReportInput): string {
  const job = input.job
  const extra = job ? jobAsRecord(job) : {}
  const ctx = input.context
  const settings = (ctx?.settings || {}) as Record<string, unknown>
  const runtime = (ctx?.runtime || {}) as Record<string, unknown>
  const logs = (input.logs || []).map((line) => line.message).join('\n')
  const serve = input.serveLog || ''
  const appLog = ctx?.app_log?.text || ''
  const lines: string[] = [
    'START HERE — aMIR-mini issue summary',
    '',
    `kind: ${job ? 'job' : 'general'}`,
    `exported_at: ${input.exportedAt}`,
    `app: ${ctx?.meta?.app_name || 'aMIR-mini'} ${ctx?.meta?.version || runtime.osm_version || ''}`,
    `platform: ${runtime.platform || runtime.system || '(unknown)'}`,
    `opencode: ${JSON.stringify((runtime.cli_versions as Record<string, unknown> | undefined)?.opencode || runtime.which || '')}`,
    '',
  ]
  if (job) {
    lines.push(
      'Job',
      `  job_id: ${job.job_id}`,
      `  ticket / mr: ${job.jira_id}`,
      `  kind: ${job.job_kind || 'ticket'}  provider: ${job.provider || '-'}  trigger: ${job.trigger || '-'}`,
      `  status: ${job.status}  live: ${job.live}`,
      `  title: ${(job.mr_title || '').trim() || '-'}`,
      `  error: ${job.error_message || '(none)'}`,
      `  model: ${job.model || '-'}  agent: ${job.agent_mode || '-'}`,
      `  session: ${job.session_id || '-'}  attempt: ${job.attempt ?? '-'} / ${job.retry_count ?? '-'}`,
      `  serve: pid=${job.serve_pid ?? '-'} port=${job.serve_port ?? '-'}`,
      `  timeout_in_seconds: ${job.timeout_in_seconds ?? '-'}`,
      `  repo: ${job.repo_url || '-'}`,
      `  branches: ${job.source_branch || '-'} -> ${extra.target_branch || '-'}`,
      `  clone_path: ${job.clone_path || '-'}`,
      `  started: ${job.started_at || '-'}  completed: ${job.completed_at || '-'}`,
      `  prompts_posted: ${(input.prompts || []).map((p) => p.id).join(', ') || '(none)'}`,
      `  chat_messages: ${(input.messages || []).length}`,
      `  result_chars: ${(job.text || '').length}`,
      '',
    )
    const attempts = job.attempts || []
    if (attempts.length) {
      lines.push('Attempts')
      for (const row of attempts) {
        lines.push(
          `  #${row.number} ${row.kind || '-'} prompt=${row.prompt_id || '-'} session=${row.session_id || '-'} err=${row.error || '-'}`,
        )
      }
      lines.push('')
    }
  }
  lines.push(
    'Timeouts (settings)',
    `  hang_timeout_seconds: ${settings.hang_timeout_seconds ?? '-'}`,
    `  git_clone_timeout_seconds: ${settings.git_clone_timeout_seconds ?? '-'}`,
    `  review_timeout_seconds: ${settings.review_timeout_seconds ?? '-'}`,
    `  max_concurrent_jobs: ${settings.max_concurrent_jobs ?? '-'}`,
    `  max_concurrent_reviews: ${settings.max_concurrent_reviews ?? '-'}`,
    `  live: running=${ctx?.live?.running ?? '-'} queued=${ctx?.live?.queued ?? '-'}`,
    '',
    'Where to look next',
    '  1. This file (status + error + last FAIL lines)',
    '  2. job/system.log          OSM timeline for this job_id',
    '  3. job/opencode-serve.log  that serve stdout',
    '  4. job/prompts/            what OSM POSTed',
    '  5. job/chat.md             model/tool output',
    '  6. system/app.log          other jobs around the same time',
    '  7. system/wrapper-exit.log if the backend vanished',
    '',
  )
  const problems = [
    ...lastProblemLines(logs),
    ...lastProblemLines(serve),
    ...(!job ? lastProblemLines(appLog) : []),
  ]
  const unique = [...new Set(problems)].slice(-30)
  lines.push('Last FAIL / ERROR / timeout lines')
  if (!unique.length) {
    lines.push('  (none matched in job log / serve log)')
  } else {
    for (const line of unique) {
      lines.push(`  ${line}`)
    }
  }
  lines.push('')
  return `${lines.join('\n')}\n`
}

export function chatMarkdown(jobId: string, messages: ChatMessage[]): string {
  const sessions = [...new Set(messages.map((m) => m.session_id).filter(Boolean))]
  const lines = [
    `# Chat for ${jobId || 'job'}`,
    '',
    `Sessions: ${sessions.join(', ') || '(none)'}`,
    '',
  ]
  for (const msg of messages) {
    const when = msg.created_at == null ? '' : String(msg.created_at)
    lines.push(`## ${msg.role || 'unknown'} ${when}`.trimEnd())
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
  if (ptype === 'text' || part.text) {
    let text = String(part.text || '')
    if (text.length > 20_000) text = `${text.slice(0, 20_000)}\n…[truncated]…`
    return [text]
  }
  if (ptype === 'tool' || part.tool) {
    const out: string[] = [
      `- tool \`${part.tool || ''}\` status=${part.status || ''}`.trimEnd(),
    ]
    if (part.output) {
      let chunk = String(part.output)
      if (chunk.length > 4000) chunk = `${chunk.slice(0, 4000)}\n…[truncated]…`
      out.push('```', chunk, '```')
    }
    return out
  }
  return []
}

function gitExplanation(job: JobItem): string {
  return [
    'No live git snapshot is available for this job.',
    '',
    'aMIR-mini always deletes the clone when the job ends (success or fail).',
    'The next job for the same ticket re-clones to the same path.',
    'Chat vs disk drift is expected after delete.',
    '',
    `clone_path: ${job.clone_path || '(none recorded)'}`,
    `repo_url: ${job.repo_url || '(none)'}`,
    `source_branch: ${job.source_branch || '(none)'}`,
    `status: ${job.status}`,
    `started_at: ${job.started_at || '?'}`,
    `completed_at: ${job.completed_at || '?'}`,
    '',
    'Do not treat a missing working tree as a product-repo problem.',
    '',
  ].join('\n')
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
    }),
    'runtime.json': jsonFile(ctx?.runtime || { error: input.contextError || 'report-context not loaded' }),
    'settings.json': jsonFile(ctx?.settings || { error: input.contextError || 'report-context not loaded' }),
    'queue.json': jsonFile(ctx?.queue || { items: [], queued_count: 0 }),
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
  if (ctx?.serve_logs_present) {
    files['system/serve-logs-present.json'] = jsonFile({ files: ctx.serve_logs_present })
  }
  return files
}

function readme(kind: 'job' | 'general', job?: JobItem | null): string {
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
    'queue.json                    Queued tickets (public fields only)',
    'system/app.log                Process app.log (redacted, may be truncated)',
    'system/crash.log              Uncaught / abrupt-exit log',
    'system/wrapper-exit.log       start-backend wrapper exit codes (if any)',
    'system/opencode-logs/         Recent OpenCode CLI logs from this machine',
    'system/serve-logs-present.json  Names of per-job serve logs still on disk',
  ]
  if (kind === 'job' && job) {
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
      'job/result.txt              Last assistant text (the job product)',
      'job/system.log              aMIR-mini per-job manager log',
      'job/opencode-serve.log      stdout/stderr from this job\'s opencode serve',
      'job/git.txt                 Clone path / repo — no live git (clone is deleted)',
    )
  }
  lines.push('')
  return `${lines.join('\n')}\n`
}

export function buildJobReportFiles(input: JobReportInput): Record<string, string> {
  const job = input.job || null
  const kind: 'job' | 'general' = job ? 'job' : input.kind || 'general'
  const note = input.note.trim()
  const prompts = input.prompts || []
  const messages = input.messages || []
  const logs = (input.logs || []).map((line) => line.message).join('\n')
  const files: Record<string, string> = {
    'SUMMARY.txt': buildIssueSummary(input),
    'NOTE.txt': [
      job ? `jira_id: ${job.jira_id}` : 'kind: general',
      job ? `job_id: ${job.job_id}` : '',
      `exported_at: ${input.exportedAt}`,
      '',
      note,
      '',
    ]
      .filter((line, i, all) => line !== '' || i === 0 || all[i - 1] !== '')
      .join('\n'),
    'README.txt': readme(kind, job),
    ...processFiles(input),
  }

  if (job) {
    const jobLog = logs ? `${logs}\n` : ''
    const extra = jobAsRecord(job)
    if (job.error_message) {
      files['job/error.txt'] = `${job.error_message}\n`
    }
    if (extra.diagnostics && typeof extra.diagnostics === 'object') {
      files['job/diagnostics.json'] = jsonFile(extra.diagnostics)
    }
    files['job/record.json'] = jsonFile({ exported_at: input.exportedAt, job })
    files['job/parameters.json'] = jsonFile(jobParameters(job))
    files['job/retry_attempts.json'] = jsonFile(job.attempts || [])
    files['job/prompts.json'] = jsonFile({ prompts })
    files['job/chat.json'] = jsonFile({ job_id: job.job_id, messages })
    files['job/chat.md'] = redactSecrets(chatMarkdown(job.job_id, messages))
    files['job/result.txt'] = job.text ? (job.text.endsWith('\n') ? job.text : `${job.text}\n`) : ''
    files['job/system.log'] = jobLog
    files['job/opencode-serve.log'] = serveLogText(input.serveLog, input.serveLogMissing)
    files['job/git.txt'] = gitExplanation(job)
    for (const prompt of prompts) {
      files[`job/prompts/${safeName(prompt.id)}.txt`] = [
        `id: ${prompt.id}`,
        `posted_at: ${prompt.posted_at}`,
        '',
        redactSecrets(prompt.text || ''),
        '',
      ].join('\n')
    }
    // Flat names kept so older unzip checklists still find them.
    files['job.json'] = files['job/record.json']
    files['prompts.json'] = files['job/prompts.json']
    files['chat.json'] = files['job/chat.json']
    files['logs.txt'] = jobLog
    files['opencode-serve.log'] = files['job/opencode-serve.log']
  }

  return files
}

export function buildGeneralReportFiles(input: Omit<JobReportInput, 'job'>): Record<string, string> {
  return buildJobReportFiles({ ...input, job: null, kind: 'general' })
}

