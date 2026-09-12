import type { PremiumSource } from '@/api/types'
import { PremiumSourceBadge } from '@/components/common/PremiumSourceBadge'
import { describeExpiry } from '@/lib/time'

// The line above the price chart: what is plotted, and where the premium
// values came from.
export function ChartLegendRow({
  symbol,
  exchange,
  interval,
  strike,
  expiry,
  premiumSource,
  expirySelection,
  note,
}: {
  symbol: string
  exchange: string
  interval: string
  strike?: number | null
  expiry?: string | null
  premiumSource?: PremiumSource | null
  expirySelection?: string | null
  note?: string
}) {
  return (
    <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
      <span>
        <span className="font-medium">{symbol}</span>{' '}
        <span className="text-muted-foreground">
          {exchange}, {interval} bars
        </span>
      </span>
      <span>
        <span className="text-muted-foreground">Straddle </span>
        <span className="tabular font-medium">
          {strike
            ? `${strike}${expiry ? ` ${describeExpiry(expiry, expirySelection)}` : ''}`
            : 'ATM, no position'}
        </span>
      </span>
      <PremiumSourceBadge source={premiumSource} className="px-1.5 py-0 text-[10px]" />
      {!premiumSource && <span className="text-muted-foreground">premium source not reported</span>}
      {note && <span className="ml-auto text-muted-foreground">{note}</span>}
    </div>
  )
}
