import { NavLink, Outlet } from 'react-router-dom'
import { ReportIssue } from '../ui/ReportIssue'
import { useLive } from './live'

export function Shell() {
  const live = useLive()
  return (
    <div className="vd-app">
      <aside className="vd-sidebar">
        <div className="vd-brand">
          <div className="vd-mark">aM</div>
          <div>
            <div className="text-sm font-semibold">aMIR-mini</div>
            <div className="text-[11px] text-text-muted">
              {live.connected ? 'live' : 'offline'}
              {live.running ? ` · ${live.running} running` : ''}
            </div>
          </div>
        </div>
        <nav className="vd-nav">
          <NavLink to="/jobs" className={({ isActive }) => (isActive ? 'active' : '')}>
            Jobs
          </NavLink>
        </nav>
        <div className="mt-3 space-y-2 px-1 text-xs">
          <ReportIssue />
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
