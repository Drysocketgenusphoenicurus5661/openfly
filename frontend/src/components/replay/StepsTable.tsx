// All steps of a replay, virtualized: 375 one-minute rows render as a
// window of about forty. Clicking a row seeks the player.

import { useEffect, useMemo, useRef } from 'react'
import type { ReplayStep } from '@/api/types'
import { ActionBadge } from '@/components/common/ActionBadge'
import { Pnl } from '@/components/common/Pnl'
import { useVirtualRows } from '@/hooks/useVirtualRows'
import { numberStraddles } from '@/lib/actions'
import { fmtNum } from '@/lib/format'
import { formatTime } from '@/lib/time'
import { cn } from '@/lib/utils'

const ROW = 32
const HEIGHT = 420

export function StepsTable({
  steps,
  index,
  onSelect,
}: {
  steps: ReplayStep[]
  index: number
  onSelect: (i: number) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const { start, end, topPad, bottomPad, onScroll } = useVirtualRows(steps.length, ROW, HEIGHT)
  const { perStep } = useMemo(() => numberStraddles(steps), [steps])

  // Keep the selected row in view while playing.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const top = index * ROW
    if (top < el.scrollTop || top + ROW > el.scrollTop + el.clientHeight) {
      el.scrollTop = Math.max(0, top - el.clientHeight / 2)
    }
  }, [index])

  return (
    <div
      ref={ref}
      onScroll={onScroll}
      className="overflow-auto rounded-md border"
      style={{ height: HEIGHT }}
    >
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 z-10 bg-card">
          <tr className="border-b text-left text-muted-foreground">
            <th className="px-2 py-1.5 font-medium">Time</th>
            <th className="px-2 py-1.5 font-medium">Straddle</th>
            <th className="px-2 py-1.5 text-right font-medium">NIFTY</th>
            <th className="px-2 py-1.5 text-right font-medium">Premium</th>
            <th className="px-2 py-1.5 text-right font-medium">Prediction</th>
            <th className="px-2 py-1.5 font-medium">Action</th>
            <th className="px-2 py-1.5 text-right font-medium">Day P&L</th>
          </tr>
        </thead>
        <tbody>
          {topPad > 0 && (
            <tr>
              <td colSpan={7} style={{ height: topPad }} />
            </tr>
          )}
          {steps.slice(start, end).map((step, k) => {
            const i = start + k
            const straddle = perStep[i]
            return (
              <tr
                key={step.i}
                onClick={() => onSelect(i)}
                className={cn(
                  'cursor-pointer border-b border-border/60 hover:bg-accent/60',
                  i === index && 'bg-accent'
                )}
                style={{ height: ROW }}
                data-selected={i === index || undefined}
              >
                <td className="tabular px-2">{formatTime(step.t)}</td>
                <td className="px-2 text-muted-foreground">
                  {straddle && straddle.n > 0
                    ? `${straddle.n}${straddle.strike ? ` at ${straddle.strike}` : ''}`
                    : ''}
                </td>
                <td className="tabular px-2 text-right">{fmtNum(step.index, 1)}</td>
                <td className="tabular px-2 text-right">{fmtNum(step.premium, 1)}</td>
                <td className="tabular px-2 text-right">
                  {fmtNum(step.prediction.realized_over_implied, 2)}
                </td>
                <td className="px-2">
                  <ActionBadge action={step.action} className="px-1.5 py-0 text-[10px]" />
                </td>
                <td className="px-2 text-right">
                  <Pnl value={step.pnl_day} />
                </td>
              </tr>
            )
          })}
          {bottomPad > 0 && (
            <tr>
              <td colSpan={7} style={{ height: bottomPad }} />
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
