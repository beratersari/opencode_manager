export const JOB_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'In flight' },
  { id: 'queue', label: 'Queue' },
  { id: 'review', label: 'Review' },
  { id: 'error', label: 'Error' },
  { id: 'completed', label: 'Completed' },
] as const

export type JobListFilter = (typeof JOB_FILTERS)[number]['id']

export function jobChannelLabel(job: { job_kind?: string; provider?: string }): string {
  if ((job.job_kind || '') === 'review') {
    return (job.provider || '').toLowerCase() === 'azure' ? 'azure-review' : 'gitlab-review'
  }
  return 'n8n'
}

export function jobMatchesFilter(
  job: { status?: string; live?: boolean; job_kind?: string },
  filter: JobListFilter,
): boolean {
  const s = (job.status || '').toLowerCase()
  if (filter === 'all') return s !== 'queued'
  if (filter === 'active') return s === 'running' || Boolean(job.live && s !== 'queued')
  if (filter === 'queue') return s === 'queued'
  if (filter === 'review') return (job.job_kind || '') === 'review'
  if (filter === 'error') return s === 'error' || s === 'timeout' || s === 'not_found'
  if (filter === 'completed') return s === 'success'
  return true
}

export function emptyJobDetail() {
  return {
    job: null as null,
    prompts: [] as unknown[],
    messages: [] as unknown[],
    logs: [] as unknown[],
    error: null as string | null,
  }
}
