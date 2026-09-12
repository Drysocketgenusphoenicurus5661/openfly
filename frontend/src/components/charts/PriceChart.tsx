// Two titled panes on one time axis: NIFTY one minute candles with action
// markers on top, the ATM straddle premium in points below with the
// combined stop, target, credit and per-leg stop lines. With a cursor
// (replay) the whole day is drawn: fully coloured up to the cursor, dimmed
// beyond it, and the visible range stays the full session.

import {
  type Chart,
  createChart,
  PaneLegend,
  type PriceLine,
  type SeriesApi,
  type SeriesMarker,
  type SeriesMarkers,
  withAlpha,
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
  // Replay cursor: bars at or before this index are drawn in full colour,
  // later ones dimmed. Undefined means live: everything in full colour.
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
  color?: string
}

// Opacity of everything beyond the replay cursor.
const DIM = 0.35

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
  // Invisible series whose two points force the premium scale to cover every level.
  const rangeRef = useRef<SeriesApi | null>(null)
  const markersRef = useRef<SeriesMarkers | null>(null)
  const legendsRef = useRef<{ price: PaneLegend; premium: PaneLegend } | null>(null)
  const linesRef = useRef<Map<string, PriceLine>>(new Map())
  const pointRef = useRef<{ x: number; y: number } | null>(null)
  const fittedRef = useRef<ChartBar[] | null>(null)
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
  const cursorTime =
    replayIndex !== undefined && chartBars.length
      ? chartBars[Math.max(0, Math.min(replayIndex, chartBars.length - 1))].time
      : Infinity

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
    rangeRef.current = chart.addSeries('line', {
      paneIndex: 1,
      style: {
        color: 'rgba(0,0,0,0)',
        lineWidth: 1,
        title: '',
        priceLineVisible: false,
        lastValueVisible: false,
      },
    })
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
      linesRef.current.clear()
      legendsRef.current = null
      fittedRef.current = null
      chart.destroy()
      chartRef.current = null
      candlesRef.current = null
      premiumRef.current = null
      rangeRef.current = null
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

  // Data: the whole day, coloured up to the cursor and dimmed beyond it.
  useEffect(() => {
    const chart = chartRef.current
    const candles = candlesRef.current
    const premiumSeries = premiumRef.current
    if (!chart || !candles || !premiumSeries || !ready) return
    const theme = chartTheme(dark)
    const premiumColor = dark ? PREMIUM_COLOR.dark : PREMIUM_COLOR.light
    const replay = replayIndex !== undefined

    candles.setData(
      chartBars.map((b) =>
        replay && b.time > cursorTime
          ? { ...b, color: withAlpha(b.close >= b.open ? theme.upColor : theme.downColor, DIM) }
          : b
      )
    )
    premiumSeries.setData(
      premiumPoints.map((p) =>
        replay
          ? { ...p, color: p.time > cursorTime ? withAlpha(premiumColor, DIM) : premiumColor }
          : p
      )
    )

    if (replay) {
      // The visible range is the full session; refit only when the day changes.
      if (fittedRef.current !== chartBars) {
        chart.timeScale.fitContent(Math.max(chartBars.length, 1))
        fittedRef.current = chartBars
      }
      return
    }
    // Live: keep the right edge in view as bars arrive.
    const previous = fittedRef.current?.length ?? 0
    if (previous === 0 || Math.abs(chartBars.length - previous) > 5) {
      chart.timeScale.fitContent(Math.max(chartBars.length, 1))
    } else {
      const range = chart.getVisibleLogicalRange()
      const span = Math.max(20, range.to - range.from)
      if (range.to >= previous - 3) {
        chart.setVisibleLogicalRange({ from: chartBars.length - span, to: chartBars.length + 2 })
      }
    }
    fittedRef.current = chartBars
  }, [chartBars, premiumPoints, replayIndex, cursorTime, ready, dark])

  // Markers on the price pane, dimmed beyond the cursor.
  useEffect(() => {
    const layer = markersRef.current
    if (!layer || !ready) return
    const list: SeriesMarker[] = (markers ?? []).map((m) => {
      const shape = markerShape(m.action)
      const hex = actionHex(m.action, dark)
      const future = m.time > cursorTime
      // Labels only where they carry information: entries and exits that
      // have happened. Vetoes and future markers stay as bare glyphs.
      const labelled = !future && m.action !== 'VETO' && m.action !== 'HOLD' && m.action !== 'NONE'
      return {
        id: m.id,
        time: m.time,
        color: future ? withAlpha(hex, DIM) : hex,
        ...(labelled ? { text: actionStyle(m.action).label } : {}),
        ...shape,
      }
    })
    layer.setMarkers(list)
  }, [markers, cursorTime, dark, ready])

  // Levels on the premium pane. The invisible range series spans the lowest
  // and highest level across the whole day so autoscale always shows them.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart || !ready) return
    const range = rangeRef.current
    if (range) {
      const prices = (levels ?? []).map((l) => l.price)
      if (prices.length && chartBars.length) {
        const first = chartBars[0].time
        const last = chartBars[chartBars.length - 1].time
        const lo = Math.min(...prices)
        const hi = Math.max(...prices)
        range.setData(
          first === last
            ? [
                { time: first, value: lo },
                { time: first + 60, value: hi },
              ]
            : [
                { time: first, value: lo },
                { time: last, value: hi },
              ]
        )
      } else {
        range.setData([])
      }
    }
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
      const lineWidth = level.kind === 'stop' || level.kind === 'target' ? 2 : 1
      if (existing) {
        existing.setPrice(level.price)
        existing.setOptions({
          color,
          lineWidth,
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
            lineWidth,
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
  }, [levels, chartBars, dark, ready])

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
