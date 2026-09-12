// Deterministic helpers for the synthetic data. Same seed, same day.

export function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function gaussian(rand: () => number): number {
  let u = 0
  let v = 0
  while (u === 0) u = rand()
  while (v === 0) v = rand()
  return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v)
}

// Abramowitz and Stegun approximation of the standard normal CDF.
export function normalCdf(x: number): number {
  const t = 1 / (1 + 0.2316419 * Math.abs(x))
  const d = 0.3989423 * Math.exp((-x * x) / 2)
  const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
  return x > 0 ? 1 - p : p
}

// Black 76 prices on the forward with zero rates: enough for a mock chain.
export function blackPrices(
  forward: number,
  strike: number,
  daysToExpiry: number,
  iv: number
): { ce: number; pe: number } {
  const t = Math.max(daysToExpiry, 0.01) / 365
  const sigmaT = iv * Math.sqrt(t)
  const d1 = (Math.log(forward / strike) + (sigmaT * sigmaT) / 2) / sigmaT
  const d2 = d1 - sigmaT
  const ce = forward * normalCdf(d1) - strike * normalCdf(d2)
  const pe = strike * normalCdf(-d2) - forward * normalCdf(-d1)
  return { ce: Math.max(ce, 0.05), pe: Math.max(pe, 0.05) }
}

export function round(value: number, step = 0.05): number {
  return Math.round(value / step) * step
}

export function round2(value: number): number {
  return Math.round(value * 100) / 100
}

export function round1(value: number): number {
  return Math.round(value * 10) / 10
}

export function hashHex(seed: string): string {
  let h1 = 0xdeadbeef
  let h2 = 0x41c6ce57
  for (let i = 0; i < seed.length; i++) {
    const ch = seed.charCodeAt(i)
    h1 = Math.imul(h1 ^ ch, 2654435761)
    h2 = Math.imul(h2 ^ ch, 1597334677)
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909)
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909)
  const a = (h2 >>> 0).toString(16).padStart(8, '0')
  const b = (h1 >>> 0).toString(16).padStart(8, '0')
  return `${a}${b}${a.split('').reverse().join('')}${b.split('').reverse().join('')}`
}
