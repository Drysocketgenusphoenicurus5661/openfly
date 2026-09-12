import { type ChartTheme, darkTheme, lightTheme } from 'openalgo-charts'

// The chart palette follows the app theme: transparent background so the
// card shows through, neutral grid, the same green and red as P&L text.
export function chartTheme(dark: boolean): ChartTheme {
  if (dark) {
    return {
      ...darkTheme,
      background: 'transparent',
      grid: 'rgba(255,255,255,0.06)',
      axisText: '#a3a3a3',
      axisLine: 'rgba(255,255,255,0.14)',
      paneSeparator: 'rgba(255,255,255,0.16)',
      crosshair: '#737373',
      upColor: '#4ade80',
      downColor: '#f87171',
      wickUpColor: '#4ade80',
      wickDownColor: '#f87171',
      lineColor: '#60a5fa',
      axisFontSize: 11,
    }
  }
  return {
    ...lightTheme,
    background: 'transparent',
    grid: 'rgba(0,0,0,0.06)',
    axisText: '#525252',
    axisLine: 'rgba(0,0,0,0.14)',
    paneSeparator: 'rgba(0,0,0,0.16)',
    crosshair: '#737373',
    upColor: '#15803d',
    downColor: '#dc2626',
    wickUpColor: '#15803d',
    wickDownColor: '#dc2626',
    lineColor: '#2563eb',
    axisFontSize: 11,
  }
}

export const PREMIUM_COLOR = { light: '#b45309', dark: '#fbbf24' }
export const CURVE_COLORS: Record<string, { light: string; dark: string }> = {
  strategy: { light: '#2563eb', dark: '#60a5fa' },
  fixed_0920: { light: '#b45309', dark: '#fbbf24' },
  random_entry: { light: '#6b7280', dark: '#9ca3af' },
  shuffled: { light: '#7c3aed', dark: '#c084fc' },
  flat: { light: '#a3a3a3', dark: '#525252' },
}
