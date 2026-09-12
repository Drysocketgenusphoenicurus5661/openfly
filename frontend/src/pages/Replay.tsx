import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { toast } from 'sonner'
import { useLastEvent } from '@/api/events'
import {
  useExperiments,
  useReplay,
  useReplayDates,
  useReplays,
  useRunReplay,
  useSettings,
} from '@/api/hooks'
import type { Bar, Encoder, Readout, ReplayRunBody, ReplayStep } from '@/api/types'
import { ChartLegendRow } from '@/components/charts/ChartLegendRow'
import { PriceChart } from '@/components/charts/PriceChart'
import { EmptyState, LoadingState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import { Pnl } from '@/components/common/Pnl'
import { Stat } from '@/components/common/Stat'
import { StatusBadge, toneFor } from '@/components/common/StatusBadge'
import { DecisionPanel } from '@/components/decision/DecisionPanel'
import { StepsTable } from '@/components/replay/StepsTable'
import { Timeline } from '@/components/replay/Timeline'
import { Transport } from '@/components/replay/Transport'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { numberStraddles } from '@/lib/actions'
import { levelsFromStraddle, markersFromSteps, premiumFromSteps } from '@/lib/chartData'
import { fmtNum } from '@/lib/format'
import { formatDate } from '@/lib/time'
import { cn } from '@/lib/utils'
import { DEFAULT_SPEED, useReplayPlayer } from '@/stores/replayPlayer'

// The step shape carries the index close, not OHLC, so candles for the
// replay chart are built from consecutive closes.
function barsFromSteps(steps: ReplayStep[]): Bar[] {
  const bars: Bar[] = []
  let prev = steps[0]?.index ?? 0
  for (const s of steps) {
    const o = prev
    const c = s.index
    bars.push({ t: s.t, o, h: Math.max(o, c), l: Math.min(o, c), c, v: 0 })
    prev = c
  }
  return bars
}

function RunForm({ dates, onStarted }: { dates: string[]; onStarted: (id: string) => void }) {
  const { data: settings } = useSettings()
  const { data: experiments } = useExperiments(false)
  const run = useRunReplay()
  const [form, setForm] = useState<ReplayRunBody>({
    date: dates[0] ?? '',
    encoder: 'B',
    readout: 'reservoir',
    neural_ms: 200,
    lots: 1,
    stop_pct: 25,
    target_pct: 40,
    experiment_id: null,
  })
  useEffect(() => {
    if (settings) {
      setForm((f) => ({
        ...f,
        encoder: settings.neural.encoder,
        readout: settings.neural.readout,
        neural_ms: settings.neural.neural_ms,
        lots: settings.strategy.lots,
        stop_pct: settings.strategy.stop_pct,
        target_pct: settings.strategy.target_pct,
      }))
    }
  }, [settings])
  useEffect(() => {
    if (!form.date && dates[0]) setForm((f) => ({ ...f, date: dates[0] }))
  }, [dates, form.date])

  const submit = async () => {
    if (!form.date) {
      toast.error('Pick a date')
      return
    }
    try {
      const { id } = await run.mutateAsync(form)
      toast.success(`Replay ${id} started`)
      onStarted(id)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const num = (key: keyof ReplayRunBody, min: number, max: number, step = 1) => (
    <Input
      type="number"
      className="h-8"
      min={min}
      max={max}
      step={step}
      value={form[key] as number}
      onChange={(e) => setForm({ ...form, [key]: Number(e.target.value) })}
    />
  )

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <Label className="text-xs">Date</Label>
          <Select value={form.date} onValueChange={(v) => setForm({ ...form, date: v })}>
            <SelectTrigger size="sm" className="w-full">
              <SelectValue placeholder="Pick a date" />
            </SelectTrigger>
            <SelectContent>
              {dates.map((d) => (
                <SelectItem key={d} value={d}>
                  {formatDate(d)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Experiment (optional)</Label>
          <Select
            value={form.experiment_id ?? 'none'}
            onValueChange={(v) => setForm({ ...form, experiment_id: v === 'none' ? null : v })}
          >
            <SelectTrigger size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">None (fresh readout)</SelectItem>
              {(experiments?.experiments ?? [])
                .filter((e) => e.state === 'done')
                .map((e) => (
                  <SelectItem key={e.id} value={e.id}>
                    {e.name}
                  </SelectItem>
                ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Encoder</Label>
          <Select
            value={form.encoder}
            onValueChange={(v) => setForm({ ...form, encoder: v as Encoder })}
          >
            <SelectTrigger size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="A">A (chart image)</SelectItem>
              <SelectItem value="B">B (bar map)</SelectItem>
              <SelectItem value="C">C (bar map, premium)</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Readout</Label>
          <Select
            value={form.readout}
            onValueChange={(v) => setForm({ ...form, readout: v as Readout })}
          >
            <SelectTrigger size="sm" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="fixed">fixed decoder</SelectItem>
              <SelectItem value="reservoir">reservoir</SelectItem>
              <SelectItem value="plastic">plastic</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Neural ms</Label>
          {num('neural_ms', 50, 5000, 50)}
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Lots</Label>
          {num('lots', 1, 10)}
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Stop percent</Label>
          {num('stop_pct', 1, 100)}
        </div>
        <div className="space-y-1">
          <Label className="text-xs">Target percent</Label>
          {num('target_pct', 1, 99)}
        </div>
      </div>
      <Button size="sm" onClick={submit} disabled={run.isPending || !form.date}>
        {run.isPending ? 'Starting' : 'Run replay'}
      </Button>
    </div>
  )
}

export default function ReplayPage() {
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('id')
  const { data: dates } = useReplayDates()
  const { data: list } = useReplays()
  const { data: replay, isLoading } = useReplay(selectedId)
  const progress = useLastEvent<{ id: string; done: number; total: number }>('replay.progress')
  const index = useReplayPlayer((s) => s.index)
  const loadAndPlay = useReplayPlayer((s) => s.loadAndPlay)
  const setIndex = useReplayPlayer((s) => s.setIndex)

  const select = (id: string | null) => {
    const next = new URLSearchParams(params)
    if (id) next.set('id', id)
    else next.delete('id')
    setParams(next, { replace: true })
  }

  // Default to the newest finished replay.
  // biome-ignore lint/correctness/useExhaustiveDependencies: select is rebuilt every render from the search params
  useEffect(() => {
    if (!selectedId && list?.replays.length) {
      const done = list.replays.find((r) => r.state === 'done') ?? list.replays[0]
      select(done.id)
    }
  }, [list, selectedId])

  const steps = useMemo(() => (replay?.state === 'done' ? replay.steps : []), [replay])
  const finishedId = replay?.state === 'done' ? replay.id : null
  // A finished replay (picked from the list, the URL, or just run) starts
  // playing from the first step at the default speed.
  useEffect(() => {
    if (finishedId && steps.length) loadAndPlay(finishedId, steps.length, DEFAULT_SPEED)
  }, [finishedId, steps.length, loadAndPlay])

  const bars = useMemo(() => barsFromSteps(steps), [steps])
  const numbering = useMemo(() => numberStraddles(steps), [steps])
  const markers = useMemo(() => markersFromSteps(steps, numbering.perStep), [steps, numbering])
  const premium = useMemo(() => premiumFromSteps(steps), [steps])
  const step = steps[index]
  const levels = useMemo(() => levelsFromStraddle(step?.straddle), [step])
  const times = useMemo(() => steps.map((s) => s.t), [steps])
  const running = replay && replay.state !== 'done'
  const runningProgress =
    progress?.data && progress.data.id === replay?.id ? progress.data : replay?.progress

  return (
    <div className="space-y-4">
      <PageHeader
        title="Replay"
        description="Play a recorded or simulated day back one observation at a time, with the decision behind each step."
      />
      <div className="grid grid-cols-3 gap-4">
        <div className="col-span-2 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-sm">
                <span>
                  {replay ? `${formatDate(replay.date)} (${replay.id})` : 'No replay selected'}
                  {replay && (
                    <StatusBadge tone={toneFor(replay.state)} className="ml-2">
                      {replay.state}
                    </StatusBadge>
                  )}
                </span>
                {replay?.summary && (
                  <span className="text-xs font-normal text-muted-foreground">
                    P&L <Pnl value={replay.summary.pnl} />, {replay.summary.trades} straddle
                    {replay.summary.trades === 1 ? '' : 's'}
                  </span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {isLoading && <LoadingState text="Loading replay" />}
              {running && (
                <div className="text-sm text-muted-foreground">
                  Running:{' '}
                  {runningProgress
                    ? `${runningProgress.done} of ${runningProgress.total} steps`
                    : 'queued'}
                  .
                </div>
              )}
              <ChartLegendRow
                symbol="NIFTY"
                exchange="NSE_INDEX"
                interval="1m"
                strike={step?.straddle.strike ?? null}
                premiumSource={step?.premium_source ?? step?.straddle.premium_source ?? null}
                note={steps.length ? 'candles built from the step index closes' : undefined}
              />
              <PriceChart
                bars={bars}
                premium={premium}
                markers={markers}
                levels={levels}
                replayIndex={steps.length ? index : undefined}
                height={400}
                onMarkerClick={(id) => {
                  const m = /^step-(\d+)$/.exec(id)
                  if (m) setIndex(Number(m[1]))
                }}
                emptyText={running ? 'Replay in progress' : 'Pick a replay or run one'}
              />
              {steps.length > 0 && (
                <>
                  <Timeline steps={steps} index={index} onSeek={setIndex} />
                  <Transport times={times} />
                </>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Steps</CardTitle>
              <CardDescription>
                {steps.length} observations. Click a row to jump; the straddle column numbers the
                day's straddles.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {steps.length ? (
                <StepsTable steps={steps} index={index} onSelect={setIndex} />
              ) : (
                <EmptyState text="No steps to show." />
              )}
            </CardContent>
          </Card>
        </div>
        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Decision at this step</CardTitle>
            </CardHeader>
            <CardContent>
              {step ? (
                <DecisionPanel
                  step={step}
                  straddleNo={numbering.perStep[index]?.n || undefined}
                  title={`Step ${index + 1}`}
                />
              ) : (
                <EmptyState text="Select a replay to see its decisions." />
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Run a replay</CardTitle>
              <CardDescription>
                {dates ? `${dates.dates.length} dates from ${dates.source}` : 'Loading dates'}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <RunForm dates={dates?.dates ?? []} onStarted={(id) => select(id)} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Replays</CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {list?.replays.length ? (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-muted-foreground">
                      <th className="px-4 py-1.5 font-medium">Date</th>
                      <th className="px-2 py-1.5 font-medium">Config</th>
                      <th className="px-2 py-1.5 font-medium">State</th>
                      <th className="px-4 py-1.5 text-right font-medium">P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.replays.map((r) => {
                      const c = r.config as Partial<ReplayRunBody>
                      return (
                        <tr
                          key={r.id}
                          onClick={() => select(r.id)}
                          className={cn(
                            'cursor-pointer border-t hover:bg-accent/60',
                            r.id === selectedId && 'bg-accent'
                          )}
                        >
                          <td className="px-4 py-1.5">{formatDate(r.date)}</td>
                          <td className="px-2 py-1.5 text-muted-foreground">
                            {c.encoder ?? '?'} {c.readout ?? ''}{' '}
                            {c.neural_ms ? `${c.neural_ms} ms` : ''}
                          </td>
                          <td className="px-2 py-1.5">
                            <StatusBadge
                              tone={toneFor(r.state)}
                              className="px-1.5 py-0 text-[10px]"
                            >
                              {r.state}
                            </StatusBadge>
                          </td>
                          <td className="px-4 py-1.5 text-right">
                            {r.summary ? <Pnl value={r.summary.pnl} /> : '-'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              ) : (
                <div className="p-4 text-sm text-muted-foreground">No replays yet.</div>
              )}
            </CardContent>
          </Card>
          {replay?.summary && (
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">Summary</CardTitle>
              </CardHeader>
              <CardContent className="grid grid-cols-3 gap-3">
                <Stat label="P&L" value={<Pnl value={replay.summary.pnl} />} />
                <Stat label="Straddles" value={replay.summary.trades} />
                <Stat label="Stops" value={replay.summary.stop_hits ?? '-'} />
                <Stat label="Leg stops" value={replay.summary.leg_stop_hits ?? '-'} />
                <Stat label="Targets" value={replay.summary.target_hits ?? '-'} />
                <Stat label="Steps" value={fmtNum(steps.length, 0)} />
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
