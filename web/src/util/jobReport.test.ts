import { describe, expect, it } from 'vitest'
import { buildGeneralReportFiles, buildJobReportFiles, reportNoteReady, reportZipName } from './jobReport'
import { crc32, zipTextFiles } from './zipStore'

describe('job report zip', () => {
  const job = {
    job_id: 'job_aaa',
    jira_id: 'PROJ-1',
    status: 'error',
    live: false,
    repo_url: 'https://gitlab.example/g/r.git',
    source_branch: 'develop',
    clone_path: 'C:/osm/.temp/PROJ-1',
    text: 'last assistant answer',
    attempts: [{ number: 1, kind: 'hang', prompt_id: 'HANG_RESUME', session_id: 'ses_1' }],
  }

  const context = {
    meta: { app_name: 'OpenCode Session Manager', version: '0.1.0' },
    runtime: { python: '3.12', live: { running: 0, queued: 1 } },
    settings: { listen_port: 4096, opencode_bin: 'opencode' },
    queue: { items: [{ job_id: 'job_q', jira_id: 'Q-1' }], queued_count: 1 },
    app_log: { text: 'app started\n', missing: false },
    crash_log: { text: '', missing: true },
    wrapper_exit_log: { text: 'exit 1\n', missing: false },
    opencode_logs: [{ name: 'dev.log', text: 'opencode boot\n', missing: false }],
    serve_logs_present: ['job_aaa.log'],
  }

  it('requires a 20-character note', () => {
    expect(reportNoteReady('')).toBe(false)
    expect(reportNoteReady('   short   ')).toBe(false)
    expect(reportNoteReady('1234567890123456789')).toBe(false)
    expect(reportNoteReady('12345678901234567890')).toBe(true)
    expect(reportNoteReady('  hung after compact mid-turn  ')).toBe(true)
  })

  it('names the zip from ticket, job id, and stamp', () => {
    expect(reportZipName(job, '2026-08-30T12:00:00.000Z')).toBe(
      'osm-report-PROJ-1-job_aaa-20260830-120000.zip',
    )
    expect(reportZipName({ jira_id: 'A/B', job_id: 'job x' }, '2026-01-02T03:04:05Z')).toBe(
      'osm-report-A_B-job_x-20260102-030405.zip',
    )
    expect(reportZipName(null, '2026-08-30T12:00:00.000Z')).toBe('osm-report-general-20260830-120000.zip')
  })

  it('puts the note, job records, and process extras in the bundle', () => {
    const files = buildJobReportFiles({
      job,
      prompts: [{ id: 'ORIGINAL', text: 'do it', posted_at: 't' }],
      messages: [
        {
          id: 'm1',
          session_id: 'ses_1',
          role: 'assistant',
          parts: [{ type: 'text', text: 'hello' }],
        },
      ],
      logs: [{ timestamp: 'ts', message: 'line one' }],
      serveLog: 'opencode serve listening\n',
      serveLogMissing: false,
      context,
      note: '  hung after compact  ',
      exportedAt: '2026-08-30T12:00:00.000Z',
    })
    expect(files['NOTE.txt']).toContain('hung after compact')
    expect(files['NOTE.txt']).toContain('job_aaa')
    expect(files['job.json']).toContain('"job_id": "job_aaa"')
    expect(files['job/record.json']).toContain('"job_id": "job_aaa"')
    expect(files['job/parameters.json']).toContain('gitlab.example')
    expect(files['job/retry_attempts.json']).toContain('HANG_RESUME')
    expect(files['prompts.json']).toContain('ORIGINAL')
    expect(files['job/prompts/ORIGINAL.txt']).toContain('do it')
    expect(files['chat.json']).toContain('ses_1')
    expect(files['job/chat.md']).toContain('hello')
    expect(files['job/result.txt']).toContain('last assistant answer')
    expect(files['logs.txt']).toBe('line one\n')
    expect(files['job/system.log']).toBe('line one\n')
    expect(files['opencode-serve.log']).toBe('opencode serve listening\n')
    expect(files['job/git.txt']).toContain('C:/osm/.temp/PROJ-1')
    expect(files['job/git.txt']).toContain('always deletes the clone')
    expect(files['runtime.json']).toContain('3.12')
    expect(files['settings.json']).toContain('opencode')
    expect(files['queue.json']).toContain('job_q')
    expect(files['system/app.log']).toContain('app started')
    expect(files['system/crash.log']).toContain('no crash.log')
    expect(files['system/wrapper-exit.log']).toContain('exit 1')
    expect(files['system/opencode-logs/dev.log']).toContain('opencode boot')
    expect(files['README.txt']).toContain('job/opencode-serve.log')
  })

  it('builds a general report without a job folder', () => {
    const files = buildGeneralReportFiles({
      context,
      note: 'dashboard froze after refresh',
      exportedAt: '2026-08-30T12:00:00.000Z',
    })
    expect(files['NOTE.txt']).toContain('kind: general')
    expect(files['system/app.log']).toContain('app started')
    expect(files['job/record.json']).toBeUndefined()
    expect(files['job.json']).toBeUndefined()
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
    expect(files['job/opencode-serve.log']).toContain('no serve log')
  })

  it('writes a zip that contains each file name', () => {
    const zip = zipTextFiles({ 'NOTE.txt': 'hello', 'job/system.log': 'x' })
    const asText = new TextDecoder().decode(zip)
    expect(asText).toContain('NOTE.txt')
    expect(asText).toContain('job/system.log')
    expect(asText).toContain('hello')
    expect(zip[0]).toBe(0x50)
    expect(zip[1]).toBe(0x4b)
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926)
  })
})
