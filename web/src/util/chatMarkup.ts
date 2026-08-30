function innerTag(xml: string, name: string): string {
  const m = xml.match(new RegExp(`<${name}>([\\s\\S]*?)</${name}>`, 'i'))
  return m ? m[1].trim() : ''
}

function bulletsFromFiles(block: string): string {
  return block
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => (line.startsWith('- ') ? line : `- ${line}`))
    .join('\n')
}

/** Turn OpenCode / explore XML blobs into markdown the renderer can style. */
export function prepareMarkdown(raw: string): string {
  let text = raw || ''
  text = text.replace(/<results>([\s\S]*?)<\/results>/gi, (_m, inner: string) => {
    const files = innerTag(inner, 'files')
    const answer = innerTag(inner, 'answer')
    const next = innerTag(inner, 'next_steps')
    const bits: string[] = ['']
    if (files) bits.push('### Files', bulletsFromFiles(files))
    if (answer) bits.push('### Answer', answer)
    if (next) bits.push('### Next steps', next)
    return `\n\n${bits.join('\n\n')}\n\n`
  })
  text = text.replace(
    /<path>([\s\S]*?)<\/path>\s*<type>([\s\S]*?)<\/type>\s*(?:<entries>([\s\S]*?)<\/entries>|<content>([\s\S]*?)<\/content>)?/gi,
    (_m, path: string, type: string, entries?: string, content?: string) => {
      const bits = ['', `**\`${path.trim()}\`**`, `_${type.trim()}_`]
      if (entries?.trim()) {
        bits.push(
          entries
            .split('\n')
            .map((l) => l.trim())
            .filter((l) => l && !/^\(\d+ entries\)$/i.test(l))
            .map((l) => `- \`${l}\``)
            .join('\n'),
        )
      }
      if (content?.trim()) bits.push('```', stripLineNumbers(content.trim()), '```')
      return `\n\n${bits.join('\n\n')}\n\n`
    },
  )
  return text
}

export function stripLineNumbers(content: string): string {
  const lines = content.split('\n')
  const numbered = lines.filter((l) => l.trim()).every((l) => /^\d+:/.test(l) || /^\(End of file/i.test(l))
  if (!numbered) return content
  return lines
    .filter((l) => !/^\(End of file/i.test(l))
    .map((l) => l.replace(/^\d+:\s?/, ''))
    .join('\n')
    .replace(/\n+$/, '')
}

export type ParsedToolOutput =
  | { kind: 'directory'; path: string; entries: string[] }
  | { kind: 'file'; path: string; content: string }
  | { kind: 'raw'; text: string }

export function parseToolOutput(output: string): ParsedToolOutput {
  const text = (output || '').trim()
  if (!text) return { kind: 'raw', text: '' }
  const path = innerTag(text, 'path')
  const type = innerTag(text, 'type').toLowerCase()
  if (path && type === 'directory') {
    const entries = innerTag(text, 'entries')
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l && !/^\(\d+ entries\)$/i.test(l))
    return { kind: 'directory', path, entries }
  }
  if (path && type === 'file') {
    return { kind: 'file', path, content: stripLineNumbers(innerTag(text, 'content')) }
  }
  return { kind: 'raw', text }
}
