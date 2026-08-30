import { useEffect, useMemo, useRef } from 'react'
import type { ChatMessage, ChatPart } from '../../api/types'
import { MarkdownBody } from '../../ui/MarkdownBody'
import {
  formatToolInput,
  groupChatMessages,
  partText,
  toolStatusTone,
  type ChatGroup,
} from '../../util/chatParts'
import { parseToolOutput } from '../../util/chatMarkup'
import { formatChatTime } from '../../util/time'

function ToolOutput({ output }: { output: string }) {
  const parsed = parseToolOutput(output)
  if (parsed.kind === 'directory') {
    return (
      <div className="vd-chat-tool-struct">
        <div className="vd-chat-tool-path">{parsed.path}</div>
        {parsed.entries.length ? (
          <ul>
            {parsed.entries.map((e) => (
              <li key={e}>
                <code>{e}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p className="vd-chat-tool-empty">Empty directory</p>
        )}
      </div>
    )
  }
  if (parsed.kind === 'file') {
    return (
      <div className="vd-chat-tool-struct">
        <div className="vd-chat-tool-path">{parsed.path}</div>
        <pre className="vd-chat-tool-out">{parsed.content || '(empty)'}</pre>
      </div>
    )
  }
  return <pre className="vd-chat-tool-out">{parsed.text}</pre>
}

function ToolBlock({ part }: { part: ChatPart }) {
  const summary = formatToolInput(part.input)
  const status = (part.status || '').trim()
  return (
    <details className="vd-chat-tool">
      <summary>
        <span className="vd-chat-tool-name">{part.tool || 'tool'}</span>
        {status ? <span className={`vd-chat-tool-status ${toolStatusTone(status)}`}>{status}</span> : null}
        {summary ? <span className="vd-chat-tool-summary">{summary}</span> : null}
      </summary>
      {part.output ? <ToolOutput output={part.output} /> : <p className="vd-chat-tool-empty">No output stored</p>}
    </details>
  )
}

function ThinkingBlock({ part }: { part: ChatPart }) {
  const text = partText(part)
  if (!text.trim()) return null
  return (
    <details className="vd-chat-think">
      <summary>Thinking</summary>
      <div className="vd-chat-think-body">
        <MarkdownBody text={text} />
      </div>
    </details>
  )
}

function MessageBubble({ group }: { group: ChatGroup }) {
  const role = group.role
  const when = formatChatTime(group.created_at)

  if (role === 'compaction') {
    const text = group.parts.map((p) => partText(p)).find((t) => t.trim()) || 'Session compacted'
    return (
      <div className="vd-chat-divider">
        <span>{text.trim() || 'Session compacted'}</span>
        {when ? <time>{when}</time> : null}
      </div>
    )
  }

  if (role === 'system') {
    const text = group.parts.map((p) => partText(p)).find((t) => t.trim()) || 'Resume'
    const label = text.toLowerCase().startsWith('you are running unattended')
      ? 'Unattended nudge'
      : text.toLowerCase().startsWith('auto-compact')
        ? 'Compact-loop nudge'
        : text.toLowerCase().startsWith('the last turn stopped')
          ? 'Hang resume'
          : text.toLowerCase().startsWith('finish remaining todos')
            ? 'Incomplete resume'
            : 'System'
    return (
      <div className="vd-chat-divider vd-chat-divider-system" title={text}>
        <span>{label}</span>
        {when ? <time>{when}</time> : null}
      </div>
    )
  }

  const isUser = role === 'user'
  const label = isUser ? 'Prompt' : role === 'assistant' ? 'Assistant' : role
  const mark = isUser ? 'P' : 'A'

  return (
    <article className={`vd-chat-row ${isUser ? 'is-user' : 'is-assistant'}`}>
      {!isUser ? <div className="vd-chat-avatar" aria-hidden>{mark}</div> : null}
      <div className={`vd-chat-bubble ${isUser ? 'is-user' : 'is-assistant'}`}>
        <header className="vd-chat-meta">
          <span className="vd-chat-role">{label}</span>
          {when ? <time className="vd-chat-time">{when}</time> : null}
        </header>
        <div className="vd-chat-body">
          {group.parts.map((p, i) => {
            const key = p.id || `${group.key}-${p.type}-${i}`
            const kind = (p.type || '').toLowerCase()
            if (kind === 'reasoning') return <ThinkingBlock key={key} part={p} />
            if (kind === 'tool' || p.tool) return <ToolBlock key={key} part={p} />
            if (kind === 'compaction' || kind === 'compact') {
              return (
                <p key={key} className="vd-chat-compact-inline">
                  {partText(p).trim() || 'Session compacted'}
                </p>
              )
            }
            const text = partText(p)
            if (!text.trim()) return null
            return <MarkdownBody key={key} text={text} className="vd-chat-md" />
          })}
        </div>
      </div>
      {isUser ? <div className="vd-chat-avatar is-user" aria-hidden>{mark}</div> : null}
    </article>
  )
}

export function JobChatTab({ messages, live = false }: { messages: ChatMessage[]; live?: boolean }) {
  const groups = useMemo(() => groupChatMessages(messages), [messages])
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const stick = useRef(true)

  useEffect(() => {
    if (!stick.current) return
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [groups])

  if (messages.length === 0) {
    return (
      <div className="vd-alert vd-alert-warning">
        {live
          ? 'Waiting for the first turn on this job’s serve…'
          : 'No transcript stored for this job. Chat is this job’s snapshot after the serve ends.'}
      </div>
    )
  }

  if (groups.length === 0) {
    return <p className="text-sm text-text-muted">Messages were recorded, but none are displayable.</p>
  }

  return (
    <div className="vd-chat">
      <div className="vd-chat-toolbar">
        <p>
          {groups.length} turn{groups.length === 1 ? '' : 's'}
          {live ? (
            <span className="vd-chat-live">
              <span className="vd-chat-live-dot" />
              live
            </span>
          ) : (
            <span className="text-text-muted"> · snapshot</span>
          )}
        </p>
      </div>
      <div
        ref={scrollerRef}
        className="vd-chat-thread"
        onScroll={() => {
          const el = scrollerRef.current
          if (!el) return
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 96
        }}
      >
        {groups.map((group) => (
          <MessageBubble key={group.key} group={group} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
