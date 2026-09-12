import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export function Stat({
  label,
  value,
  sub,
  className,
  valueClassName,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  className?: string
  valueClassName?: string
}) {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={cn('tabular truncate text-base font-semibold leading-tight', valueClassName)}>
        {value}
      </div>
      {sub !== undefined && <div className="tabular text-xs text-muted-foreground">{sub}</div>}
    </div>
  )
}
