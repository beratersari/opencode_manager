import { useEffect, useState, type ReactNode } from 'react'
import { dashboardWsUrl } from '../api/client'
import { LiveContext } from './live'

export function LiveProvider({ children }: { children: ReactNode }) {
  const [value, setValue] = useState({
    connected: false,
    generation: 0,
    running: 0,
    queueQueued: 0,
  })

  useEffect(() => {
    let ws: WebSocket | null = null
    let closed = false
    const connect = () => {
      if (closed) return
      ws = new WebSocket(dashboardWsUrl())
      ws.onopen = () => setValue((v) => ({ ...v, connected: true }))
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data)
          setValue((v) => ({
            connected: true,
            generation: v.generation + 1,
            running: Number(data.running || 0),
            queueQueued: Number(data.queue_queued || 0),
          }))
        } catch {
          /* ignore */
        }
      }
      ws.onclose = () => {
        setValue((v) => ({ ...v, connected: false }))
        if (!closed) window.setTimeout(connect, 2000)
      }
    }
    connect()
    return () => {
      closed = true
      ws?.close()
    }
  }, [])

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}
