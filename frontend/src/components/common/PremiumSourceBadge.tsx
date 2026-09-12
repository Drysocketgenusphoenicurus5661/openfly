import type { PremiumSource } from '@/api/types'
import { StatusBadge } from './StatusBadge'

// Where the straddle premium came from: recorded quotes or a synthetic
// (Black-Scholes) reconstruction.
export function PremiumSourceBadge({
  source,
  className,
}: {
  source: PremiumSource | null | undefined
  className?: string
}) {
  if (!source) return null
  return (
    <StatusBadge
      tone={source === 'recorded' ? 'good' : 'warn'}
      className={className}
      data-testid="premium-source"
    >
      {source === 'recorded' ? 'recorded premium' : 'synthetic premium'}
    </StatusBadge>
  )
}
