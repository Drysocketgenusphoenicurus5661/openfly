import { useEffect } from 'react'
import { stepIntervalMs, useReplayPlayer } from '@/stores/replayPlayer'

// Drives the replay store while it is playing. Mount once per player.
export function useReplayClock() {
  const playing = useReplayPlayer((s) => s.playing)
  const speed = useReplayPlayer((s) => s.speed)
  useEffect(() => {
    if (!playing) return
    const timer = window.setInterval(() => useReplayPlayer.getState().tick(), stepIntervalMs(speed))
    return () => window.clearInterval(timer)
  }, [playing, speed])
}
