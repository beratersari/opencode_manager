import { describe, expect, it } from 'vitest'
import { parseToolOutput, prepareMarkdown, stripLineNumbers } from './chatMarkup'

const RESULTS = `### Bottom Line

This is a blank project.

<results>
<files>
- /tmp/TEST-1/README.md - Default GitLab template README
- /tmp/TEST-1/.codegraph/source.json - CodeGraph config
</files>
<answer>
This is an empty GitLab project called "test_project".
</answer>
<next_steps>
No action needed — the repo is essentially a blank template.
</next_steps>
</results>
`

describe('prepareMarkdown', () => {
  it('lifts <results> XML into Files / Answer / Next steps headings', () => {
    const out = prepareMarkdown(RESULTS)
    expect(out).toContain('### Files')
    expect(out).toContain('- /tmp/TEST-1/README.md - Default GitLab template README')
    expect(out).toContain('### Answer')
    expect(out).toContain('empty GitLab project')
    expect(out).toContain('### Next steps')
    expect(out).not.toContain('<results>')
    expect(out).not.toContain('<answer>')
  })
})

describe('parseToolOutput', () => {
  it('parses directory listings', () => {
    const got = parseToolOutput(`<path>/tmp/TEST-1</path>
<type>directory</type>
<entries>
.codegraph/
.git/
README.md

(3 entries)
</entries>`)
    expect(got).toEqual({
      kind: 'directory',
      path: '/tmp/TEST-1',
      entries: ['.codegraph/', '.git/', 'README.md'],
    })
  })

  it('parses file reads and strips line prefixes', () => {
    const got = parseToolOutput(`<path>/tmp/source.json</path>
<type>file</type>
<content>
1: {
2:   "version": 1
3: }

(End of file - total 3 lines)
</content>`)
    expect(got.kind).toBe('file')
    if (got.kind === 'file') {
      expect(got.path).toBe('/tmp/source.json')
      expect(got.content).toBe('{\n  "version": 1\n}')
    }
  })
})

describe('stripLineNumbers', () => {
  it('leaves unnumbered text alone', () => {
    expect(stripLineNumbers('hello\nworld')).toBe('hello\nworld')
  })
})
