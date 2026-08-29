import { NavLink, Outlet } from 'react-router-dom'
import { useLive } from './live'

export function Shell() {
  const live = useLive()
  return (
    <div className="vd-app">
      <aside className="vd-sidebar">
        <div className="vd-brand">
          <div className="vd-mark">OM</div>
          <div>
            <div className="text-sm font-semibold">OpenCode Manager</div>
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
      </aside>
      <main className="vd-main">
        <div className="vd-main-inner">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
