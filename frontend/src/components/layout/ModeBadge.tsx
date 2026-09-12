import type { Status } from '@/api/types'
import { cn } from '@/lib/utils'

export type Mode = 'live' | 'analyzer' | 'stopped' | 'unknown'

export function modeOf(status: Status | undefined): { mode: Mode; label: string } {
  if (!status) return { mode: 'unknown', label: 'Connecting' }
  const active =
    status.worker.state === 'running' ||
    status.worker.state === 'starting' ||
    status.worker.state === 'halted'
  if (active && status.worker.mode === 'live') return { mode: 'live', label: 'Live' }
  if (active && status.worker.mode === 'paper') return { mode: 'analyzer', label: 'Analyzer' }
  return { mode: 'stopped', label: 'Stopped' }
}

const STYLES: Record<Mode, string> = {
  live: 'bg-loss text-white border-loss',
  analyzer: 'bg-amber text-black border-amber',
  stopped: 'bg-muted text-muted-foreground border-border',
  unknown: 'bg-muted text-muted-foreground border-border',
}

export function ModeBadge({
  status,
  className,
}: {
  status: Status | undefined
  className?: string
}) {
  const { mode, label } = modeOf(status)
  const halted = status?.worker.state === 'halted'
  return (
    <span className={cn('inline-flex items-center gap-1.5', className)}>
      <span
        className={cn(
          'rounded-md border px-2.5 py-1 text-xs font-semibold uppercase tracking-wide',
          STYLES[mode]
        )}
        data-mode={mode}
        title={
          status
            ? `worker ${status.worker.state}${status.worker.mode ? `, ${status.worker.mode}` : ''}`
            : undefined
        }
      >
        {label}
      </span>
      {halted && (
        <span className="rounded-md border border-loss/50 bg-loss/10 px-1.5 py-0.5 text-[11px] font-medium text-loss">
          halted
        </span>
      )}
    </span>
  )
}
