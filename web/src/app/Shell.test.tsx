import React from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Shell } from './Shell'

const fetchMeta = vi.fn()

vi.mock('../api/client', () => ({
  fetchMeta: (...args: unknown[]) => fetchMeta(...args),
}))

vi.mock('./live', () => ({
  useLive: () => ({ connected: true, generation: 0, running: 1, queueQueued: 0 }),
}))

vi.mock('../ui/ReportIssue', () => ({
  ReportIssue: () => <div>Report issue</div>,
}))

function renderShell() {
  const router = createMemoryRouter(
    [{ path: '/', element: <Shell />, children: [{ index: true, element: <div>jobs</div> }] }],
    { initialEntries: ['/'] },
  )
  return render(<RouterProvider router={router} />)
}

describe('Shell brand', () => {
  beforeEach(() => {
    fetchMeta.mockResolvedValue({ app_name: 'aMIR-mini', version: '0.1.1', server_time: 't' })
  })

  afterEach(() => {
    cleanup()
    fetchMeta.mockReset()
  })

  it('shows brand name and version without the mark logo', async () => {
    renderShell()
    expect(screen.getByText('aMIR-mini')).toBeTruthy()
    expect(document.querySelector('.vd-mark')).toBeNull()
    expect(screen.queryByText('aM')).toBeNull()
    await waitFor(() => {
      expect(screen.getByText('0.1.1')).toBeTruthy()
    })
    expect(screen.queryByText(/live/i)).toBeNull()
    expect(screen.queryByText(/offline/i)).toBeNull()
  })

  it('keeps the brand name when meta fails', async () => {
    fetchMeta.mockRejectedValue(new Error('down'))
    renderShell()
    expect(screen.getByText('aMIR-mini')).toBeTruthy()
    await waitFor(() => {
      expect(fetchMeta).toHaveBeenCalled()
    })
    expect(screen.queryByText('0.1.1')).toBeNull()
    expect(document.querySelector('.vd-mark')).toBeNull()
  })
})
