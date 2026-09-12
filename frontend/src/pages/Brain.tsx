import { useMemo } from 'react'
import { useBrainState, useCircuits, useReplays } from '@/api/hooks'
import type { RatesHz } from '@/api/types'
import { DnGauge, PredictionGauge } from '@/components/brain/Gauges'
import { Heatmap } from '@/components/brain/Heatmap'
import { StimulusImage } from '@/components/brain/StimulusImage'
import { EmptyState } from '@/components/common/EmptyState'
import { KeyValueGrid } from '@/components/common/KeyValueGrid'
import { PageHeader } from '@/components/common/PageHeader'
import { DecisionPanel } from '@/components/decision/DecisionPanel'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useLatestDecision, useObservationSteps } from '@/hooks/useLatestDecision'
import { fmtInt, fmtNum } from '@/lib/format'
import { formatTime } from '@/lib/time'

interface Observation {
  t: string
  rates_hz: RatesHz
  action?: string
}

// The image the fly saw: the URL the state names, else the live endpoint
// with the stimulus hash as a cache buster so a new observation reloads it.
function stimulusSource(
  stimulusPng: string | null | undefined,
  hash: string | null | undefined
): string {
  if (stimulusPng) return stimulusPng
  return hash ? `/api/brain/stimulus.png?k=${encodeURIComponent(hash)}` : '/api/brain/stimulus.png'
}

export default function BrainPage() {
  const { data: circuits } = useCircuits()
  const { data: replays } = useReplays(5000)
  const replayRunning = (replays?.replays ?? []).some(
    (r) => r.state === 'running' || r.state === 'queued'
  )
  // A running replay changes the last observation every second or two.
  const { data: state } = useBrainState(replayRunning ? 2000 : 5000)
  const liveSteps = useObservationSteps()
  const { step: latest } = useLatestDecision()

  // Seed from the state's history on load, then keep appending live
  // observations; one column per distinct time, newest last.
  const observations = useMemo<Observation[]>(() => {
    const byTime = new Map<string, Observation>()
    for (const h of state?.history ?? []) byTime.set(h.t, h)
    for (const s of liveSteps) byTime.set(s.t, { t: s.t, rates_hz: s.rates_hz, action: s.action })
    if (state && !byTime.has(state.observed_at)) {
      byTime.set(state.observed_at, {
        t: state.observed_at,
        rates_hz: state.rates_hz,
        action: state.action,
      })
    }
    return [...byTime.values()].sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : 0)).slice(-30)
  }, [state, liveSteps])

  const populations = useMemo(() => {
    const names = circuits?.populations.map((p) => p.name) ?? []
    const seen = new Set(names)
    for (const o of observations) {
      for (const k of Object.keys(o.rates_hz)) {
        if (!seen.has(k)) {
          names.push(k)
          seen.add(k)
        }
      }
    }
    return names
  }, [circuits, observations])

  const decoder = state?.fixed_decoder ?? latest?.fixed_decoder
  const prediction = state?.prediction ?? latest?.prediction
  const stimulusSrc = stimulusSource(state?.stimulus_png, state?.stimulus_hash)
  const sourceLine = !state
    ? null
    : state.source === 'replay'
      ? `From replay ${state.replay_id ?? '?'}${(state.step ?? state.step_i) != null ? ` step ${state.step ?? state.step_i}` : ''} at ${formatTime(state.observed_at)}`
      : state.source === 'worker'
        ? `Live worker, observed ${formatTime(state.observed_at)}`
        : `Observed ${formatTime(state.observed_at)}`

  return (
    <div className="space-y-4">
      <PageHeader
        title="Brain"
        description={
          state
            ? `${sourceLine}. ${state.neural_ms} ms of neural time per observation, ${fmtNum(state.compute_seconds, 2)} s compute, simulated ${fmtInt(state.sim_ms)} ms so far.`
            : 'What the fly saw, what it spiked, and what the readouts made of it.'
        }
      />
      <div className="grid grid-cols-5 gap-4">
        <Card className="col-span-2">
          <CardHeader>
            <CardTitle className="text-sm">Stimulus</CardTitle>
            <CardDescription>
              {sourceLine ? `${sourceLine}. ` : ''}The photoreceptor input for the last observation.
              {state?.stimulus_hash && (
                <span className="ml-1 font-mono text-[11px]" title={state.stimulus_hash}>
                  {state.stimulus_hash.slice(0, 22)}
                </span>
              )}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <StimulusImage key={stimulusSrc} src={stimulusSrc} />
          </CardContent>
        </Card>
        <Card className="col-span-3">
          <CardHeader>
            <CardTitle className="text-sm">Population firing rates</CardTitle>
            <CardDescription>
              Rows are populations, columns the last 30 observations; one shared colour scale.
              {state?.history
                ? ` Seeded from the ${state.history.length} observations the API remembers.`
                : ''}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Heatmap observations={observations} populations={populations} />
          </CardContent>
        </Card>
      </div>
      <div className="grid grid-cols-3 gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Fixed decoder: DNp20 left against right</CardTitle>
            <CardDescription>
              Control readout. Right minus left, gated by any DNpe017 spike, 2 Hz threshold.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {decoder ? <DnGauge decoder={decoder} /> : <EmptyState text="No decoder output yet." />}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Reservoir readout: realized over implied</CardTitle>
            <CardDescription>
              Enter below 1 - tau, exit above 1 + tau, hold in between.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {prediction ? (
              <PredictionGauge prediction={prediction} />
            ) : (
              <EmptyState text="No prediction yet." />
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Plastic edges</CardTitle>
            <CardDescription>
              Dopamine-gated KC to MBON weights, when the plastic arm is on.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {state?.plastic ? (
              <KeyValueGrid data={state.plastic as unknown as Record<string, unknown>} />
            ) : (
              <div className="text-sm text-muted-foreground">
                Plastic arm off (neural.plastic is false); weights are frozen.
              </div>
            )}
            {circuits && (
              <div className="mt-3 text-xs text-muted-foreground">
                {fmtInt(circuits.n)} neurons, {circuits.populations.length} named populations:{' '}
                {circuits.populations.map((p) => `${p.name} (${fmtInt(p.size)})`).join(', ')}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Latest decision</CardTitle>
        </CardHeader>
        <CardContent>
          {latest ? (
            <DecisionPanel step={latest} title="Decision" defaultTab="technical" />
          ) : (
            <EmptyState text="No observation from the worker yet." />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
