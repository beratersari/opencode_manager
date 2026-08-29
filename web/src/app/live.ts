import { createContext, useContext } from 'react'

export type LiveValue = {
  connected: boolean
  generation: number
  running: number
  queueQueued: number
}

export const LiveContext = createContext<LiveValue>({
  connected: false,
  generation: 0,
  running: 0,
  queueQueued: 0,
})

export function useLive(): LiveValue {
  return useContext(LiveContext)
}
