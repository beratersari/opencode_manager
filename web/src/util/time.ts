import { useEffect, useMemo, useState } from 'react'

export function parseTimeMs(value: unknown): number | null {
  if (value == null || value === '') return null
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 1e12 ? value * 1000 : value
  }
  if (typeof value === 'string') {
    const asNum = Number(value)
    if (Number.isFinite(asNum) && value.trim() !== '') {
      return asNum < 1e12 ? asNum * 1000 : asNum
    }
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  if (typeof value === 'object') {
    const rec = value as { created?: unknown; created_at?: unknown }
    return parseTimeMs(rec.created ?? rec.created_at)
  }
  return null
}

export function formatChatTime(value: unknown): string {
  const ms = parseTimeMs(value)
  if (ms == null) return ''
  try {
    return new Date(ms).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
}

export function elapsedSecondsBetween(
  startedAt: string | null | undefined,
  completedAt: string | null | undefined,
  nowMs: number = Date.now(),
): number | null {
  const start = parseTimeMs(startedAt)
  if (start == null) return null
  const end = completedAt ? parseTimeMs(completedAt) : nowMs
  if (end == null) return null
  return Math.max(0, Math.floor((end - start) / 1000))
}

export function formatElapsedSeconds(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null || !Number.isFinite(totalSeconds) || totalSeconds < 0) {
    return '—'
  }
  const s = Math.floor(totalSeconds)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) {
    return `${h}h ${String(m).padStart(2, '0')}m ${String(sec).padStart(2, '0')}s`
  }
  if (m > 0) {
    return `${m}m ${String(sec).padStart(2, '0')}s`
  }
  return `${sec}s`
}

export function formatElapsedBetween(
  startedAt: string | null | undefined,
  completedAt: string | null | undefined,
  nowMs: number = Date.now(),
): string {
  return formatElapsedSeconds(elapsedSecondsBetween(startedAt, completedAt, nowMs))
}

/** Live / queued: clock from start. Terminal: start → completed_at. */
export function jobElapsedWindow(job: {
  started_at?: string | null
  accepted_at?: string | null
  completed_at?: string | null
  live?: boolean
}): { start: string | null; end: string | null; ticking: boolean } {
  const start = job.started_at || job.accepted_at || null
  const done = Boolean(job.completed_at) && !job.live
  return {
    start,
    end: done ? job.completed_at || null : null,
    ticking: Boolean(start) && !done,
  }
}

export function formatJobElapsed(
  job: {
    started_at?: string | null
    accepted_at?: string | null
    completed_at?: string | null
    live?: boolean
  },
  nowMs: number = Date.now(),
): string {
  const { start, end } = jobElapsedWindow(job)
  return formatElapsedBetween(start, end, nowMs)
}

export function useNow(enabled = true, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [enabled, intervalMs])
  return now
}

export function useJobElapsed(job: {
  started_at?: string | null
  accepted_at?: string | null
  completed_at?: string | null
  live?: boolean
} | null): string {
  const window = job ? jobElapsedWindow(job) : { start: null, end: null, ticking: false }
  const now = useNow(window.ticking)
  return useMemo(
    () => formatElapsedBetween(window.start, window.end, now),
    [window.start, window.end, now],
  )
}
