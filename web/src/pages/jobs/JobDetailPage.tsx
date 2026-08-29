import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchChat, fetchJob, fetchLogs, fetchPrompts } from '../../api/client'
import type { ChatMessage, JobItem, LogLine, PromptRow } from '../../api/types'
import { useLive } from '../../app/live'
import { LiveDot } from '../../ui/LiveDot'
import { MarkdownBody } from '../../ui/MarkdownBody'
import { MetaCard } from '../../ui/MetaCard'
import { StatusBadge } from '../../ui/StatusBadge'
import { Tabs } from '../../ui/Tabs'

type Tab = 'overview' | 'prompt' | 'chat' | 'logs'

export function JobDetailPage() {
  const { jobId = '' } = useParams()
  const live = useLive()
  const [job, setJob] = useState<JobItem | null>(null)
  const [prompts, setPrompts] = useState<PromptRow[]>([])
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [logs, setLogs] = useState<LogLine[]>([])
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('overview')

  const load = useCallback(async () => {
    const id = jobId.trim()
    if (!id) return
    try {
      const body = await fetchJob(id)
      setJob(body.job)
      setError(null)
      const [p, c, l] = await Promise.all([fetchPrompts(id), fetchChat(id), fetchLogs(id)])
      setPrompts(p.prompts || [])
      setMessages(c.messages || [])
      setLogs(l.lines || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load job')
    }
  }, [jobId])

  useEffect(() => {
    setTab('overview')
    void load()
  }, [jobId, load])

  useEffect(() => {
    if (job?.live) void load()
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

      <div className="flex justify-end">
        <button type="button" className="vd-btn vd-btn-secondary" onClick={() => void load()}>
          Refresh
        </button>
      </div>

      <Tabs
        tabs={[
          { id: 'overview', label: 'Details' },
          { id: 'prompt', label: 'Prompt', count: prompts.length },
          { id: 'chat', label: 'Transcript' },
          { id: 'logs', label: 'Logs', count: logs.length },
        ]}
        value={tab}
        onChange={setTab}
      />

      <div className="vd-panel min-h-[50vh] p-5">
        {error && <p className="text-sm text-danger-text">{error}</p>}
        {job && tab === 'overview' && <Overview job={job} />}
        {job && tab === 'prompt' && <PromptTab prompts={prompts} />}
        {job && tab === 'chat' && <ChatTab messages={messages} />}
        {job && tab === 'logs' && <LogsTab lines={logs} />}
      </div>
    </section>
  )
}

function Overview({ job }: { job: JobItem }) {
  const attempts = job.attempts || []
  return (
    <div className="space-y-6 text-sm">
      {job.text && (
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-text-muted">Result</div>
          <MarkdownBody text={job.text} />
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

function ChatTab({ messages }: { messages: ChatMessage[] }) {
  if (messages.length === 0) {
    return <p className="text-sm text-text-muted">No transcript stored yet.</p>
  }
  return (
    <div className="space-y-3">
      {messages.map((m) => (
        <div key={m.id} className="rounded border border-border bg-bg px-3 py-2">
          <div className="mb-1 text-[11px] font-semibold uppercase text-text-muted">{m.role}</div>
          {m.parts.map((part, i) => {
            if (part.type === 'text' && part.text) {
              return <MarkdownBody key={i} text={part.text} />
            }
            if (part.tool) {
              return (
                <details key={i} className="mt-1">
                  <summary className="font-mono text-[11px]">
                    {part.tool} {part.status}
                  </summary>
                  {part.output && <pre className="vd-pre mt-1">{part.output}</pre>}
                </details>
              )
            }
            return null
          })}
        </div>
      ))}
    </div>
  )
}

function LogsTab({ lines }: { lines: LogLine[] }) {
  if (lines.length === 0) {
    return <p className="text-sm text-text-muted">No log lines for this job_id.</p>
  }
  return (
    <div className="max-h-[70vh] overflow-auto rounded border border-border bg-bg p-4 font-mono text-[11px] leading-relaxed text-text-secondary">
      {lines.map((line, i) => (
        <div key={`${line.timestamp}-${i}`} className="border-b border-border/50 py-0.5">
          {line.message}
        </div>
      ))}
    </div>
  )
}
