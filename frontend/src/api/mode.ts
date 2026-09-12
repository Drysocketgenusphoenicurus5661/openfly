// Mock mode. The app serves generated data from src/mock when the backend
// is not reachable or when the URL carries ?mock=1. The decision is made
// once at startup by probeBackend() and is visible everywhere as a badge.

import { create } from 'zustand'

export type MockReason = 'query' | 'unreachable' | null

interface ModeState {
  resolved: boolean
  mock: boolean
  reason: MockReason
  setMock: (mock: boolean, reason: MockReason) => void
}

export const useModeStore = create<ModeState>((set) => ({
  resolved: false,
  mock: false,
  reason: null,
  setMock: (mock, reason) => set({ resolved: true, mock, reason }),
}))

export function isMock(): boolean {
  return useModeStore.getState().mock
}

export function mockRequestedByUrl(): boolean {
  if (typeof window === 'undefined') return false
  const params = new URLSearchParams(window.location.search)
  const value = params.get('mock')
  return value === '1' || value === 'true'
}

// Returns true when the app should run against the real backend.
export async function probeBackend(timeoutMs = 2500): Promise<boolean> {
  const store = useModeStore.getState()
  if (mockRequestedByUrl()) {
    store.setMock(true, 'query')
    return false
  }
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch('/api/status', {
      signal: controller.signal,
      headers: { Accept: 'application/json' },
    })
    const contentType = response.headers.get('content-type') ?? ''
    if (!response.ok || !contentType.includes('application/json')) {
      store.setMock(true, 'unreachable')
      return false
    }
    store.setMock(false, null)
    return true
  } catch {
    store.setMock(true, 'unreachable')
    return false
  } finally {
    window.clearTimeout(timer)
  }
}
