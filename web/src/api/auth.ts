export type AuthStatus = {
  required: boolean
  authenticated: boolean
  hasUsername: boolean
}

export const AUTH_EVENT = 'amir-mini-auth'

export function notifyAuthChanged(): void {
  window.dispatchEvent(new Event(AUTH_EVENT))
}

export async function fetchAuth(): Promise<AuthStatus> {
  const res = await fetch('/api/auth')
  const body = (await res.json().catch(() => ({}))) as {
    required?: boolean
    authenticated?: boolean
    has_username?: boolean
  }
  return {
    required: Boolean(body.required),
    authenticated: Boolean(body.authenticated),
    hasUsername: Boolean(body.has_username),
  }
}

export async function login(username: string, password: string): Promise<void> {
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const body = (await res.json().catch(() => ({}))) as { detail?: string }
  if (!res.ok) {
    throw new Error(body.detail || 'Invalid username or password')
  }
  notifyAuthChanged()
}

export async function logout(): Promise<void> {
  await fetch('/api/logout', { method: 'POST' })
  notifyAuthChanged()
}
