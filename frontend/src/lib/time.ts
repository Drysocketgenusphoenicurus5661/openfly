// Time helpers. Every timestamp from the backend is ISO 8601 with +05:30.
// The UI shows Indian Standard Time regardless of the browser's zone.

export const IST_OFFSET_SECONDS = 5.5 * 3600

const timeFormatter = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const timeWithSecondsFormatter = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

const dateFormatter = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})

export function parseIso(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatTime(iso: string | Date | null | undefined, seconds = false): string {
  const d = iso instanceof Date ? iso : parseIso(iso)
  if (!d) return '--:--'
  return (seconds ? timeWithSecondsFormatter : timeFormatter).format(d)
}

export function formatDate(iso: string | Date | null | undefined): string {
  const d = iso instanceof Date ? iso : parseIso(iso)
  if (!d) return '-'
  return dateFormatter.format(d)
}

export function formatDateTime(iso: string | Date | null | undefined): string {
  const d = iso instanceof Date ? iso : parseIso(iso)
  if (!d) return '-'
  return `${dateFormatter.format(d)} ${timeFormatter.format(d)}`
}

// Minutes since midnight IST for a timestamp, used to place markers on the
// session strip and to compare against HH:MM settings.
export function istMinutes(iso: string | Date | null | undefined): number | null {
  const d = iso instanceof Date ? iso : parseIso(iso)
  if (!d) return null
  const text = timeFormatter.format(d)
  const [h, m] = text.split(':').map(Number)
  return h * 60 + m
}

export function hmToMinutes(hm: string): number {
  const [h, m] = hm.split(':').map(Number)
  return (h || 0) * 60 + (m || 0)
}

export function minutesToHm(minutes: number): string {
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

// Builds an ISO timestamp in IST for a trading date and a wall clock time.
export function istIso(date: string, hm: string, seconds = 0): string {
  const [h, m] = hm.split(':').map(Number)
  return `${date}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(seconds).padStart(2, '0')}+05:30`
}

// openalgo-charts takes UTC seconds and formats the axis in the chart's
// timezone (Asia/Kolkata here), so no offset games are needed.
export function toChartTime(iso: string): number {
  const d = parseIso(iso)
  if (!d) return 0
  return Math.floor(d.getTime() / 1000)
}

export function nowIst(): Date {
  return new Date()
}

export function isValidHm(hm: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(hm)
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

const expiryPartsFormatter = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  day: '2-digit',
  month: '2-digit',
  year: '2-digit',
})

// Contract style expiry, for example 29-SEP-26.
export function formatExpiry(iso: string | null | undefined): string {
  const d = parseIso(iso?.length === 10 ? `${iso}T00:00:00+05:30` : iso)
  if (!d) return '-'
  const parts = expiryPartsFormatter.formatToParts(d)
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? ''
  const month = MONTHS[Number(get('month')) - 1] ?? get('month')
  return `${get('day')}-${month}-${get('year')}`
}

// "29-SEP-26 monthly"
export function describeExpiry(iso: string | null | undefined, selection?: string | null): string {
  const text = formatExpiry(iso)
  return selection ? `${text} ${selection}` : text
}
