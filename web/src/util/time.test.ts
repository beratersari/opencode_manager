import { describe, expect, it } from 'vitest'
import {
  elapsedSecondsBetween,
  formatElapsedBetween,
  formatElapsedSeconds,
  formatJobElapsed,
  jobElapsedWindow,
} from './time'

describe('elapsed time', () => {
  const start = '2026-09-02T12:00:00.000Z'
  const end = '2026-09-02T13:02:05.000Z'
  const now = Date.parse('2026-09-02T12:00:30.000Z')

  it('formats compact durations', () => {
    expect(formatElapsedSeconds(3725)).toBe('1h 02m 05s')
    expect(formatElapsedSeconds(65)).toBe('1m 05s')
    expect(formatElapsedSeconds(9)).toBe('9s')
    expect(formatElapsedSeconds(null)).toBe('—')
    expect(formatElapsedSeconds(-1)).toBe('—')
  })

  it('uses completed_at when the job is finished', () => {
    expect(elapsedSecondsBetween(start, end)).toBe(3725)
    expect(formatElapsedBetween(start, end)).toBe('1h 02m 05s')
    expect(formatElapsedBetween(null, end)).toBe('—')
  })

  it('uses now while the job is still running', () => {
    expect(elapsedSecondsBetween(start, null, now)).toBe(30)
    expect(formatElapsedBetween(start, null, now)).toBe('30s')
  })

  it('falls back to accepted_at and ticks until completed', () => {
    const queued = jobElapsedWindow({
      accepted_at: start,
      live: false,
      completed_at: null,
    })
    expect(queued.start).toBe(start)
    expect(queued.end).toBeNull()
    expect(queued.ticking).toBe(true)

    const done = jobElapsedWindow({
      started_at: start,
      completed_at: end,
      live: false,
    })
    expect(done.end).toBe(end)
    expect(done.ticking).toBe(false)
    expect(formatJobElapsed({ started_at: start, completed_at: end, live: false })).toBe('1h 02m 05s')
  })
})
