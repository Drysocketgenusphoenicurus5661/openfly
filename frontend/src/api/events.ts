// The /api/events websocket. One connection for the whole app, owned by the
// zustand store below; components read the ring buffer through useEvents().
// In mock mode the src/mock/events generator feeds the same store.

import { useEffect } from 'react'
import { create } from 'zustand'
import { isServerEvent } from './guards'
import { useModeStore } from './mode'
import type { ServerEvent } from './types'

export const EVENT_BUFFER_SIZE = 500

export type SocketStatus = 'idle' | 'connecting' | 'open' | 'closed' | 'mock'

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
let stopMock: (() => void) | null = null

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
  if (useModeStore.getState().mock) {
    useEventStore.getState().setStatus('mock')
    import('@/mock/events').then((m) => {
      stopMock = m.startMockEvents((event) => useEventStore.getState().push(event))
    })
    return
  }
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
  stopMock?.()
  stopMock = null
}

export function useEvents(): {
  events: ServerEvent[]
  status: SocketStatus
  last: <T = unknown>(type: string) => ServerEvent<T> | undefined
} {
  const resolved = useModeStore((s) => s.resolved)
  const events = useEventStore((s) => s.events)
  const status = useEventStore((s) => s.status)
  const lastByType = useEventStore((s) => s.lastByType)
  useEffect(() => {
    if (resolved) startEvents()
  }, [resolved])
  return {
    events,
    status,
    last: <T = unknown>(type: string) => lastByType[type] as ServerEvent<T> | undefined,
  }
}

export function useLastEvent<T = unknown>(type: string): ServerEvent<T> | undefined {
  const resolved = useModeStore((s) => s.resolved)
  const event = useEventStore((s) => s.lastByType[type])
  useEffect(() => {
    if (resolved) startEvents()
  }, [resolved])
  return event as ServerEvent<T> | undefined
}
