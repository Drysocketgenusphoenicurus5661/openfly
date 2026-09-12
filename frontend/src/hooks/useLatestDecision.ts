// The most recent decision of the running worker, assembled from the event
// stream. A backend that sends the full step as the observation payload is
// used as is; otherwise the pieces (observation, prediction, guard,
// straddle) are composed into one step-shaped object.

import { useMemo } from 'react'
import { useEventStore, useEvents } from '@/api/events'
import { isReplayStep } from '@/api/guards'
import type { Action, ReplayStep, ServerEvent } from '@/api/types'

export interface LatestDecision {
  step: ReplayStep | null
  source: 'event' | 'composed' | 'none'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function compose(events: ServerEvent[]): ReplayStep | null {
  let observation: ServerEvent | undefined
  let prediction: ServerEvent | undefined
  let guard: ServerEvent | undefined
  let straddle: ServerEvent | undefined
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i]
    if (!observation && e.type === 'observation') observation = e
    else if (!prediction && e.type === 'prediction') prediction = e
    else if (!guard && e.type === 'guard') guard = e
    else if (!straddle && e.type === 'straddle') straddle = e
    if (observation && prediction && guard && straddle) break
  }
  if (!observation) return null
  const o = isRecord(observation.data) ? observation.data : {}
  const p = isRecord(prediction?.data) ? prediction?.data : {}
  const g = isRecord(guard?.data) ? guard?.data : {}
  const s = isRecord(straddle?.data) ? straddle?.data : {}
  const rates = isRecord(o.rates_hz) ? (o.rates_hz as Record<string, number>) : {}
  const action = typeof o.action === 'string' ? (o.action as Action) : 'NONE'
  return {
    i: typeof o.i === 'number' ? o.i : 0,
    t: typeof o.t === 'string' ? o.t : observation.at,
    index: typeof o.index === 'number' ? o.index : Number.NaN,
    vix: typeof o.vix === 'number' ? o.vix : Number.NaN,
    premium: typeof o.premium === 'number' ? o.premium : Number(s.combined_ltp ?? Number.NaN),
    days_to_expiry: typeof o.days_to_expiry === 'number' ? o.days_to_expiry : Number.NaN,
    stimulus_hash: typeof o.stimulus_hash === 'string' ? o.stimulus_hash : '',
    stimulus_png: typeof o.stimulus_png === 'string' ? o.stimulus_png : '/api/brain/stimulus.png',
    rates_hz: rates,
    fixed_decoder: isRecord(o.fixed_decoder)
      ? (o.fixed_decoder as unknown as ReplayStep['fixed_decoder'])
      : { left_hz: 0, right_hz: 0, difference_hz: 0, gate_spikes: 0, side: 'HOLD' },
    prediction:
      isRecord(p) && 'realized_over_implied' in p
        ? (p as unknown as ReplayStep['prediction'])
        : isRecord(o.prediction)
          ? (o.prediction as unknown as ReplayStep['prediction'])
          : { realized_over_implied: Number.NaN, confidence: 0, decision: 'HOLD' },
    guard:
      isRecord(g) && Array.isArray(g.checks)
        ? (g as unknown as ReplayStep['guard'])
        : { allowed: true, checks: [] },
    action,
    straddle: {
      in_position: Boolean(s.in_position),
      strike: (s.strike as number | null) ?? null,
      lots: (s.lots as number | null) ?? null,
      entry_credit: (s.entry_credit as number | null) ?? null,
      combined_ltp: (s.combined_ltp as number | null) ?? null,
      stop_level: (s.stop_level as number | null) ?? null,
      target_level: (s.target_level as number | null) ?? null,
      pnl: (s.pnl as number | null) ?? null,
      legs: (s.legs as ReplayStep['straddle']['legs']) ?? null,
    },
    fills: Array.isArray(o.fills) ? (o.fills as ReplayStep['fills']) : [],
    pnl_day: typeof o.pnl_day === 'number' ? o.pnl_day : Number(s.pnl ?? 0),
    compute_seconds: typeof o.compute_seconds === 'number' ? o.compute_seconds : Number.NaN,
    narrative:
      typeof o.narrative === 'string'
        ? o.narrative
        : 'The worker has not written a narrative for this observation yet.',
    technical: isRecord(o.technical) ? (o.technical as ReplayStep['technical']) : {},
  }
}

export function useLatestDecision(): LatestDecision {
  useEvents()
  const events = useEventStore((s) => s.events)
  return useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.type === 'observation' && isReplayStep(e.data)) return { step: e.data, source: 'event' }
    }
    const composed = compose(events)
    return composed ? { step: composed, source: 'composed' } : { step: null, source: 'none' }
  }, [events])
}

// All step-shaped observations in the buffer, oldest first.
export function useObservationSteps(): ReplayStep[] {
  useEvents()
  const events = useEventStore((s) => s.events)
  return useMemo(() => {
    const steps: ReplayStep[] = []
    for (const e of events) {
      if (e.type === 'observation' && isReplayStep(e.data)) steps.push(e.data)
    }
    return steps
  }, [events])
}
