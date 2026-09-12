import type { Session } from '@/api/types'
import { useSessionNow } from '@/hooks/useSessionNow'
import { formatTime, hmToMinutes, istMinutes } from '@/lib/time'
import { cn } from '@/lib/utils'

const DAY_START = hmToMinutes('09:15')
const DAY_END = hmToMinutes('15:30')

function pct(minutes: number): string {
  return `${((Math.max(DAY_START, Math.min(DAY_END, minutes)) - DAY_START) / (DAY_END - DAY_START)) * 100}%`
}

// The session strip: 09:15 to 15:30 with the trade start, last entry and
// square-off markers and a moving "now" line. Compact enough for a header.
export function SessionClock({
  session,
  compact = false,
  className,
}: {
  session: Session | undefined
  compact?: boolean
  className?: string
}) {
  const now = useSessionNow()
  const nowMinutes = istMinutes(now) ?? 0
  const tradeStart = istMinutes(session?.trade_start) ?? hmToMinutes('09:20')
  const lastEntry = istMinutes(session?.last_entry) ?? hmToMinutes('14:30')
  const squareOff = istMinutes(session?.square_off) ?? hmToMinutes('15:15')
  const phase =
    session && !session.is_trading_day
      ? 'Not a trading day'
      : nowMinutes < tradeStart
        ? 'Before trade start'
        : nowMinutes <= lastEntry
          ? 'Trade window'
          : nowMinutes <= squareOff
            ? 'No new entries'
            : 'After square-off'
  const minutesToSquareOff = squareOff - nowMinutes

  return (
    <div className={cn('min-w-0', className)}>
      <div className="flex items-baseline justify-between gap-3 text-xs">
        <span className="tabular font-semibold text-foreground">
          {formatTime(now, true)} <span className="font-normal text-muted-foreground">IST</span>
        </span>
        {!compact && <span className="text-muted-foreground">{session?.trading_date ?? ''}</span>}
        <span
          className={cn(
            'truncate',
            phase === 'Trade window' ? 'text-profit' : 'text-muted-foreground'
          )}
        >
          {phase}
          {minutesToSquareOff > 0 &&
            minutesToSquareOff <= 60 &&
            ` (${minutesToSquareOff} min to square-off)`}
          {session?.is_expiry_day ? ', expiry day' : ''}
        </span>
      </div>
      <div className="relative mt-1 h-2 rounded bg-muted" title="Session 09:15 to 15:30 IST">
        <div
          className="absolute top-0 h-2 rounded-l bg-profit/30"
          style={{ left: pct(tradeStart), width: `calc(${pct(lastEntry)} - ${pct(tradeStart)})` }}
        />
        <div
          className="absolute top-0 h-2 bg-amber/30"
          style={{ left: pct(lastEntry), width: `calc(${pct(squareOff)} - ${pct(lastEntry)})` }}
        />
        <div
          className="absolute -top-0.5 h-3 w-px bg-profit"
          style={{ left: pct(tradeStart) }}
          title={`trade start ${formatTime(session?.trade_start) === '--:--' ? '09:20' : formatTime(session?.trade_start)}`}
        />
        <div
          className="absolute -top-0.5 h-3 w-px bg-amber"
          style={{ left: pct(lastEntry) }}
          title={`last entry ${formatTime(session?.last_entry)}`}
        />
        <div
          className="absolute -top-0.5 h-3 w-px bg-loss"
          style={{ left: pct(squareOff) }}
          title={`square off ${formatTime(session?.square_off)}`}
        />
        <div
          className="absolute -top-1 h-4 w-0.5 bg-foreground"
          style={{ left: pct(nowMinutes) }}
        />
      </div>
      <div className="relative mt-0.5 h-3 text-[10px] text-muted-foreground">
        <span className="absolute -translate-x-1/2 text-profit" style={{ left: pct(tradeStart) }}>
          {formatTime(session?.trade_start) === '--:--'
            ? '09:20'
            : formatTime(session?.trade_start)}
        </span>
        <span className="absolute -translate-x-1/2 text-amber" style={{ left: pct(lastEntry) }}>
          {formatTime(session?.last_entry) === '--:--' ? '14:30' : formatTime(session?.last_entry)}
        </span>
        <span className="absolute -translate-x-1/2 text-loss" style={{ left: pct(squareOff) }}>
          {formatTime(session?.square_off) === '--:--' ? '15:15' : formatTime(session?.square_off)}
        </span>
      </div>
    </div>
  )
}
