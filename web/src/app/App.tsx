import { useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AUTH_EVENT, fetchAuth, type AuthStatus } from '../api/auth'
import { JobDetailPage } from '../pages/jobs/JobDetailPage'
import { JobsPage } from '../pages/jobs/JobsPage'
import { LoginPage } from '../pages/login/LoginPage'
import { SettingsPage } from '../pages/settings/SettingsPage'
import { LiveProvider } from './LiveProvider'
import { Shell } from './Shell'

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null)

  useEffect(() => {
    let gone = false
    const load = () => {
      void fetchAuth().then((status) => {
        if (!gone) setAuth(status)
      })
    }
    load()
    window.addEventListener(AUTH_EVENT, load)
    return () => {
      gone = true
      window.removeEventListener(AUTH_EVENT, load)
    }
  }, [])

  if (!auth) {
    return <div className="flex min-h-screen items-center justify-center text-sm text-text-muted">Loading</div>
  }
  if (auth.required && !auth.authenticated) {
    return <LoginPage hasUsername={auth.hasUsername} />
  }

  return (
    <BrowserRouter>
      <LiveProvider>
        <Routes>
          <Route element={<Shell showLogout={auth.required} />}>
            <Route path="/" element={<Navigate to="/jobs" replace />} />
            <Route path="/login" element={<Navigate to="/jobs" replace />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/jobs/:jobId" element={<JobDetailPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/jobs" replace />} />
          </Route>
        </Routes>
      </LiveProvider>
    </BrowserRouter>
  )
}
