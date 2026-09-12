import type { ExpirySelection, Straddle } from '@/api/types'
import { Pnl } from '@/components/common/Pnl'
import { PremiumSourceBadge } from '@/components/common/PremiumSourceBadge'
import { Stat } from '@/components/common/Stat'
import { StatusBadge, toneFor } from '@/components/common/StatusBadge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { fmtNum } from '@/lib/format'
import { describeStopBasis } from '@/lib/stops'
import { formatExpiry, formatTime } from '@/lib/time'

export function StraddleCard({
  straddle,
  straddleNo,
  expirySelection,
}: {
  straddle: Straddle | undefined
  straddleNo?: number
  expirySelection?: ExpirySelection | null
}) {
  if (!straddle?.in_position) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">Current straddle</CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Flat. No straddle is open{straddleNo ? ` (${straddleNo} entered today)` : ''}.
        </CardContent>
      </Card>
    )
  }
  const stopDistance = straddle.stop_level - straddle.combined_ltp
  const targetDistance = straddle.combined_ltp - straddle.target_level
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-sm">
          <span>Current straddle{straddleNo ? ` (straddle ${straddleNo} today)` : ''}</span>
          <span className="flex items-center gap-2 text-xs font-normal text-muted-foreground">
            <PremiumSourceBadge
              source={straddle.premium_source}
              className="px-1.5 py-0 text-[10px]"
            />
            <span className="tabular">entered {formatTime(straddle.entered_at)}</span>
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-x-3 gap-y-2">
          <Stat label="Strike" value={straddle.strike} />
          <Stat
            label="Expiry"
            value={formatExpiry(straddle.expiry)}
            sub={expirySelection ?? undefined}
          />
          <Stat label="Lots" value={straddle.lots} />
          <Stat label="Credit" value={fmtNum(straddle.entry_credit, 1)} />
          <Stat label="Combined LTP" value={fmtNum(straddle.combined_ltp, 1)} />
          <Stat label="P&L" value={<Pnl value={straddle.pnl} />} />
          <Stat
            label="Stop"
            value={fmtNum(straddle.stop_level, 1)}
            sub={`${fmtNum(stopDistance, 1)} away`}
            valueClassName="text-action-stop"
          />
          <Stat
            label="Target"
            value={fmtNum(straddle.target_level, 1)}
            sub={`${fmtNum(targetDistance, 1)} away`}
            valueClassName="text-action-exit"
          />
          <Stat label="Square off" value={formatTime(straddle.square_off_at)} />
        </div>
        {straddle.stop_basis && (
          <p className="text-xs text-muted-foreground" data-testid="stop-basis">
            {describeStopBasis(straddle.stop_basis)}
          </p>
        )}
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Leg</th>
              <th className="py-1 text-right font-medium">Entry</th>
              <th className="py-1 text-right font-medium">LTP</th>
              <th className="py-1 text-right font-medium">Leg stop</th>
              <th className="py-1 text-right font-medium">Stop order</th>
              <th className="py-1 text-right font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {straddle.legs.map((leg) => (
              <tr key={leg.symbol} className="border-t">
                <td className="py-1 font-mono text-[11px]">{leg.symbol}</td>
                <td className="tabular py-1 text-right">{fmtNum(leg.entry_price, 2)}</td>
                <td className="tabular py-1 text-right">{fmtNum(leg.ltp, 2)}</td>
                <td className="tabular py-1 text-right text-action-stop">
                  {leg.stop_price != null ? fmtNum(leg.stop_price, 2) : '-'}
                </td>
                <td className="py-1 text-right">
                  <StatusBadge tone={toneFor(leg.stop_status)} className="px-1.5 py-0 text-[10px]">
                    {leg.stop_status ?? 'none'}
                  </StatusBadge>
                </td>
                <td className="py-1 text-right">
                  <StatusBadge
                    tone={leg.status === 'stopped' ? 'bad' : toneFor(leg.status)}
                    className="px-1.5 py-0 text-[10px]"
                  >
                    {leg.status ?? 'open'}
                  </StatusBadge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  )
}
