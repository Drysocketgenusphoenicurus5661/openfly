import { useEffect, useState } from 'react'

// "Now" for the session clock: the browser clock, ticking every second.
// Callers format it in IST.
export function useSessionNow(): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  return now
}
