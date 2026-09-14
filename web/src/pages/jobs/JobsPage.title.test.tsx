import React from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LiveContext } from '../../app/live'
import { JobsPage } from './JobsPage'

const fetchJobs = vi.fn()
const fetchQueue = vi.fn()

vi.mock('../../api/client', () => ({
  fetchJobs: (...args: unknown[]) => fetchJobs(...args),
  fetchQueue: (...args: unknown[]) => fetchQueue(...args),
}))

describe('JobsPage MR title', () => {
  beforeEach(() => {
    fetchJobs.mockResolvedValue({
      jobs: [
        {
          job_id: 'job_aaa',
          jira_id: '109-3',
          mr_title: 'Resolve VOLKAN-12884 "Feat/"',
          source: 'group/app',
          job_kind: 'review',
          provider: 'gitlab',
          status: 'success',
          live: false,
          agent_mode: 'code-reviewer',
          model: 'opencode/x',
        },
      ],
      total: 1,
      page: 1,
      page_size: 25,
      filter: 'all',
      server_time: 't',
    })
    fetchQueue.mockResolvedValue({ items: [], queued_count: 0 })
  })

  afterEach(() => {
    cleanup()
    fetchJobs.mockReset()
    fetchQueue.mockReset()
  })

  it('uses the merge-request title as the job card title', async () => {
    render(
      <MemoryRouter>
        <LiveContext.Provider value={{ connected: true, generation: 0, running: 0, queueQueued: 0 }}>
          <JobsPage />
        </LiveContext.Provider>
      </MemoryRouter>,
    )
    await waitFor(() => {
      expect(screen.getByText('Resolve VOLKAN-12884 "Feat/"')).toBeTruthy()
    })
    expect(screen.getByText(/109-3/)).toBeTruthy()
    expect(screen.getByText(/job_aaa/)).toBeTruthy()
    expect(screen.queryByText('group/app')).toBeNull()
  })
})
