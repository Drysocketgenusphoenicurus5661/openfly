// A tiny emitter so mock endpoints (data prepare, replay run) can push
// progress events into the same store the websocket would feed.

import type { ServerEvent } from '@/api/types'

type Listener = (event: ServerEvent) => void

const listeners = new Set<Listener>()

export const mockBus = {
  subscribe(listener: Listener): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
  emit(type: string, data: unknown, at?: string) {
    const event: ServerEvent = { type, at: at ?? new Date().toISOString(), data }
    for (const listener of listeners) listener(event)
  },
}
