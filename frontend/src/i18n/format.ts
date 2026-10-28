import { currentLocale } from './index'

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
]

/** List-style relative time, e.g. "3 分钟前" / "3 minutes ago" (GLB-086). */
export function formatRelativeTime(value: string | Date, now: Date = new Date()) {
  const seconds = Math.round((new Date(value).getTime() - now.getTime()) / 1000)
  const formatter = new Intl.RelativeTimeFormat(currentLocale(), { numeric: 'auto' })
  for (const [unit, size] of RELATIVE_UNITS) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit)
  }
  return formatter.format(Math.round(seconds / 60) || 0, 'minute')
}

/** Detail-style full time in the tenant's time zone (an IANA name such as "Asia/Shanghai"). */
export function formatDateTime(value: string | Date, timeZone?: string) {
  return new Intl.DateTimeFormat(currentLocale(), {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false, timeZone, timeZoneName: 'short',
  }).format(new Date(value))
}

/** Compact counts: 18200 -> "18.2k", 2480000 -> "2.48M". */
export function formatCompactNumber(value: number) {
  const abs = Math.abs(value)
  const trim = (n: number, digits: number) => String(Number(n.toFixed(digits)))
  if (abs >= 1e9) return `${trim(value / 1e9, 2)}B`
  if (abs >= 1e6) return `${trim(value / 1e6, 2)}M`
  if (abs >= 1e3) return `${trim(value / 1e3, 1)}k`
  return new Intl.NumberFormat(currentLocale()).format(value)
}

/** Percentage with one decimal; the input is a ratio (0.1234 -> "12.3%"). */
export function formatPercent(ratio: number) {
  return new Intl.NumberFormat(currentLocale(), {
    style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1,
  }).format(ratio)
}
