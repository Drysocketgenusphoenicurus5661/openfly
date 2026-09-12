import type { FixedDecoder, Prediction } from '@/api/types'
import { fmtNum } from '@/lib/format'
import { cn } from '@/lib/utils'

// DNp20 left against right, gated by DNpe017. The fixed decoder is the
// control readout: right minus left over a 2 Hz threshold.
export function DnGauge({ decoder, threshold = 2 }: { decoder: FixedDecoder; threshold?: number }) {
  const max = Math.max(decoder.left_hz, decoder.right_hz, threshold * 2, 1)
  const gateOpen = decoder.gate_spikes > 0
  const bar = (label: string, value: number, colour: string) => (
    <div className="grid grid-cols-[4.5rem_1fr_3.5rem] items-center gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <div className="h-3 rounded bg-muted">
        <div className={cn('h-3 rounded', colour)} style={{ width: `${(value / max) * 100}%` }} />
      </div>
      <span className="tabular text-right font-medium">{fmtNum(value, 2)} Hz</span>
    </div>
  )
  return (
    <div className="space-y-2">
      {bar('DNp20 left', decoder.left_hz, 'bg-action-exit')}
      {bar('DNp20 right', decoder.right_hz, 'bg-action-enter')}
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span>
          <span className="text-muted-foreground">Difference </span>
          <span className="tabular font-medium">{fmtNum(decoder.difference_hz, 2)} Hz</span>
          <span className="text-muted-foreground"> (threshold {threshold} Hz)</span>
        </span>
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-medium',
            gateOpen ? 'bg-profit/15 text-profit' : 'bg-muted text-muted-foreground'
          )}
        >
          DNpe017 gate {gateOpen ? `open (${decoder.gate_spikes} spikes)` : 'closed'}
        </span>
        <span className="rounded border px-1.5 py-0.5 font-medium">side {decoder.side}</span>
      </div>
    </div>
  )
}

// Realized over implied on a horizontal scale with the hysteresis band.
export function PredictionGauge({
  prediction,
  min = 0.5,
  max = 1.5,
}: {
  prediction: Prediction
  min?: number
  max?: number
}) {
  const tau = prediction.tau ?? 0.1
  const roi = prediction.realized_over_implied
  const pct = (v: number) => `${((Math.max(min, Math.min(max, v)) - min) / (max - min)) * 100}%`
  const tone =
    roi < 1 - tau
      ? 'text-action-enter'
      : roi > 1 + tau
        ? 'text-action-exit'
        : 'text-muted-foreground'
  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between">
        <span className={cn('tabular text-2xl font-semibold', tone)}>
          {Number.isFinite(roi) ? fmtNum(roi, 2) : '-'}
        </span>
        <span className="text-xs text-muted-foreground">
          confidence{' '}
          <span className="tabular font-medium text-foreground">
            {fmtNum(prediction.confidence, 2)}
          </span>
          , decision <span className="font-medium text-foreground">{prediction.decision}</span>
        </span>
      </div>
      <div className="relative h-6">
        <div className="absolute inset-x-0 top-2 h-2 rounded bg-muted" />
        <div
          className="absolute top-2 h-2 bg-amber/40"
          style={{ left: pct(1 - tau), width: `calc(${pct(1 + tau)} - ${pct(1 - tau)})` }}
          title={`hysteresis band ${(1 - tau).toFixed(2)} to ${(1 + tau).toFixed(2)}`}
        />
        <div className="absolute top-0 h-6 w-px bg-foreground/60" style={{ left: pct(1) }} />
        {Number.isFinite(roi) && (
          <div
            className={cn(
              'absolute top-0.5 size-5 -translate-x-1/2 rounded-full border-2 border-background',
              roi < 1 - tau
                ? 'bg-action-enter'
                : roi > 1 + tau
                  ? 'bg-action-exit'
                  : 'bg-muted-foreground'
            )}
            style={{ left: pct(roi) }}
          />
        )}
      </div>
      <div className="relative h-4 text-[10px] text-muted-foreground">
        <span className="absolute -translate-x-1/2" style={{ left: pct(min) }}>
          {min.toFixed(1)}
        </span>
        <span
          className="absolute -translate-x-1/2 text-action-enter"
          style={{ left: pct(1 - tau) }}
        >
          enter below {(1 - tau).toFixed(2)}
        </span>
        <span className="absolute -translate-x-1/2" style={{ left: pct(1) }}>
          1.00
        </span>
        <span className="absolute -translate-x-1/2 text-action-exit" style={{ left: pct(1 + tau) }}>
          exit above {(1 + tau).toFixed(2)}
        </span>
        <span className="absolute -translate-x-1/2" style={{ left: pct(max) }}>
          {max.toFixed(1)}
        </span>
      </div>
    </div>
  )
}
