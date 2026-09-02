import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ReportIssue } from './ReportIssue'

const fetchJobs = vi.fn()
const fetchJob = vi.fn()
const fetchPrompts = vi.fn()
const fetchChat = vi.fn()
const fetchLogs = vi.fn()
const fetchServeLog = vi.fn()
const fetchReportContext = vi.fn()

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(message: string, status: number) {
      super(message)
      this.status = status
    }
  },
  fetchJobs: (...args: unknown[]) => fetchJobs(...args),
  fetchJob: (...args: unknown[]) => fetchJob(...args),
  fetchPrompts: (...args: unknown[]) => fetchPrompts(...args),
  fetchChat: (...args: unknown[]) => fetchChat(...args),
  fetchLogs: (...args: unknown[]) => fetchLogs(...args),
  fetchServeLog: (...args: unknown[]) => fetchServeLog(...args),
  fetchReportContext: (...args: unknown[]) => fetchReportContext(...args),
}))

function renderAt(path: string) {
  const router = createMemoryRouter(
    [{ path: '/jobs/:jobId?', element: <ReportIssue /> }],
    { initialEntries: [path] },
  )
  return render(<RouterProvider router={router} />)
}

const jobA = {
  job_id: 'job_aaa',
  jira_id: 'AAA-1',
  status: 'error',
  live: false,
}

describe('ReportIssue', () => {
  beforeEach(() => {
    fetchJobs.mockResolvedValue({ jobs: [jobA], total: 1, page: 1, page_size: 100 })
    fetchJob.mockResolvedValue({ job: jobA, system_logs: [] })
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [] })
    fetchServeLog.mockResolvedValue({ job_id: 'job_aaa', missing: true, text: '' })
    fetchReportContext.mockResolvedValue({
      meta: { app_name: 'OpenCode Session Manager' },
      runtime: {},
      settings: {},
      queue: { items: [], queued_count: 0 },
      app_log: { text: 'boot\n', missing: false },
      crash_log: { text: '', missing: true },
    })
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:report')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    fetchJobs.mockReset()
    fetchJob.mockReset()
    fetchPrompts.mockReset()
    fetchChat.mockReset()
    fetchLogs.mockReset()
    fetchServeLog.mockReset()
    fetchReportContext.mockReset()
  })

  it('lets the operator pick a job from the dashboard', async () => {
    renderAt('/jobs')
    fireEvent.click(screen.getByRole('button', { name: 'Report issue' }))
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /job_aaa/ })).toBeTruthy()
    })
    fireEvent.click(screen.getByRole('option', { name: /job_aaa/ }))
    expect(screen.getByRole('option', { name: /job_aaa/ }).getAttribute('aria-selected')).toBe('true')
    const download = screen.getByRole('button', { name: 'Download zip' }) as HTMLButtonElement
    expect(download.disabled).toBe(true)
    fireEvent.change(screen.getByPlaceholderText('What went wrong? What did you expect?'), {
      target: { value: 'it stopped mid-answer after compact' },
    })
    fireEvent.click(download)
    await waitFor(() => {
      expect(fetchLogs).toHaveBeenCalledWith('job_aaa', { limit: 0 })
      expect(fetchServeLog).toHaveBeenCalledWith('job_aaa')
      expect(fetchReportContext).toHaveBeenCalled()
    })
  })

  it('pre-selects the job on the detail route', async () => {
    renderAt('/jobs/job_aaa')
    fireEvent.click(screen.getByRole('button', { name: 'Report issue' }))
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /job_aaa/ }).getAttribute('aria-selected')).toBe(
        'true',
      )
    })
  })
})
