import { useIntents, useOrders, usePositions } from '@/api/hooks'
import { EmptyState, ErrorState, LoadingState } from '@/components/common/EmptyState'
import { PageHeader } from '@/components/common/PageHeader'
import { Pnl } from '@/components/common/Pnl'
import { StatusBadge, toneFor } from '@/components/common/StatusBadge'
import { Card, CardContent } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fmtNum, toNumber } from '@/lib/format'
import { formatDateTime, formatTime } from '@/lib/time'
import { cn } from '@/lib/utils'

const INTENT_LIFECYCLE = ['PREPARED', 'UNKNOWN', 'ACCEPTED', 'PARTIAL', 'SETTLED', 'REJECTED']

function Lifecycle({ status }: { status: string }) {
  return (
    <div className="flex items-center gap-1">
      {INTENT_LIFECYCLE.map((s) => (
        <span
          key={s}
          className={cn(
            'rounded px-1 py-0.5 text-[10px] uppercase tracking-wide',
            s === status
              ? s === 'REJECTED' || s === 'UNKNOWN'
                ? 'bg-loss/15 font-semibold text-loss'
                : s === 'SETTLED'
                  ? 'bg-profit/15 font-semibold text-profit'
                  : 'bg-amber/15 font-semibold text-amber'
              : 'text-muted-foreground/50'
          )}
        >
          {s}
        </span>
      ))}
    </div>
  )
}

function IntentsTab() {
  const { data, isLoading, error } = useIntents()
  if (isLoading) return <LoadingState />
  if (error) return <ErrorState error={error} />
  if (!data?.intents.length) return <EmptyState text="No intents in the ledger." />
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-muted-foreground">
          <th className="px-4 py-2 font-medium">Created</th>
          <th className="px-2 py-2 font-medium">Kind</th>
          <th className="px-2 py-2 font-medium">Lifecycle</th>
          <th className="px-2 py-2 font-medium">Reason</th>
          <th className="px-4 py-2 font-medium">Intent</th>
        </tr>
      </thead>
      <tbody>
        {data.intents.map((intent) => (
          <>
            <tr key={intent.intent_id} className="border-t bg-muted/20">
              <td className="tabular px-4 py-2">{formatDateTime(intent.created_at)}</td>
              <td className="px-2 py-2 font-medium">{intent.kind}</td>
              <td className="px-2 py-2">
                <Lifecycle status={intent.status} />
              </td>
              <td className="px-2 py-2 text-muted-foreground">{intent.reason}</td>
              <td className="px-4 py-2 font-mono text-[11px] text-muted-foreground">
                {intent.intent_id}
              </td>
            </tr>
            {intent.legs.map((leg, k) => (
              <tr key={`${intent.intent_id}-${k}`} className="border-t border-border/50">
                <td className="px-4 py-1 pl-8 font-mono text-[11px]">{leg.symbol}</td>
                <td
                  className={cn(
                    'px-2 py-1 font-medium',
                    leg.side === 'SELL' ? 'text-action-square-off' : 'text-action-exit'
                  )}
                >
                  {leg.side} {leg.quantity}
                </td>
                <td className="px-2 py-1">
                  <StatusBadge tone={toneFor(leg.status)} className="px-1.5 py-0 text-[10px]">
                    {leg.status}
                  </StatusBadge>
                </td>
                <td className="tabular px-2 py-1 text-muted-foreground">
                  avg {leg.average_price != null ? fmtNum(leg.average_price, 2) : '-'}
                </td>
                <td className="px-4 py-1 font-mono text-[11px] text-muted-foreground">
                  order {leg.order_id ?? 'none yet'}
                </td>
              </tr>
            ))}
          </>
        ))}
      </tbody>
    </table>
  )
}

function OrdersTab() {
  const { data, isLoading, error } = useOrders()
  if (isLoading) return <LoadingState />
  if (error) return <ErrorState error={error} />
  if (!data?.orders.length)
    return <EmptyState text="No orders tagged openfly in the OpenAlgo orderbook." />
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-muted-foreground">
          <th className="px-4 py-2 font-medium">Time</th>
          <th className="px-2 py-2 font-medium">Order id</th>
          <th className="px-2 py-2 font-medium">Symbol</th>
          <th className="px-2 py-2 font-medium">Action</th>
          <th className="px-2 py-2 text-right font-medium">Qty</th>
          <th className="px-2 py-2 font-medium">Type</th>
          <th className="px-2 py-2 text-right font-medium">Price</th>
          <th className="px-2 py-2 text-right font-medium">Trigger</th>
          <th className="px-2 py-2 text-right font-medium">Avg</th>
          <th className="px-2 py-2 font-medium">Product</th>
          <th className="px-4 py-2 font-medium">Status</th>
        </tr>
      </thead>
      <tbody>
        {data.orders.map((o) => (
          <tr key={o.orderid} className="border-t">
            <td className="tabular px-4 py-1.5">
              {o.timestamp
                ? formatTime(o.timestamp.includes('+') ? o.timestamp : `${o.timestamp}+05:30`)
                : '-'}
            </td>
            <td className="px-2 py-1.5 font-mono text-[11px]">{o.orderid}</td>
            <td className="px-2 py-1.5 font-mono text-[11px]">{o.symbol}</td>
            <td
              className={cn(
                'px-2 py-1.5 font-medium',
                o.action === 'SELL' ? 'text-action-square-off' : 'text-action-exit'
              )}
            >
              {o.action}
            </td>
            <td className="tabular px-2 py-1.5 text-right">{o.quantity}</td>
            <td className="px-2 py-1.5">{o.pricetype}</td>
            <td className="tabular px-2 py-1.5 text-right">{fmtNum(toNumber(o.price), 2)}</td>
            <td className="tabular px-2 py-1.5 text-right">
              {toNumber(o.trigger_price) ? fmtNum(toNumber(o.trigger_price), 2) : '-'}
            </td>
            <td className="tabular px-2 py-1.5 text-right">
              {toNumber(o.average_price) ? fmtNum(toNumber(o.average_price), 2) : '-'}
            </td>
            <td className="px-2 py-1.5">{o.product}</td>
            <td className="px-4 py-1.5">
              <StatusBadge tone={toneFor(o.order_status)} className="px-1.5 py-0 text-[10px]">
                {o.order_status}
              </StatusBadge>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function PositionsTab() {
  const { data, isLoading, error } = usePositions()
  if (isLoading) return <LoadingState />
  if (error) return <ErrorState error={error} />
  if (!data?.positions.length)
    return <EmptyState text="No positions in the OpenAlgo positionbook." />
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-muted-foreground">
          <th className="px-4 py-2 font-medium">Symbol</th>
          <th className="px-2 py-2 font-medium">Exchange</th>
          <th className="px-2 py-2 font-medium">Product</th>
          <th className="px-2 py-2 text-right font-medium">Qty</th>
          <th className="px-2 py-2 text-right font-medium">Avg price</th>
          <th className="px-2 py-2 text-right font-medium">LTP</th>
          <th className="px-4 py-2 text-right font-medium">P&L</th>
        </tr>
      </thead>
      <tbody>
        {data.positions.map((p) => {
          const qty = toNumber(p.quantity) ?? 0
          return (
            <tr
              key={`${p.symbol}-${p.product}`}
              className={cn('border-t', qty === 0 && 'text-muted-foreground')}
            >
              <td className="px-4 py-1.5 font-mono text-[11px]">{p.symbol}</td>
              <td className="px-2 py-1.5">{p.exchange}</td>
              <td className="px-2 py-1.5">{p.product}</td>
              <td
                className={cn(
                  'tabular px-2 py-1.5 text-right',
                  qty < 0 && 'text-action-square-off',
                  qty > 0 && 'text-action-exit'
                )}
              >
                {qty}
              </td>
              <td className="tabular px-2 py-1.5 text-right">
                {fmtNum(toNumber(p.average_price), 2)}
              </td>
              <td className="tabular px-2 py-1.5 text-right">{fmtNum(toNumber(p.ltp), 2)}</td>
              <td className="px-4 py-1.5 text-right">
                <Pnl value={toNumber(p.pnl)} />
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

export default function OrdersPage() {
  return (
    <div className="space-y-4">
      <PageHeader
        title="Orders and positions"
        description="The intent ledger (written before any call to OpenAlgo) next to the OpenAlgo orderbook and positionbook filtered to the openfly strategy tag."
      />
      <Tabs defaultValue="intents">
        <TabsList>
          <TabsTrigger value="intents">Ledger intents</TabsTrigger>
          <TabsTrigger value="orders">Orderbook</TabsTrigger>
          <TabsTrigger value="positions">Positionbook</TabsTrigger>
        </TabsList>
        <Card className="mt-3">
          <CardContent className="p-0">
            <TabsContent value="intents" className="m-0">
              <IntentsTab />
            </TabsContent>
            <TabsContent value="orders" className="m-0">
              <OrdersTab />
            </TabsContent>
            <TabsContent value="positions" className="m-0">
              <PositionsTab />
            </TabsContent>
          </CardContent>
        </Card>
      </Tabs>
    </div>
  )
}
