import { useEffect, useState } from 'react'
import { useEventStore } from '@/api/events'
import { useModeStore } from '@/api/mode'
import { parseIso } from '@/lib/time'

// "Now" for the session clock. Real mode: the browser clock (formatted in
// IST by the callers). Mock mode: the simulated day's time, which advances
// with the mock event ticker.
export function useSessionNow(): Date {
  const mock = useModeStore((s) => s.mock)
  const lastObservation = useEventStore((s) => s.lastByType.observation)
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    if (mock) return
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [mock])
  if (mock) {
    const simulated = parseIso(lastObservation?.at ?? null)
    return simulated ?? now
  }
  return now
}
