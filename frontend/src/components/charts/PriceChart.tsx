// Two titled panes on one time axis: NIFTY one minute candles with action
// markers on top, the ATM straddle premium in points below with the
// combined stop, target, credit and per-leg stop lines. In replay mode the
// chart shows a prefix of the day through openalgo-charts' ReplayController.

import {
  type Chart,
  createChart,
  PaneLegend,
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
  priceTitle?: string
  premiumTitle?: string
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
  height = 460,
  className,
  priceTitle = 'NIFTY 1 minute (NSE_INDEX)',
  premiumTitle = 'ATM straddle premium (points)',
  onMarkerClick,
  emptyText = 'No bars yet',
}: PriceChartProps) {
  const dark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<Chart | null>(null)
  const candlesRef = useRef<SeriesApi | null>(null)
  const premiumRef = useRef<SeriesApi | null>(null)
  const markersRef = useRef<SeriesMarkers | null>(null)
  const legendsRef = useRef<{ price: PaneLegend; premium: PaneLegend } | null>(null)
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

  // Create once: both panes, both series, both titles.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the chart is created once; later effects follow the props
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
    const premiumSeries = chart.addSeries('line', {
      paneIndex: 1,
      style: {
        color: dark ? PREMIUM_COLOR.dark : PREMIUM_COLOR.light,
        lineWidth: 2,
        title: premiumTitle,
        priceLineVisible: false,
        lastValueVisible: true,
        precision: 1,
      },
    })
    premiumRef.current = premiumSeries
    chart.setPaneWeight(0, 3)
    chart.setPaneWeight(1, 2)
    chart.setCanvasOptions({ margins: { top: 20, bottom: 14 } })
    const priceLegend = new PaneLegend({ id: 'price-title', title: priceTitle, actions: [] })
    const premiumLegend = new PaneLegend({ id: 'premium-title', title: premiumTitle, actions: [] })
    chart.addPrimitive(priceLegend, 0)
    chart.addPrimitive(premiumLegend, 1)
    legendsRef.current = { price: priceLegend, premium: premiumLegend }

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
      legendsRef.current = null
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

  useEffect(() => {
    legendsRef.current?.price.setOptions({ title: priceTitle })
    legendsRef.current?.premium.setOptions({ title: premiumTitle })
  }, [priceTitle, premiumTitle])

  // Data and replay.
  useEffect(() => {
    const chart = chartRef.current
    const candles = candlesRef.current
    const premiumSeries = premiumRef.current
    if (!chart || !candles || !premiumSeries || !ready) return

    if (replayIndex !== undefined) {
      if (!replayRef.current) {
        candles.setData(chartBars)
        premiumSeries.setData(premiumPoints)
        if (chartBars.length === 0) return
        replayRef.current = new ReplayController(chart, {
          series: [candles, premiumSeries],
          bars: chartBars,
          startIndex: Math.min(replayIndex, chartBars.length - 1),
        })
        chart.timeScale.fitContent(chartBars.length)
      } else {
        replayRef.current.seek(Math.min(replayIndex, chartBars.length - 1))
      }
      return
    }

    if (replayRef.current) {
      replayRef.current.stop()
      replayRef.current = null
    }
    const previous = candles.getData().length
    candles.setData(chartBars)
    premiumSeries.setData(premiumPoints)
    if (previous === 0 || Math.abs(chartBars.length - previous) > 5) {
      chart.timeScale.fitContent(Math.max(chartBars.length, 1))
    } else {
      const range = chart.getVisibleLogicalRange()
      const span = Math.max(20, range.to - range.from)
      if (range.to >= previous - 3) {
        chart.setVisibleLogicalRange({ from: chartBars.length - span, to: chartBars.length + 2 })
      }
    }
  }, [chartBars, premiumPoints, replayIndex, ready])

  // A new day means a new replay controller.
  // biome-ignore lint/correctness/useExhaustiveDependencies: chartBars is the trigger, not a value the cleanup reads
  useEffect(() => {
    return () => {
      replayRef.current?.stop()
      replayRef.current = null
    }
  }, [chartBars])

  // Markers on the price pane.
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
        }
      })
    layer.setMarkers(list)
  }, [markers, replayIndex, chartBars, dark, ready])

  // Levels on the premium pane.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
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
      // Leg stops are stubs from the right axis, staggered so their pills do not stack.
      const extentFromRight = level.kind === 'leg_stop' ? (level.id.endsWith('CE') ? 0.62 : 0.4) : 1
      if (existing) {
        existing.setPrice(level.price)
        existing.setOptions({
          color,
          label: level.price.toFixed(1),
          badge: level.label,
          dashed,
          extentFromRight,
        })
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
            extentFromRight,
          },
          1
        )
        lines.set(level.id, line)
      }
    }
  }, [levels, dark, ready])

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
