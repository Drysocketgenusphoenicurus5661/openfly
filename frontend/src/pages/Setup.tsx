import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { useLastEvent } from '@/api/events'
import {
  useDataPrepare,
  useDataStatus,
  useSettings,
  useStatus,
  useUpdateSettings,
} from '@/api/hooks'
import type { DataProgress } from '@/api/types'
import { PageHeader } from '@/components/common/PageHeader'
import { StatusBadge, toneFor } from '@/components/common/StatusBadge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { fmtBytes, fmtInt } from '@/lib/format'
import { cn } from '@/lib/utils'

function Check({ label, ok, detail }: { label: string; ok: boolean | null; detail: string }) {
  return (
    <li className="grid grid-cols-[4rem_10rem_1fr] items-baseline gap-2 text-sm">
      <span
        className={cn(
          'rounded px-1.5 py-0.5 text-center text-xs font-semibold uppercase tracking-wide',
          ok === null
            ? 'bg-muted text-muted-foreground'
            : ok
              ? 'bg-profit/15 text-profit'
              : 'bg-loss/15 text-loss'
        )}
      >
        {ok === null ? 'n/a' : ok ? 'ok' : 'fail'}
      </span>
      <span className="font-medium">{label}</span>
      <span className="text-muted-foreground">{detail}</span>
    </li>
  )
}

export default function SetupPage() {
  const { data: settings } = useSettings()
  const { data: status, refetch: refetchStatus } = useStatus()
  const { data: dataStatus, refetch: refetchData } = useDataStatus(5000)
  const update = useUpdateSettings()
  const prepare = useDataPrepare()
  const progressEvent = useLastEvent<DataProgress>('data.progress')
  const [form, setForm] = useState({ host: '', ws_url: '', api_key: '' })
  const [testing, setTesting] = useState(false)

  useEffect(() => {
    if (settings)
      setForm((f) => ({ ...f, host: settings.openalgo.host, ws_url: settings.openalgo.ws_url }))
  }, [settings])

  const save = async () => {
    const patch = {
      openalgo: {
        host: form.host.trim(),
        ws_url: form.ws_url.trim(),
        ...(form.api_key ? { api_key: form.api_key } : {}),
      },
    }
    await update.mutateAsync(patch)
    setForm((f) => ({ ...f, api_key: '' }))
  }

  const onSave = async () => {
    try {
      await save()
      toast.success('OpenAlgo settings saved')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const onTest = async () => {
    setTesting(true)
    try {
      await save()
      const s = await api.status()
      await refetchStatus()
      if (s.openalgo.reachable) {
        toast.success(
          `OpenAlgo reachable at ${s.openalgo.host}${s.openalgo.broker ? `, broker ${s.openalgo.broker}` : ''}, analyzer ${s.openalgo.analyzer_mode ? 'on' : 'off'}`
        )
      } else {
        toast.error(`OpenAlgo not reachable at ${s.openalgo.host}`)
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setTesting(false)
    }
  }

  const onPrepare = async () => {
    try {
      await prepare.mutateAsync()
      toast.success('Data pipeline started')
      window.setTimeout(() => refetchData(), 1000)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const progress: DataProgress | null =
    progressEvent?.data &&
    (dataStatus?.stage === 'downloading' ||
      dataStatus?.stage === 'compiling' ||
      progressEvent.data.stage !== 'compiled')
      ? progressEvent.data
      : (dataStatus?.progress ?? null)
  const busy =
    dataStatus?.stage === 'downloading' || dataStatus?.stage === 'compiling' || prepare.isPending
  const percent =
    progress?.done_bytes != null && progress?.total_bytes
      ? Math.min(100, (progress.done_bytes / progress.total_bytes) * 100)
      : null

  return (
    <div className="space-y-4">
      <PageHeader
        title="Setup"
        description="Connect OpenAlgo, prepare the connectome, and check the environment."
      />
      <div className="grid grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">OpenAlgo connection</CardTitle>
            <CardDescription>
              The API key is stored in data/openfly.db and is never read back.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="host">Host</Label>
              <Input
                id="host"
                value={form.host}
                onChange={(e) => setForm({ ...form, host: e.target.value })}
                placeholder="http://127.0.0.1:5000"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="ws">Websocket URL</Label>
              <Input
                id="ws"
                value={form.ws_url}
                onChange={(e) => setForm({ ...form, ws_url: e.target.value })}
                placeholder="ws://127.0.0.1:8765"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="key">
                API key{' '}
                {settings?.openalgo.api_key_set && (
                  <span className="ml-1 rounded bg-profit/15 px-1.5 py-0.5 text-[11px] font-medium text-profit">
                    configured
                  </span>
                )}
              </Label>
              <Input
                id="key"
                type="password"
                autoComplete="off"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder={
                  settings?.openalgo.api_key_set
                    ? 'Leave blank to keep the stored key'
                    : 'Paste the OpenAlgo API key'
                }
              />
            </div>
            <div className="flex gap-2 pt-1">
              <Button onClick={onSave} disabled={update.isPending || testing}>
                Save
              </Button>
              <Button variant="outline" onClick={onTest} disabled={update.isPending || testing}>
                {testing ? 'Testing' : 'Test connection'}
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Environment</CardTitle>
            <CardDescription>What GET /api/status reports.</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-2">
              <Check
                label="Python"
                ok={status ? Boolean(status.python) : null}
                detail={status?.python ?? 'unknown'}
              />
              <Check
                label="numba"
                ok={
                  status?.numba === undefined || status.numba === null
                    ? null
                    : Boolean(status.numba)
                }
                detail={
                  status?.numba === undefined || status?.numba === null
                    ? 'not reported by this backend'
                    : typeof status.numba === 'string'
                      ? status.numba
                      : status.numba
                        ? 'available'
                        : 'missing'
                }
              />
              <Check
                label="Graph present"
                ok={status ? status.data.ready : null}
                detail={
                  status?.data.ready
                    ? `${fmtInt(status.data.neurons)} neurons, ${fmtInt(status.data.edges)} edges at ${status.data.graph_path}`
                    : 'run Prepare data'
                }
              />
              <Check
                label="OpenAlgo reachable"
                ok={status ? status.openalgo.reachable : null}
                detail={
                  status
                    ? `${status.openalgo.host}${status.openalgo.broker ? `, broker ${status.openalgo.broker}` : ''}`
                    : 'unknown'
                }
              />
              <Check
                label="Analyzer mode"
                ok={status ? status.openalgo.analyzer_mode : null}
                detail={
                  status?.openalgo.analyzer_mode
                    ? 'on: orders go to the OpenAlgo sandbox'
                    : 'off: orders would reach the broker; paper mode refuses to start'
                }
              />
              <Check
                label="Worker"
                ok={status ? status.worker.state !== 'halted' : null}
                detail={
                  status
                    ? `${status.worker.state}${status.worker.mode ? `, ${status.worker.mode}` : ''}${status.worker.run_dir ? `, ${status.worker.run_dir}` : ''}`
                    : 'unknown'
                }
              />
            </ul>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            Connectome data
            {dataStatus && (
              <StatusBadge tone={toneFor(dataStatus.stage)}>{dataStatus.stage}</StatusBadge>
            )}
          </CardTitle>
          <CardDescription>
            MaleCNS v1.0 flat files, verified and compiled to a sparse graph (data/graph.npz).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {(busy || progress) && (
            <div className="space-y-1">
              <div className="flex justify-between text-xs text-muted-foreground">
                <span>
                  {progress?.stage ?? dataStatus?.stage}
                  {progress?.file ? `: ${progress.file}` : ''}
                  {progress?.message ? `: ${progress.message}` : ''}
                </span>
                <span className="tabular">
                  {percent !== null ? `${percent.toFixed(0)} percent` : ''}
                  {progress?.done_bytes != null && progress?.total_bytes
                    ? ` (${fmtBytes(progress.done_bytes)} of ${fmtBytes(progress.total_bytes)})`
                    : ''}
                </span>
              </div>
              <Progress
                value={percent ?? (busy ? 100 : 0)}
                className={cn(percent === null && busy && 'animate-pulse')}
              />
            </div>
          )}
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted-foreground">
                <th className="py-1 font-medium">File</th>
                <th className="py-1 text-right font-medium">Size</th>
                <th className="py-1 text-right font-medium">Present</th>
                <th className="py-1 text-right font-medium">Verified</th>
              </tr>
            </thead>
            <tbody>
              {(dataStatus?.files ?? []).map((f) => (
                <tr key={f.name} className="border-t">
                  <td className="py-1 font-mono text-[11px]">{f.name}</td>
                  <td className="tabular py-1 text-right">{fmtBytes(f.bytes)}</td>
                  <td className={cn('py-1 text-right', f.present ? 'text-profit' : 'text-loss')}>
                    {f.present ? 'yes' : 'no'}
                  </td>
                  <td
                    className={cn(
                      'py-1 text-right',
                      f.verified ? 'text-profit' : 'text-muted-foreground'
                    )}
                  >
                    {f.verified ? 'yes' : 'no'}
                  </td>
                </tr>
              ))}
              {dataStatus && (
                <tr className="border-t">
                  <td className="py-1 font-mono text-[11px]">graph.npz</td>
                  <td className="tabular py-1 text-right">
                    {dataStatus.graph.present
                      ? `${fmtInt(dataStatus.graph.neurons)} neurons, ${fmtInt(dataStatus.graph.edges)} edges`
                      : '-'}
                  </td>
                  <td
                    className={cn(
                      'py-1 text-right',
                      dataStatus.graph.present ? 'text-profit' : 'text-loss'
                    )}
                  >
                    {dataStatus.graph.present ? 'yes' : 'no'}
                  </td>
                  <td
                    className={cn(
                      'py-1 text-right',
                      dataStatus.graph.verified ? 'text-profit' : 'text-muted-foreground'
                    )}
                  >
                    {dataStatus.graph.verified ? 'yes' : 'no'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          {dataStatus?.error && <div className="text-sm text-loss">{dataStatus.error}</div>}
          <div>
            <Button onClick={onPrepare} disabled={busy}>
              {busy
                ? 'Preparing'
                : dataStatus?.stage === 'compiled'
                  ? 'Prepare data again'
                  : 'Prepare data'}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
