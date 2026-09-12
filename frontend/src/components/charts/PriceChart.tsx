// NIFTY candles on pane 0, the combined straddle premium on pane 1 with
// horizontal lines for the combined stop, target and per-leg stops, and
// action markers with tooltips. In replay mode the chart shows a prefix of
// the day through openalgo-charts' ReplayController.

import {
  type Chart,
  createChart,
  type PriceLine,
  ReplayController,
  type SeriesApi,
  type SeriesMarker,
  type SeriesMarkers,
} from 'openalgo-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Bar } from '@/api/types'
import { useIsDark } from '@/hooks/useIsDark'
import { actionHex, actionStyle } from '@/lib/actions'
import { toChartTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { chartTheme, PREMIUM_COLOR } from './theme'

export interface ChartMarker {
  id: string
  time: number
  action: string
  text: string
  price?: number
}

export interface ChartLevel {
  id: string
  price: number
  kind: 'stop' | 'target' | 'leg_stop' | 'entry'
  label: string
}

export interface PremiumPoint {
  time: number
  value: number
}

export interface PriceChartProps {
  bars: Bar[]
  premium?: PremiumPoint[]
  markers?: ChartMarker[]
  levels?: ChartLevel[]
  // When set, only bars up to this index are shown (replay).
  replayIndex?: number
  height?: number
  className?: string
  onMarkerClick?: (id: string) => void
  emptyText?: string
}

interface ChartBar {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume?: number
}

function toChartBar(bar: Bar): ChartBar {
  return {
    time: toChartTime(bar.t),
    open: bar.o,
    high: bar.h,
    low: bar.l,
    close: bar.c,
    volume: bar.v,
  }
}

function markerShape(action: string): {
  shape: SeriesMarker['shape']
  position: SeriesMarker['position']
  size: SeriesMarker['size']
} {
  switch (action) {
    case 'ENTER':
    case 'REENTRY':
      return { shape: 'arrowUp', position: 'belowBar', size: 'medium' }
    case 'EXIT':
    case 'TARGET':
      return { shape: 'arrowDown', position: 'aboveBar', size: 'medium' }
    case 'STOP':
      return { shape: 'arrowDown', position: 'aboveBar', size: 'medium' }
    case 'STOP_LEG':
      return { shape: 'triangleDown', position: 'aboveBar', size: 'medium' }
    case 'SQUARE_OFF':
      return { shape: 'square', position: 'aboveBar', size: 'small' }
    case 'LOCK':
      return { shape: 'diamond', position: 'inBar', size: 'small' }
    case 'VETO':
      return { shape: 'circle', position: 'belowBar', size: 'tiny' }
    default:
      return { shape: 'circle', position: 'inBar', size: 'tiny' }
  }
}

function levelColor(kind: ChartLevel['kind'], dark: boolean): string {
  switch (kind) {
    case 'stop':
      return actionHex('STOP', dark)
    case 'target':
      return actionHex('TARGET', dark)
    case 'leg_stop':
      return actionHex('STOP_LEG', dark)
    case 'entry':
      return dark ? '#a3a3a3' : '#525252'
  }
}

export function PriceChart({
  bars,
  premium,
  markers,
  levels,
  replayIndex,
  height = 420,
  className,
  onMarkerClick,
  emptyText = 'No bars yet',
}: PriceChartProps) {
  const dark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<Chart | null>(null)
  const candlesRef = useRef<SeriesApi | null>(null)
  const premiumRef = useRef<SeriesApi | null>(null)
  const markersRef = useRef<SeriesMarkers | null>(null)
  const replayRef = useRef<ReplayController | null>(null)
  const linesRef = useRef<Map<string, PriceLine>>(new Map())
  const pointRef = useRef<{ x: number; y: number } | null>(null)
  const clickRef = useRef(onMarkerClick)
  clickRef.current = onMarkerClick
  const [hovered, setHovered] = useState<{ id: string; x: number; y: number } | null>(null)
  const [ready, setReady] = useState(false)

  const chartBars = useMemo(() => bars.map(toChartBar), [bars])
  const premiumPoints = useMemo(
    () => (premium ?? []).filter((p) => Number.isFinite(p.value)),
    [premium]
  )
  const markerText = useMemo(() => {
    const map = new Map<string, string>()
    for (const m of markers ?? []) map.set(m.id, m.text)
    return map
  }, [markers])

  // Create once.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the chart is created once; the theme effect below follows `dark`
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, {
      theme: chartTheme(dark),
      timezone: 'Asia/Kolkata',
      navigation: { defaultVisibleBars: 0 },
    })
    chartRef.current = chart
    const candles = chart.addSeries('candlestick', {
      paneIndex: 0,
      style: { priceLineVisible: true, lastValueVisible: true, precision: 1 },
    })
    candlesRef.current = candles
    markersRef.current = candles.createMarkers()
    chart.on('click', (payload) => {
      const id = (payload as { id: string | null }).id
      if (id && clickRef.current) clickRef.current(id)
    })
    chart.on('crosshair:move', (payload) => {
      const point = (payload as { point: { x: number; y: number } | null }).point
      pointRef.current = point
    })
    chart.on('hover', (payload) => {
      const id = (payload as { id: string | null }).id
      if (id && pointRef.current) setHovered({ id, x: pointRef.current.x, y: pointRef.current.y })
      else setHovered(null)
    })
    setReady(true)
    return () => {
      replayRef.current?.stop()
      replayRef.current = null
      linesRef.current.clear()
      chart.destroy()
      chartRef.current = null
      candlesRef.current = null
      premiumRef.current = null
      markersRef.current = null
      setReady(false)
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setTheme(chartTheme(dark))
    premiumRef.current?.applyOptions({ color: dark ? PREMIUM_COLOR.dark : PREMIUM_COLOR.light })
  }, [dark])

  // Data and replay.
  useEffect(() => {
    const chart = chartRef.current
    const candles = candlesRef.current
    if (!chart || !candles || !ready) return
    if (premiumPoints.length > 0 && !premiumRef.current) {
      premiumRef.current = chart.addSeries('line', {
        paneIndex: 1,
        style: {
          color: dark ? PREMIUM_COLOR.dark : PREMIUM_COLOR.light,
          lineWidth: 2,
          title: 'Premium',
          priceLineVisible: false,
          lastValueVisible: true,
          precision: 1,
        },
      })
      chart.setPaneWeight(0, 3)
      chart.setPaneWeight(1, 1.4)
    }
    const premiumSeries = premiumRef.current

    if (replayIndex !== undefined) {
      // Replay: full data once, then the controller shows a prefix.
      if (!replayRef.current) {
        candles.setData(chartBars)
        premiumSeries?.setData(premiumPoints)
        if (chartBars.length === 0) return
        const series = premiumSeries ? [candles, premiumSeries] : [candles]
        replayRef.current = new ReplayController(chart, {
          series,
          bars: chartBars,
          startIndex: Math.min(replayIndex, chartBars.length - 1),
        })
        chart.timeScale.fitContent(chartBars.length)
      } else {
        replayRef.current.seek(Math.min(replayIndex, chartBars.length - 1))
      }
      return
    }

    // Live: replace data and keep the right edge in view.
    if (replayRef.current) {
      replayRef.current.stop()
      replayRef.current = null
    }
    const previous = candles.getData().length
    candles.setData(chartBars)
    premiumSeries?.setData(premiumPoints)
    if (previous === 0 || Math.abs(chartBars.length - previous) > 5) {
      chart.timeScale.fitContent(chartBars.length)
    } else {
      const range = chart.getVisibleLogicalRange()
      const span = Math.max(20, range.to - range.from)
      if (range.to >= previous - 3) {
        chart.setVisibleLogicalRange({ from: chartBars.length - span, to: chartBars.length + 2 })
      }
    }
  }, [chartBars, premiumPoints, replayIndex, ready, dark])

  // Tear the controller down when the underlying day changes.
  // biome-ignore lint/correctness/useExhaustiveDependencies: chartBars is the trigger, not a value the cleanup reads
  useEffect(() => {
    return () => {
      replayRef.current?.stop()
      replayRef.current = null
    }
  }, [chartBars])

  // Markers.
  useEffect(() => {
    const layer = markersRef.current
    if (!layer || !ready) return
    const limit =
      replayIndex !== undefined && chartBars[replayIndex] ? chartBars[replayIndex].time : Infinity
    const list: SeriesMarker[] = (markers ?? [])
      .filter((m) => m.time <= limit)
      .map((m) => {
        const shape = markerShape(m.action)
        return {
          id: m.id,
          time: m.time,
          color: actionHex(m.action, dark),
          text: actionStyle(m.action).label,
          ...shape,
          ...(m.price !== undefined && shape.position === 'atPrice' ? { price: m.price } : {}),
        }
      })
    layer.setMarkers(list)
  }, [markers, replayIndex, chartBars, dark, ready])

  // Levels on the premium pane.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the premium series (and its pane) may only exist once points arrive
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready || !premiumRef.current) return
    const lines = linesRef.current
    const wanted = new Map((levels ?? []).map((l) => [l.id, l]))
    for (const [id, line] of lines) {
      if (!wanted.has(id)) {
        chart.removePrimitive(line)
        lines.delete(id)
      }
    }
    for (const level of wanted.values()) {
      const existing = lines.get(level.id)
      const color = levelColor(level.kind, dark)
      const dashed = level.kind === 'leg_stop' || level.kind === 'entry'
      if (existing) {
        existing.setPrice(level.price)
        existing.setOptions({ color, label: level.price.toFixed(1), badge: level.label, dashed })
      } else {
        const line = chart.addPriceLine(
          {
            id: level.id,
            price: level.price,
            color,
            lineWidth: 1,
            dashed,
            label: level.price.toFixed(1),
            badge: level.label,
            extentFromRight: 1,
          },
          1
        )
        lines.set(level.id, line)
      }
    }
  }, [levels, dark, ready, premiumPoints.length])

  const hoveredText = hovered ? markerText.get(hovered.id) : null

  return (
    <div className={cn('relative w-full', className)} style={{ height }}>
      <div ref={containerRef} className="h-full w-full" />
      {bars.length === 0 && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
          {emptyText}
        </div>
      )}
      {hovered && hoveredText && (
        <div
          className="pointer-events-none absolute z-10 max-w-xs rounded-md border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-md"
          style={{
            left: Math.min(
              hovered.x + 12,
              Math.max(0, (containerRef.current?.clientWidth ?? 400) - 260)
            ),
            top: Math.max(4, hovered.y - 36),
          }}
        >
          {hoveredText}
        </div>
      )}
    </div>
  )
}
