import { useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { useSetAnalyzer, useSettings, useStatus, useUpdateSettings } from '@/api/hooks'
import { LIVE_INTERVALS, type Settings } from '@/api/types'
import { ConfirmDialog } from '@/components/common/ConfirmDialog'
import { LoadingState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
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
import { Switch } from '@/components/ui/switch'
import { hmToMinutes, isValidHm } from '@/lib/time'
import { cn } from '@/lib/utils'

type Section = 'strategy' | 'risk' | 'neural' | 'costs'

interface Field {
  key: string
  label: string
  type: 'number' | 'text' | 'time' | 'select' | 'switch'
  help?: string
  min?: number
  max?: number
  step?: number
  options?: { value: string; label: string }[]
}

const FIELDS: Record<Section, Field[]> = {
  strategy: [
    { key: 'underlying', label: 'Underlying', type: 'text' },
    { key: 'lot_size', label: 'Lot size', type: 'number', min: 1, step: 1 },
    {
      key: 'lots',
      label: 'Lots',
      type: 'number',
      min: 1,
      step: 1,
      help: 'Capped by risk.max_lots and the margin check',
    },
    {
      key: 'product',
      label: 'Product',
      type: 'select',
      options: [
        { value: 'NRML', label: 'NRML' },
        { value: 'MIS', label: 'MIS' },
      ],
    },
    { key: 'trade_start', label: 'Trade start', type: 'time', help: 'HH:MM, first entry allowed' },
    {
      key: 'last_entry',
      label: 'Last entry',
      type: 'time',
      help: 'HH:MM, no new entries after this',
    },
    {
      key: 'square_off',
      label: 'Square off',
      type: 'time',
      help: 'HH:MM, the exit basket goes 30 seconds early',
    },
    {
      key: 'max_entries_per_day',
      label: 'Max entries per day',
      type: 'number',
      min: 0,
      step: 1,
      help: '0 means unlimited; straddles run one at a time',
    },
    {
      key: 'reentry_cooldown_minutes',
      label: 'Re-entry cooldown (minutes)',
      type: 'number',
      min: 0,
      step: 1,
      help: 'Wait after an exit before the next entry',
    },
    {
      key: 'vix_ceiling',
      label: 'INDIAVIX ceiling',
      type: 'number',
      min: 1,
      step: 0.5,
      help: 'The guard vetoes entries above this',
    },
    {
      key: 'min_days_to_expiry',
      label: 'Min days to expiry',
      type: 'number',
      min: 0,
      step: 1,
      help: '0 trades the expiring contract on expiry day',
    },
    {
      key: 'leg_stop_pct',
      label: 'Leg stop percent',
      type: 'number',
      min: 1,
      max: 200,
      step: 1,
      help: 'Fixed stop per leg, placed at the broker as SL-M and re-placed if it goes missing; never trailed',
    },
    {
      key: 'leg_stop_mode',
      label: 'Leg stop mode',
      type: 'select',
      options: [
        { value: 'broker', label: 'broker (real SL-M orders)' },
        { value: 'software', label: 'software (worker watches the leg price)' },
      ],
      help: 'Broker stops survive a worker crash; software stops do not',
    },
    {
      key: 'on_leg_stop',
      label: 'When one leg stops',
      type: 'select',
      options: [
        { value: 'hold_other', label: 'hold the other leg with its own stop' },
        { value: 'exit_both', label: 'exit both legs' },
      ],
    },
    {
      key: 'combined_stop_enabled',
      label: 'Combined stop, target and lock',
      type: 'switch',
      help: 'Software rules on the summed premium, in addition to the leg stops',
    },
    {
      key: 'stop_pct',
      label: 'Combined stop percent',
      type: 'number',
      min: 1,
      max: 100,
      step: 1,
      help: 'Exit both legs when the summed premium rises this much above the credit',
    },
    {
      key: 'target_pct',
      label: 'Combined target percent',
      type: 'number',
      min: 1,
      max: 99,
      step: 1,
      help: 'Exit both legs when the summed premium falls this much below the credit',
    },
    {
      key: 'lock_after_pct',
      label: 'Lock after percent',
      type: 'number',
      min: 0,
      max: 99,
      step: 1,
      help: 'After this much decay, move the combined stop to the entry credit',
    },
  ],
  risk: [
    {
      key: 'daily_loss_limit_pct',
      label: 'Daily loss limit (percent of capital)',
      type: 'number',
      min: 0.1,
      max: 100,
      step: 0.1,
    },
    {
      key: 'risk_budget_pct',
      label: 'Risk budget per straddle (percent of capital)',
      type: 'number',
      min: 0.1,
      max: 100,
      step: 0.1,
      help: 'lots = floor(budget / (credit x stop percent x lot size))',
    },
    { key: 'max_lots', label: 'Max lots', type: 'number', min: 1, step: 1 },
    {
      key: 'spread_pct_max',
      label: 'Max spread (percent of premium)',
      type: 'number',
      min: 0.01,
      step: 0.05,
      help: 'Either leg wider than this vetoes',
    },
    { key: 'quote_max_age_s', label: 'Max quote age (seconds)', type: 'number', min: 1, step: 1 },
    {
      key: 'index_move_veto_pct',
      label: 'Index move veto (percent)',
      type: 'number',
      min: 0.01,
      step: 0.05,
      help: 'Index moved more than this since the observation vetoes',
    },
  ],
  neural: [
    {
      key: 'neural_ms',
      label: 'Neural ms per observation',
      type: 'number',
      min: 50,
      max: 5000,
      step: 50,
      help: 'Simulated time the fly gets to see each stimulus',
    },
    {
      key: 'live_interval',
      label: 'Live observation cadence',
      type: 'select',
      options: LIVE_INTERVALS.map((v) => ({ value: v, label: v })),
      help: 'How often the fly observes in the live worker; replays and simulations use 1 minute bars',
    },
    {
      key: 'encoder',
      label: 'Encoder',
      type: 'select',
      options: [
        { value: 'A', label: 'A (chart image)' },
        { value: 'B', label: 'B (bar map)' },
        { value: 'C', label: 'C (bar map with premium)' },
      ],
    },
    {
      key: 'readout',
      label: 'Readout',
      type: 'select',
      options: [
        { value: 'fixed', label: 'fixed decoder (control)' },
        { value: 'reservoir', label: 'reservoir (primary)' },
        { value: 'plastic', label: 'plastic (experiment)' },
      ],
    },
    {
      key: 'plastic',
      label: 'Plastic KC to MBON edges',
      type: 'switch',
      help: 'Dopamine-gated learning; always compared against a frozen twin',
    },
  ],
  costs: [
    {
      key: 'brokerage_per_order',
      label: 'Brokerage per order (INR, cap)',
      type: 'number',
      min: 0,
      step: 1,
    },
    { key: 'brokerage_pct', label: 'Brokerage percent', type: 'number', min: 0, step: 0.01 },
    {
      key: 'stt_sell_pct',
      label: 'STT on sold premium (percent)',
      type: 'number',
      min: 0,
      step: 0.01,
    },
    {
      key: 'exchange_pct',
      label: 'Exchange charges (percent)',
      type: 'number',
      min: 0,
      step: 0.00001,
    },
    { key: 'sebi_pct', label: 'SEBI charges (percent)', type: 'number', min: 0, step: 0.0001 },
    {
      key: 'stamp_buy_pct',
      label: 'Stamp duty on bought premium (percent)',
      type: 'number',
      min: 0,
      step: 0.001,
    },
    { key: 'gst_pct', label: 'GST (percent)', type: 'number', min: 0, step: 1 },
  ],
}

const TITLES: Record<Section, { title: string; description: string }> = {
  strategy: {
    title: 'Strategy',
    description: 'The short straddle: entries, exits, per-leg stops and the combined rules.',
  },
  risk: { title: 'Risk', description: 'Sizing and the reject-only guard thresholds.' },
  neural: { title: 'Neural', description: 'What the fly sees and how it is read out.' },
  costs: {
    title: 'Costs',
    description: 'One formula for backtests, the paper ledger and the expected-cost check.',
  },
}

type Form = Pick<Settings, Section>

function validate(form: Form): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const section of Object.keys(FIELDS) as Section[]) {
    for (const field of FIELDS[section]) {
      const value = (form[section] as unknown as Record<string, unknown>)[field.key]
      const id = `${section}.${field.key}`
      if (field.type === 'number') {
        const n = Number(value)
        if (!Number.isFinite(n)) errors[id] = 'Enter a number'
        else if (field.min !== undefined && n < field.min) errors[id] = `Minimum ${field.min}`
        else if (field.max !== undefined && n > field.max) errors[id] = `Maximum ${field.max}`
      } else if (field.type === 'time') {
        if (!isValidHm(String(value))) errors[id] = 'Use HH:MM'
      } else if (field.type === 'text') {
        if (!String(value).trim()) errors[id] = 'Required'
      }
    }
  }
  const s = form.strategy
  if (
    isValidHm(s.trade_start) &&
    isValidHm(s.last_entry) &&
    hmToMinutes(s.trade_start) >= hmToMinutes(s.last_entry)
  ) {
    errors['strategy.last_entry'] = 'Must be after trade start'
  }
  if (
    isValidHm(s.last_entry) &&
    isValidHm(s.square_off) &&
    hmToMinutes(s.last_entry) >= hmToMinutes(s.square_off)
  ) {
    errors['strategy.square_off'] = 'Must be after last entry'
  }
  if (isValidHm(s.square_off) && hmToMinutes(s.square_off) > hmToMinutes('15:30')) {
    errors['strategy.square_off'] = 'Must be at or before 15:30'
  }
  if (Number(s.lots) > Number(form.risk.max_lots))
    errors['strategy.lots'] = `Above risk.max_lots (${form.risk.max_lots})`
  if (Number(s.lock_after_pct) >= Number(s.target_pct))
    errors['strategy.lock_after_pct'] = 'Must be below the target percent'
  return errors
}

function FieldInput({
  section,
  field,
  value,
  error,
  onChange,
}: {
  section: Section
  field: Field
  value: unknown
  error?: string
  onChange: (value: unknown) => void
}) {
  const id = `${section}.${field.key}`
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id} className="text-xs">
          {field.label}
        </Label>
        {field.type === 'switch' && (
          <Switch id={id} checked={Boolean(value)} onCheckedChange={(v) => onChange(v)} />
        )}
      </div>
      {field.type === 'select' && (
        <Select value={String(value)} onValueChange={(v) => onChange(v)}>
          <SelectTrigger id={id} size="sm" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {field.options?.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {(field.type === 'number' || field.type === 'text' || field.type === 'time') && (
        <Input
          id={id}
          className={cn('h-8', error && 'border-loss')}
          type={field.type === 'number' ? 'number' : 'text'}
          min={field.min}
          max={field.max}
          step={field.step}
          value={String(value ?? '')}
          onChange={(e) =>
            onChange(
              field.type === 'number'
                ? e.target.value === ''
                  ? ''
                  : Number(e.target.value)
                : e.target.value
            )
          }
          aria-invalid={!!error}
        />
      )}
      {error ? (
        <p className="text-[11px] text-loss">{error}</p>
      ) : field.help ? (
        <p className="text-[11px] text-muted-foreground">{field.help}</p>
      ) : null}
    </div>
  )
}

export default function SettingsPage() {
  const { data: settings } = useSettings()
  const { data: status } = useStatus()
  const update = useUpdateSettings()
  const setAnalyzer = useSetAnalyzer()
  const [form, setForm] = useState<Form | null>(null)
  const [dirty, setDirty] = useState(false)
  const [analyzerPending, setAnalyzerPending] = useState<boolean | null>(null)

  useEffect(() => {
    if (settings && !dirty) {
      setForm({
        strategy: settings.strategy,
        risk: settings.risk,
        neural: settings.neural,
        costs: settings.costs,
      })
    }
  }, [settings, dirty])

  const errors = useMemo(() => (form ? validate(form) : {}), [form])
  const errorCount = Object.keys(errors).length

  const change = (section: Section, key: string, value: unknown) => {
    setForm((f) => (f ? { ...f, [section]: { ...f[section], [key]: value } } : f))
    setDirty(true)
  }

  const save = async () => {
    if (!form || errorCount) return
    try {
      await update.mutateAsync(form)
      setDirty(false)
      toast.success('Settings saved')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    }
  }

  const toggleAnalyzer = async () => {
    if (analyzerPending === null) return
    try {
      const r = await setAnalyzer.mutateAsync(analyzerPending)
      toast.success(`OpenAlgo analyzer ${r.analyzer_mode ? 'on' : 'off'}`)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error))
    } finally {
      setAnalyzerPending(null)
    }
  }

  if (!form) return <LoadingState text="Loading settings" />
  const analyzerOn = status?.openalgo.analyzer_mode ?? false

  return (
    <div className="space-y-4">
      <PageHeader
        title="Settings"
        description="Stored in data/openfly.db. The worker reads them at start; a running worker keeps the values it started with."
        actions={
          <>
            {dirty && (
              <Button
                variant="outline"
                onClick={() => {
                  setDirty(false)
                  if (settings)
                    setForm({
                      strategy: settings.strategy,
                      risk: settings.risk,
                      neural: settings.neural,
                      costs: settings.costs,
                    })
                }}
              >
                Discard
              </Button>
            )}
            <Button onClick={save} disabled={!dirty || errorCount > 0 || update.isPending}>
              {update.isPending
                ? 'Saving'
                : errorCount
                  ? `${errorCount} error${errorCount > 1 ? 's' : ''}`
                  : 'Save'}
            </Button>
          </>
        }
      />

      <Card className={cn(analyzerOn ? 'border-amber/50' : 'border-loss/50')}>
        <CardHeader>
          <CardTitle className="flex items-center justify-between text-sm">
            <span>OpenAlgo analyzer mode</span>
            <div className="flex items-center gap-2">
              <span className={cn('text-xs font-medium', analyzerOn ? 'text-amber' : 'text-loss')}>
                {analyzerOn ? 'on (paper)' : 'off (live orders)'}
              </span>
              <Switch
                checked={analyzerOn}
                onCheckedChange={(v) => setAnalyzerPending(v)}
                disabled={!status || setAnalyzer.isPending}
              />
            </div>
          </CardTitle>
          <CardDescription>
            Global to the OpenAlgo installation: every strategy and client connected to this
            OpenAlgo instance switches with it. Paper mode refuses to start while it is off.
          </CardDescription>
        </CardHeader>
      </Card>

      <div className="grid grid-cols-2 gap-4">
        {(Object.keys(FIELDS) as Section[]).map((section) => (
          <Card key={section}>
            <CardHeader>
              <CardTitle className="text-sm">{TITLES[section].title}</CardTitle>
              <CardDescription>{TITLES[section].description}</CardDescription>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-x-4 gap-y-3">
              {FIELDS[section].map((field) => (
                <FieldInput
                  key={field.key}
                  section={section}
                  field={field}
                  value={(form[section] as unknown as Record<string, unknown>)[field.key]}
                  error={errors[`${section}.${field.key}`]}
                  onChange={(v) => change(section, field.key, v)}
                />
              ))}
            </CardContent>
          </Card>
        ))}
      </div>

      <ConfirmDialog
        open={analyzerPending !== null}
        onOpenChange={(open) => !open && setAnalyzerPending(null)}
        title={
          analyzerPending ? 'Turn the OpenAlgo analyzer on?' : 'Turn the OpenAlgo analyzer off?'
        }
        confirmLabel={analyzerPending ? 'Turn on' : 'Turn off'}
        destructive={analyzerPending === false}
        loading={setAnalyzer.isPending}
        onConfirm={toggleAnalyzer}
        description={
          analyzerPending ? (
            <p>
              Orders from every strategy on this OpenAlgo installation, not only OpenFly, will go to
              the sandbox instead of the broker until it is turned off again.
            </p>
          ) : (
            <p>
              Orders from every strategy on this OpenAlgo installation, not only OpenFly, will reach
              the broker. OpenFly's paper worker refuses to start with the analyzer off; live mode
              still needs OPENFLY_LIVE and a passed experiment.
            </p>
          )
        }
      />
    </div>
  )
}
