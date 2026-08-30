import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../api/types'
import {
  extractThinkFromText,
  groupChatMessages,
  isResumePrompt,
  normalizeChatParts,
} from './chatParts'

describe('chatParts', () => {
  it('extracts think tags from text', () => {
    const got = extractThinkFromText('hello <think>secret plan</think> world')
    expect(got.thinking).toBe('secret plan')
    expect(got.rest).toContain('hello')
    expect(got.rest).toContain('world')
  })

  it('normalizes parts and merges consecutive thinking', () => {
    const parts = normalizeChatParts([
      { id: 's', type: 'step-start' },
      { id: 'r1', type: 'reasoning', text: 'look around' },
      { id: 'r2', type: 'thinking', text: 'then edit' },
      { id: 't', type: 'tool', tool: 'bash', status: 'completed' },
      { id: 'x', type: 'text', text: 'Done.\n<think>hidden</think>' },
      { id: 'f', type: 'step-finish' },
    ])
    expect(parts.map((p) => p.type)).toEqual(['reasoning', 'tool', 'reasoning', 'text'])
    expect(parts[0].text).toContain('look around')
    expect(parts[0].text).toContain('then edit')
    expect(parts[2].text).toBe('hidden')
    expect(parts[3].text).toBe('Done.')
  })

  it('groups consecutive assistant turns and labels resume prompts as system', () => {
    const msgs: ChatMessage[] = [
      { id: 'u', session_id: 'ses_a', role: 'user', parts: [{ id: 'u1', type: 'text', text: 'fix the bug' }] },
      {
        id: 'a1',
        session_id: 'ses_a',
        role: 'assistant',
        parts: [
          { id: 'r', type: 'reasoning', text: 'step 1' },
          { id: 'tb', type: 'tool', tool: 'read' },
        ],
      },
      {
        id: 'a2',
        session_id: 'ses_a',
        role: 'assistant',
        parts: [{ id: 'tx', type: 'text', text: 'done' }],
      },
      {
        id: 'u2',
        session_id: 'ses_a',
        role: 'user',
        parts: [
          {
            id: 'u2p',
            type: 'text',
            text: 'The last turn stopped early (timeout, hang, or the OpenCode server was\nrestarted). Stay in this session.',
          },
        ],
      },
    ]
    const groups = groupChatMessages(msgs)
    expect(groups.map((g) => g.role)).toEqual(['user', 'assistant', 'system'])
    expect(groups[1].parts.map((p) => p.type)).toEqual(['reasoning', 'tool', 'text'])
  })

  it('detects orchestrator resume prefixes', () => {
    expect(isResumePrompt('You are running unattended — there is no human')).toBe(true)
    expect(isResumePrompt('Finish remaining todos and complete the original task')).toBe(true)
    expect(isResumePrompt('fix the login button')).toBe(false)
  })
})
