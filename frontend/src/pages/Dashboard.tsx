import { useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useBars, useSettings, useStatus, useStraddle, useWorkerControls } from '@/api/hooks'
import type { WorkerMode } from '@/api/types'
import { ChartLegendRow } from '@/components/charts/ChartLegendRow'
import { PriceChart } from '@/components/charts/PriceChart'
import { ConfirmDialog } from '@/components/common/ConfirmDialog'
import { EmptyState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import { Pnl } from '@/components/common/Pnl'
import { Stat } from '@/components/common/Stat'
import { DecisionPanel } from '@/components/decision/DecisionPanel'
import { SessionClock } from '@/components/layout/SessionClock'
import { StraddleCard } from '@/components/straddle/StraddleCard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useLatestDecision, useObservationSteps } from '@/hooks/useLatestDecision'
import { numberStraddles } from '@/lib/actions'
import { levelsFromStraddle, markersFromSteps, premiumFromSteps } from '@/lib/chartData'
import { fmtNum } from '@/lib/format'
import { formatTime } from '@/lib/time'

type Pending = { kind: 'start'; mode: WorkerMode } | { kind: 'stop' } | { kind: 'squareoff' } | null

export default function DashboardPage() {
  const { data: status } = useStatus()
  const { data: settings } = useSettings()
  const { data: bars } = useBars(1)
  const { data: straddle } = useStraddle()
  const steps = useObservationSteps()
  const { step: latest, source } = useLatestDecision()
  const { start, stop, squareOff } = useWorkerControls()
  const [pending, setPending] = useState<Pending>(null)
  const [lots, setLots] = useState<number | null>(null)

  const lotsValue = lots ?? settings?.strategy.lots ?? 1
  const numbering = useMemo(() => numberStraddles(steps), [steps])
  const straddleNo = latest ? (numbering.perStep[steps.indexOf(latest)]?.n ?? undefined) : undefined
  const enteredToday = numbering.spans.length
  const markers = useMemo(() => markersFromSteps(steps, numbering.perStep), [steps, numbering])
  const premium = useMemo(() => premiumFromSteps(steps), [steps])
  const levels = useMemo(() => levelsFromStraddle(straddle ?? latest?.straddle), [straddle, latest])

  const worker = status?.worker.state ?? 'stopped'
  const stopped = worker === 'stopped'
  const canPaper =
    stopped &&
    !!status?.openalgo.reachable &&
    !!status?.openalgo.analyzer_mode &&
    !!status?.data.ready
  const canLive =
    stopped &&
    status?.live_allowed === true &&
    !!status?.openalgo.reachable &&
    !status?.openalgo.analyzer_mode
  const paperReason = !status
    ? 'status unknown'
    : !stopped
      ? `worker is ${worker}`
      : !status.openalgo.reachable
        ? 'OpenAlgo unreachable'
        : !status.openalgo.analyzer_mode
          ? 'analyzer is off'
          : !status.data.ready
            ? 'graph not ready'
            : 'start the worker in paper mode'
  const liveReason = !status
    ? 'status unknown'
    : status.live_allowed !== true
      ? 'needs OPENFLY_LIVE and a passed experiment'
      : status.openalgo.analyzer_mode
        ? 'analyzer is on; live needs it off'
        : !stopped
          ? `worker is ${worker}`
          : 'start the worker with real orders'

  const run = async () => {
    if (!pending) return
    try {
      if (pending.kind === 'start') {
        const r = await start.mutateAsync({ mode: pending.mode, lots: lotsValue, run_dir: null })
        toast.success(`Worker ${r.state} in ${pending.mode} mode (${r.run_dir})`)
      } else if (pending.kind === 'stop') {
        await stop.mutateAsync()
        toast.success('Worker stopped')
      } else {
        await squareOff.mutateAsync()
        toast.success('Square off sent')
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setPending(null)
    }
  }

  const dayPnl = latest?.pnl_day ?? straddle?.pnl ?? null
  const busy = start.isPending || stop.isPending || squareOff.isPending

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dashboard"
        description={
          status
            ? `${status.session.trading_date}${status.session.is_expiry_day ? ', expiry day' : ''}. Worker ${status.worker.state}${status.worker.mode ? ` (${status.worker.mode})` : ''}${status.worker.last_event_at ? `, last event ${formatTime(status.worker.last_event_at)}` : ''}.`
            : 'Waiting for status.'
        }
      />
      <div className="grid grid-cols-3 gap-4">
        <div className="col-span-2 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-sm">
                <span>Price and premium</span>
                <span className="text-xs font-normal text-muted-foreground">
                  {bars ? `${bars.bars.length} bars` : ''}
                  {latest ? `, last observation ${formatTime(latest.t)}` : ''}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ChartLegendRow
                symbol={bars?.symbol ?? 'NIFTY'}
                exchange={bars?.exchange ?? 'NSE_INDEX'}
                interval={bars?.interval ?? '1m'}
                strike={straddle?.in_position ? straddle.strike : (latest?.straddle.strike ?? null)}
                expiry={straddle?.in_position ? straddle.expiry : null}
                expirySelection={settings?.strategy.expiry_selection ?? null}
                premiumSource={
                  straddle?.in_position
                    ? straddle.premium_source
                    : (latest?.premium_source ?? latest?.straddle.premium_source)
                }
              />
              <PriceChart
                bars={bars?.bars ?? []}
                premium={premium}
                markers={markers}
                levels={levels}
                height={440}
              />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center justify-between text-sm">
                <span>Latest decision</span>
                <span className="text-xs font-normal text-muted-foreground">
                  {source === 'event'
                    ? 'from the event stream'
                    : source === 'composed'
                      ? 'composed from observation, prediction and guard events'
                      : ''}
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent>
              {latest ? (
                <DecisionPanel step={latest} straddleNo={straddleNo} title="Decision" />
              ) : (
                <EmptyState
                  text={
                    stopped
                      ? 'Worker stopped. Start it in paper mode to see decisions.'
                      : 'No observation yet. Waiting for the next bar.'
                  }
                />
              )}
            </CardContent>
          </Card>
        </div>
        <div className="space-y-4">
          <StraddleCard
            straddle={straddle}
            expirySelection={settings?.strategy.expiry_selection ?? null}
            straddleNo={
              straddle?.in_position
                ? straddleNo || enteredToday || undefined
                : enteredToday || undefined
            }
          />
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Day P&L</CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-3 gap-3">
              <Stat label="After costs" value={<Pnl value={dayPnl} className="text-xl" />} />
              <Stat label="Open" value={<Pnl value={straddle?.in_position ? straddle.pnl : 0} />} />
              <Stat
                label="Straddles"
                value={enteredToday}
                sub={
                  settings
                    ? `max ${settings.strategy.max_entries_per_day || 'unlimited'}`
                    : undefined
                }
              />
              {latest && (
                <>
                  <Stat label="NIFTY" value={fmtNum(latest.index, 1)} />
                  <Stat label="INDIAVIX" value={fmtNum(latest.vix, 2)} />
                  <Stat label="Premium" value={fmtNum(latest.premium, 1)} />
                </>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Session</CardTitle>
            </CardHeader>
            <CardContent>
              <SessionClock session={status?.session} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">Worker</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex items-end gap-3">
                <div className="space-y-1">
                  <Label htmlFor="lots" className="text-xs">
                    Lots
                  </Label>
                  <Input
                    id="lots"
                    type="number"
                    min={1}
                    max={settings?.risk.max_lots ?? 10}
                    className="h-8 w-20"
                    value={lotsValue}
                    onChange={(e) => setLots(Math.max(1, Number(e.target.value) || 1))}
                  />
                </div>
                <div className="text-xs text-muted-foreground">
                  {settings
                    ? `lot size ${settings.strategy.lot_size}, max ${settings.risk.max_lots} lots`
                    : ''}
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <Button
                  size="sm"
                  disabled={!canPaper || busy}
                  onClick={() => setPending({ kind: 'start', mode: 'paper' })}
                  title={paperReason}
                >
                  Start paper
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="border-loss/60 text-loss hover:bg-loss/10 hover:text-loss"
                  disabled={!canLive || busy}
                  onClick={() => setPending({ kind: 'start', mode: 'live' })}
                  title={liveReason}
                >
                  Start live
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={stopped || busy}
                  onClick={() => setPending({ kind: 'stop' })}
                >
                  Stop
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={!straddle?.in_position || busy}
                  onClick={() => setPending({ kind: 'squareoff' })}
                >
                  Square off
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Paper: {paperReason}. Live: {liveReason}.
              </p>
            </CardContent>
          </Card>
        </div>
      </div>

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => !open && setPending(null)}
        loading={busy}
        destructive={pending?.kind === 'start' && pending.mode === 'live'}
        title={
          pending?.kind === 'start'
            ? pending.mode === 'live'
              ? 'Start the worker with real orders?'
              : 'Start the worker in paper mode?'
            : pending?.kind === 'stop'
              ? 'Stop the worker?'
              : 'Square off the straddle?'
        }
        confirmLabel={
          pending?.kind === 'start'
            ? `Start ${pending.mode}`
            : pending?.kind === 'stop'
              ? 'Stop'
              : 'Square off'
        }
        onConfirm={run}
        description={
          pending?.kind === 'start' ? (
            pending.mode === 'live' ? (
              <p>
                Orders will reach the broker through OpenAlgo with {lotsValue} lot
                {lotsValue > 1 ? 's' : ''} per straddle. The preflight (funds, foreign orders,
                symbol master, expiry list) runs first and refuses if anything is off.
              </p>
            ) : (
              <p>
                The worker observes every {settings?.neural.live_interval ?? '1m'} bar and sends
                orders to the OpenAlgo analyzer sandbox with {lotsValue} lot
                {lotsValue > 1 ? 's' : ''} per straddle. Nothing reaches the broker.
              </p>
            )
          ) : pending?.kind === 'stop' ? (
            <p>
              Stops observations and orders. An open straddle stays open with its leg stops resting
              at the broker; use Square off to exit it.
            </p>
          ) : (
            <p>
              Sends the exit basket for both legs at market-ish limit prices and cancels the leg
              stops first. The worker keeps running and may re-enter if the guard and the readout
              allow.
            </p>
          )
        }
      />
    </div>
  )
}
