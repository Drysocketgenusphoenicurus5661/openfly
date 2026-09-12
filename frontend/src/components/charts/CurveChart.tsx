// Cumulative P&L curves for an experiment: the strategy against its
// controls, one line series each on a single pane, with a hover legend.

import { type Chart, createChart, type SeriesApi } from 'openalgo-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useIsDark } from '@/hooks/useIsDark'
import { fmtSignedInr } from '@/lib/format'
import { cn } from '@/lib/utils'
import { CURVE_COLORS, chartTheme } from './theme'

export interface CurveSeries {
  key: string
  label: string
  values: number[]
  dashed?: boolean
}

export interface CurveChartProps {
  dates: string[]
  series: CurveSeries[]
  height?: number
  className?: string
}

function dateToTime(date: string): number {
  return Math.floor(Date.parse(`${date}T00:00:00Z`) / 1000)
}

export function CurveChart({ dates, series, height = 320, className }: CurveChartProps) {
  const dark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<Chart | null>(null)
  const seriesRef = useRef<Map<string, SeriesApi>>(new Map())
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const times = useMemo(() => dates.map(dateToTime), [dates])

  // biome-ignore lint/correctness/useExhaustiveDependencies: the chart is created once; the theme effect below follows `dark`
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, { theme: chartTheme(dark), timezone: 'Asia/Kolkata' })
    chartRef.current = chart
    chart.on('crosshair:move', (payload) => {
      const index = (payload as { index: number | null }).index
      setHoverIndex(index)
    })
    return () => {
      seriesRef.current.clear()
      chart.destroy()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setTheme(chartTheme(dark))
    for (const [key, s] of seriesRef.current) {
      const colour = CURVE_COLORS[key] ?? CURVE_COLORS.strategy
      s.applyOptions({ color: dark ? colour.dark : colour.light })
    }
  }, [dark])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const existing = seriesRef.current
    const wanted = new Set(series.map((s) => s.key))
    for (const [key, s] of existing) {
      if (!wanted.has(key)) {
        s.remove()
        existing.delete(key)
      }
    }
    for (const s of series) {
      const colour = CURVE_COLORS[s.key] ?? CURVE_COLORS.strategy
      let api = existing.get(s.key)
      if (!api) {
        api = chart.addSeries('line', {
          paneIndex: 0,
          style: {
            color: dark ? colour.dark : colour.light,
            lineWidth: s.key === 'strategy' ? 2 : 1,
            lineStyle: s.dashed ? 'dashed' : 'solid',
            title: s.label,
            priceLineVisible: false,
            lastValueVisible: s.key === 'strategy',
          },
        })
        existing.set(s.key, api)
      }
      api.setData(times.map((time, i) => ({ time, value: s.values[i] ?? Number.NaN })))
    }
    chart.timeScale.fitContent(times.length)
  }, [series, times, dark])

  const legendIndex = hoverIndex ?? times.length - 1

  return (
    <div className={cn('w-full', className)}>
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {series.map((s) => {
          const colour = CURVE_COLORS[s.key] ?? CURVE_COLORS.strategy
          const value = s.values[legendIndex]
          return (
            <span key={s.key} className="inline-flex items-center gap-1.5">
              <span
                className="inline-block h-0.5 w-4 rounded"
                style={{ backgroundColor: dark ? colour.dark : colour.light }}
              />
              <span className="text-muted-foreground">{s.label}</span>
              <span className="tabular font-medium">{fmtSignedInr(value)}</span>
            </span>
          )
        })}
        {dates[legendIndex] && (
          <span className="ml-auto text-muted-foreground tabular">{dates[legendIndex]}</span>
        )}
      </div>
      <div ref={containerRef} style={{ height }} className="w-full" />
    </div>
  )
}
