import React from 'react'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MarkdownBody, unwrapStoredText } from './MarkdownBody'

const SAMPLE = `Here's what I found about this repository:

---

## Repository Overview

**Name:** \`test_project\`

**What it is:** This is a **brand-new, empty GitLab project**.

### Key Details

| Detail | Value |
|---|---|
| **Git host** | [GitLab](https://gitlab.com/beratersari0/test_project) |
| **Owner** | \`beratersari0\` |
| **Branch** | \`main\` |

### Directory Contents

- **\`README.md\`** — The default GitLab repository template README
- **\`.codegraph/\`** — A local **CodeGraph** indexing directory
`

describe('MarkdownBody', () => {
  it('unwraps quoted stored text with escaped newlines', () => {
    expect(unwrapStoredText('"## Title\\n\\nbody"')).toBe('## Title\n\nbody')
  })

  it('renders headings, tables, lists, links, and inline code', () => {
    const { container } = render(<MarkdownBody text={SAMPLE} />)
    expect(container.querySelector('h2')?.textContent).toBe('Repository Overview')
    expect(container.querySelector('h3')?.textContent).toBe('Key Details')
    expect(container.querySelector('hr')).toBeTruthy()
    expect(container.querySelectorAll('table').length).toBe(1)
    expect(container.querySelectorAll('th').length).toBe(2)
    expect(screen.getByText('Git host')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'GitLab' }).getAttribute('href')).toContain('gitlab.com')
    expect(container.querySelectorAll('ul li').length).toBe(2)
    expect(container.querySelector('code.vd-md-code')?.textContent).toBe('test_project')
  })

  it('keeps directory-content list items with nested code spans', () => {
    const { container } = render(
      <MarkdownBody
        text={`### Directory Contents

- **\`README.md\`** — The default GitLab repository template README (a generic "getting started" guide with links to GitLab features). It has not been customized.
- **\`.codegraph/\`** — A local **CodeGraph** indexing directory (symlinked to \`~/.omo/codegraph/projects/TEST-1-d6ab76d1dd6b2281\`).
- **\`.git/\`** — Standard Git metadata.
`}
      />,
    )
    expect(container.querySelectorAll('ul li').length).toBe(3)
    expect(container.textContent).toContain('.codegraph/')
    expect(container.textContent).toContain('.git/')
  })

  it('renders the trailing <results> XML as markdown sections, not raw tags', () => {
    const { container } = render(
      <MarkdownBody
        text={`### Bottom Line

A blank project.

<results>
<files>
- /tmp/README.md - Default GitLab template README
</files>
<answer>
Empty GitLab project.
</answer>
<next_steps>
No action needed.
</next_steps>
</results>`}
      />,
    )
    expect(container.textContent).not.toContain('<results>')
    expect(container.textContent).not.toContain('<answer>')
    const headings = [...container.querySelectorAll('h3')].map((h) => h.textContent)
    expect(headings).toContain('Bottom Line')
    expect(headings).toContain('Files')
    expect(headings).toContain('Answer')
    expect(headings).toContain('Next steps')
    expect(container.querySelector('ul')?.textContent).toContain('README.md')
    expect(container.textContent).toContain('Empty GitLab project.')
  })
})
