import type { ChatMessage, ChatPart } from '../api/types'

const SKIP_PART_TYPES = new Set(['step-start', 'step-finish'])
const THINKING_TYPES = new Set(['reasoning', 'thinking'])
const THINK_TAG = /<think(?:ing)?>([\s\S]*?)<\/think(?:ing)?>/gi

const RESUME_PREFIXES = [
  'you are running unattended',
  'auto-compact looped and was aborted',
  'the last turn stopped early',
  'finish remaining todos and complete the original task',
]

export function extractThinkFromText(text: string): { thinking: string; rest: string } {
  const chunks: string[] = []
  const rest = String(text || '').replace(THINK_TAG, (_m, inner: string) => {
    const t = String(inner || '').trim()
    if (t) chunks.push(t)
    return '\n'
  })
  return { thinking: chunks.join('\n\n'), rest: rest.trim() }
}

export function partText(part: { text?: unknown }): string {
  const t = part.text
  if (typeof t === 'string') return t
  if (t && typeof t === 'object' && 'value' in t) return String((t as { value?: unknown }).value ?? '')
  return ''
}

export function normalizeChatParts(parts: ChatPart[]): ChatPart[] {
  const expanded: ChatPart[] = []
  for (const part of parts || []) {
    const t = (part.type || '').toLowerCase()
    if (SKIP_PART_TYPES.has(t)) continue
    if (t === 'text') {
      const { thinking, rest } = extractThinkFromText(partText(part))
      if (thinking) {
        expanded.push({ ...part, id: `${part.id || 't'}:think`, type: 'reasoning', text: thinking })
      }
      if (rest) expanded.push({ ...part, type: 'text', text: rest })
      continue
    }
    if (THINKING_TYPES.has(t)) {
      if (partText(part).trim()) expanded.push({ ...part, type: 'reasoning', text: partText(part) })
      continue
    }
    expanded.push({ ...part, text: part.text ?? partText(part) })
  }

  const merged: ChatPart[] = []
  for (const part of expanded) {
    const prev = merged[merged.length - 1]
    if (part.type === 'reasoning' && prev?.type === 'reasoning') {
      prev.text = `${(prev.text || '').trim()}\n\n${(part.text || '').trim()}`
      continue
    }
    merged.push({ ...part })
  }
  return merged
}

export type ChatGroup = {
  key: string
  role: string
  created_at?: unknown
  session_id?: string
  parts: ChatPart[]
}

function joinedText(parts: ChatPart[]): string {
  return parts
    .filter((p) => (p.type || 'text') === 'text')
    .map((p) => partText(p))
    .join('\n')
    .trim()
}

export function isResumePrompt(text: string): boolean {
  const t = (text || '').trim().toLowerCase()
  return RESUME_PREFIXES.some((p) => t.startsWith(p))
}

export function chatDisplayRole(msg: ChatMessage, parts: ChatPart[]): string {
  const raw = (msg.role || 'unknown').toLowerCase()
  const types = parts.map((p) => (p.type || '').toLowerCase())
  if (raw === 'compaction' || types.some((t) => t === 'compaction' || t === 'compact')) {
    return 'compaction'
  }
  const text = joinedText(parts)
  if (raw === 'user' && isResumePrompt(text)) return 'system'
  if (raw === 'user' && text && /session compacted|context compacted/i.test(text) && text.length < 400) {
    return 'compaction'
  }
  return raw
}

export function groupChatMessages(messages: ChatMessage[]): ChatGroup[] {
  const groups: ChatGroup[] = []
  for (const msg of messages || []) {
    const parts = normalizeChatParts(msg.parts || [])
    if (parts.length === 0) continue
    const role = chatDisplayRole(msg, parts)
    if (role === 'skip') continue
    const last = groups[groups.length - 1]
    const sameAssistant =
      last &&
      last.role === 'assistant' &&
      role === 'assistant' &&
      (last.session_id || '') === (msg.session_id || '')
    if (sameAssistant) {
      last.parts = normalizeChatParts([...last.parts, ...parts])
      if (msg.created_at) last.created_at = msg.created_at
      continue
    }
    groups.push({
      key: msg.id || `${msg.session_id || 'ses'}-${groups.length}`,
      role,
      created_at: msg.created_at,
      session_id: msg.session_id,
      parts,
    })
  }
  return groups
}

export function formatToolInput(input?: Record<string, unknown>): string {
  if (!input || Object.keys(input).length === 0) return ''
  const preferred = ['command', 'filePath', 'path', 'pattern', 'query', 'content', 'name']
  const bits: string[] = []
  for (const key of preferred) {
    const v = input[key]
    if (v == null || v === '') continue
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    bits.push(s.length > 160 ? `${s.slice(0, 160)}…` : s)
  }
  if (bits.length) return bits.join(' · ')
  try {
    const s = JSON.stringify(input)
    return s.length > 160 ? `${s.slice(0, 160)}…` : s
  } catch {
    return ''
  }
}

export function toolStatusTone(status?: string): string {
  const s = (status || '').toLowerCase()
  if (s === 'error' || s === 'failed') return 'text-danger-text'
  if (s === 'completed' || s === 'success') return 'text-success-text'
  if (s === 'running' || s === 'pending') return 'text-info-text'
  return 'text-text-muted'
}
