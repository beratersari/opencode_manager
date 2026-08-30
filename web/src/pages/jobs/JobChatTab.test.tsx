import React from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { JobChatTab } from './JobChatTab'

describe('JobChatTab', () => {
  it('renders prompt and assistant bubbles, not raw role labels', () => {
    render(
      <JobChatTab
        messages={[
          {
            id: 'u1',
            session_id: 'ses_1',
            role: 'user',
            parts: [{ type: 'text', text: 'Add a health check' }],
          },
          {
            id: 'a1',
            session_id: 'ses_1',
            role: 'assistant',
            parts: [
              { type: 'tool', tool: 'bash', status: 'completed', input: { command: 'echo ok' }, output: 'ok\n' },
              { type: 'text', text: 'Health check added.' },
            ],
          },
        ]}
      />,
    )
    expect(screen.getByText('Prompt')).toBeTruthy()
    expect(screen.getByText('Assistant')).toBeTruthy()
    expect(screen.queryByText(/^user$/i)).toBeNull()
    expect(screen.getByText('Add a health check')).toBeTruthy()
    expect(screen.getByText('Health check added.')).toBeTruthy()
    expect(screen.getByText('bash')).toBeTruthy()
    expect(screen.getByText('completed')).toBeTruthy()
  })

  it('shows an empty snapshot state', () => {
    render(<JobChatTab messages={[]} />)
    expect(screen.getByText(/No transcript stored for this job/)).toBeTruthy()
  })
})
