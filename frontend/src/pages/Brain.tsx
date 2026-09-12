import { useMemo } from 'react'
import { api } from '@/api/client'
import { useBrainState, useCircuits } from '@/api/hooks'
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

export default function BrainPage() {
  const { data: circuits } = useCircuits()
  const { data: state } = useBrainState()
  const steps = useObservationSteps()
  const { step: latest } = useLatestDecision()

  const observations = useMemo(() => {
    if (steps.length > 0) return steps.map((s) => ({ t: s.t, rates_hz: s.rates_hz }))
    if (state) return [{ t: state.observed_at, rates_hz: state.rates_hz }]
    return []
  }, [steps, state])
  const populations = useMemo(() => circuits?.populations.map((p) => p.name), [circuits])
  const decoder = state?.fixed_decoder ?? latest?.fixed_decoder
  const prediction = state?.prediction ?? latest?.prediction
  const stimulusSrc =
    latest?.stimulus_png && latest.stimulus_png !== '/api/brain/stimulus.png'
      ? latest.stimulus_png
      : api.stimulusUrl()

  return (
    <div className="space-y-4">
      <PageHeader
        title="Brain"
        description={
          state
            ? `Observed ${formatTime(state.observed_at)}, ${state.neural_ms} ms of neural time per observation, ${fmtNum(state.compute_seconds, 2)} s compute, simulated ${fmtInt(state.sim_ms)} ms so far.`
            : 'What the fly saw, what it spiked, and what the readouts made of it.'
        }
      />
      <div className="grid grid-cols-5 gap-4">
        <Card className="col-span-2">
          <CardHeader>
            <CardTitle className="text-sm">Stimulus</CardTitle>
            <CardDescription>
              {latest?.technical?.encoder ? `Encoder ${latest.technical.encoder}: ` : ''}
              the photoreceptor input for the last observation.
              {state?.stimulus_hash && (
                <span className="ml-1 font-mono text-[11px]" title={state.stimulus_hash}>
                  {state.stimulus_hash.slice(0, 22)}
                </span>
              )}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <StimulusImage
              src={stimulusSrc}
              cacheKey={state?.stimulus_hash ?? latest?.stimulus_hash}
            />
          </CardContent>
        </Card>
        <Card className="col-span-3">
          <CardHeader>
            <CardTitle className="text-sm">Population firing rates</CardTitle>
            <CardDescription>
              Rows are populations, columns the last 30 observations; one shared colour scale.
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
            <EmptyState text="No observation yet." />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
