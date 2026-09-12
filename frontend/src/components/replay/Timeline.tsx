// A strip across the trading day: straddle spans as bands with a numbered
// pill each (the strike lives in the tooltip and the steps table), one
// coloured marker per action, and the playhead. Clicking anywhere seeks.

import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReplayStep } from '@/api/types'
import { useIsDark } from '@/hooks/useIsDark'
import { ACTION_STYLES, actionHex, actionStyle, numberStraddles } from '@/lib/actions'
import { formatTime } from '@/lib/time'

const LEGEND: string[] = [
  'ENTER',
  'REENTRY',
  'EXIT',
  'TARGET',
  'STOP',
  'STOP_LEG',
  'LOCK',
  'SQUARE_OFF',
  'VETO',
  'HOLD',
]

const PILL = 18
const GAP = 2

export function Timeline({
  steps,
  index,
  onSeek,
}: {
  steps: ReplayStep[]
  index: number
  onSeek: (i: number) => void
}) {
  const dark = useIsDark()
  const stripRef = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(0)
  const total = Math.max(steps.length, 1)
  const { spans } = useMemo(() => numberStraddles(steps), [steps])
  const events = useMemo(() => steps.filter((s) => actionStyle(s.action).marker), [steps])
  const x = (i: number) => `${((i + 0.5) / total) * 100}%`

  useEffect(() => {
    const el = stripRef.current
    if (!el) return
    const update = () => setWidth(el.clientWidth)
    update()
    const observer = new ResizeObserver(update)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // Pills sit at the start of their span and slide right just enough never
  // to overlap the previous one.
  const pills = useMemo(() => {
    let right = -GAP
    return spans.map((span) => {
      const start = (span.from / total) * width
      const left = Math.min(Math.max(start, right + GAP), Math.max(0, width - PILL))
      right = left + PILL
      return { span, left }
    })
  }, [spans, total, width])

  return (
    <div className="space-y-2">
      <div
        ref={stripRef}
        className="relative h-14 cursor-pointer select-none rounded-md border bg-muted/30"
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect()
          const fraction = (event.clientX - rect.left) / rect.width
          onSeek(Math.round(fraction * total - 0.5))
        }}
        role="slider"
        aria-valuemin={0}
        aria-valuemax={total - 1}
        aria-valuenow={index}
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'ArrowRight') onSeek(index + 1)
          if (event.key === 'ArrowLeft') onSeek(index - 1)
        }}
      >
        {spans.map((span) => (
          <div
            key={`band-${span.n}`}
            className="absolute top-1 h-5 rounded border border-action-enter/40 bg-action-enter/10"
            style={{
              left: `${(span.from / total) * 100}%`,
              width: `${((span.to - span.from + 1) / total) * 100}%`,
            }}
            title={`Straddle ${span.n} at ${span.strike ?? '?'}: ${formatTime(steps[span.from]?.t)} to ${formatTime(steps[span.to]?.t)}`}
          />
        ))}
        {pills.map(({ span, left }) => (
          <span
            key={`pill-${span.n}`}
            className="absolute top-1.5 flex h-4 items-center justify-center rounded-sm bg-action-enter px-1 font-mono text-[10px] font-semibold leading-none text-black"
            style={{ left, minWidth: PILL }}
            title={`Straddle ${span.n} at ${span.strike ?? '?'}: ${formatTime(steps[span.from]?.t)} to ${formatTime(steps[span.to]?.t)}`}
          >
            {span.n}
          </span>
        ))}
        {events.map((s) => (
          <div
            key={s.i}
            className="absolute top-7 h-4 w-1.5 -translate-x-1/2 rounded-sm"
            style={{ left: x(s.i), backgroundColor: actionHex(s.action, dark) }}
            title={`${formatTime(s.t)} ${actionStyle(s.action).label}${s.straddle.strike ? ` at ${s.straddle.strike}` : ''}`}
          />
        ))}
        <div className="absolute bottom-0 left-0 right-0 flex justify-between px-1 text-[9px] text-muted-foreground">
          <span>{formatTime(steps[0]?.t)}</span>
          <span>{formatTime(steps[Math.floor(total / 2)]?.t)}</span>
          <span>{formatTime(steps[total - 1]?.t)}</span>
        </div>
        <div
          className="absolute top-0 h-full w-0.5 -translate-x-1/2 bg-foreground"
          style={{ left: x(index) }}
        />
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-2.5 w-4 rounded-sm border border-action-enter/40 bg-action-enter/10" />
          straddle span, numbered; strike in the tooltip and the steps table
        </span>
        {LEGEND.map((action) => (
          <span key={action} className="inline-flex items-center gap-1">
            <span
              className="inline-block size-2.5 rounded-sm"
              style={{ backgroundColor: actionHex(action, dark) }}
            />
            {ACTION_STYLES[action as keyof typeof ACTION_STYLES].label}
          </span>
        ))}
      </div>
    </div>
  )
}
