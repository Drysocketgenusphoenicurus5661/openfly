// Firing-rate heatmap: populations as rows, the last N observations as
// columns. Plain SVG; the colour scale runs per row would hide the
// asymmetries traders care about, so one scale is shared.

import { useMemo } from 'react'
import type { RatesHz } from '@/api/types'
import { useIsDark } from '@/hooks/useIsDark'
import { fmtNum } from '@/lib/format'
import { formatTime } from '@/lib/time'

export interface HeatmapProps {
  observations: { t: string; rates_hz: RatesHz }[]
  populations?: string[]
  columns?: number
  cell?: number
}

function heat(t: number, dark: boolean): string {
  const clamped = Math.max(0, Math.min(1, t))
  // Dark: deep slate to bright sky. Light: pale to deep blue.
  const l = dark ? 18 + clamped * 52 : 94 - clamped * 52
  const s = dark ? 40 + clamped * 45 : 30 + clamped * 55
  return `hsl(212 ${s}% ${l}%)`
}

export function Heatmap({ observations, populations, columns = 30, cell = 18 }: HeatmapProps) {
  const dark = useIsDark()
  const recent = observations.slice(-columns)
  const rows = useMemo(() => {
    if (populations?.length) return populations
    const names = new Set<string>()
    for (const o of recent) for (const k of Object.keys(o.rates_hz)) names.add(k)
    return [...names]
  }, [populations, recent])
  const max = useMemo(() => {
    let m = 0
    for (const o of recent) for (const k of rows) m = Math.max(m, o.rates_hz[k] ?? 0)
    return m || 1
  }, [recent, rows])

  const labelWidth = 72
  const width = labelWidth + columns * cell
  const height = rows.length * cell + 18
  const offset = columns - recent.length

  if (rows.length === 0) {
    return <div className="text-sm text-muted-foreground">No observations yet.</div>
  }

  return (
    <div className="overflow-x-auto">
      <svg
        width={width}
        height={height}
        className="block"
        role="img"
        aria-label="Population firing rates"
      >
        {rows.map((name, r) => (
          <g key={name}>
            <text
              x={labelWidth - 6}
              y={r * cell + cell * 0.7}
              textAnchor="end"
              className="fill-muted-foreground"
              fontSize={11}
            >
              {name}
            </text>
            {recent.map((o, c) => {
              const value = o.rates_hz[name] ?? 0
              return (
                <rect
                  key={o.t}
                  x={labelWidth + (offset + c) * cell}
                  y={r * cell}
                  width={cell - 1}
                  height={cell - 1}
                  fill={heat(value / max, dark)}
                >
                  <title>{`${name} ${fmtNum(value, 2)} Hz at ${formatTime(o.t)}`}</title>
                </rect>
              )
            })}
          </g>
        ))}
        {recent.map((o, c) =>
          c % 5 === 0 || c === recent.length - 1 ? (
            <text
              key={`t-${o.t}`}
              x={labelWidth + (offset + c) * cell + cell / 2}
              y={rows.length * cell + 13}
              textAnchor="middle"
              className="fill-muted-foreground"
              fontSize={9}
            >
              {formatTime(o.t)}
            </text>
          ) : null
        )}
      </svg>
      <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
        <span>0 Hz</span>
        <div
          className="h-2 w-40 rounded"
          style={{
            background: `linear-gradient(to right, ${heat(0, dark)}, ${heat(0.5, dark)}, ${heat(1, dark)})`,
          }}
        />
        <span className="tabular">{fmtNum(max, 1)} Hz</span>
        <span className="ml-auto">
          {recent.length} of {columns} observations
        </span>
      </div>
    </div>
  )
}
