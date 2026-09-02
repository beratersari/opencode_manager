import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { JobDetailPage } from './JobDetailPage'

const fetchJob = vi.fn()
const fetchPrompts = vi.fn()
const fetchChat = vi.fn()
const fetchLogs = vi.fn()
const fetchServeLog = vi.fn()
const fetchReportContext = vi.fn()

vi.mock('../../api/client', () => ({
  fetchJob: (...args: unknown[]) => fetchJob(...args),
  fetchPrompts: (...args: unknown[]) => fetchPrompts(...args),
  fetchChat: (...args: unknown[]) => fetchChat(...args),
  fetchLogs: (...args: unknown[]) => fetchLogs(...args),
  fetchServeLog: (...args: unknown[]) => fetchServeLog(...args),
  fetchReportContext: (...args: unknown[]) => fetchReportContext(...args),
}))

vi.mock('../../app/live', () => ({
  useLive: () => ({ connected: true, generation: 0, running: 0, queueQueued: 0 }),
}))

function renderAt(jobId: string) {
  const router = createMemoryRouter(
    [{ path: '/jobs/:jobId', element: <JobDetailPage /> }],
    { initialEntries: [`/jobs/${jobId}`] },
  )
  return { router, ...render(<RouterProvider router={router} />) }
}

const jobA = {
  job_id: 'job_aaa',
  jira_id: 'AAA-1',
  status: 'success',
  live: false,
  agent_mode: 'build',
  model: 'opencode/x',
  attempt: 1,
  retry_count: 1,
}

const jobB = {
  ...jobA,
  job_id: 'job_bbb',
  jira_id: 'BBB-1',
}

describe('JobDetailPage', () => {
  beforeEach(() => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:report')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    fetchReportContext.mockResolvedValue({
      meta: { app_name: 'OpenCode Session Manager' },
      runtime: {},
      settings: {},
      queue: { items: [], queued_count: 0 },
      app_log: { text: '', missing: true },
      crash_log: { text: '', missing: true },
    })
  })

  afterEach(() => {
    cleanup()
    fetchJob.mockReset()
    fetchPrompts.mockReset()
    fetchChat.mockReset()
    fetchLogs.mockReset()
    fetchServeLog.mockReset()
    fetchReportContext.mockReset()
    vi.restoreAllMocks()
  })

  it('clears the previous job when the next id 404s', async () => {
    fetchJob.mockImplementation(async (id: string) => {
      if (id === 'job_aaa') return { job: jobA, system_logs: [] }
      const err = new Error('No job job_missing')
      ;(err as Error & { status: number }).status = 404
      throw err
    })
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [] })
    fetchServeLog.mockResolvedValue({ job_id: 'job_aaa', missing: true, text: '' })

    const { router } = renderAt('job_aaa')

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'job_aaa' })).toBeTruthy()
    })

    await router.navigate('/jobs/job_missing')

    await waitFor(() => {
      expect(screen.getByText(/No job job_missing|Failed to load job/)).toBeTruthy()
    })
    expect(screen.queryByRole('heading', { name: 'job_aaa' })).toBeNull()
    expect(screen.queryByText('AAA-1')).toBeNull()
  })

  it('does not show Result from last assistant text while the job is live', async () => {
    fetchJob.mockResolvedValue({
      job: {
        ...jobA,
        job_id: 'job_run',
        jira_id: 'RUN-1',
        status: 'running',
        live: true,
        text: 'partial last assistant message',
      },
      system_logs: [],
    })
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [] })
    fetchServeLog.mockResolvedValue({ job_id: 'job_aaa', missing: true, text: '' })

    renderAt('job_run')
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'job_run' })).toBeTruthy()
    })
    expect(screen.queryByText('Result')).toBeNull()
    expect(screen.queryByText('partial last assistant message')).toBeNull()
  })

  it('shows Result only after a successful finish', async () => {
    fetchJob.mockResolvedValue({
      job: { ...jobA, text: 'final assistant answer' },
      system_logs: [],
    })
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [] })
    fetchServeLog.mockResolvedValue({ job_id: 'job_aaa', missing: true, text: '' })

    renderAt('job_aaa')
    await waitFor(() => {
      expect(screen.getByText('Result')).toBeTruthy()
    })
    expect(screen.getByText('final assistant answer')).toBeTruthy()
  })

  it('does not treat a finished error as Result', async () => {
    fetchJob.mockResolvedValue({
      job: {
        ...jobA,
        job_id: 'job_err',
        status: 'error',
        live: false,
        text: 'attempt 1 ended: hang',
        error_message: 'attempt 1 ended: hang',
      },
      system_logs: [],
    })
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [] })
    fetchServeLog.mockResolvedValue({ job_id: 'job_aaa', missing: true, text: '' })

    renderAt('job_err')
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'job_err' })).toBeTruthy()
    })
    expect(screen.queryByText('Result')).toBeNull()
    expect(screen.getByText('attempt 1 ended: hang')).toBeTruthy()
  })

  it('shows the job that matches the route', async () => {
    fetchJob.mockImplementation(async (id: string) => ({
      job: id === 'job_bbb' ? jobB : jobA,
      system_logs: [],
    }))
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [] })
    fetchServeLog.mockResolvedValue({ job_id: 'job_aaa', missing: true, text: '' })

    renderAt('job_bbb')
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'job_bbb' })).toBeTruthy()
    })
    expect(screen.getAllByText('BBB-1').length).toBeGreaterThan(0)
  })

  it('shows the serve log under the job log on the Logs tab', async () => {
    fetchJob.mockResolvedValue({ job: jobA, system_logs: [] })
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [{ timestamp: 't', message: 'osm line' }] })
    fetchServeLog.mockResolvedValue({
      job_id: 'job_aaa',
      missing: false,
      text: 'opencode serve listening',
    })

    renderAt('job_aaa')
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'job_aaa' })).toBeTruthy()
    })
    fireEvent.click(screen.getByRole('button', { name: /Logs/ }))
    expect(screen.getByText('Job log')).toBeTruthy()
    expect(screen.getByText('osm line')).toBeTruthy()
    expect(screen.getByText('OpenCode serve')).toBeTruthy()
    expect(screen.getByText('opencode serve listening')).toBeTruthy()
  })

  it('opens the report dialog for the selected job', async () => {
    fetchJob.mockResolvedValue({ job: jobA, system_logs: [] })
    fetchPrompts.mockResolvedValue({ prompts: [] })
    fetchChat.mockResolvedValue({ messages: [] })
    fetchLogs.mockResolvedValue({ lines: [] })
    fetchServeLog.mockResolvedValue({ job_id: 'job_aaa', missing: true, text: '' })

    renderAt('job_aaa')
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Report issue' })).toBeTruthy()
    })
    fireEvent.click(screen.getByRole('button', { name: 'Report issue' }))
    expect(screen.getByRole('dialog', { name: 'Report issue' })).toBeTruthy()
    expect(screen.getByText(/AAA-1 · job_aaa/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Download zip' }) as HTMLButtonElement).disabled).toBe(
      true,
    )
    fireEvent.change(screen.getByPlaceholderText('What went wrong?'), {
      target: { value: 'too short' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Download zip' }))
    expect(fetchLogs).not.toHaveBeenCalledWith('job_aaa', { limit: 0 })
    fireEvent.change(screen.getByPlaceholderText('What went wrong?'), {
      target: { value: 'it stopped mid-answer' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Download zip' }))
    await waitFor(() => {
      expect(fetchLogs).toHaveBeenCalledWith('job_aaa', { limit: 0 })
      expect(fetchServeLog).toHaveBeenCalledWith('job_aaa')
      expect(fetchReportContext).toHaveBeenCalled()
    })
  })
})
