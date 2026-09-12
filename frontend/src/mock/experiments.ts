import type { Experiment, ExperimentMetrics, ExperimentSummary } from '@/api/types'
import { gaussian, mulberry32, round2 } from './random'

function metrics(seed: number, driftPerDay: number, accuracy: number): ExperimentMetrics {
  const rand = mulberry32(seed)
  const trades = 38 + Math.floor(rand() * 10)
  return {
    net_pnl_per_lot: round2(driftPerDay * 52 + gaussian(rand) * 900),
    sharpe: round2(driftPerDay / 420 + gaussian(rand) * 0.15),
    max_drawdown: round2(-4200 - Math.abs(gaussian(rand)) * 3200),
    trades,
    stop_hits: Math.floor(trades * (0.28 + rand() * 0.08)),
    target_hits: Math.floor(trades * (0.2 + rand() * 0.08)),
    accuracy: round2(accuracy),
    accuracy_ci: [round2(accuracy - 0.045), round2(accuracy + 0.045)],
  }
}

function curve(
  seed: number,
  days: number,
  drift: number,
  start: string
): { t: string[]; values: number[] } {
  const rand = mulberry32(seed)
  const t: string[] = []
  const values: number[] = []
  let total = 0
  const from = new Date(`${start}T00:00:00+05:30`)
  let d = 0
  while (t.length < days) {
    const day = new Date(from.getTime() + d * 86_400_000)
    d += 1
    const weekday = day.getUTCDay()
    if (weekday === 0 || weekday === 6) continue
    total += drift + gaussian(rand) * 1400
    t.push(day.toISOString().slice(0, 10))
    values.push(round2(total))
  }
  return { t, values }
}

function build(
  id: string,
  name: string,
  createdAt: string,
  config: Experiment['config'],
  state: Experiment['state'],
  drift: number,
  accuracy: number,
  passed: boolean | null,
  verdict: string,
  progress: { done: number; total: number } | null
): Experiment {
  const seed = id.length * 7919 + drift
  const days = 52
  const strategy = curve(seed, days, drift, '2026-07-01')
  const fixed = curve(seed + 1, days, 95, '2026-07-01')
  const random = curve(seed + 2, days, 40, '2026-07-01')
  const shuffled = curve(seed + 3, days, 10, '2026-07-01')
  const flat = { t: strategy.t, values: strategy.t.map(() => 0) }
  const done = state === 'done'
  return {
    id,
    name,
    config,
    state,
    created_at: createdAt,
    progress,
    metrics: done
      ? {
          validation: metrics(seed + 10, drift * 0.9, accuracy + 0.01),
          test: metrics(seed + 11, drift, accuracy),
        }
      : {},
    controls: done
      ? {
          fixed_0920: metrics(seed + 20, 95, 0.5),
          random_entry: metrics(seed + 21, 40, 0.5),
          shuffled: metrics(seed + 22, 10, 0.49),
          flat: {
            ...metrics(seed + 23, 0, 0.5),
            net_pnl_per_lot: 0,
            sharpe: 0,
            max_drawdown: 0,
            trades: 0,
            stop_hits: 0,
            target_hits: 0,
          },
        }
      : {},
    curves: done
      ? {
          test: {
            t: strategy.t,
            strategy: strategy.values,
            fixed_0920: fixed.values,
            random_entry: random.values,
            shuffled: shuffled.values,
            flat: flat.values,
          },
        }
      : {},
    passed,
    verdict,
  }
}

const WINDOWS = {
  train: ['2025-08-08', '2026-03-31'] as [string, string],
  validation: ['2026-04-01', '2026-06-30'] as [string, string],
  test: ['2026-07-01', '2026-09-11'] as [string, string],
}

export const MOCK_EXPERIMENTS: Experiment[] = [
  build(
    'exp_20260912_193000_b_reservoir',
    'encoder B, reservoir, frozen',
    '2026-09-12T19:30:00+05:30',
    { encoder: 'B', readout: 'reservoir', plastic: false, neural_ms: 200, ...WINDOWS },
    'done',
    60,
    0.51,
    false,
    'no edge found: accuracy 0.51 within bootstrap interval of 0.5 (p = 0.31); strategy P&L 3120 per lot did not beat fixed 09:20 entry 4940',
    { done: 21000, total: 21000 }
  ),
  build(
    'exp_20260910_083000_a_fixed',
    'encoder A, fixed decoder',
    '2026-09-10T08:30:00+05:30',
    { encoder: 'A', readout: 'fixed', plastic: false, neural_ms: 200, ...WINDOWS },
    'done',
    -20,
    0.49,
    false,
    'no edge found: accuracy 0.49 within bootstrap interval of 0.5 (p = 0.62)',
    { done: 21000, total: 21000 }
  ),
  build(
    'exp_20260911_214500_b_reservoir_500',
    'encoder B, reservoir, frozen, 500 ms',
    '2026-09-11T21:45:00+05:30',
    { encoder: 'B', readout: 'reservoir', plastic: false, neural_ms: 500, ...WINDOWS },
    'done',
    170,
    0.55,
    true,
    'passed: accuracy 0.55 above 0.52 with block-bootstrap p = 0.02; strategy P&L 8840 per lot beat fixed 09:20 entry 4940 and random entry 2080 at equal trade count',
    { done: 21000, total: 21000 }
  ),
  build(
    'exp_20260912_221000_c_plastic',
    'encoder C, plastic arm against frozen twin',
    '2026-09-12T22:10:00+05:30',
    { encoder: 'C', readout: 'plastic', plastic: true, neural_ms: 200, ...WINDOWS },
    'running',
    0,
    0.5,
    null,
    '',
    { done: 8340, total: 21000 }
  ),
]

export function summaries(): ExperimentSummary[] {
  return MOCK_EXPERIMENTS.map((e) => ({
    id: e.id,
    name: e.name ?? e.id,
    state: e.state,
    created_at: e.created_at ?? '',
    config: e.config,
    progress: e.progress ?? null,
  }))
}
