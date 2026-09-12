// Backend connectivity. The app probes GET /api/status at startup and shows
// one full-page state while the backend is down; every query is enabled
// only while it is up. A network failure on any request triggers a re-probe.

import { create } from 'zustand'

export type BackendState = 'probing' | 'up' | 'down'

interface BackendStore {
  state: BackendState
  lastError: string | null
  checkedAt: number | null
  probe: () => Promise<boolean>
  markUp: () => void
}

let inFlight: Promise<boolean> | null = null

export const useBackendStore = create<BackendStore>((set) => ({
  state: 'probing',
  lastError: null,
  checkedAt: null,
  markUp: () => set({ state: 'up', lastError: null, checkedAt: Date.now() }),
  probe: () => {
    if (inFlight) return inFlight
    inFlight = (async () => {
      const controller = new AbortController()
      const timer = window.setTimeout(() => controller.abort(), 4000)
      try {
        const response = await fetch('/api/status', {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        })
        const contentType = response.headers.get('content-type') ?? ''
        if (!response.ok) {
          set({
            state: 'down',
            lastError: `GET /api/status returned ${response.status}`,
            checkedAt: Date.now(),
          })
          return false
        }
        if (!contentType.includes('application/json')) {
          set({
            state: 'down',
            lastError: 'GET /api/status did not return JSON',
            checkedAt: Date.now(),
          })
          return false
        }
        set({ state: 'up', lastError: null, checkedAt: Date.now() })
        return true
      } catch (error) {
        const message =
          error instanceof DOMException && error.name === 'AbortError'
            ? 'GET /api/status timed out'
            : error instanceof Error
              ? error.message
              : String(error)
        set({ state: 'down', lastError: message, checkedAt: Date.now() })
        return false
      } finally {
        window.clearTimeout(timer)
        inFlight = null
      }
    })()
    return inFlight
  },
}))

export function isBackendUp(): boolean {
  return useBackendStore.getState().state === 'up'
}
