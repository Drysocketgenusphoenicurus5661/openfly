import type { Guard } from '@/api/types'
import { cn } from '@/lib/utils'

export function GuardChecklist({ guard, compact = false }: { guard: Guard; compact?: boolean }) {
  const passed = guard.checks.filter((c) => c.ok).length
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between text-xs">
        <span className="font-medium">Guard</span>
        <span className={cn('tabular', guard.allowed ? 'text-profit' : 'text-loss')}>
          {guard.allowed ? 'allowed' : 'rejected'}, {passed} of {guard.checks.length} checks passed
        </span>
      </div>
      {guard.checks.length === 0 ? (
        <div className="text-xs text-muted-foreground">No checks reported.</div>
      ) : (
        <ul className={cn('space-y-1', compact && 'space-y-0.5')} data-testid="guard-checklist">
          {guard.checks.map((check) => (
            <li
              key={check.name}
              className="grid grid-cols-[3.25rem_9rem_1fr] items-baseline gap-2 text-xs"
            >
              <span
                className={cn(
                  'rounded px-1 py-0.5 text-center font-semibold uppercase tracking-wide',
                  check.ok ? 'bg-profit/15 text-profit' : 'bg-loss/15 text-loss'
                )}
              >
                {check.ok ? 'pass' : 'fail'}
              </span>
              <span className="truncate font-medium">{check.name.replace(/_/g, ' ')}</span>
              <span className="text-muted-foreground">{check.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
