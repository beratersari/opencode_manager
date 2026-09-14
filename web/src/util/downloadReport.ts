import {
  fetchChat,
  fetchJob,
  fetchJobs,
  fetchLogs,
  fetchPrompts,
  fetchQueue,
  fetchReportContext,
  fetchServeLog,
} from '../api/client'
import type { JobItem, ReportContext } from '../api/types'
import { downloadBlob } from './download'
import {
  buildJobReportFiles,
  REPORT_JOB_MAX,
  reportNoteReady,
  reportZipName,
  type JobReportBundle,
} from './jobReport'
import { zipTextFiles } from './zipStore'

export async function downloadIssueReport(opts: {
  kind: 'job' | 'general' | 'jobs'
  jobId?: string
  jobIds?: string[]
  note: string
}): Promise<string> {
  const note = opts.note.trim()
  if (!reportNoteReady(note)) {
    throw new Error('Note is required')
  }
  const exportedAt = new Date().toISOString()
  let context: ReportContext | null = null
  let contextError: string | null = null
  try {
    context = await fetchReportContext()
  } catch (err) {
    contextError = err instanceof Error ? err.message : 'Failed to load report context'
  }

  const jobIds = uniqueJobIds(opts.jobIds?.length ? opts.jobIds : opts.jobId ? [opts.jobId] : [])
  if (opts.kind === 'job' || opts.kind === 'jobs' || jobIds.length) {
    if (!jobIds.length) throw new Error('job_id is required')
    if (jobIds.length > REPORT_JOB_MAX) {
      throw new Error(`Select at most ${REPORT_JOB_MAX} jobs`)
    }
    const bundles = await Promise.all(jobIds.map((id) => loadJobBundle(id)))
    const files = buildJobReportFiles({
      kind: bundles.length > 1 ? 'jobs' : 'job',
      job: bundles.length === 1 ? bundles[0].job : null,
      prompts: bundles.length === 1 ? bundles[0].prompts : [],
      messages: bundles.length === 1 ? bundles[0].messages : [],
      sessionIds: bundles.length === 1 ? bundles[0].sessionIds : [],
      logs: bundles.length === 1 ? bundles[0].logs : [],
      serveLog: bundles.length === 1 ? bundles[0].serveLog : '',
      serveLogMissing: bundles.length === 1 ? bundles[0].serveLogMissing : false,
      bundles,
      context,
      contextError,
      note,
      exportedAt,
    })
    const name = reportZipName(bundles[0]?.job, exportedAt, bundles.map((b) => b.job))
    downloadBlob(name, zipTextFiles(files), 'application/zip')
    return name
  }

  let recentJobs: JobItem[] = []
  try {
    const listed = await fetchJobs({ page: 1, pageSize: 100 })
    recentJobs = listed.jobs || []
  } catch {
    recentJobs = []
  }
  try {
    if (context && !context.queue) {
      const queue = await fetchQueue()
      context = { ...context, queue: { items: queue.items || [], queued_count: queue.queued_count || 0 } }
    }
  } catch {
    /* queue already optional */
  }
  const files = buildJobReportFiles({
    kind: 'general',
    context,
    contextError,
    recentJobs,
    note,
    exportedAt,
  })
  const name = reportZipName(null, exportedAt)
  downloadBlob(name, zipTextFiles(files), 'application/zip')
  return name
}

function uniqueJobIds(ids: string[]): string[] {
  const out: string[] = []
  const seen = new Set<string>()
  for (const raw of ids) {
    const id = (raw || '').trim()
    if (!id || seen.has(id)) continue
    seen.add(id)
    out.push(id)
  }
  return out
}

async function loadJobBundle(id: string): Promise<JobReportBundle> {
  try {
    const [body, prompts, chat, logs, serve] = await Promise.all([
      fetchJob(id),
      fetchPrompts(id),
      fetchChat(id),
      fetchLogs(id, { limit: 0 }),
      fetchServeLog(id),
    ])
    return {
      job: body.job,
      prompts: prompts.prompts || [],
      messages: chat.messages || [],
      sessionIds: chat.session_ids || [],
      logs: logs.lines || [],
      serveLog: serve.text || '',
      serveLogMissing: serve.missing,
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : `Failed to load ${id}`
    return {
      job: { job_id: id, jira_id: '', status: 'error', live: false, error_message: message },
      prompts: [],
      messages: [],
      logs: [],
      serveLog: '',
      serveLogMissing: true,
      error: message,
    }
  }
}
