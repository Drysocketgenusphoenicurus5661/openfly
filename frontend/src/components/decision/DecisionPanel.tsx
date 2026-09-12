// One decision, two audiences. "What happened" is the trader's account;
// "Technical" is every number behind it. Used on Dashboard, Replay, Brain.

import type { ReplayStep } from '@/api/types'
import { ActionBadge } from '@/components/common/ActionBadge'
import { KeyValueGrid } from '@/components/common/KeyValueGrid'
import { Pnl } from '@/components/common/Pnl'
import { PremiumSourceBadge } from '@/components/common/PremiumSourceBadge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fmtNum } from '@/lib/format'
import { describeStopBasis } from '@/lib/stops'
import { formatTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { GuardChecklist } from './GuardChecklist'

export interface DecisionPanelProps {
  step: ReplayStep
  straddleNo?: number
  title?: string
  className?: string
  defaultTab?: 'narrative' | 'technical'
}

function Cell({
  label,
  value,
  className,
}: {
  label: string
  value: React.ReactNode
  className?: string
}) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={cn('tabular truncate text-sm font-semibold', className)}>{value}</div>
    </div>
  )
}

export function DecisionPanel({
  step,
  straddleNo,
  title,
  className,
  defaultTab = 'narrative',
}: DecisionPanelProps) {
  const tau = step.prediction.tau ?? 0.1
  const roi = step.prediction.realized_over_implied
  const band = `${(1 - tau).toFixed(2)} to ${(1 + tau).toFixed(2)}`
  const technical = step.technical ?? {}
  const basis = step.stop_basis ?? step.straddle.stop_basis ?? null
  const source = step.premium_source ?? step.straddle.premium_source ?? null
  const rates = Object.entries(step.rates_hz ?? {})

  return (
    <div className={cn('flex flex-col gap-3', className)} data-testid="decision-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{title ?? 'Decision'}</span>
          <span className="tabular text-sm text-muted-foreground">{formatTime(step.t)}</span>
          {straddleNo ? (
            <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
              Straddle {straddleNo}
              {step.straddle.strike ? ` at ${step.straddle.strike}` : ''}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <PremiumSourceBadge source={source} className="px-1.5 py-0 text-[10px]" />
          <ActionBadge action={step.action} />
        </div>
      </div>

      <div className="grid grid-cols-4 gap-x-3 gap-y-2 rounded-md border bg-muted/30 p-2.5 md:grid-cols-7">
        <Cell label="Time" value={formatTime(step.t)} />
        <Cell label="NIFTY" value={fmtNum(step.index, 1)} />
        <Cell label="INDIAVIX" value={fmtNum(step.vix, 2)} />
        <Cell label="Premium" value={fmtNum(step.premium, 1)} />
        <Cell
          label="Prediction"
          value={Number.isFinite(roi) ? `${fmtNum(roi, 2)} of implied` : '-'}
          className={
            roi < 1 - tau ? 'text-action-enter' : roi > 1 + tau ? 'text-action-exit' : undefined
          }
        />
        <Cell label="Action" value={<ActionBadge action={step.action} className="text-[11px]" />} />
        <Cell label="Day P&L" value={<Pnl value={step.pnl_day} />} />
      </div>
      <p className="text-[11px] text-muted-foreground" data-testid="prediction-legend">
        Prediction is realized over implied: below 1 calm (sell premium), above 1 wild. The fixed
        decoder reports 2.00 when it says wild and 0.00 when calm.
      </p>

      <Tabs defaultValue={defaultTab}>
        <TabsList>
          <TabsTrigger value="narrative">What happened</TabsTrigger>
          <TabsTrigger value="technical">Technical</TabsTrigger>
        </TabsList>
        <TabsContent value="narrative" className="mt-2">
          <p className="text-sm leading-relaxed" data-testid="narrative">
            {step.narrative}
          </p>
          {step.fills.length > 0 && (
            <div className="mt-3 text-xs text-muted-foreground">
              Fills:{' '}
              {step.fills
                .map((f) => `${f.side} ${f.qty} ${f.symbol} at ${fmtNum(f.price, 2)}`)
                .join('; ')}
            </div>
          )}
        </TabsContent>
        <TabsContent value="technical" className="mt-2 space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <section>
              <h4 className="mb-1.5 text-xs font-medium">Readout</h4>
              <KeyValueGrid
                data={technical as Record<string, unknown>}
                order={[
                  'encoder',
                  'readout',
                  'neural_ms',
                  'features',
                  'ridge_alpha',
                  'implied_move_points',
                  'predicted_move_points',
                  'top_populations',
                ]}
              />
            </section>
            <section>
              <h4 className="mb-1.5 text-xs font-medium">Prediction</h4>
              <KeyValueGrid
                data={{
                  realized_over_implied: roi,
                  confidence: step.prediction.confidence,
                  decision: step.prediction.decision,
                  tau,
                  hysteresis_band: band,
                  enter_below: (1 - tau).toFixed(2),
                  exit_above: (1 + tau).toFixed(2),
                }}
              />
              <h4 className="mt-3 mb-1.5 text-xs font-medium">Fixed decoder</h4>
              <KeyValueGrid
                data={{
                  DNp20_left_hz: step.fixed_decoder.left_hz,
                  DNp20_right_hz: step.fixed_decoder.right_hz,
                  difference_hz: step.fixed_decoder.difference_hz,
                  DNpe017_gate_spikes: step.fixed_decoder.gate_spikes,
                  side: step.fixed_decoder.side,
                }}
              />
            </section>
          </div>
          {basis && (
            <section>
              <h4 className="mb-1.5 text-xs font-medium">Stop basis</h4>
              <p className="mb-1.5 text-xs text-muted-foreground">{describeStopBasis(basis)}</p>
              <KeyValueGrid
                className="md:grid-cols-[auto_1fr_auto_1fr]"
                data={{
                  mode: basis.mode,
                  horizon_minutes: basis.horizon_minutes,
                  expected_move_points: basis.expected_move_points,
                  implied_move_points: basis.implied_move_points,
                  realized_move_points: basis.realized_move_points,
                  leg_stop_pct_ce: basis.leg_stop_pct.ce,
                  leg_stop_pct_pe: basis.leg_stop_pct.pe,
                  combined_stop_pct: basis.combined_stop_pct,
                }}
              />
            </section>
          )}
          <GuardChecklist guard={step.guard} />
          <section>
            <h4 className="mb-1.5 text-xs font-medium">Population rates (Hz)</h4>
            {rates.length === 0 ? (
              <div className="text-xs text-muted-foreground">No rates reported.</div>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {rates.map(([name, hz]) => (
                  <span key={name} className="tabular rounded border px-1.5 py-0.5 text-xs">
                    <span className="text-muted-foreground">{name}</span> {fmtNum(hz, 2)}
                  </span>
                ))}
              </div>
            )}
          </section>
          <section className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs md:grid-cols-4">
            <div>
              <span className="text-muted-foreground">Compute </span>
              <span className="tabular font-medium">{fmtNum(step.compute_seconds, 2)} s</span>
            </div>
            <div>
              <span className="text-muted-foreground">Days to expiry </span>
              <span className="tabular font-medium">{fmtNum(step.days_to_expiry, 2)}</span>
            </div>
            <div className="col-span-2 min-w-0">
              <span className="text-muted-foreground">Stimulus </span>
              <span className="truncate font-mono text-[11px]" title={step.stimulus_hash}>
                {step.stimulus_hash}
              </span>
            </div>
          </section>
        </TabsContent>
      </Tabs>
    </div>
  )
}
