import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { ApiError, fetchJob, fetchJobs } from '../api/client'
import type { JobItem } from '../api/types'
import { downloadIssueReport } from '../util/downloadReport'
import { REPORT_JOB_MAX, REPORT_NOTE_MIN, reportNoteReady } from '../util/jobReport'

function jobLabel(job: JobItem): string {
  const ticket = (job.jira_id || '').trim()
  return ticket ? `${job.job_id} — ${ticket}` : job.job_id
}

function jobIdFromPath(pathname: string): string | null {
  const m = pathname.match(/^\/jobs\/([^/]+)/)
  return m ? decodeURIComponent(m[1]) : null
}

export function ReportIssue() {
  const location = useLocation()
  const rootRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [jobs, setJobs] = useState<JobItem[]>([])
  const [loadingJobs, setLoadingJobs] = useState(false)
  const [query, setQuery] = useState('')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, busy])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoadingJobs(true)
    setError(null)
    setDone(null)
    const fromPath = jobIdFromPath(location.pathname)
    Promise.all([
      fetchJobs({ page: 1, pageSize: 100 }),
      fromPath ? fetchJob(fromPath).catch(() => null) : Promise.resolve(null),
    ])
      .then(([payload, current]) => {
        if (cancelled) return
        const list = [...(payload.jobs || [])]
        const currentJob = current && 'job' in current ? current.job : null
        if (currentJob && !list.some((j) => j.job_id === currentJob.job_id)) {
          list.unshift(currentJob)
        }
        setJobs(list)
        const match = fromPath ? list.find((j) => j.job_id === fromPath) : null
        setSelectedIds(match ? [match.job_id] : [])
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setJobs([])
        setSelectedIds([])
        setError(err instanceof Error ? err.message : 'Could not load jobs')
      })
      .finally(() => {
        if (!cancelled) setLoadingJobs(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, location.pathname])

  useEffect(() => {
    if (!open) return
    const q = query.trim()
    if (!q) return
    const t = window.setTimeout(() => {
      void fetchJobs({ page: 1, pageSize: 100, jiraId: q })
        .then((payload) => {
          const extra = payload.jobs || []
          if (!extra.length) return
          setJobs((prev) => {
            const seen = new Set(prev.map((j) => j.job_id))
            const more = extra.filter((j) => !seen.has(j.job_id))
            return more.length ? [...prev, ...more] : prev
          })
        })
        .catch(() => undefined)
    }, 250)
    return () => window.clearTimeout(t)
  }, [open, query])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return jobs
    return jobs.filter((j) => jobLabel(j).toLowerCase().includes(q))
  }, [jobs, query])

  const canSubmit = Boolean(reportNoteReady(note) && !busy)

  function toggleJob(jobId: string) {
    setSelectedIds((prev) => {
      if (prev.includes(jobId)) return prev.filter((id) => id !== jobId)
      if (prev.length >= REPORT_JOB_MAX) return prev
      return [...prev, jobId]
    })
  }

  async function submit() {
    if (!reportNoteReady(note)) return
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const filename = await downloadIssueReport({
        kind: selectedIds.length ? (selectedIds.length > 1 ? 'jobs' : 'job') : 'general',
        note: note.trim(),
        jobIds: selectedIds.length ? selectedIds : undefined,
      })
      setDone(filename)
      setNote('')
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Download failed'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        className="vd-btn vd-btn-secondary w-full justify-start px-3 py-1.5 text-xs"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
      >
        <IconFlag />
        Report issue
      </button>

      {open && (
        <div className="vd-report-panel" role="dialog" aria-label="Report issue">
          <div className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
            What is this about?
          </div>
          <p className="mb-2 text-[11px] text-text-muted">
            Select one or more jobs. None selected = general process report.
          </p>
          <input
            className="vd-input mb-2 py-1.5 text-xs"
            type="search"
            placeholder="Filter jobs…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            disabled={loadingJobs}
          />
          <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px]">
            <span className="text-text-muted">
              {selectedIds.length
                ? `${selectedIds.length} job${selectedIds.length === 1 ? '' : 's'} selected`
                : 'General report'}
              {selectedIds.length >= REPORT_JOB_MAX ? ` (max ${REPORT_JOB_MAX})` : ''}
            </span>
            <button
              type="button"
              className="vd-btn vd-btn-secondary px-2 py-0.5 text-[11px]"
              disabled={loadingJobs || !filtered.length}
              onClick={() => {
                setSelectedIds((prev) => {
                  const next = [...prev]
                  for (const job of filtered) {
                    if (next.includes(job.job_id)) continue
                    if (next.length >= REPORT_JOB_MAX) break
                    next.push(job.job_id)
                  }
                  return next
                })
              }}
            >
              Select visible
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-secondary px-2 py-0.5 text-[11px]"
              disabled={!selectedIds.length}
              onClick={() => setSelectedIds([])}
            >
              Clear
            </button>
          </div>
          <div
            className="vd-report-list"
            role="listbox"
            aria-multiselectable="true"
            aria-label="Issue target"
          >
            <button
              type="button"
              role="option"
              aria-selected={selectedIds.length === 0}
              className={selectedIds.length === 0 ? 'vd-report-option is-selected' : 'vd-report-option'}
              onClick={() => setSelectedIds([])}
            >
              <span className="font-medium text-text">General issue</span>
              <span className="block text-[11px] text-text-muted">
                Process logs, queue, recent jobs, layout, and your note
              </span>
            </button>
            {loadingJobs && (
              <div className="px-2 py-2 text-xs text-text-muted">Loading jobs…</div>
            )}
            {!loadingJobs &&
              filtered.map((job) => {
                const selected = selectedIds.includes(job.job_id)
                const atCap = !selected && selectedIds.length >= REPORT_JOB_MAX
                return (
                  <button
                    key={job.job_id}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    disabled={atCap}
                    className={selected ? 'vd-report-option is-selected' : 'vd-report-option'}
                    onClick={() => toggleJob(job.job_id)}
                    title={atCap ? `At most ${REPORT_JOB_MAX} jobs` : jobLabel(job)}
                  >
                    <span className="font-mono text-[11px] text-text-secondary">
                      {selected ? '☑ ' : '☐ '}
                      {job.job_id}
                    </span>
                    <span className="block truncate text-xs text-text">
                      {(job.jira_id || '—') + (job.status ? ` · ${job.status}` : '')}
                    </span>
                  </button>
                )
              })}
            {!loadingJobs && jobs.length === 0 && (
              <div className="px-2 py-2 text-[11px] text-text-muted">No jobs yet.</div>
            )}
            {!loadingJobs && jobs.length > 0 && filtered.length === 0 && (
              <div className="px-2 py-2 text-[11px] text-text-muted">No matching jobs.</div>
            )}
          </div>

          <label className="mt-3 block text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
            Note <span className="text-danger-text">*</span>
            <textarea
              className="vd-input mt-1 min-h-[5.5rem] resize-y py-1.5 text-xs leading-relaxed"
              value={note}
              maxLength={8000}
              placeholder="What went wrong? What did you expect?"
              onChange={(e) => setNote(e.target.value)}
              disabled={busy}
              required
              minLength={REPORT_NOTE_MIN}
              aria-required="true"
            />
          </label>
          <p className={`mt-1 text-xs ${reportNoteReady(note) ? 'text-text-muted' : 'text-danger-text'}`}>
            {note.trim().length}/{REPORT_NOTE_MIN} characters required
          </p>

          {error && <div className="mt-2 text-xs text-danger-text">{error}</div>}
          {done && <div className="mt-2 text-xs text-success-text">Downloaded {done}</div>}

          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              className="vd-btn vd-btn-secondary px-3 py-1.5 text-xs"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              Close
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-primary px-3 py-1.5 text-xs"
              disabled={!canSubmit}
              onClick={() => void submit()}
            >
              {busy ? 'Preparing…' : 'Download zip'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function IconFlag() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M3.5 13.5V3.5h7.2l-.8 2.4 1.2.6H12.5v4.5H8.8l.7-2.1-1.3-.6H3.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  )
}
