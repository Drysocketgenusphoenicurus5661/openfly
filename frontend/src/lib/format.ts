const inr = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
})

const inr2 = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const plain = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 })

export function fmtInr(value: number | null | undefined, decimals: 0 | 2 = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return (decimals === 2 ? inr2 : inr).format(value)
}

export function fmtSignedInr(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  const text = inr.format(Math.abs(value))
  return value < 0 ? `-${text}` : `+${text}`
}

export function fmtNum(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return value.toFixed(decimals)
}

export function fmtInt(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return plain.format(value)
}

export function fmtPct(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return `${value.toFixed(decimals)} percent`
}

export function fmtPctShort(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '-'
  return `${value.toFixed(decimals)}%`
}

export function fmtBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

export function toNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const n = typeof value === 'number' ? value : Number.parseFloat(value)
  return Number.isFinite(n) ? n : null
}

export function pnlClass(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value))
    return 'text-muted-foreground'
  if (value > 0) return 'text-profit'
  if (value < 0) return 'text-loss'
  return 'text-foreground'
}

export function titleCase(text: string): string {
  return text.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}
