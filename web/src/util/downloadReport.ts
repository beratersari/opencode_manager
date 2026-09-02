import { fetchChat, fetchJob, fetchLogs, fetchPrompts, fetchReportContext, fetchServeLog } from '../api/client'
import type { ReportContext } from '../api/types'
import { downloadBlob } from './download'
import { buildJobReportFiles, reportNoteReady, reportZipName } from './jobReport'
import { zipTextFiles } from './zipStore'

export async function downloadIssueReport(opts: {
  kind: 'job' | 'general'
  jobId?: string
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

  if (opts.kind === 'job') {
    const id = (opts.jobId || '').trim()
    if (!id) throw new Error('job_id is required')
    const [body, prompts, chat, logs, serve] = await Promise.all([
      fetchJob(id),
      fetchPrompts(id),
      fetchChat(id),
      fetchLogs(id, { limit: 0 }),
      fetchServeLog(id),
    ])
    const files = buildJobReportFiles({
      kind: 'job',
      job: body.job,
      prompts: prompts.prompts || [],
      messages: chat.messages || [],
      logs: logs.lines || [],
      serveLog: serve.text || '',
      serveLogMissing: serve.missing,
      context,
      contextError,
      note,
      exportedAt,
    })
    const name = reportZipName(body.job, exportedAt)
    downloadBlob(name, zipTextFiles(files), 'application/zip')
    return name
  }

  const files = buildJobReportFiles({
    kind: 'general',
    context,
    contextError,
    note,
    exportedAt,
  })
  const name = reportZipName(null, exportedAt)
  downloadBlob(name, zipTextFiles(files), 'application/zip')
  return name
}
