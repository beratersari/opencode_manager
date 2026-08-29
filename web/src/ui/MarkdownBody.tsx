import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function MarkdownBody({ text, className = '' }: { text: string; className?: string }) {
  if (!text?.trim()) return null
  return (
    <div className={`vd-md ${className}`.trim()}>
      <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
    </div>
  )
}
