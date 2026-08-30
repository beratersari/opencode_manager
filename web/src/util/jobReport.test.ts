import { describe, expect, it } from 'vitest'
import { buildJobReportFiles, reportNoteReady, reportZipName } from './jobReport'
import { crc32, zipTextFiles } from './zipStore'

describe('job report zip', () => {
  const job = {
    job_id: 'job_aaa',
    jira_id: 'PROJ-1',
    status: 'error',
    live: false,
  }

  it('requires a 20-character note', () => {
    expect(reportNoteReady('')).toBe(false)
    expect(reportNoteReady('   short   ')).toBe(false)
    expect(reportNoteReady('1234567890123456789')).toBe(false)
    expect(reportNoteReady('12345678901234567890')).toBe(true)
    expect(reportNoteReady('  hung after compact mid-turn  ')).toBe(true)
  })

  it('names the zip from ticket and job id', () => {
    expect(reportZipName(job)).toBe('PROJ-1_job_aaa_report.zip')
    expect(reportZipName({ jira_id: 'A/B', job_id: 'job x' })).toBe('A_B_job_x_report.zip')
  })

  it('puts the note and job records in the bundle', () => {
    const files = buildJobReportFiles({
      job,
      prompts: [{ id: 'ORIGINAL', text: 'do it', posted_at: 't' }],
      messages: [{ id: 'm1', session_id: 'ses_1', role: 'assistant', parts: [] }],
      logs: [{ timestamp: 'ts', message: 'line one' }],
      serveLog: 'opencode serve listening\n',
      serveLogMissing: false,
      note: '  hung after compact  ',
      exportedAt: '2026-08-30T12:00:00.000Z',
    })
    expect(files['NOTE.txt']).toContain('hung after compact')
    expect(files['NOTE.txt']).toContain('job_aaa')
    expect(files['job.json']).toContain('"job_id": "job_aaa"')
    expect(files['prompts.json']).toContain('ORIGINAL')
    expect(files['chat.json']).toContain('ses_1')
    expect(files['logs.txt']).toBe('line one\n')
    expect(files['opencode-serve.log']).toBe('opencode serve listening\n')
    expect(files['README.txt']).toContain('opencode-serve.log')
  })

  it('notes a missing serve log instead of omitting the file', () => {
    const files = buildJobReportFiles({
      job,
      prompts: [],
      messages: [],
      logs: [],
      serveLog: '',
      serveLogMissing: true,
      note: '',
      exportedAt: 't',
    })
    expect(files['opencode-serve.log']).toContain('no serve log')
  })

  it('writes a zip that contains each file name', () => {
    const zip = zipTextFiles({ 'NOTE.txt': 'hello', 'logs.txt': 'x' })
    const asText = new TextDecoder().decode(zip)
    expect(asText).toContain('NOTE.txt')
    expect(asText).toContain('logs.txt')
    expect(asText).toContain('hello')
    expect(zip[0]).toBe(0x50)
    expect(zip[1]).toBe(0x4b)
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926)
  })
})
