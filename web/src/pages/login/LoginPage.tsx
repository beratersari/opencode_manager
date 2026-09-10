import { FormEvent, useState } from 'react'
import { login } from '../../api/auth'

export function LoginPage({ hasUsername }: { hasUsername: boolean }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, password)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Invalid username or password')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        className="vd-panel w-full max-w-sm space-y-4 p-6"
        onSubmit={(event) => void onSubmit(event)}
      >
        <div>
          <div className="text-sm font-semibold">aMIR-mini</div>
          <div className="text-[11px] text-text-muted">Sign in to the dashboard</div>
        </div>
        {hasUsername ? (
          <label className="block text-xs text-text-muted">
            Username
            <input
              className="vd-input mt-1"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
        ) : null}
        <label className="block text-xs text-text-muted">
          Password
          <input
            className="vd-input mt-1"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error ? <div className="vd-alert vd-alert-danger text-xs">{error}</div> : null}
        <button type="submit" className="vd-btn vd-btn-primary w-full" disabled={busy}>
          {busy ? 'Signing in...' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
