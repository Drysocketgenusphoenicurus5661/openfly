import { fmtSignedInr, pnlClass } from '@/lib/format'
import { cn } from '@/lib/utils'

export function Pnl({
  value,
  className,
}: {
  value: number | null | undefined
  className?: string
}) {
  return <span className={cn('tabular', pnlClass(value), className)}>{fmtSignedInr(value)}</span>
}
