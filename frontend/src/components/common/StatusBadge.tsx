import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

export type Tone = 'neutral' | 'good' | 'bad' | 'warn' | 'info' | 'muted'

const TONES: Record<Tone, string> = {
  neutral: 'border-border text-foreground',
  good: 'border-profit/40 bg-profit/10 text-profit',
  bad: 'border-loss/40 bg-loss/10 text-loss',
  warn: 'border-amber/50 bg-amber/10 text-amber',
  info: 'border-action-exit/40 bg-action-exit/10 text-action-exit',
  muted: 'border-transparent bg-muted text-muted-foreground',
}

export function StatusBadge({
  tone = 'neutral',
  children,
  className,
}: {
  tone?: Tone
  children: React.ReactNode
  className?: string
}) {
  return (
    <Badge variant="outline" className={cn('font-medium', TONES[tone], className)}>
      {children}
    </Badge>
  )
}

// Lifecycle tones shared by intents, legs, orders and worker states.
export function toneFor(status: string | null | undefined): Tone {
  const s = (status ?? '').toLowerCase()
  if (
    ['settled', 'complete', 'open', 'running', 'done', 'accepted', 'compiled', 'passed'].includes(s)
  )
    return 'good'
  if (['rejected', 'failed', 'halted', 'error', 'stopped_leg', 'cancelled'].includes(s))
    return 'bad'
  if (
    [
      'partial',
      'pending',
      'trigger pending',
      'starting',
      'running',
      'downloading',
      'compiling',
      'queued',
      'prepared',
    ].includes(s)
  )
    return 'warn'
  if (['unknown'].includes(s)) return 'bad'
  if (['stopped', 'closed', 'none', 'missing', 'flat'].includes(s)) return 'muted'
  return 'neutral'
}
