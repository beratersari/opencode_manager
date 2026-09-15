import { FormEvent, useEffect, useState } from 'react'
import { fetchSettings, saveSettings } from '../../api/client'
import type { ReviewSettings } from '../../api/types'
import { PageHeader } from '../../ui/PageHeader'

const CUSTOM = '__custom__'

export function SettingsPage() {
  const [loaded, setLoaded] = useState<ReviewSettings | null>(null)
  const [model, setModel] = useState('')
  const [agent, setAgent] = useState('code-reviewer')
  const [timeout, setTimeoutSeconds] = useState('1800')
  const [choice, setChoice] = useState(CUSTOM)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let gone = false
    fetchSettings()
      .then((data) => {
        if (gone) return
        applyPayload(data)
        setError(null)
      })
      .catch((err: unknown) => {
        if (!gone) setError(err instanceof Error ? err.message : 'Failed to load settings')
      })
    return () => {
      gone = true
    }
  }, [])

  function applyPayload(data: ReviewSettings) {
    setLoaded(data)
    setModel(data.review_model)
    setAgent(data.review_agent)
    setTimeoutSeconds(String(data.review_timeout_seconds))
    setChoice(data.models.includes(data.review_model) ? data.review_model : CUSTOM)
  }

  function onPick(value: string) {
    setChoice(value)
    setSaved(null)
    if (value !== CUSTOM) setModel(value)
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    setSaved(null)
    const seconds = Number(timeout)
    try {
      const data = await saveSettings({
        review_model: model.trim(),
        review_timeout_seconds: Number.isFinite(seconds) ? seconds : 0,
        review_agent: agent.trim(),
      })
      applyPayload(data)
      setSaved('Saved. New review jobs use this agent, model, and timeout.')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  async function onResetYaml() {
    if (!loaded) return
    setModel(loaded.env_model)
    setAgent(loaded.env_agent)
    setTimeoutSeconds(String(loaded.env_timeout))
    setChoice(loaded.models.includes(loaded.env_model) ? loaded.env_model : CUSTOM)
    setSaved(null)
  }

  const models = loaded?.models || []
  const agents = loaded?.agents || ['code-reviewer']

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Review"
        title="Settings"
        description="Change the review agent, OpenCode model, and turn timeout without restarting. Webhook URLs below are for GitLab and Azure hooks. Queued and running jobs keep the values they already have."
      />

      <form className="vd-panel max-w-xl space-y-4 p-5" onSubmit={(event) => void onSubmit(event)}>
        <label className="block text-xs text-text-muted">
          Review agent
          <select
            className="vd-input mt-1 font-mono"
            value={agents.includes(agent) ? agent : CUSTOM}
            onChange={(e) => {
              const value = e.target.value
              setSaved(null)
              if (value !== CUSTOM) setAgent(value)
            }}
            disabled={!loaded}
          >
            {agents.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
            <option value={CUSTOM}>Custom…</option>
          </select>
        </label>
        {!agents.includes(agent) || agent === CUSTOM ? (
          <label className="block text-xs text-text-muted">
            Custom agent
            <input
              className="vd-input mt-1 font-mono"
              name="review_agent"
              placeholder="code-reviewer"
              value={agent}
              onChange={(e) => {
                setAgent(e.target.value)
                setSaved(null)
              }}
              disabled={!loaded}
            />
          </label>
        ) : null}

        <label className="block text-xs text-text-muted">
          Model
          <select
            className="vd-input mt-1 font-mono"
            value={choice}
            onChange={(e) => onPick(e.target.value)}
            disabled={!loaded}
          >
            {models.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
            <option value={CUSTOM}>Custom…</option>
          </select>
        </label>

        {choice === CUSTOM ? (
          <label className="block text-xs text-text-muted">
            Custom model
            <input
              className="vd-input mt-1 font-mono"
              name="review_model"
              placeholder="provider/id"
              value={model}
              onChange={(e) => {
                setModel(e.target.value)
                setSaved(null)
              }}
              disabled={!loaded}
            />
          </label>
        ) : null}

        <label className="block text-xs text-text-muted">
          Timeout (seconds)
          <input
            className="vd-input mt-1 font-mono"
            name="review_timeout_seconds"
            type="number"
            min={1}
            max={86400}
            step={1}
            value={timeout}
            onChange={(e) => {
              setTimeoutSeconds(e.target.value)
              setSaved(null)
            }}
            disabled={!loaded}
          />
        </label>
        <p className="text-[11px] text-text-muted">
          One OpenCode turn. 1800 is 30 minutes. Range 1–86400. Stored in data_dir/settings.json.
        </p>

        {error ? <div className="vd-alert vd-alert-danger text-xs">{error}</div> : null}
        {saved ? <div className="vd-alert vd-alert-success text-xs">{saved}</div> : null}

        <div className="flex flex-wrap gap-2">
          <button type="submit" className="vd-btn vd-btn-primary px-4" disabled={busy || !loaded}>
            {busy ? 'Saving…' : 'Save'}
          </button>
          <button
            type="button"
            className="vd-btn vd-btn-secondary px-4"
            disabled={busy || !loaded}
            onClick={() => void onResetYaml()}
          >
            Restore settings.yaml values
          </button>
        </div>
      </form>

      {loaded?.webhook_gitlab_url || loaded?.webhook_azure_url ? (
        <div className="vd-panel max-w-xl space-y-3 p-5">
          <div>
            <h2 className="text-sm font-semibold text-text">Webhook URLs</h2>
            <p className="mt-1 text-[11px] text-text-muted">
              Copy these into GitLab and Azure service hooks. Replace {'<ip>'} with this
              machine's address those servers can reach. They are not saved from this page.
            </p>
          </div>
          <WebhookUrlField label="GitLab" value={loaded.webhook_gitlab_url || ''} />
          <WebhookUrlField label="Azure" value={loaded.webhook_azure_url || ''} />
        </div>
      ) : null}
    </section>
  )
}

function WebhookUrlField({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)

  async function onCopy() {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  return (
    <label className="block text-xs text-text-muted">
      {label}
      <div className="mt-1 flex gap-2">
        <input className="vd-input min-w-0 flex-1 font-mono" value={value} readOnly />
        <button type="button" className="vd-btn vd-btn-secondary shrink-0 px-3" onClick={() => void onCopy()} disabled={!value}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
    </label>
  )
}
