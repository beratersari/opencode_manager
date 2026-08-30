import type { ChatMessage, JobItem, LogLine, PromptRow } from '../api/types'

export const REPORT_NOTE_MIN = 20

export function reportNoteReady(note: string): boolean {
  return note.trim().length >= REPORT_NOTE_MIN
}

export type JobReportInput = {
  job: JobItem
  prompts: PromptRow[]
  messages: ChatMessage[]
  logs: LogLine[]
  serveLog?: string
  serveLogMissing?: boolean
  note: string
  exportedAt: string
}

export function reportZipName(job: Pick<JobItem, 'jira_id' | 'job_id'>): string {
  const ticket = (job.jira_id || 'ticket').replace(/[^\w.-]+/g, '_')
  const id = (job.job_id || 'job').replace(/[^\w.-]+/g, '_')
  return `${ticket}_${id}_report.zip`
}

export function buildJobReportFiles(input: JobReportInput): Record<string, string> {
  const note = input.note.trim()
  const logs = input.logs.map((line) => line.message).join('\n')
  return {
    'NOTE.txt': [
      `jira_id: ${input.job.jira_id}`,
      `job_id: ${input.job.job_id}`,
      `exported_at: ${input.exportedAt}`,
      '',
      note,
      '',
    ].join('\n'),
    'job.json': JSON.stringify(
      {
        exported_at: input.exportedAt,
        job: input.job,
      },
      null,
      2,
    ),
    'prompts.json': JSON.stringify({ prompts: input.prompts }, null, 2),
    'chat.json': JSON.stringify({ messages: input.messages }, null, 2),
    'logs.txt': logs ? `${logs}\n` : '',
    'opencode-serve.log': input.serveLogMissing
      ? '(no serve log for this job — serve never started or the file was removed)\n'
      : input.serveLog
        ? input.serveLog.endsWith('\n')
          ? input.serveLog
          : `${input.serveLog}\n`
        : '',
    'README.txt': [
      'OpenCode Session Manager job report',
      '',
      'NOTE.txt             reporter note (not stored on the server)',
      'job.json             dashboard job record (no PAT, no callback_url)',
      'prompts.json         user messages OSM POSTed to OpenCode',
      'chat.json            transcript snapshot or live serve copy',
      'logs.txt             OSM per-job manager log',
      'opencode-serve.log   stdout/stderr from this job\'s opencode serve',
      '',
    ].join('\n'),
  }
}
