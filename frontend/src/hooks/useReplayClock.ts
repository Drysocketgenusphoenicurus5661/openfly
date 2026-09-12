import { useEffect } from 'react'
import { stepIntervalMs, useReplayPlayer } from '@/stores/replayPlayer'

const MAX_CATCH_UP = 10

// Drives the replay store while it is playing. Mount once per player. The
// number of steps owed comes from the clock, not the tick count, so a
// browser that throttles timers in a background tab still plays at the
// requested speed once it is visible again.
export function useReplayClock() {
  const playing = useReplayPlayer((s) => s.playing)
  const speed = useReplayPlayer((s) => s.speed)
  useEffect(() => {
    if (!playing) return
    const interval = stepIntervalMs(speed)
    let last = performance.now()
    const timer = window.setInterval(() => {
      const now = performance.now()
      const owed = Math.floor((now - last) / interval)
      if (owed <= 0) return
      last += owed * interval
      useReplayPlayer.getState().advance(Math.min(owed, MAX_CATCH_UP))
    }, interval)
    return () => window.clearInterval(timer)
  }, [playing, speed])
}
