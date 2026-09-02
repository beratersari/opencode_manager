import type { JobChatPayload, JobItem, JobsPayload, LogLine, PromptRow, ReportContext } from './types'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string): Promise<T> {
  const res = await fetch(path)
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new ApiError((body as { detail?: string }).detail || `HTTP ${res.status}`, res.status)
  }
  return body as T
}

export function fetchJobs(opts?: { jiraId?: string; page?: number; pageSize?: number; filter?: string }) {
  const params = new URLSearchParams()
  if (opts?.jiraId) params.set('jira_id', opts.jiraId)
  if (opts?.page) params.set('page', String(opts.page))
  if (opts?.pageSize) params.set('page_size', String(opts.pageSize))
  if (opts?.filter && opts.filter !== 'all') params.set('filter', opts.filter)
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<JobsPayload>(`/api/jobs${q}`)
}

export function fetchJob(jobId: string) {
  return request<{ job: JobItem; system_logs: LogLine[] }>(`/api/jobs/${encodeURIComponent(jobId)}`)
}

export function fetchPrompts(jobId: string) {
  return request<{ prompts: PromptRow[] }>(`/api/jobs/${encodeURIComponent(jobId)}/prompts`)
}

export function fetchChat(jobId: string) {
  return request<JobChatPayload>(`/api/jobs/${encodeURIComponent(jobId)}/chat`)
}

export function fetchServeLog(jobId: string) {
  return request<{ job_id: string; missing: boolean; text: string }>(
    `/api/jobs/${encodeURIComponent(jobId)}/serve-log`,
  )
}

export function fetchLogs(jobId: string, opts?: { limit?: number }) {
  const params = new URLSearchParams()
  if (opts?.limit !== undefined) params.set('limit', String(opts.limit))
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<{ lines: LogLine[] }>(`/api/jobs/${encodeURIComponent(jobId)}/logs${q}`)
}

export function fetchQueue(opts?: { jiraId?: string }) {
  const params = new URLSearchParams()
  if (opts?.jiraId) params.set('jira_id', opts.jiraId)
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<{ items: JobItem[]; queued_count: number }>(`/api/queue${q}`)
}

export function fetchMeta() {
  return request<{ version: string; server_time: string; app_name: string }>('/api/meta')
}

export function fetchReportContext() {
  return request<ReportContext>('/api/report-context')
}

export function dashboardWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws`
}
