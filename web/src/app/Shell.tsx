import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { logout } from '../api/auth'
import { fetchMeta } from '../api/client'
import { ReportIssue } from '../ui/ReportIssue'
import { useLive } from './live'

const FALLBACK_NAME = 'aMIR-mini'

export function Shell({ showLogout = false }: { showLogout?: boolean }) {
  const live = useLive()
  const [brand, setBrand] = useState({ app_name: FALLBACK_NAME, version: '' })

  useEffect(() => {
    let cancelled = false
    fetchMeta()
      .then((meta) => {
        if (cancelled) return
        setBrand({
          app_name: (meta.app_name || '').trim() || FALLBACK_NAME,
          version: (meta.version || '').trim(),
        })
      })
      .catch(() => {
        /* keep fallback name; version stays empty */
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="vd-app">
      <aside className="vd-sidebar">
        <div className="vd-brand">
          <div className="text-sm font-semibold">{brand.app_name}</div>
          {brand.version ? (
            <div className="text-[11px] text-text-muted">{brand.version}</div>
          ) : null}
        </div>
        <nav className="vd-nav">
          <NavLink to="/jobs" className={({ isActive }) => (isActive ? 'active' : '')}>
            Jobs
          </NavLink>
          <NavLink to="/settings" className={({ isActive }) => (isActive ? 'active' : '')}>
            Settings
          </NavLink>
        </nav>
        <div className="mt-3 space-y-2 px-1 text-xs">
          <ReportIssue />
          {showLogout ? (
            <button
              type="button"
              className="vd-btn vd-btn-secondary w-full px-3 py-1.5 text-xs"
              onClick={() => void logout()}
            >
              Sign out
            </button>
          ) : null}
          <div className="hidden items-center gap-2 px-1 md:flex">
            <span className={`h-2 w-2 rounded-full ${live.connected ? 'bg-live' : 'bg-warning'}`} />
            <span className={live.connected ? 'text-success-text' : 'text-warning-text'}>
              {live.connected ? 'Connected' : 'Reconnecting'}
            </span>
          </div>
        </div>
      </aside>
      <main className="vd-main">
        <div className="vd-main-inner">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
