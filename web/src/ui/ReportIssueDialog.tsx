import { useEffect, useId, useState } from 'react'
import { REPORT_NOTE_MIN, reportNoteReady } from '../util/jobReport'

type Props = {
  title: string
  busy?: boolean
  error?: string | null
  onClose: () => void
  onDownload: (note: string) => void
}

export function ReportIssueDialog({ title, busy, error, onClose, onDownload }: Props) {
  const [note, setNote] = useState('')
  const headingId = useId()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [busy, onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/55 p-3 sm:items-center"
      role="presentation"
      onClick={() => {
        if (!busy) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        className="vd-panel w-full max-w-lg p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id={headingId} className="text-lg font-semibold tracking-tight">
          Report issue
        </h2>
        <p className="mt-1 text-sm text-text-muted">
          {title}. A note is required (at least {REPORT_NOTE_MIN} characters). Then download a zip
          of this job (details, prompts, chat, logs). The note is only in the zip — nothing is POSTed.
        </p>
        <label className="mt-4 block text-xs font-semibold uppercase tracking-wide text-text-muted">
          Note <span className="text-danger-text">*</span>
          <textarea
            className="vd-input mt-1 min-h-32 font-sans"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="What went wrong?"
            disabled={busy}
            required
            minLength={REPORT_NOTE_MIN}
            aria-required="true"
          />
        </label>
        <p className={`mt-1 text-xs ${reportNoteReady(note) ? 'text-text-muted' : 'text-danger-text'}`}>
          {note.trim().length}/{REPORT_NOTE_MIN} characters required
        </p>
        {error && <p className="mt-2 text-sm text-danger-text">{error}</p>}
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <button type="button" className="vd-btn vd-btn-secondary" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="vd-btn vd-btn-primary"
            disabled={busy || !reportNoteReady(note)}
            onClick={() => {
              if (!reportNoteReady(note)) return
              onDownload(note.trim())
            }}
          >
            {busy ? 'Preparing…' : 'Download zip'}
          </button>
        </div>
      </div>
    </div>
  )
}
