// Stimulus images for mock mode, as SVG data URLs so <img> works offline.
// Encoder A renders the last bars as a small chart; B and C render a bar
// map of photoreceptor currents.

import type { Bar } from '@/api/types'
import { mulberry32 } from './random'

function encode(svg: string): string {
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

export function chartStimulus(bars: Bar[], width = 320, height = 160): string {
  const recent = bars.slice(-60)
  if (recent.length === 0) return barMapStimulus('empty')
  const lows = recent.map((b) => b.l)
  const highs = recent.map((b) => b.h)
  const min = Math.min(...lows)
  const max = Math.max(...highs)
  const span = Math.max(max - min, 1)
  const cw = width / 60
  const y = (v: number) => height - ((v - min) / span) * (height - 8) - 4
  const parts: string[] = []
  recent.forEach((b, k) => {
    const x = k * cw + cw / 2
    const up = b.c >= b.o
    const colour = up ? '#d9d9d9' : '#5a5a5a'
    parts.push(
      `<line x1="${x.toFixed(1)}" y1="${y(b.h).toFixed(1)}" x2="${x.toFixed(1)}" y2="${y(b.l).toFixed(1)}" stroke="${colour}" stroke-width="1"/>`
    )
    const top = y(Math.max(b.o, b.c))
    const bottom = y(Math.min(b.o, b.c))
    parts.push(
      `<rect x="${(x - cw * 0.3).toFixed(1)}" y="${top.toFixed(1)}" width="${(cw * 0.6).toFixed(1)}" height="${Math.max(bottom - top, 1).toFixed(1)}" fill="${colour}"/>`
    )
  })
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#000"/>${parts.join('')}</svg>`
  return encode(svg)
}

export function barMapStimulus(seed: string, cols = 24, rows = 12, cell = 12): string {
  let n = 0
  for (let i = 0; i < seed.length; i++) n = (n * 31 + seed.charCodeAt(i)) >>> 0
  const rand = mulberry32(n)
  const width = cols * cell
  const height = rows * cell
  const cells: string[] = []
  for (let r = 0; r < rows; r++) {
    // A smooth field with a bright band that moves with the seed.
    const band = (n % rows) + Math.sin(r / 2) * 1.5
    for (let c = 0; c < cols; c++) {
      const distance = Math.abs(r - band) + Math.abs(Math.sin(c / 3 + n / 100)) * 2
      const base = Math.max(0, 1 - distance / 6)
      const value = Math.min(1, Math.max(0, base * 0.8 + rand() * 0.3))
      const g = Math.round(40 + value * 200)
      cells.push(
        `<rect x="${c * cell}" y="${r * cell}" width="${cell}" height="${cell}" fill="rgb(${Math.round(g * 0.35)},${g},${Math.round(g * 0.45)})"/>`
      )
    }
  }
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#050505"/>${cells.join('')}</svg>`
  return encode(svg)
}
