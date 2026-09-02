import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchChat, fetchJob, fetchLogs, fetchPrompts, fetchServeLog } from '../../api/client'
import { downloadIssueReport } from '../../util/downloadReport'
import type { ChatMessage, JobItem, LogLine, PromptRow } from '../../api/types'
import { useLive } from '../../app/live'
import { LiveDot } from '../../ui/LiveDot'
import { MarkdownBody } from '../../ui/MarkdownBody'
import { MetaCard } from '../../ui/MetaCard'
import { ReportIssueDialog } from '../../ui/ReportIssueDialog'
import { StatusBadge } from '../../ui/StatusBadge'
import { Tabs } from '../../ui/Tabs'
import { reportNoteReady } from '../../util/jobReport'
import { JobChatTab } from './JobChatTab'

type Tab = 'overview' | 'prompt' | 'chat' | 'logs'

export function JobDetailPage() {
  const { jobId = '' } = useParams()
  const live = useLive()
  const [job, setJob] = useState<JobItem | null>(null)
  const [prompts, setPrompts] = useState<PromptRow[]>([])
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [logs, setLogs] = useState<LogLine[]>([])
  const [serveLog, setServeLog] = useState('')
  const [serveLogMissing, setServeLogMissing] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [reportOpen, setReportOpen] = useState(false)
  const [reportBusy, setReportBusy] = useState(false)
  const [reportError, setReportError] = useState<string | null>(null)

  const seqRef = useRef(0)

  const load = useCallback(async (id: string, mine: number, opts: { clearOnError: boolean }) => {
    if (!id) return
    try {
      const body = await fetchJob(id)
      if (seqRef.current !== mine) return
      setJob(body.job)
      setError(null)
      const [p, c, l, s] = await Promise.all([
        fetchPrompts(id),
        fetchChat(id),
        fetchLogs(id),
        fetchServeLog(id),
      ])
      if (seqRef.current !== mine) return
      setPrompts(p.prompts || [])
      setMessages(c.messages || [])
      setLogs(l.lines || [])
      setServeLog(s.text || '')
      setServeLogMissing(Boolean(s.missing))
    } catch (e) {
      if (seqRef.current !== mine) return
      if (opts.clearOnError) {
        setJob(null)
        setPrompts([])
        setMessages([])
        setLogs([])
        setServeLog('')
        setServeLogMissing(true)
      }
      setError(e instanceof Error ? e.message : 'Failed to load job')
    }
  }, [])

  useEffect(() => {
    const mine = ++seqRef.current
    setTab('overview')
    setJob(null)
    setPrompts([])
    setMessages([])
    setLogs([])
    setServeLog('')
    setServeLogMissing(true)
    setError(null)
    setReportOpen(false)
    setReportBusy(false)
    setReportError(null)
    void load(jobId.trim(), mine, { clearOnError: true })
  }, [jobId, load])

  const downloadReport = useCallback(
    async (note: string) => {
      const id = jobId.trim()
      if (!id || !reportNoteReady(note)) return
      setReportBusy(true)
      setReportError(null)
      try {
        await downloadIssueReport({ kind: 'job', jobId: id, note })
        setReportOpen(false)
      } catch (e) {
        setReportError(e instanceof Error ? e.message : 'Failed to build report')
      } finally {
        setReportBusy(false)
      }
    },
    [jobId],
  )

  useEffect(() => {
    if (job?.live) void load(jobId.trim(), seqRef.current, { clearOnError: false })
  }, [live.generation]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section className="space-y-5">
      <div>
        <Link to="/jobs" className="vd-btn-ghost mb-3 inline-block text-sm">
          ← Jobs
        </Link>
        <div className="flex flex-wrap items-center gap-2">
          {job?.jira_id && <span className="font-mono text-lg font-semibold">{job.jira_id}</span>}
          {job && <StatusBadge status={job.status} />}
          {job?.live && <LiveDot />}
        </div>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">{job?.job_id || 'Job'}</h1>
        <p className="mt-1 font-mono text-xs text-text-muted">
          {job?.agent_mode} · {job?.model} · attempt {job?.attempt}/{job?.retry_count}
        </p>
      </div>

      <div className="flex flex-wrap justify-end gap-2">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={!job}
          onClick={() => {
            setReportError(null)
            setReportOpen(true)
          }}
        >
          Report issue
        </button>
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          onClick={() => void load(jobId.trim(), seqRef.current, { clearOnError: true })}
        >
          Refresh
        </button>
      </div>

      {reportOpen && job && (
        <ReportIssueDialog
          title={`${job.jira_id} · ${job.job_id}`}
          busy={reportBusy}
          error={reportError}
          onClose={() => {
            if (!reportBusy) setReportOpen(false)
          }}
          onDownload={(note) => void downloadReport(note)}
        />
      )}

      <Tabs
        tabs={[
          { id: 'overview', label: 'Details' },
          { id: 'prompt', label: 'Prompt', count: prompts.length },
          { id: 'chat', label: 'Transcript', count: messages.length },
          { id: 'logs', label: 'Logs', count: logs.length + (serveLog ? serveLog.split('\n').filter(Boolean).length : 0) },
        ]}
        value={tab}
        onChange={setTab}
      />

      <div className="vd-panel min-h-[50vh] p-5">
        {error && <p className="text-sm text-danger-text">{error}</p>}
        {job && tab === 'overview' && <Overview job={job} />}
        {job && tab === 'prompt' && <PromptTab prompts={prompts} />}
        {job && tab === 'chat' && <JobChatTab messages={messages} live={job.live} />}
        {job && tab === 'logs' && (
          <LogsTab lines={logs} serveLog={serveLog} serveMissing={serveLogMissing} />
        )}
      </div>
    </section>
  )
}

function isTerminalSuccess(job: JobItem): boolean {
  return !job.live && (job.status || '').toLowerCase() === 'success'
}

function Overview({ job }: { job: JobItem }) {
  const attempts = job.attempts || []
  const showResult = isTerminalSuccess(job) && Boolean(job.text)
  return (
    <div className="space-y-6 text-sm">
      {showResult && (
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">Result</div>
          <MarkdownBody text={job.text || ''} />
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <MetaCard label="Job id" mono value={job.job_id} />
        <MetaCard label="Status" valueNode={<StatusBadge status={job.status} />} />
        <MetaCard label="Jira" mono value={job.jira_id} />
        <MetaCard label="Agent" value={job.agent_mode || '—'} />
        <MetaCard label="Model" mono value={job.model || '—'} />
        <MetaCard label="Session" mono value={job.session_id || '—'} />
        <MetaCard label="Branch" mono value={job.source_branch || '—'} />
        <MetaCard label="Repo" mono className="sm:col-span-2" value={job.repo_url || '—'} />
        <MetaCard label="Clone" mono className="sm:col-span-2 lg:col-span-3" value={job.clone_path || '—'} />
        <MetaCard label="Serve" mono value={job.serve_port ? `${job.serve_pid}@${job.serve_port}` : '—'} />
        <MetaCard label="Attempt" value={`${job.attempt || 1} / ${job.retry_count || 1}`} />
        <MetaCard label="Timeout" value={`${job.timeout_in_seconds}s`} />
        <MetaCard label="Started" mono value={job.started_at || '—'} />
        <MetaCard label="Completed" mono value={job.completed_at || '—'} />
        <MetaCard label="Callback" value={job.callback_status_code ? String(job.callback_status_code) : '—'} />
      </div>
      {job.error_message && (
        <pre className="vd-pre text-danger-text">{job.error_message}</pre>
      )}
      {attempts.length > 0 && (
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full min-w-[32rem] text-left text-xs">
            <thead className="bg-bg-elevated text-[10px] uppercase tracking-wide text-text-muted">
              <tr>
                <th className="px-3 py-2">#</th>
                <th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2">Prompt</th>
                <th className="px-3 py-2">Session</th>
                <th className="px-3 py-2">Error</th>
                <th className="px-3 py-2">Ended</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {attempts.map((a) => (
                <tr key={`${a.number}-${a.ended_at}`}>
                  <td className="px-3 py-2 font-mono">{a.number}</td>
                  <td className="px-3 py-2">{a.kind}</td>
                  <td className="px-3 py-2 font-mono">{a.prompt_id || '—'}</td>
                  <td className="px-3 py-2 font-mono">{a.session_id || '—'}</td>
                  <td className="max-w-xs truncate px-3 py-2 font-mono">{a.error || '—'}</td>
                  <td className="px-3 py-2 font-mono text-text-muted">{a.ended_at || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function PromptTab({ prompts }: { prompts: PromptRow[] }) {
  if (prompts.length === 0) {
    return <div className="vd-alert vd-alert-warning">No user messages posted yet.</div>
  }
  return (
    <div className="space-y-3">
      {prompts.map((p) => (
        <details key={`${p.id}-${p.posted_at}`} open className="rounded border border-border bg-bg px-3 py-2">
          <summary className="cursor-pointer font-mono text-xs">
            {p.id} · {p.posted_at}
          </summary>
          <pre className="vd-pre mt-2">{p.text}</pre>
        </details>
      ))}
    </div>
  )
}

function LogsTab({
  lines,
  serveLog,
  serveMissing,
}: {
  lines: LogLine[]
  serveLog: string
  serveMissing: boolean
}) {
  return (
    <div className="space-y-6">
      <LogBlock title="Job log" empty="No OSM log lines for this job_id.">
        {lines.length > 0
          ? lines.map((line, i) => (
              <div key={`${line.timestamp}-${i}`} className="border-b border-border/50 py-0.5">
                {line.message}
              </div>
            ))
          : null}
      </LogBlock>
      <LogBlock
        title="OpenCode serve"
        empty={
          serveMissing
            ? 'No serve log — serve never started or the file was removed.'
            : 'Serve log is empty.'
        }
      >
        {serveLog
          ? serveLog.split('\n').map((line, i) => (
              <div key={`serve-${i}`} className="border-b border-border/50 py-0.5 whitespace-pre-wrap">
                {line || ' '}
              </div>
            ))
          : null}
      </LogBlock>
    </div>
  )
}

function LogBlock({
  title,
  empty,
  children,
}: {
  title: string
  empty: string
  children: ReactNode
}) {
  const hasBody = children != null && !(Array.isArray(children) && children.length === 0)
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">{title}</div>
      {hasBody ? (
        <div className="max-h-[50vh] overflow-auto rounded border border-border bg-bg p-4 font-mono text-[11px] leading-relaxed text-text-secondary">
          {children}
        </div>
      ) : (
        <p className="text-sm text-text-muted">{empty}</p>
      )}
    </div>
  )
}
