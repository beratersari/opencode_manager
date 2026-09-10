import { describe, expect, it } from 'vitest'
import { jobMatchesFilter } from './filters'

describe('jobMatchesFilter', () => {
  it('treats running as in flight and queued as not', () => {
    expect(jobMatchesFilter({ status: 'running', live: true }, 'active')).toBe(true)
    expect(jobMatchesFilter({ status: 'queued', live: true }, 'active')).toBe(false)
    expect(jobMatchesFilter({ status: 'queued', live: true }, 'queue')).toBe(true)
  })

  it('matches review jobs only on the review tab', () => {
    expect(jobMatchesFilter({ status: 'success', job_kind: 'review' }, 'review')).toBe(true)
    expect(jobMatchesFilter({ status: 'success', job_kind: 'ticket' }, 'review')).toBe(false)
    expect(jobMatchesFilter({ status: 'success' }, 'review')).toBe(false)
  })

  it('groups error statuses', () => {
    expect(jobMatchesFilter({ status: 'error' }, 'error')).toBe(true)
    expect(jobMatchesFilter({ status: 'timeout' }, 'error')).toBe(true)
    expect(jobMatchesFilter({ status: 'not_found' }, 'error')).toBe(true)
    expect(jobMatchesFilter({ status: 'success' }, 'error')).toBe(false)
    expect(jobMatchesFilter({ status: 'success' }, 'completed')).toBe(true)
  })
})
