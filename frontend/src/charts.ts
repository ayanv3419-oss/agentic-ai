/**
 * Chart axis helpers — keeps date-bucket labels readable across granularities.
 *
 * Charts in this app receive a `series` of `{ bucket, sales, ... }` rows.
 * `bucket` is either:
 *   - YYYY-MM-DD  (daily / weekly buckets)
 *   - YYYY-MM     (monthly buckets)
 *   - YYYY        (yearly buckets)
 *
 * The helpers below pick a tick format and density that prevents label
 * collisions on the X axis regardless of the time range.
 *
 * Used by both the Dashboard charts and the AI Assistant chat-bubble chart;
 * the visualisation components themselves (BarChart / AreaChart) live in the
 * pages module since they're only consumed by their owner page.
 */

export type Granularity = 'daily' | 'weekly' | 'monthly' | 'yearly'

const MONTHS_SHORT = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
]

function parseBucket(bucket: unknown): Date | null {
  if (typeof bucket !== 'string' || !bucket) return null
  const m = /^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?/.exec(bucket)
  if (!m) return null
  const y = Number(m[1])
  const mo = m[2] ? Number(m[2]) - 1 : 0
  const d = m[3] ? Number(m[3]) : 1
  const dt = new Date(y, mo, d)
  return Number.isFinite(dt.getTime()) ? dt : null
}

interface SeriesPoint {
  bucket: string
}

/**
 * Best-effort granularity detection. Falls back to 'daily' for ambiguous /
 * empty inputs since that is the densest layout (most label spacing applied).
 */
export function inferGranularity(
  series: ReadonlyArray<SeriesPoint> | null | undefined,
): Granularity {
  if (!series || series.length === 0) return 'daily'
  const first = series[0]?.bucket
  if (typeof first !== 'string') return 'daily'

  if (/^\d{4}$/.test(first)) return 'yearly'
  if (/^\d{4}-\d{2}$/.test(first)) return 'monthly'

  if (series.length < 2) return 'daily'

  let total = 0
  let samples = 0
  for (let i = 1; i < series.length; i++) {
    const a = parseBucket(series[i - 1].bucket)
    const b = parseBucket(series[i].bucket)
    if (!a || !b) continue
    total += Math.abs(b.getTime() - a.getTime()) / 86_400_000
    samples++
  }
  const avg = samples > 0 ? total / samples : 1
  if (avg >= 200) return 'yearly'
  if (avg >= 25) return 'monthly'
  if (avg >= 5) return 'weekly'
  return 'daily'
}

/** Compact tick label for the X axis — never longer than ~7 chars. */
export function formatBucketTick(bucket: string, granularity: Granularity): string {
  const d = parseBucket(bucket)
  if (!d) return String(bucket ?? '')
  switch (granularity) {
    case 'yearly':
      return String(d.getFullYear())
    case 'monthly':
      return `${MONTHS_SHORT[d.getMonth()]} ${String(d.getFullYear()).slice(-2)}`
    case 'weekly':
      return `${MONTHS_SHORT[d.getMonth()]} ${d.getDate()}`
    case 'daily':
    default:
      return String(d.getDate())
  }
}

/** Verbose label used inside tooltips (full date so users can read it). */
export function formatBucketTooltip(bucket: string, granularity: Granularity): string {
  const d = parseBucket(bucket)
  if (!d) return String(bucket ?? '')
  switch (granularity) {
    case 'yearly':
      return String(d.getFullYear())
    case 'monthly':
      return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
    case 'weekly':
      return `Week of ${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}`
    case 'daily':
    default:
      return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
  }
}

export interface XAxisTickConfig {
  /** Minimum px gap Recharts must keep between two rendered ticks. */
  minTickGap: number
  /** Rotation angle in degrees (0 = horizontal). */
  angle: number
  /** SVG text-anchor matching the rotation. */
  textAnchor: 'middle' | 'end'
  /** Reserved height for the X-axis band so rotated labels don't clip. */
  height: number
  /** Recharts interval mode — always preserves first + last ticks. */
  interval: 'preserveStartEnd'
}

/**
 * Density-aware tick configuration. The numbers below are tuned for the
 * dashboard's ~700–900px chart widths and the chat's ~520px chat-bubble width;
 * they degrade gracefully on narrower viewports because Recharts will skip
 * ticks until `minTickGap` is satisfied.
 */
export function getXAxisTickConfig(
  seriesLength: number,
  granularity: Granularity,
): XAxisTickConfig {
  const labelChars =
    granularity === 'monthly' ? 6 :
    granularity === 'weekly'  ? 5 :
    granularity === 'yearly'  ? 4 : 2

  let minTickGap: number
  switch (granularity) {
    case 'yearly':  minTickGap = 36; break
    case 'monthly': minTickGap = 32; break
    case 'weekly':  minTickGap = 28; break
    case 'daily':
    default:        minTickGap = seriesLength > 31 ? 32 : (seriesLength > 14 ? 20 : 12)
  }

  const longLabels = labelChars >= 5
  const dense = seriesLength > 12
  const angle = longLabels && dense ? -32 : 0
  const textAnchor: 'middle' | 'end' = angle === 0 ? 'middle' : 'end'
  const height = angle === 0 ? 32 : 56

  return { minTickGap, angle, textAnchor, height, interval: 'preserveStartEnd' }
}
