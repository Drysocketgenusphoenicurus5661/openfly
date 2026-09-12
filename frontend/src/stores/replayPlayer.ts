// Playback state for the Replay page. The store owns the playhead so the
// chart, the timeline, the decision panel and the steps table all read one
// index. A React hook (useReplayClock) drives tick() while playing.

import { create } from 'zustand'

export type Speed = 1 | 5 | 20
export const SPEEDS: Speed[] = [1, 5, 20]
export const DEFAULT_SPEED: Speed = 5

// Steps per second at each speed: 1x plays one observation a second.
export function stepIntervalMs(speed: Speed): number {
  return Math.round(1000 / speed)
}

export interface ReplayPlayerState {
  replayId: string | null
  total: number
  index: number
  playing: boolean
  speed: Speed
  load: (replayId: string | null, total: number, startIndex?: number) => void
  // Load a replay and start playing it from the first step.
  loadAndPlay: (replayId: string, total: number, speed?: Speed) => void
  setIndex: (index: number) => void
  next: () => void
  prev: () => void
  first: () => void
  last: () => void
  play: () => void
  pause: () => void
  toggle: () => void
  setSpeed: (speed: Speed) => void
  tick: () => void
  // Advance several steps at once (a throttled timer catching up); pauses at the end.
  advance: (n: number) => void
  reset: () => void
}

function clamp(index: number, total: number): number {
  if (total <= 0) return 0
  return Math.max(0, Math.min(total - 1, Math.floor(index)))
}

export const useReplayPlayer = create<ReplayPlayerState>((set, get) => ({
  replayId: null,
  total: 0,
  index: 0,
  playing: false,
  speed: DEFAULT_SPEED,
  load: (replayId, total, startIndex = 0) =>
    set({ replayId, total, index: clamp(startIndex, total), playing: false }),
  loadAndPlay: (replayId, total, speed = DEFAULT_SPEED) =>
    set({ replayId, total, index: 0, speed, playing: total > 0 }),
  setIndex: (index) => set((s) => ({ index: clamp(index, s.total) })),
  next: () => set((s) => ({ index: clamp(s.index + 1, s.total) })),
  prev: () => set((s) => ({ index: clamp(s.index - 1, s.total) })),
  first: () => set({ index: 0, playing: false }),
  last: () => set((s) => ({ index: clamp(s.total - 1, s.total), playing: false })),
  play: () =>
    set((s) => {
      if (s.total === 0) return {}
      // Playing from the end starts over.
      const index = s.index >= s.total - 1 ? 0 : s.index
      return { playing: true, index }
    }),
  pause: () => set({ playing: false }),
  toggle: () => (get().playing ? get().pause() : get().play()),
  setSpeed: (speed) => set({ speed }),
  tick: () =>
    set((s) => {
      if (!s.playing) return {}
      if (s.index >= s.total - 1) return { playing: false }
      return { index: s.index + 1 }
    }),
  advance: (n) =>
    set((s) => {
      if (!s.playing || n <= 0) return {}
      const index = Math.min(s.total - 1, s.index + Math.floor(n))
      return { index, playing: index < s.total - 1 }
    }),
  reset: () => set({ replayId: null, total: 0, index: 0, playing: false }),
}))
