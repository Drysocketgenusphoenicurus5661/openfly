import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { toast } from 'sonner'
import { useLastEvent } from '@/api/events'
import { useCreateExperiment, useExperiment, useExperiments } from '@/api/hooks'
import type { Encoder, ExperimentConfig, ExperimentMetrics, Readout } from '@/api/types'
import { CurveChart, type CurveSeries } from '@/components/charts/CurveChart'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/EmptyState'
import { KeyValueGrid } from '@/components/common/KeyValueGrid'
import { PageHeader } from '@/components/common/PageHeader'
import { Pnl } from '@/components/common/Pnl'
import { StatusBadge, toneFor } from '@/components/common/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { fmtInr, fmtNum, fmtPctShort } from '@/lib/format'
import { formatDateTime } from '@/lib/time'
import { cn } from '@/lib/utils'

const CONTROL_LABELS: Record<string, string> = {
  strategy: 'Strategy',
  fixed_0920: 'Fixed 09:20 entry',
  random_entry: 'Random entry',
  shuffled: 'Shuffled labels',
  flat: 'Flat',
}

function MetricsTable({
  rows,
}: {
  rows: { label: string; metrics: ExperimentMetrics | undefined; primary?: boolean }[]
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th className="py-1.5 pr-2 font-medium">Series</th>
            <th className="py-1.5 pr-2 text-right font-medium">Net P&L per lot</th>
            <th className="py-1.5 pr-2 text-right font-medium">Sharpe</th>
            <th className="py-1.5 pr-2 text-right font-medium">Max drawdown</th>
            <th className="py-1.5 pr-2 text-right font-medium">Trades</th>
            <th className="py-1.5 pr-2 text-right font-medium">Stops</th>
            <th className="py-1.5 pr-2 text-right font-medium">Targets</th>
            <th className="py-1.5 pr-2 text-right font-medium">Accuracy</th>
            <th className="py-1.5 text-right font-medium">95 percent CI</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label} className={cn('border-t', row.primary && 'font-medium')}>
              <td className="py-1.5 pr-2">{row.label}</td>
              <td className="py-1.5 pr-2 text-right">
                {row.metrics ? <Pnl value={row.metrics.net_pnl_per_lot} /> : '-'}
              </td>
              <td className="tabular py-1.5 pr-2 text-right">{fmtNum(row.metrics?.sharpe, 2)}</td>
              <td className="tabular py-1.5 pr-2 text-right">
                {row.metrics ? fmtInr(row.metrics.max_drawdown) : '-'}
              </td>
              <td className="tabular py-1.5 pr-2 text-right">{row.metrics?.trades ?? '-'}</td>
              <td className="tabular py-1.5 pr-2 text-right">{row.metrics?.stop_hits ?? '-'}</td>
              <td className="tabular py-1.5 pr-2 text-right">{row.metrics?.target_hits ?? '-'}</td>
              <td className="tabular py-1.5 pr-2 text-right">
                {row.metrics ? fmtPctShort(row.metrics.accuracy * 100, 1) : '-'}
              </td>
              <td className="tabular py-1.5 text-right">
                {row.metrics
                  ? `${fmtPctShort(row.metrics.accuracy_ci[0] * 100, 1)} to ${fmtPctShort(row.metrics.accuracy_ci[1] * 100, 1)}`
                  : '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function NewExperimentDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const create = useCreateExperiment()
  const navigate = useNavigate()
  const [form, setForm] = useState<ExperimentConfig>({
    name: '',
    encoder: 'B',
    readout: 'reservoir',
    plastic: false,
    neural_ms: 200,
    train: ['2025-08-08', '2026-03-31'],
    validation: ['2026-04-01', '2026-06-30'],
    test: ['2026-07-01', '2026-09-11'],
  })
  const dateOk = (d: string) => /^\d{4}-\d{2}-\d{2}$/.test(d)
  const valid =
    form.neural_ms >= 50 &&
    [...form.train, ...form.validation, ...form.test].every(dateOk) &&
    form.train[1] < form.validation[0] &&
    form.validation[1] < form.test[0]

  const submit = async () => {
    try {
      const { id } = await create.mutateAsync({ ...form, name: form.name || undefined })
      toast.success(`Experiment ${id} queued`)
      onOpenChange(false)
      navigate(`/experiments/${encodeURIComponent(id)}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const range = (key: 'train' | 'validation' | 'test', label: string) => (
    <div className="grid grid-cols-[6rem_1fr_1fr] items-center gap-2">
      <Label className="text-xs">{label}</Label>
      <Input
        className="h-8"
        value={form[key][0]}
        onChange={(e) => setForm({ ...form, [key]: [e.target.value, form[key][1]] })}
        placeholder="YYYY-MM-DD"
      />
      <Input
        className="h-8"
        value={form[key][1]}
        onChange={(e) => setForm({ ...form, [key]: [form[key][0], e.target.value] })}
        placeholder="YYYY-MM-DD"
      />
    </div>
  )

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>New experiment</DialogTitle>
          <DialogDescription>
            Fit the readout on the train window, choose regularization on validation, and judge once
            on the untouched test window.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label className="text-xs">Name</Label>
            <Input
              className="h-8"
              value={form.name ?? ''}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="encoder B, reservoir, frozen"
            />
          </div>
          <div className="grid grid-cols-3 gap-2">
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
                  <SelectItem value="A">A</SelectItem>
                  <SelectItem value="B">B</SelectItem>
                  <SelectItem value="C">C</SelectItem>
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
                  <SelectItem value="fixed">fixed</SelectItem>
                  <SelectItem value="reservoir">reservoir</SelectItem>
                  <SelectItem value="plastic">plastic</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Neural ms</Label>
              <Input
                className="h-8"
                type="number"
                min={50}
                step={50}
                value={form.neural_ms}
                onChange={(e) => setForm({ ...form, neural_ms: Number(e.target.value) })}
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Switch
              checked={form.plastic}
              onCheckedChange={(v) => setForm({ ...form, plastic: v })}
              id="plastic"
            />
            <Label htmlFor="plastic" className="text-xs">
              Plastic arm (always run against a frozen twin)
            </Label>
          </div>
          {range('train', 'Train')}
          {range('validation', 'Validation')}
          {range('test', 'Test')}
          {!valid && (
            <p className="text-xs text-loss">
              Dates must be YYYY-MM-DD and the windows must not overlap, in order train, validation,
              test.
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={!valid || create.isPending}>
            {create.isPending ? 'Starting' : 'Start experiment'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ExperimentsListPage() {
  const { data, isLoading, error } = useExperiments()
  const progress = useLastEvent<{ id: string; done: number; total: number }>('experiment.progress')
  const [open, setOpen] = useState(false)

  return (
    <div className="space-y-4">
      <PageHeader
        title="Experiments"
        description="Does the network carry information about NIFTY's next hour? Each run answers with a pass or a fail against the Section 8 criterion."
        actions={<Button onClick={() => setOpen(true)}>New experiment</Button>}
      />
      <Card>
        <CardContent className="p-0">
          {isLoading && <LoadingState />}
          {error && <ErrorState error={error} className="m-4" />}
          {data && data.experiments.length === 0 && (
            <EmptyState text="No experiments yet." className="m-4" />
          )}
          {data && data.experiments.length > 0 && (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-muted-foreground">
                  <th className="px-4 py-2 font-medium">Name</th>
                  <th className="px-2 py-2 font-medium">State</th>
                  <th className="px-2 py-2 font-medium">Progress</th>
                  <th className="px-2 py-2 font-medium">Encoder</th>
                  <th className="px-2 py-2 font-medium">Readout</th>
                  <th className="px-2 py-2 text-right font-medium">Neural ms</th>
                  <th className="px-2 py-2 font-medium">Test window</th>
                  <th className="px-4 py-2 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {data.experiments.map((e) => {
                  const p = progress?.data && progress.data.id === e.id ? progress.data : e.progress
                  const pct =
                    p && p.total > 0 ? (p.done / p.total) * 100 : e.state === 'done' ? 100 : 0
                  return (
                    <tr key={e.id} className="border-t hover:bg-accent/60">
                      <td className="px-4 py-2">
                        <Link
                          to={`/experiments/${encodeURIComponent(e.id)}`}
                          className="font-medium hover:underline"
                        >
                          {e.name}
                        </Link>
                        <div className="font-mono text-[11px] text-muted-foreground">{e.id}</div>
                      </td>
                      <td className="px-2 py-2">
                        <StatusBadge tone={toneFor(e.state)}>{e.state}</StatusBadge>
                      </td>
                      <td className="w-40 px-2 py-2">
                        <Progress value={pct} className="h-1.5" />
                        <div className="tabular mt-0.5 text-[11px] text-muted-foreground">
                          {p ? `${p.done} of ${p.total}` : ''}
                        </div>
                      </td>
                      <td className="px-2 py-2">{e.config.encoder}</td>
                      <td className="px-2 py-2">
                        {e.config.readout}
                        {e.config.plastic ? ' (plastic)' : ''}
                      </td>
                      <td className="tabular px-2 py-2 text-right">{e.config.neural_ms}</td>
                      <td className="tabular px-2 py-2 text-xs text-muted-foreground">
                        {e.config.test[0]} to {e.config.test[1]}
                      </td>
                      <td className="tabular px-4 py-2 text-xs text-muted-foreground">
                        {formatDateTime(e.created_at)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
      <NewExperimentDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}

export function ExperimentDetailPage() {
  const { id } = useParams()
  const { data: experiment, isLoading, error } = useExperiment(id ?? null)
  const progress = useLastEvent<{ id: string; done: number; total: number }>('experiment.progress')

  if (isLoading) return <LoadingState text="Loading experiment" />
  if (error) return <ErrorState error={error} />
  if (!experiment) return <EmptyState text="Experiment not found." />

  const test = experiment.curves.test
  const series: CurveSeries[] = test
    ? (['strategy', 'fixed_0920', 'random_entry', 'shuffled', 'flat'] as const)
        .filter((key) => Array.isArray(test[key]))
        .map((key) => ({
          key,
          label: CONTROL_LABELS[key],
          values: test[key] as number[],
          dashed: key === 'flat',
        }))
    : []
  const windows = (['train', 'validation', 'test'] as const).filter((w) => experiment.metrics[w])
  const p =
    progress?.data && progress.data.id === experiment.id ? progress.data : experiment.progress
  const verdictTone =
    experiment.passed === true ? 'good' : experiment.passed === false ? 'bad' : 'warn'

  return (
    <div className="space-y-4">
      <PageHeader
        title={experiment.name ?? experiment.id}
        description={
          <span>
            <Link to="/experiments" className="hover:underline">
              Experiments
            </Link>{' '}
            / <span className="font-mono text-xs">{experiment.id}</span>
            {experiment.created_at ? `, created ${formatDateTime(experiment.created_at)}` : ''}
          </span>
        }
        actions={<StatusBadge tone={toneFor(experiment.state)}>{experiment.state}</StatusBadge>}
      />

      <div
        className={cn(
          'rounded-md border p-4',
          verdictTone === 'good' && 'border-profit/40 bg-profit/10',
          verdictTone === 'bad' && 'border-loss/40 bg-loss/10',
          verdictTone === 'warn' && 'border-amber/40 bg-amber/10'
        )}
      >
        <div className="text-sm font-semibold">
          {experiment.passed === true
            ? 'Passed'
            : experiment.passed === false
              ? 'No edge found'
              : experiment.state === 'done'
                ? 'No verdict'
                : 'Running'}
        </div>
        <div className="mt-1 text-sm">
          {experiment.verdict ||
            (p
              ? `${p.done} of ${p.total} observations simulated`
              : 'The verdict is written when the test window completes.')}
        </div>
        {experiment.state !== 'done' && p && (
          <Progress value={(p.done / Math.max(1, p.total)) * 100} className="mt-3 h-1.5" />
        )}
      </div>

      <div className="grid grid-cols-3 gap-4">
        <Card className="col-span-2">
          <CardHeader>
            <CardTitle className="text-sm">Cumulative P&L per lot on the test window</CardTitle>
            <CardDescription>
              Strategy against the fixed 09:20 entry, random entry with the same trade count,
              shuffled labels, and flat.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {series.length && test ? (
              <CurveChart dates={test.t} series={series} />
            ) : (
              <EmptyState text="Curves appear when the test window completes." />
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Configuration</CardTitle>
          </CardHeader>
          <CardContent>
            <KeyValueGrid
              data={{
                encoder: experiment.config.encoder,
                readout: experiment.config.readout,
                plastic: experiment.config.plastic,
                neural_ms: experiment.config.neural_ms,
                train: experiment.config.train.join(' to '),
                validation: experiment.config.validation.join(' to '),
                test: experiment.config.test.join(' to '),
              }}
            />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Metrics per window</CardTitle>
        </CardHeader>
        <CardContent>
          {windows.length ? (
            <MetricsTable
              rows={windows.map((w) => ({
                label: w,
                metrics: experiment.metrics[w],
                primary: w === 'test',
              }))}
            />
          ) : (
            <EmptyState text="No metrics yet." />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Controls on the test window</CardTitle>
          <CardDescription>
            The strategy must beat the fixed 09:20 straddle and the random entry control after
            measured costs.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {experiment.metrics.test ? (
            <MetricsTable
              rows={[
                { label: CONTROL_LABELS.strategy, metrics: experiment.metrics.test, primary: true },
                ...(['fixed_0920', 'random_entry', 'shuffled', 'flat'] as const).map((key) => ({
                  label: CONTROL_LABELS[key],
                  metrics: experiment.controls[key],
                })),
              ]}
            />
          ) : (
            <EmptyState text="Controls appear with the test metrics." />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
