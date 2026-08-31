import http from 'node:http'
import type { AddressInfo } from 'node:net'
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LiveContext } from '../../app/live'
import { JobsPage } from './JobsPage'

type Job = { job_id: string; jira_id: string; status: string; live: boolean }

function job(id: string, status: string): Job {
  return { job_id: id, jira_id: id.replace('job_', '').toUpperCase(), status, live: false }
}

function payload(filter: string, jobs: Job[]) {
  return { jobs, total: jobs.length, page: 1, page_size: 25, filter, server_time: 't' }
}

describe('JobsPage filter race', () => {
  let server: http.Server
  let origin = ''
  let realFetch: typeof fetch
  let releaseAll: (() => void) | null = null

  beforeEach(async () => {
    const allJobs = [job('job_ok', 'success'), job('job_err', 'error')]
    const errorJobs = [job('job_err', 'error')]
    server = http.createServer((req, res) => {
      if (req.method === 'OPTIONS') {
        res.writeHead(204, {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET',
        })
        res.end()
        return
      }
      const url = new URL(req.url || '/', 'http://127.0.0.1')
      const filter = url.searchParams.get('filter') || 'all'
      const body = filter === 'error' ? payload('error', errorJobs) : payload('all', allJobs)
      const send = () => {
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*',
        })
        res.end(JSON.stringify(body))
      }
      if (filter === 'all' || !url.searchParams.has('filter')) {
        const timer = setTimeout(send, 80)
        releaseAll = () => {
          clearTimeout(timer)
          send()
        }
        return
      }
      send()
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    realFetch = globalThis.fetch
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      const url = raw.startsWith('/') ? `${origin}${raw}` : raw
      return realFetch(url, init)
    }) as typeof fetch
  })

  afterEach(async () => {
    cleanup()
    globalThis.fetch = realFetch
    releaseAll = null
    await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())))
  })

  it('does not keep All tickets after Error returns and the late All response arrives', async () => {
    render(
      <MemoryRouter>
        <LiveContext.Provider value={{ connected: true, generation: 0, running: 0, queueQueued: 0 }}>
          <JobsPage />
        </LiveContext.Provider>
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Error' }))
    await waitFor(() => {
      expect(screen.getByText('ERR')).toBeTruthy()
    })
    releaseAll?.()
    await new Promise((r) => setTimeout(r, 120))
    expect(screen.queryByText('OK')).toBeNull()
    expect(screen.getByText('ERR')).toBeTruthy()
    expect(screen.getByText(/1\s+total/)).toBeTruthy()
  })
})

describe('JobsPage queue vs list race', () => {
  let server: http.Server
  let origin = ''
  let realFetch: typeof fetch
  let releaseJobs: (() => void) | null = null

  beforeEach(async () => {
    server = http.createServer((req, res) => {
      if (req.method === 'OPTIONS') {
        res.writeHead(204, { 'Access-Control-Allow-Origin': '*' })
        res.end()
        return
      }
      const url = new URL(req.url || '/', 'http://127.0.0.1')
      const send = (body: object) => {
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': '*',
        })
        res.end(JSON.stringify(body))
      }
      if (url.pathname === '/api/queue') {
        send({
          items: [{ job_id: 'job_q', jira_id: 'Q-1', status: 'queued', live: true }],
          queued_count: 1,
          server_time: 't',
        })
        return
      }
      const timer = setTimeout(
        () =>
          send(
            payload('all', [
              { job_id: 'job_ok', jira_id: 'OK', status: 'success', live: false },
            ]),
          ),
        80,
      )
      releaseJobs = () => {
        clearTimeout(timer)
        send(
          payload('all', [
            { job_id: 'job_ok', jira_id: 'OK', status: 'success', live: false },
          ]),
        )
      }
    })
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
    origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    realFetch = globalThis.fetch
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      const href = raw.startsWith('/') ? `${origin}${raw}` : raw
      return realFetch(href, init)
    }) as typeof fetch
  })

  afterEach(async () => {
    cleanup()
    globalThis.fetch = realFetch
    releaseJobs = null
    await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())))
  })

  it('does not paint All tickets after switching to Queue', async () => {
    render(
      <MemoryRouter>
        <LiveContext.Provider value={{ connected: true, generation: 0, running: 0, queueQueued: 1 }}>
          <JobsPage />
        </LiveContext.Provider>
      </MemoryRouter>,
    )
    fireEvent.click(screen.getByRole('button', { name: /Queue/ }))
    await waitFor(() => {
      expect(screen.getByText('Q-1')).toBeTruthy()
    })
    releaseJobs?.()
    await new Promise((r) => setTimeout(r, 120))
    expect(screen.queryByText('OK')).toBeNull()
    expect(screen.getByText('Q-1')).toBeTruthy()
  })
})
