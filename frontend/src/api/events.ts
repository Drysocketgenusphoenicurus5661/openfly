// The /api/events websocket. One connection for the whole app, owned by the
// zustand store below; components read the ring buffer through useEvents().

import { useEffect } from 'react'
import { create } from 'zustand'
import { useBackendStore } from './backend'
import { isServerEvent } from './guards'
import type { ServerEvent } from './types'

export const EVENT_BUFFER_SIZE = 500

export type SocketStatus = 'idle' | 'connecting' | 'open' | 'closed'

interface EventState {
  events: ServerEvent[]
  status: SocketStatus
  lastByType: Record<string, ServerEvent>
  push: (event: ServerEvent) => void
  setStatus: (status: SocketStatus) => void
  clear: () => void
}

export const useEventStore = create<EventState>((set) => ({
  events: [],
  status: 'idle',
  lastByType: {},
  push: (event) =>
    set((state) => {
      const events =
        state.events.length >= EVENT_BUFFER_SIZE
          ? [...state.events.slice(state.events.length - EVENT_BUFFER_SIZE + 1), event]
          : [...state.events, event]
      return { events, lastByType: { ...state.lastByType, [event.type]: event } }
    }),
  setStatus: (status) => set({ status }),
  clear: () => set({ events: [], lastByType: {} }),
}))

function socketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/api/events`
}

let socket: WebSocket | null = null
let reconnectTimer: number | null = null
let pingTimer: number | null = null
let attempts = 0
let started = false

function scheduleReconnect() {
  if (reconnectTimer !== null) return
  const delay = Math.min(30_000, 1000 * 2 ** Math.min(attempts, 5))
  attempts += 1
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null
    connect()
  }, delay)
}

function connect() {
  const store = useEventStore.getState()
  store.setStatus('connecting')
  try {
    socket = new WebSocket(socketUrl())
  } catch {
    store.setStatus('closed')
    scheduleReconnect()
    return
  }
  socket.onopen = () => {
    attempts = 0
    useEventStore.getState().setStatus('open')
    if (pingTimer !== null) window.clearInterval(pingTimer)
    pingTimer = window.setInterval(() => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ action: 'ping' }))
    }, 20_000)
  }
  socket.onmessage = (message) => {
    let parsed: unknown
    try {
      parsed = JSON.parse(String(message.data))
    } catch {
      return
    }
    if (!isServerEvent(parsed) || parsed.type === 'pong') return
    useEventStore.getState().push(parsed)
  }
  socket.onclose = () => {
    useEventStore.getState().setStatus('closed')
    if (pingTimer !== null) {
      window.clearInterval(pingTimer)
      pingTimer = null
    }
    socket = null
    // The socket dropping may mean the backend is gone; a probe decides.
    void useBackendStore.getState().probe()
    scheduleReconnect()
  }
  socket.onerror = () => {
    socket?.close()
  }
}

// Starts the connection once. Safe to call from several components.
export function startEvents() {
  if (started) return
  started = true
  connect()
}

export function stopEvents() {
  started = false
  if (reconnectTimer !== null) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (pingTimer !== null) {
    window.clearInterval(pingTimer)
    pingTimer = null
  }
  socket?.close()
  socket = null
}

export function useEvents(): {
  events: ServerEvent[]
  status: SocketStatus
  last: <T = unknown>(type: string) => ServerEvent<T> | undefined
} {
  const up = useBackendStore((s) => s.state === 'up')
  const events = useEventStore((s) => s.events)
  const status = useEventStore((s) => s.status)
  const lastByType = useEventStore((s) => s.lastByType)
  useEffect(() => {
    if (up) startEvents()
  }, [up])
  return {
    events,
    status,
    last: <T = unknown>(type: string) => lastByType[type] as ServerEvent<T> | undefined,
  }
}

export function useLastEvent<T = unknown>(type: string): ServerEvent<T> | undefined {
  const up = useBackendStore((s) => s.state === 'up')
  const event = useEventStore((s) => s.lastByType[type])
  useEffect(() => {
    if (up) startEvents()
  }, [up])
  return event as ServerEvent<T> | undefined
}
