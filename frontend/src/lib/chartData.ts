// Turns steps and straddles into chart inputs shared by Dashboard and Replay.

import type { ReplayStep, StepStraddle, Straddle } from '@/api/types'
import type { ChartLevel, ChartMarker, PremiumPoint } from '@/components/charts/PriceChart'
import { actionStyle } from '@/lib/actions'
import { fmtNum } from '@/lib/format'
import { formatTime, toChartTime } from '@/lib/time'

export function markersFromSteps(
  steps: ReplayStep[],
  straddleNos?: { n: number }[]
): ChartMarker[] {
  const markers: ChartMarker[] = []
  for (const step of steps) {
    const style = actionStyle(step.action)
    if (!style.marker) continue
    const n = straddleNos?.[step.i]?.n
    const parts = [`${formatTime(step.t)} ${style.label}`]
    if (step.straddle.strike) parts.push(`${n ? `straddle ${n} ` : ''}at ${step.straddle.strike}`)
    if (step.action === 'VETO') {
      const failed = step.guard.checks.filter((c) => !c.ok).map((c) => c.name.replace(/_/g, ' '))
      if (failed.length) parts.push(`guard: ${failed.join(', ')}`)
    } else if (step.fills.length) {
      parts.push(
        step.fills.map((f) => `${f.side} ${f.symbol.slice(-7)} ${fmtNum(f.price, 1)}`).join(', ')
      )
    }
    parts.push(`day P&L ${fmtNum(step.pnl_day, 0)}`)
    markers.push({
      id: `step-${step.i}`,
      time: toChartTime(step.t),
      action: step.action,
      text: parts.join('. '),
    })
  }
  return markers
}

export function premiumFromSteps(steps: ReplayStep[]): PremiumPoint[] {
  return steps.map((s) => ({ time: toChartTime(s.t), value: s.premium }))
}

export function levelsFromStraddle(
  straddle: Straddle | StepStraddle | null | undefined
): ChartLevel[] {
  if (!straddle?.in_position) return []
  const levels: ChartLevel[] = []
  if (straddle.stop_level != null)
    levels.push({ id: 'stop', price: straddle.stop_level, kind: 'stop', label: 'Stop' })
  if (straddle.target_level != null)
    levels.push({ id: 'target', price: straddle.target_level, kind: 'target', label: 'Target' })
  if (straddle.entry_credit != null)
    levels.push({ id: 'entry', price: straddle.entry_credit, kind: 'entry', label: 'Credit' })
  for (const leg of straddle.legs ?? []) {
    if (leg.stop_price != null && leg.status !== 'closed' && leg.status !== 'stopped') {
      const kind = leg.symbol.endsWith('CE')
        ? 'CE'
        : leg.symbol.endsWith('PE')
          ? 'PE'
          : leg.symbol.slice(-2)
      levels.push({
        id: `leg-${leg.symbol}`,
        price: leg.stop_price,
        kind: 'leg_stop',
        label: `${kind} stop`,
      })
    }
  }
  return levels
}
