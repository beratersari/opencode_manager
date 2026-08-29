import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchJobs, fetchQueue } from '../../api/client'
import type { JobItem, JobsPayload } from '../../api/types'
import { useLive } from '../../app/live'
import { LiveDot } from '../../ui/LiveDot'
import { PageHeader } from '../../ui/PageHeader'
import { StatusBadge, statusToneClass } from '../../ui/StatusBadge'

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'In flight' },
  { id: 'queue', label: 'Queue' },
  { id: 'error', label: 'Error' },
  { id: 'completed', label: 'Completed' },
] as const

type Filter = (typeof FILTERS)[number]['id']

function matches(job: JobItem, filter: Filter): boolean {
  const s = (job.status || '').toLowerCase()
  if (filter === 'all') return true
  if (filter === 'active') return s === 'running' || Boolean(job.live && s !== 'queued')
  if (filter === 'queue') return s === 'queued'
  if (filter === 'error') return s === 'error' || s === 'timeout' || s === 'not_found'
  if (filter === 'completed') return s === 'success'
  return true
}

export function JobsPage() {
  const navigate = useNavigate()
  const live = useLive()
  const [filter, setFilter] = useState<Filter>('all')
  const [jira, setJira] = useState('')
  const [debounced, setDebounced] = useState('')
  const [page, setPage] = useState(1)
  const [payload, setPayload] = useState<JobsPayload | null>(null)
  const [queue, setQueue] = useState<JobItem[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setDebounced(jira.trim())
      setPage(1)
    }, 250)
    return () => window.clearTimeout(t)
  }, [jira])

  const load = useCallback(async () => {
    try {
      const data = await fetchJobs({ jiraId: debounced || undefined, page, pageSize: 25 })
      setPayload(data)
      setError(null)
      if (filter === 'queue') {
        const q = await fetchQueue()
        setQueue(q.items || [])
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load jobs')
    }
  }, [debounced, page, filter])

  useEffect(() => {
    void load()
  }, [load, live.generation])

  const jobs = useMemo(
    () => (payload?.jobs || []).filter((j) => matches(j, filter)),
    [payload, filter],
  )

  const rows = filter === 'queue' ? queue : jobs

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Workbench"
        title="Jobs"
        description={live.connected ? 'One card is one accepted run.' : 'Disconnected — list may be stale.'}
        actions={
          <label className="block text-xs text-text-muted">
            Find ticket
            <input
              className="vd-input mt-1 w-52 font-mono"
              placeholder="PROJ-123"
              value={jira}
              onChange={(e) => setJira(e.target.value)}
            />
          </label>
        }
      />

      {live.running > 0 && filter !== 'active' && (
        <div className="vd-panel flex flex-wrap items-center gap-3 px-4 py-3">
          <LiveDot label={`${live.running} running`} />
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setFilter(f.id)}
              className={`rounded-full px-3 py-1 text-xs font-semibold ${
                filter === f.id ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
              }`}
            >
              {f.label}
              {f.id === 'queue' && live.queueQueued > 0 ? (
                <span className="ml-1.5 font-mono">{live.queueQueued}</span>
              ) : null}
            </button>
          ))}
        </div>
        {payload && filter !== 'queue' && (
          <div className="flex items-center gap-2 text-xs text-text-muted">
            <span>
              page {payload.page} · {payload.total} total
            </span>
            <button type="button" className="vd-btn vd-btn-secondary px-3 py-1 text-xs" onClick={() => setPage((p) => Math.max(1, p - 1))}>
              Prev
            </button>
            <button type="button" className="vd-btn vd-btn-secondary px-3 py-1 text-xs" onClick={() => setPage((p) => p + 1)}>
              Next
            </button>
          </div>
        )}
      </div>

      {error && <div className="vd-alert vd-alert-danger">{error}</div>}

      {rows.length === 0 ? (
        <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">Nothing here for this filter.</div>
      ) : (
        <div className="space-y-2.5">
          {rows.map((j) => (
            <div
              key={j.job_id}
              className="vd-job"
              role="button"
              tabIndex={0}
              onClick={() => navigate(`/jobs/${encodeURIComponent(j.job_id)}`)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') navigate(`/jobs/${encodeURIComponent(j.job_id)}`)
              }}
            >
              <div className={`vd-job-bar ${statusToneClass(j.status)}`} />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-semibold">{j.jira_id}</span>
                  {j.live && <LiveDot />}
                  <StatusBadge status={j.status} size="sm" />
                </div>
                <div className="mt-1 font-mono text-[11px] text-text-muted">
                  {j.job_id} · {j.agent_mode} · {j.model} · {j.started_at || j.accepted_at || ''}
                </div>
                {j.error_message && <div className="mt-1.5 truncate text-xs text-danger-text">{j.error_message}</div>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
