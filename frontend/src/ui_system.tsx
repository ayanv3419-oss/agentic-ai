/**
 * ui_system — every routable page + chart axis helpers + PageHeader.
 *
 * Consolidates the former pages.tsx (Dashboard, ShopInfo, UploadData,
 * AiAssistant + their helpers and inner chart components), charts.ts
 * (granularity / tick / tooltip helpers), and PageHeader (formerly
 * exported from App.tsx).
 *
 * Pages own their charts; the App shell (Sidebar / TopBar / Login)
 * lives in app.tsx.
 */
/**
 * Page modules — every routable view in the app.
 *
 * Each page is exported as a named React component:
 *   - Dashboard       (KPIs + charts, with month / range filtering)
 *   - AiAssistant     (SSE chat over POST /query_stream)
 *   - UploadData      (CSV/XLSX upload + Google Drive sync UI + uploads table)
 *   - ShopInfo        (shop identity form)
 *
 * Subcomponents that are only used by one page (e.g. UploadsTable, ChatChart,
 * KPI cards, the Google glyph) live next to that page in this file. Anything
 * that's shared across pages — chart axis helpers, the API client, the global
 * store — lives in `charts.ts` / `api.ts`.
 */
import { useEffect, useId, useMemo, useRef, useState, type ComponentType, type ReactNode } from 'react'
import {
  AlertTriangle,
  BarChart2,
  Building2,
  Calendar,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Cloud,
  FileSpreadsheet,
  LayoutDashboard,
  Loader2,
  LogOut,
  MessageSquare,
  MessageSquarePlus,
  Mic,
  RefreshCw,
  Save,
  ShoppingBag,
  ShoppingCart,
  Send,
  Square,
  Trash2,
  TrendingUp,
  Upload,
  User2,
  Users,
  X,
  XCircle,
} from 'lucide-react'
import { Logo } from '@/logo'
import { useSpeechInput } from '@/use_speech_input'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import {
  ApiError,
  cn,
  clearAuth,
  confirmSchema,
  disconnectUpload,
  fetchAuthMe,
  fetchDashboard,
  fetchDriveStatus,
  fetchUploadsList,
  getUploadStatus,
  googleLoginUrl,
  logout,
  proposeSchema,
  streamAIQuery,
  syncDrive,
  uploadSales,
  uploadWorkbookAsync,
  useAppStore,
} from '@/client_core'
import type {
  AuthMe,
  ChartKind,
  ChatChartPayload,
  ChatMessage,
  DashboardData,
  DriveFile,
  PeriodTotals,
  RankingItem,
  SalesChart,
  SchemaMapping,
  SchemaProposal,
  SseEvent,
  ShopInfo as ShopInfoType,
  ToolEvent,
  UploadEntry,
  UploadJobStatus,
  UploadStatus,
} from '@/client_core'


// ===========================================================================
// Chart axis helpers (was @/charts)
// ===========================================================================

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

// ===========================================================================
// PageHeader — shared between every page (was exported from App.tsx)
// ===========================================================================

interface PageHeaderProps {
  icon: ReactNode
  title: string
  subtitle: string
  trailing?: ReactNode
}

export function PageHeader({ icon, title, subtitle, trailing }: PageHeaderProps) {
  return (
    <div className="flex items-start justify-between gap-4 flex-wrap">
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center shrink-0">
          {icon}
        </div>
        <div>
          <h1 className="text-xl md:text-2xl font-semibold tracking-tight">{title}</h1>
          <p className="text-sm text-zinc-500 mt-1 max-w-xl">{subtitle}</p>
        </div>
      </div>
      {trailing}
    </div>
  )
}

// ===========================================================================
// Shared chart palette + currency formatting helpers
// ===========================================================================

const PALETTE = {
  primary: '#10b981',
  grid:    '#27272a',
  tick:    '#a1a1aa',
}

function formatCurrency(n: number): string {
  return `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
}

function formatINR(n: number): string {
  return `₹${(n ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
}

function formatCompact(n: number): string {
  if (n >= 1_00_00_000) return `₹${(n / 1_00_00_000).toFixed(1)}Cr`
  if (n >= 1_00_000) return `₹${(n / 1_00_000).toFixed(1)}L`
  if (n >= 1_000) return `₹${(n / 1_000).toFixed(0)}k`
  return `₹${n}`
}

// Format a chart value using the LLM-supplied y_label. 'sales' (the
// back-compat default) uses the rupee formatter; counts/quantities/
// percents use plain numeric formats so the chart no longer shows ₹40
// next to a customer's purchase count.
function formatChartValue(n: number, yLabel?: string): string {
  const lbl = (yLabel ?? 'sales').toLowerCase()
  if (lbl === 'sales') return formatINR(n)
  if (lbl === 'percent') return `${(n ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}%`
  // count / quantity / anything-else → plain integer locale format
  return (n ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })
}

function formatChartCompact(n: number, yLabel?: string): string {
  const lbl = (yLabel ?? 'sales').toLowerCase()
  if (lbl === 'sales') return formatCompact(n)
  if (lbl === 'percent') return `${(n ?? 0).toFixed(0)}%`
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return `${n}`
}

function chartValueLabel(yLabel?: string): string {
  const lbl = (yLabel ?? 'sales').toLowerCase()
  if (lbl === 'sales')    return 'Sales'
  if (lbl === 'count')    return 'Count'
  if (lbl === 'quantity') return 'Quantity'
  if (lbl === 'percent')  return 'Percent'
  return lbl.charAt(0).toUpperCase() + lbl.slice(1)
}

function formatMonth(month: string): string {
  const [y, m] = month.split('-').map(Number)
  if (!y || !m) return month
  const d = new Date(y, m - 1, 1)
  return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}

// ===========================================================================
// Dashboard
// ===========================================================================

type QuickPickId = 'this' | 'last' | 'all'

const QUICK_PICKS: ReadonlyArray<{ id: QuickPickId; label: string }> = [
  { id: 'this', label: 'This month' },
  { id: 'last', label: 'Last month' },
  { id: 'all',  label: 'All time' },
]

function toMonthString(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function quickPickValue(id: QuickPickId): string {
  if (id === 'all') return ''
  const d = new Date()
  if (id === 'last') d.setMonth(d.getMonth() - 1)
  return toMonthString(d)
}

function activeQuickPick(month: string): QuickPickId | null {
  if (!month) return 'all'
  if (month === quickPickValue('this')) return 'this'
  if (month === quickPickValue('last')) return 'last'
  return null
}

function shiftMonth(month: string, delta: number): string {
  const base = month || quickPickValue('this')
  const [y, m] = base.split('-').map(Number)
  if (!y || !m) return quickPickValue('this')
  const d = new Date(y, m - 1 + delta, 1)
  return toMonthString(d)
}

export function Dashboard() {
  const filters = useAppStore((s) => s.filters)
  const setFilters = useAppStore((s) => s.setFilters)
  // Subscribe to the global serverDataVersion so an upload (or any other
  // server-side mutation) triggers an auto-reload without the user having
  // to click Refresh.
  const serverDataVersion = useAppStore((s) => s.serverDataVersion)

  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Ignore-stale guard. Rapid month-filter changes, the serverDataVersion
  // auto-reload, the focus refetch and the Refresh button all call load();
  // their fetches can resolve out of order, so a slow earlier response could
  // otherwise overwrite a newer one. Each call captures a sequence number and
  // only applies its result if it is still the most recent load in flight.
  const loadSeqRef = useRef(0)

  const load = async (month: string) => {
    const seq = ++loadSeqRef.current
    setLoading(true)
    setError(null)
    try {
      // Empty string means "all time" — fetchDashboard treats undefined as no filter.
      const result = await fetchDashboard(month || undefined)
      if (seq !== loadSeqRef.current) return // a newer load superseded this one
      setData(result)
    } catch (e) {
      if (seq !== loadSeqRef.current) return // stale failure — keep newer state
      if (e instanceof ApiError) {
        const detail = (e.detail as { detail?: string } | undefined)?.detail
        setError(detail ?? e.message)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Failed to load dashboard')
      }
      setData(null)
    } finally {
      // Only the latest load controls the spinner so a stale resolve can't
      // flip loading off while a newer request is still running.
      if (seq === loadSeqRef.current) setLoading(false)
    }
  }

  useEffect(() => {
    void load(filters.month)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.month])

  // Auto-reload when serverDataVersion bumps (after upload, unarchive,
  // KPI toggle, etc.). The initial mount-time load above already covers
  // the very first render, so this fires only when the version moves.
  // Read filters fresh from the store: the closure captured at effect
  // registration would otherwise reload the wrong month if the user
  // changed it between version bumps.
  useEffect(() => {
    if (serverDataVersion === null) return
    void load(useAppStore.getState().filters.month)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverDataVersion])

  // Re-fetch when the user returns to this tab. Handles the "I uploaded
  // in another tab and switched back" case without needing SSE push.
  useEffect(() => {
    const onFocus = () => { void load(filters.month) }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.month])

  const series = data?.series ?? []
  const granularity = useMemo(() => inferGranularity(series), [series])
  const tickConfig = useMemo(
    () => getXAxisTickConfig(series.length, granularity),
    [series.length, granularity],
  )
  const periodLabel = filters.month ? formatMonth(filters.month) : 'All time'
  const activePick = activeQuickPick(filters.month)
  // Unique gradient id for the Revenue Trend area fill (parallels the chat
  // chart). Keeps the <linearGradient> id collision-free even if the dashboard
  // ever renders alongside another instance.
  const trendGradientId = `trend-fill-${useId()}`

  return (
    <div className="animate-fade-in">
      <PageHeader
        icon={<LayoutDashboard className="w-5 h-5 text-emerald-400" />}
        title="Dashboard"
        subtitle="Your business at a glance. Pick a month to scope the numbers."
        trailing={
          <div className="flex flex-wrap items-center gap-2">
            <div className="hidden md:flex items-center gap-1 mr-1">
              {QUICK_PICKS.map((p) => {
                const isActive = activePick === p.id
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => setFilters({ month: quickPickValue(p.id) })}
                    aria-pressed={isActive}
                    className={cn(
                      'px-3 py-1.5 rounded-lg text-xs font-medium transition-colors border',
                      isActive
                        ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30'
                        : 'bg-zinc-900 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 border-zinc-800',
                    )}
                  >
                    {p.label}
                  </button>
                )
              })}
            </div>

            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => setFilters({ month: shiftMonth(filters.month, -1) })}
                className="inline-flex items-center justify-center w-9 h-9 rounded-lg border border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 transition-colors"
                aria-label="Previous month"
                title="Previous month"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <input
                type="month"
                value={filters.month}
                onChange={(e) => setFilters({ month: e.target.value })}
                className="input w-auto"
                aria-label="Select month"
              />
              <button
                type="button"
                onClick={() => setFilters({ month: shiftMonth(filters.month, +1) })}
                className="inline-flex items-center justify-center w-9 h-9 rounded-lg border border-zinc-800 bg-zinc-900 text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 transition-colors"
                aria-label="Next month"
                title="Next month"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>

            <button
              onClick={() => void load(filters.month)}
              disabled={loading}
              className="btn btn-secondary"
              title="Refresh"
            >
              <RefreshCw className={loading ? 'w-4 h-4 animate-spin' : 'w-4 h-4'} />
              <span className="hidden sm:inline">Refresh</span>
            </button>
          </div>
        }
      />

      {error && (
        <div className="mt-6 card p-4 border-red-900/40 bg-red-950/20 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="mt-10 grid sm:grid-cols-2 lg:grid-cols-4 gap-5">
        <HeroKpi
          label="Total Sales"
          value={data ? formatCurrency(data.kpis.total_sales) : '—'}
          period={periodLabel}
        />
        <SimpleKpi
          icon={<ShoppingBag className="w-5 h-5" />}
          label="Orders"
          value={data ? data.kpis.orders.toLocaleString('en-IN') : '—'}
        />
        <SimpleKpi
          icon={<Users className="w-5 h-5" />}
          label="Customers"
          value={data ? data.kpis.customers.toLocaleString('en-IN') : '—'}
        />
        <SimpleKpi
          icon={<TrendingUp className="w-5 h-5" />}
          label="Avg Order"
          value={
            data && data.kpis.orders > 0
              ? formatCurrency(data.kpis.total_sales / data.kpis.orders)
              : '—'
          }
        />
      </div>

      {/* No-data empty state: replaces charts when nothing has been uploaded yet */}
      {!loading && !error && data && data.kpis.orders === 0 && data.series.length === 0 ? (
        <div className="mt-8 card p-10 flex flex-col items-center justify-center text-center gap-4 border-dashed">
          <div className="w-16 h-16 rounded-2xl bg-zinc-900 border border-zinc-800 flex items-center justify-center">
            <BarChart2 className="w-8 h-8 text-zinc-700" />
          </div>
          <div>
            <div className="text-base font-semibold text-zinc-300">No data yet</div>
            <div className="mt-1 text-sm text-zinc-500 max-w-xs">
              Upload a CSV or connect Google Drive to see your sales charts.
            </div>
          </div>
        </div>
      ) : (
        <>
      <section className="mt-8 card p-7">
        <SectionHeader
          title="Sales Performance"
          hint={
            filters.month
              ? `Daily sales for ${periodLabel}`
              : 'All recorded sales, bucketed by date'
          }
        />
        <div className="mt-4 -ml-2" style={{ height: 320 + Math.max(0, tickConfig.height - 32) }}>
          <ResponsiveContainer>
            <BarChart
              data={series}
              margin={{ top: 12, right: 12, left: 0, bottom: 4 }}
            >
              <CartesianGrid stroke={PALETTE.grid} strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="bucket"
                stroke={PALETTE.tick}
                fontSize={12}
                tickLine={false}
                axisLine={false}
                tickMargin={10}
                interval={tickConfig.interval}
                minTickGap={tickConfig.minTickGap}
                angle={tickConfig.angle}
                textAnchor={tickConfig.textAnchor}
                height={tickConfig.height}
                tickFormatter={(b: string) => formatBucketTick(b, granularity)}
              />
              <YAxis
                stroke={PALETTE.tick}
                fontSize={12}
                tickLine={false}
                axisLine={false}
                width={56}
                tickFormatter={(v: number) => formatCompact(v)}
              />
              <Tooltip
                contentStyle={{
                  background: '#0a0a0a',
                  border: '1px solid #27272a',
                  borderRadius: 10,
                  fontSize: 13,
                  padding: '10px 12px',
                }}
                cursor={{ fill: 'rgba(63, 63, 70, 0.3)' }}
                formatter={(v: number) => [formatCurrency(v), 'Sales']}
                labelFormatter={(l: string) => formatBucketTooltip(l, granularity)}
              />
              <Bar dataKey="sales" fill={PALETTE.primary} radius={[6, 6, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="mt-5 card p-7">
        <SectionHeader
          title="Revenue Trend"
          hint={
            filters.month
              ? `How sales moved through ${periodLabel}`
              : 'Revenue trend across all recorded data'
          }
        />
        <div className="mt-4 -ml-2" style={{ height: 288 + Math.max(0, tickConfig.height - 32) }}>
          <ResponsiveContainer>
            <AreaChart
              data={series}
              margin={{ top: 12, right: 12, left: 0, bottom: 4 }}
            >
              <defs>
                <linearGradient id={trendGradientId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={PALETTE.primary} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={PALETTE.primary} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={PALETTE.grid} strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="bucket"
                stroke={PALETTE.tick}
                fontSize={12}
                tickLine={false}
                axisLine={false}
                tickMargin={10}
                interval={tickConfig.interval}
                minTickGap={tickConfig.minTickGap}
                angle={tickConfig.angle}
                textAnchor={tickConfig.textAnchor}
                height={tickConfig.height}
                tickFormatter={(b: string) => formatBucketTick(b, granularity)}
              />
              <YAxis
                stroke={PALETTE.tick}
                fontSize={12}
                tickLine={false}
                axisLine={false}
                width={56}
                tickFormatter={(v: number) => formatCompact(v)}
              />
              <Tooltip
                contentStyle={{
                  background: '#0a0a0a',
                  border: '1px solid #27272a',
                  borderRadius: 10,
                  fontSize: 13,
                  padding: '10px 12px',
                }}
                cursor={{ stroke: '#3f3f46', strokeDasharray: '3 3' }}
                formatter={(v: number) => [formatCurrency(v), 'Revenue']}
                labelFormatter={(l: string) => formatBucketTooltip(l, granularity)}
              />
              <Area type="monotone" dataKey="sales" stroke={PALETTE.primary} strokeWidth={2.5} fill={`url(#${trendGradientId})`} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </section>

      <MonthlySalesPie data={data?.monthly_sales_pie ?? []} />
        </>
      )}
    </div>
  )
}

// ===========================================================================
// Monthly Sales Distribution — pie chart
// ===========================================================================

const PIE_PALETTE = [
  '#10b981', '#34d399', '#6ee7b7', '#22d3ee', '#0ea5e9',
  '#6366f1', '#8b5cf6', '#a855f7', '#ec4899', '#f43f5e',
  '#f97316', '#f59e0b', '#eab308', '#84cc16', '#14b8a6',
]

interface MonthlySalesPieProps {
  data: Array<{ month: string; sales: number }>
}

function MonthlySalesPie({ data }: MonthlySalesPieProps) {
  const total = data.reduce((acc, d) => acc + (d.sales || 0), 0)

  return (
    <section className="mt-5 card p-7">
      <SectionHeader
        title="Monthly Sales Distribution"
        hint="Share of total sales by month — across the full uploaded history."
      />
      {data.length === 0 ? (
        <div className="mt-6 text-sm text-zinc-500">
          No monthly sales data available.
        </div>
      ) : (
        <div className="mt-4 grid lg:grid-cols-[1fr_auto] gap-6 items-center">
          <div style={{ height: 320 }}>
            <ResponsiveContainer>
              <PieChart>
                <Pie
                  data={data}
                  dataKey="sales"
                  nameKey="month"
                  cx="50%"
                  cy="50%"
                  innerRadius={60}
                  outerRadius={120}
                  paddingAngle={data.length > 1 ? 2 : 0}
                  stroke="#0a0a0a"
                  strokeWidth={2}
                >
                  {data.map((entry, idx) => (
                    <Cell
                      key={entry.month}
                      fill={PIE_PALETTE[idx % PIE_PALETTE.length]}
                    />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{
                    background: '#0a0a0a',
                    border: '1px solid #27272a',
                    borderRadius: 10,
                    fontSize: 13,
                    padding: '10px 12px',
                  }}
                  formatter={(v: number, _name, ctx) => {
                    const pct = total > 0 ? ((v / total) * 100).toFixed(1) : '0.0'
                    const month =
                      (ctx?.payload as { month?: string } | undefined)?.month ?? ''
                    return [`${formatCurrency(v)} (${pct}%)`, month]
                  }}
                />
                <Legend
                  verticalAlign="middle"
                  align="right"
                  layout="vertical"
                  iconType="circle"
                  wrapperStyle={{ fontSize: 12, color: '#a1a1aa', paddingLeft: 16 }}
                  formatter={(value: string) => (
                    <span style={{ color: '#d4d4d8' }}>{value}</span>
                  )}
                />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="hidden lg:block text-xs text-zinc-500 max-w-[180px]">
            <div className="text-zinc-400 font-medium mb-1">
              {data.length} {data.length === 1 ? 'month' : 'months'}
            </div>
            <div>Total sales {formatCurrency(total)}</div>
          </div>
        </div>
      )}
    </section>
  )
}

interface HeroKpiProps {
  label: string
  value: string
  period: string
}

function HeroKpi({ label, value, period }: HeroKpiProps) {
  return (
    <div className="card p-6 animate-slide-up bg-gradient-to-br from-emerald-950/40 to-zinc-900/20 border-emerald-900/30">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-zinc-400 uppercase tracking-wider">{label}</span>
        <TrendingUp className="w-4 h-4 text-emerald-400" />
      </div>
      <div className="mt-3 text-3xl font-semibold tracking-tight text-emerald-50">{value}</div>
      <div className="mt-1.5 text-xs text-zinc-500">{period}</div>
    </div>
  )
}

interface SimpleKpiProps {
  icon: React.ReactNode
  label: string
  value: string
}

function SimpleKpi({ icon, label, value }: SimpleKpiProps) {
  return (
    <div className="card p-6 animate-slide-up">
      <div className="flex items-center justify-between text-zinc-400">
        <span className="text-sm font-medium uppercase tracking-wide">{label}</span>
        <span className="text-zinc-500">{icon}</span>
      </div>
      <div className="mt-2 text-3xl font-semibold tracking-tight">{value}</div>
    </div>
  )
}

function SectionHeader({ title, hint }: { title: string; hint?: string }) {
  return (
    <div>
      <h3 className="text-lg font-semibold tracking-tight">{title}</h3>
      {hint && <p className="text-sm text-zinc-500 mt-1">{hint}</p>}
    </div>
  )
}

// ===========================================================================
// AI Assistant (SSE chat)
// ===========================================================================

const TOOL_LABELS: Record<string, string> = {
  RouteClassifier: 'Understanding your question',
  TimeKPI: 'Resolving time window and metric',
  SchemaRetriever: 'Loading data schema',
  SqlWriter: 'Writing the query',
  SqlExecutor: 'Running the query',
  ResponseFormatter: 'Formatting the response',
  ResponseStored: 'Saving the response',
  Database: 'Storing data',
  // AgenticLoop capabilities — the names the LLM picks from.
  resolve_time_window: 'Resolving the time window',
  resolve_entities: 'Resolving named entities',
  run_data_query: 'Querying your data',
  generate_narrative: 'Writing the analysis',
}

interface Suggestion {
  icon: ComponentType<{ className?: string }>
  text: string
}

const SUGGESTIONS: Suggestion[] = [
  { icon: TrendingUp,     text: 'Sales this month' },
  { icon: ShoppingCart,   text: 'Top products' },
  { icon: Users,          text: 'Customer count' },
  { icon: AlertTriangle,  text: 'Lowest revenue day' },
  { icon: BarChart2,      text: 'Revenue breakdown' },
  { icon: Calendar,       text: 'Weekly trend' },
]

const newId = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`

export function AiAssistant() {
  const liveMessages = useAppStore((s) => s.chatHistory)
  const isStreaming = useAppStore((s) => s.isStreaming)
  const appendMessage = useAppStore((s) => s.appendMessage)
  const updateMessage = useAppStore((s) => s.updateMessage)
  const clearChat = useAppStore((s) => s.clearChat)
  const setStreaming = useAppStore((s) => s.setStreaming)

  // Recents (read-only history) — server-sourced session list + the
  // currently-open read-only transcript. When viewingSessionId is null the
  // live chat renders exactly as before; otherwise the main area shows the
  // fetched viewedMessages read-only and the composer is hidden.
  const sessions = useAppStore((s) => s.sessions)
  const viewingSessionId = useAppStore((s) => s.viewingSessionId)
  const viewedMessages = useAppStore((s) => s.viewedMessages)
  const refreshSessions = useAppStore((s) => s.refreshSessions)
  const closeSession = useAppStore((s) => s.closeSession)
  const isViewing = viewingSessionId !== null
  // The transcript shown in the main scroller: the read-only session when one
  // is open, otherwise the live chat.
  const messages = isViewing ? viewedMessages : liveMessages

  // Refresh the Recents list on mount.
  useEffect(() => { void refreshSessions() }, [refreshSessions])

  // Refresh after each turn finishes so a brand-new session and its (async,
  // best-effort) server-generated title surface in the list. We watch the
  // streaming flag falling edge: when a turn ends, isStreaming flips true→false.
  const wasStreamingRef = useRef(false)
  useEffect(() => {
    if (wasStreamingRef.current && !isStreaming) {
      void refreshSessions()
    }
    wasStreamingRef.current = isStreaming
  }, [isStreaming, refreshSessions])
  // Banner inputs: when serverDataVersion advances past the version
  // captured at conversation-start, the next reply uses fresher data
  // than the answers already on screen. We surface that explicitly so
  // the user isn't confused if numbers shift between turns.
  const serverDataVersion = useAppStore((s) => s.serverDataVersion)
  const conversationStartVersion = useAppStore((s) => s.conversationStartVersion)
  const dataChangedMidConversation =
    serverDataVersion !== null
    && conversationStartVersion !== null
    && serverDataVersion > conversationStartVersion
    && messages.length > 0
  const [bannerDismissed, setBannerDismissed] = useState(false)
  // A new conversation always resets the dismiss state so the banner can
  // legitimately surface on the next mid-conversation change.
  useEffect(() => { setBannerDismissed(false) }, [conversationStartVersion])

  const [input, setInput] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Voice dictation: the mic button streams recognized speech into the
  // composer. We remember whatever was already typed when listening started
  // (baseText) and append the running transcript to it, so dictation adds to —
  // never clobbers — text the user typed by hand. Sending stays manual
  // (Enter / Send), so the rest of the chat flow is untouched.
  const baseTextRef = useRef('')
  const speech = useSpeechInput((transcript) => {
    const base = baseTextRef.current.trim()
    setInput(base ? `${base} ${transcript}` : transcript)
  })
  const handleMicToggle = () => {
    if (speech.listening) {
      speech.stop()
      return
    }
    baseTextRef.current = input
    speech.start()
    textareaRef.current?.focus()
  }

  // autoscroll on new content
  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages, isStreaming])

  // autosize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`
  }, [input])

  const canSend = useMemo(
    () => input.trim().length > 0 && !isStreaming,
    [input, isStreaming],
  )

  const stop = () => {
    abortRef.current?.abort()
    abortRef.current = null
  }

  const handleSend = async () => {
    const text = input.trim()
    if (!text || isStreaming) return
    // Claim the streaming lock immediately after the guard so a second
    // Enter/submit fired before the awaits below can't slip through and
    // start a concurrent stream.
    setStreaming(true)

    // Sending ends any in-progress dictation.
    speech.stop()

    const userMsg: ChatMessage = {
      id: newId(),
      role: 'user',
      content: text,
      status: 'done',
      toolEvents: [],
      timestamp: new Date().toISOString(),
    }
    const assistantMsg: ChatMessage = {
      id: newId(),
      role: 'assistant',
      content: '',
      status: 'thinking',
      toolEvents: [],
      timestamp: new Date().toISOString(),
    }

    appendMessage(userMsg)
    appendMessage(assistantMsg)
    setInput('')

    const ctrl = new AbortController()
    abortRef.current = ctrl

    let toolEvents: ToolEvent[] = []
    // The AgenticLoop emits `loop.iteration` (LLM picked capability X, here's
    // why) immediately before the matching `tool.call`. Stash the reasoning
    // and attach it to the tool event the next `tool.call` creates.
    let pendingReasoning: string | undefined

    try {
      await streamAIQuery(
        text,
        (e: SseEvent) => {
          const data = e.data as Record<string, unknown> | undefined
          switch (e.event) {
            case 'loop.iteration': {
              const reasoning = data?.reasoning ? String(data.reasoning) : ''
              pendingReasoning = reasoning || undefined
              break
            }
            case 'tool.call': {
              const tool = String(data?.name ?? '')
              if (!tool) return
              toolEvents = [
                ...toolEvents,
                { tool, status: 'running', reasoning: pendingReasoning },
              ]
              pendingReasoning = undefined
              updateMessage(assistantMsg.id, {
                status: 'streaming',
                toolEvents,
              })
              break
            }
            case 'tool.result': {
              const tool = String(data?.name ?? '')
              const ok = Boolean(data?.ok)
              const duration = Number(data?.duration_ms ?? 0)
              const error = data?.error ? String(data.error) : undefined
              // Fix 4: match the LAST still-running entry for this tool name so
              // a tool called twice in one loop doesn't clobber the first entry.
              let matched = false
              const updated = [...toolEvents].reverse().map((te) => {
                if (!matched && te.tool === tool && te.status === 'running') {
                  matched = true
                  return { ...te, status: (ok ? 'done' : 'failed') as ToolEvent['status'], durationMs: duration, error }
                }
                return te
              }).reverse()
              toolEvents = updated
              updateMessage(assistantMsg.id, { toolEvents })
              break
            }
            case 'final': {
              const answer = String(data?.answer ?? '')
              // The final payload may carry either the simple W5 chart
              // ({kind,title,points[]}) or the legacy SalesChart. Stash
              // whichever arrived; MessageRow branches on the shape.
              const chart = (data?.chart ?? null) as
                | SalesChart
                | ChatChartPayload
                | null
              updateMessage(assistantMsg.id, {
                content: answer,
                chart,
                status: 'done',
                toolEvents,
              })
              break
            }
            case 'agent.result': {
              const err = data?.error
              if (err) {
                updateMessage(assistantMsg.id, {
                  status: 'error',
                  error: String(err),
                  toolEvents,
                })
              }
              break
            }
          }
        },
        { signal: ctrl.signal },
      )

      // If the server closed without a `final` event, fall back to a generic message.
      const cur = useAppStore.getState().chatHistory.find((m) => m.id === assistantMsg.id)
      if (cur && cur.status !== 'done' && cur.status !== 'error') {
        updateMessage(assistantMsg.id, {
          status: 'done',
          content: cur.content || 'No answer was produced.',
        })
      }
    } catch (e) {
      const isAbort = e instanceof DOMException && e.name === 'AbortError'
      if (isAbort) {
        updateMessage(assistantMsg.id, {
          status: 'done',
          content: 'Stopped.',
          toolEvents,
        })
      } else {
        const message =
          e instanceof ApiError
            ? `${e.message}${e.detail ? ` — ${JSON.stringify(e.detail)}` : ''}`
            : e instanceof Error
              ? e.message
              : 'Unknown error'
        updateMessage(assistantMsg.id, {
          status: 'error',
          error: message,
          toolEvents,
        })
      }
    } finally {
      abortRef.current = null
      setStreaming(false)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void handleSend()
    }
  }

  // "New chat" mints a fresh conversation (clearChat rotates conversationId)
  // and drops out of any read-only session view back to the live composer.
  const handleNewChat = () => {
    clearChat()
    closeSession()
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
        {isViewing ? (
          <ReadOnlyHeader
            title={sessions.find((s) => s.id === viewingSessionId)?.title || 'Conversation'}
            onBack={closeSession}
          />
        ) : (
          <ChatHeader onClear={clearChat} hasMessages={messages.length > 0} />
        )}

        <div ref={scrollerRef} className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-6 md:px-10 py-8">
            {messages.length === 0 ? (
              isViewing ? (
                <p className="py-16 text-center text-sm text-zinc-500">
                  This conversation has no messages.
                </p>
              ) : (
                <EmptyState onPick={(s) => setInput(s)} />
              )
            ) : (
              <div className="space-y-6">
                {messages.map((m) => (
                  <MessageRow key={m.id} message={m} />
                ))}
              </div>
            )}
          </div>
        </div>

        {isViewing ? (
          <div className="border-t border-zinc-800 bg-zinc-950">
            <div className="max-w-3xl mx-auto px-6 md:px-10 py-4">
              <div className="flex items-center justify-between gap-3 rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3">
                <p className="text-xs text-zinc-500">
                  You're viewing a past conversation. Start a new chat to ask more.
                </p>
                <button
                  type="button"
                  onClick={handleNewChat}
                  className="btn btn-primary h-9 px-3 shrink-0"
                  title="Start a new chat"
                >
                  <MessageSquarePlus className="w-3.5 h-3.5" />
                  <span className="hidden sm:inline">New chat</span>
                </button>
              </div>
              <p className="mt-2 text-[11px] text-zinc-600 text-center">
                <button
                  type="button"
                  onClick={closeSession}
                  className="text-zinc-500 hover:text-zinc-300 underline-offset-2 hover:underline"
                >
                  Back to live chat
                </button>
              </p>
            </div>
          </div>
        ) : (
          <div className="border-t border-zinc-800 bg-zinc-950">
            <div className="max-w-3xl mx-auto px-6 md:px-10 py-4">
              {dataChangedMidConversation && !bannerDismissed && (
                <div className="mb-3 flex items-start gap-2 text-xs text-emerald-300/90 bg-emerald-950/30 border border-emerald-900/40 rounded-md px-3 py-2">
                  <span className="mt-0.5">📊</span>
                  <span className="flex-1">
                    Data updated since this conversation started — new replies will use the latest data.
                  </span>
                  <button
                    type="button"
                    onClick={() => setBannerDismissed(true)}
                    className="text-emerald-400/60 hover:text-emerald-200 text-[10px] uppercase tracking-wide"
                    aria-label="Dismiss banner"
                  >
                    Dismiss
                  </button>
                </div>
              )}
              <div className="relative flex items-end gap-2 rounded-2xl border border-zinc-800 bg-zinc-900/60 focus-within:border-emerald-500/50 transition-colors p-2">
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={onKeyDown}
                  rows={1}
                  placeholder={
                    speech.listening
                      ? 'Listening… speak now'
                      : 'Ask anything about your business…'
                  }
                  className="flex-1 resize-none bg-transparent px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none"
                />
                {speech.supported && (
                  <button
                    type="button"
                    onClick={handleMicToggle}
                    className={cn(
                      'inline-flex items-center justify-center rounded-lg h-9 w-9 shrink-0 transition-colors',
                      speech.listening
                        ? 'bg-red-500/15 text-red-400 ring-1 ring-red-500/40 animate-pulse'
                        : 'text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800',
                    )}
                    title={speech.listening ? 'Stop dictation' : 'Dictate — speak instead of typing'}
                    aria-label={speech.listening ? 'Stop voice dictation' : 'Start voice dictation'}
                    aria-pressed={speech.listening}
                  >
                    <Mic className="w-4 h-4" />
                  </button>
                )}
                {isStreaming ? (
                  <button
                    type="button"
                    onClick={stop}
                    className="btn btn-secondary h-9 px-3"
                    title="Stop"
                  >
                    <Square className="w-3.5 h-3.5" />
                    <span className="hidden sm:inline">Stop</span>
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void handleSend()}
                    disabled={!canSend}
                    className="btn btn-primary h-9 px-3"
                    title="Send"
                  >
                    <Send className="w-3.5 h-3.5" />
                    <span className="hidden sm:inline">Send</span>
                  </button>
                )}
              </div>
              {speech.error && (
                <p className="mt-2 text-[11px] text-red-400 text-center">{speech.error}</p>
              )}
              <p className="mt-2 text-[11px] text-zinc-600 text-center">
                Press Enter to send · Shift + Enter for newline
              </p>
            </div>
          </div>
        )}
      </div>
  )
}

// Header shown while viewing a read-only past conversation: title + a control
// back to the live chat (the composer is hidden in this mode).
function ReadOnlyHeader({ title, onBack }: { title: string; onBack: () => void }) {
  return (
    <div className="border-b border-zinc-800 px-6 md:px-10 py-4 flex items-center justify-between">
      <div className="flex items-center gap-3 min-w-0">
        <div className="w-9 h-9 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center shrink-0">
          <MessageSquare className="w-4 h-4 text-zinc-400" />
        </div>
        <div className="min-w-0">
          <h1 className="text-base font-semibold tracking-tight truncate">{title}</h1>
          <p className="text-xs text-zinc-500">Past conversation · read-only</p>
        </div>
      </div>
      <button
        type="button"
        onClick={onBack}
        className="btn btn-ghost shrink-0"
        title="Back to live chat"
      >
        <X className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">Close</span>
      </button>
    </div>
  )
}

function ChatHeader({ onClear, hasMessages }: { onClear: () => void; hasMessages: boolean }) {
  return (
    <div className="border-b border-zinc-800 px-6 md:px-10 py-4 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center">
          <Logo className="w-4 h-4 text-emerald-400" />
        </div>
        <div>
          <h1 className="text-base font-semibold tracking-tight">AI Assistant</h1>
          <p className="text-xs text-zinc-500">Ask about sales, revenue, or trends in plain English.</p>
        </div>
      </div>
      <button
        type="button"
        onClick={() => {
          if (!hasMessages) return
          if (!confirm('Clear chat history?')) return
          onClear()
        }}
        disabled={!hasMessages}
        className="btn btn-ghost"
        title="Clear chat"
      >
        <Trash2 className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">Clear</span>
      </button>
    </div>
  )
}

function EmptyState({ onPick }: { onPick: (s: string) => void }) {
  return (
    <div className="py-16 animate-fade-in">
      <div className="text-center">
        <div className="inline-flex w-14 h-14 rounded-2xl bg-emerald-500/10 border border-emerald-500/30 items-center justify-center mb-5 shadow-lg shadow-emerald-900/20">
          <Logo className="w-6 h-6 text-emerald-400" />
        </div>
        <h2 className="text-2xl font-semibold tracking-tight">How can I help today?</h2>
        <p className="mt-2 text-sm text-zinc-500 max-w-sm mx-auto">
          Ask in plain English — I'll query your data, run the analysis, and explain the result.
        </p>
      </div>

      <div className="mt-10 grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {SUGGESTIONS.map((s) => {
          const Icon = s.icon
          return (
            <button
              key={s.text}
              type="button"
              onClick={() => onPick(s.text)}
              className="group text-left card px-4 py-3.5 hover:border-emerald-500/30 hover:bg-emerald-950/20 transition-all duration-150"
            >
              <div className="flex items-center gap-3">
                <span className="w-7 h-7 rounded-lg bg-zinc-800 border border-zinc-700 flex items-center justify-center shrink-0 group-hover:bg-emerald-500/10 group-hover:border-emerald-500/30 transition-colors">
                  <Icon className="w-3.5 h-3.5 text-zinc-400 group-hover:text-emerald-400 transition-colors" />
                </span>
                <span className="text-sm text-zinc-300 group-hover:text-zinc-100 transition-colors">{s.text}</span>
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ---- Lightweight Markdown renderer ---------------------------------------
// The Coordinator answers in Markdown — GFM pipe tables, **bold**, lists and
// headings. We render the common subset inline instead of pulling in a
// markdown dependency (keeps the bundle lean + offline-buildable). Anything
// we don't recognise falls through as plain text.

function mdSplitRow(row: string): string[] {
  let t = row.trim()
  if (t.startsWith('|')) t = t.slice(1)
  if (t.endsWith('|')) t = t.slice(0, -1)
  return t.split('|').map((c) => c.trim())
}

function mdIsTableSeparator(row: string | undefined): boolean {
  if (!row) return false
  const t = row.trim()
  if (!t.includes('-') || !t.includes('|')) return false
  const cells = mdSplitRow(t)
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c))
}

type MdAlign = 'left' | 'right' | 'center'

function mdAlignClass(a: MdAlign): string {
  return a === 'right' ? 'text-right' : a === 'center' ? 'text-center' : 'text-left'
}

// Inline spans: **bold**, `code`, *italic*. Everything else is plain text.
function mdInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = []
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g
  let last = 0
  let n = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('**')) {
      out.push(
        <strong key={`${keyPrefix}-b${n}`} className="font-semibold text-white">
          {tok.slice(2, -2)}
        </strong>,
      )
    } else if (tok.startsWith('`')) {
      out.push(
        <code
          key={`${keyPrefix}-c${n}`}
          className="rounded bg-zinc-800 px-1 py-0.5 font-mono text-[0.85em] text-emerald-300"
        >
          {tok.slice(1, -1)}
        </code>,
      )
    } else {
      out.push(<em key={`${keyPrefix}-i${n}`}>{tok.slice(1, -1)}</em>)
    }
    last = m.index + tok.length
    n++
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

function Markdown({ source }: { source: string }) {
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const blocks: ReactNode[] = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const trimmed = lines[i].trim()
    if (trimmed === '') {
      i++
      continue
    }

    // Table — a row with "|" immediately followed by a separator row.
    if (trimmed.includes('|') && i + 1 < lines.length && mdIsTableSeparator(lines[i + 1])) {
      const header = mdSplitRow(trimmed)
      const aligns: MdAlign[] = mdSplitRow(lines[i + 1]).map((c) =>
        c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : 'left',
      )
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].trim() !== '' && lines[i].includes('|')) {
        rows.push(mdSplitRow(lines[i]))
        i++
      }
      const tk = key++
      blocks.push(
        <div key={`md-t${tk}`} className="my-2 overflow-x-auto rounded-lg border border-zinc-800">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="bg-zinc-800/50">
                {header.map((h, hi) => (
                  <th
                    key={hi}
                    className={cn(
                      'border-b border-zinc-700 px-3 py-2 font-semibold text-zinc-200',
                      mdAlignClass(aligns[hi] ?? 'left'),
                    )}
                  >
                    {mdInline(h, `md-t${tk}h${hi}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} className="even:bg-zinc-900/40">
                  {r.map((c, ci) => (
                    <td
                      key={ci}
                      className={cn(
                        'border-b border-zinc-800/70 px-3 py-1.5 text-zinc-300',
                        mdAlignClass(aligns[ci] ?? 'left'),
                      )}
                    >
                      {mdInline(c, `md-t${tk}r${ri}c${ci}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    // Heading (#..######).
    const hm = /^(#{1,6})\s+(.*)$/.exec(trimmed)
    if (hm) {
      const level = (hm[1] ?? '').length
      const cls =
        level <= 1
          ? 'text-base font-semibold text-white'
          : level === 2
            ? 'text-sm font-semibold text-zinc-100'
            : 'text-sm font-medium text-zinc-200'
      const hk = key++
      blocks.push(
        <p key={`md-h${hk}`} className={cn('mt-1', cls)}>
          {mdInline(hm[2] ?? '', `md-h${hk}`)}
        </p>,
      )
      i++
      continue
    }

    // Bullet list (- or *).
    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, ''))
        i++
      }
      const lk = key++
      blocks.push(
        <ul key={`md-ul${lk}`} className="list-disc space-y-1 pl-5 text-zinc-100">
          {items.map((it, ii) => (
            <li key={ii}>{mdInline(it, `md-ul${lk}i${ii}`)}</li>
          ))}
        </ul>,
      )
      continue
    }

    // Numbered list (1. 2. ...).
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+\.\s+/, ''))
        i++
      }
      const lk = key++
      blocks.push(
        <ol key={`md-ol${lk}`} className="list-decimal space-y-1 pl-5 text-zinc-100">
          {items.map((it, ii) => (
            <li key={ii}>{mdInline(it, `md-ol${lk}i${ii}`)}</li>
          ))}
        </ol>,
      )
      continue
    }

    // Paragraph — gather consecutive plain lines into one wrapped block.
    const para: string[] = []
    while (i < lines.length) {
      const t = lines[i].trim()
      if (t === '' || /^(#{1,6})\s+/.test(t) || /^[-*]\s+/.test(t) || /^\d+\.\s+/.test(t)) break
      if (t.includes('|') && i + 1 < lines.length && mdIsTableSeparator(lines[i + 1])) break
      para.push(t)
      i++
    }
    if (para.length > 0) {
      const pk = key++
      blocks.push(
        <p key={`md-p${pk}`} className="leading-relaxed text-zinc-100">
          {mdInline(para.join(' '), `md-p${pk}`)}
        </p>,
      )
    }
  }

  return <div className="space-y-2 text-sm">{blocks}</div>
}

function MessageRow({ message }: { message: ChatMessage }) {
  if (message.role === 'user') {
    return (
      <div className="flex items-start gap-3 justify-end animate-slide-up">
        <div className="max-w-[85%] rounded-2xl rounded-tr-md bg-emerald-500/15 border border-emerald-500/30 px-4 py-2.5">
          <p className="whitespace-pre-wrap text-sm text-zinc-50 leading-relaxed">
            {message.content}
          </p>
        </div>
        <div className="w-8 h-8 rounded-full bg-zinc-900 border border-zinc-800 flex items-center justify-center shrink-0">
          <User2 className="w-3.5 h-3.5 text-zinc-400" />
        </div>
      </div>
    )
  }

  const showThinking = message.status === 'thinking' || message.status === 'streaming'
  const isError = message.status === 'error'

  return (
    <div className="flex items-start gap-3 animate-slide-up">
      <div className="w-8 h-8 rounded-full bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center shrink-0">
        <Logo className="w-3.5 h-3.5 text-emerald-400" />
      </div>
      <div className="min-w-0 flex-1 max-w-[85%]">
        {showThinking && <ThinkingBlock toolEvents={message.toolEvents} />}

        {message.chart &&
          (isChatChartPayload(message.chart) ? (
            <PointsChart chart={message.chart} />
          ) : (
            <ChatChart chart={message.chart} />
          ))}

        {message.content && (
          <div
            className={cn(
              'rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3',
              message.chart && 'mt-2',
            )}
          >
            <Markdown source={message.content} />
          </div>
        )}

        {isError && message.error && (
          <div className="rounded-2xl rounded-tl-md bg-red-950/40 border border-red-900/50 px-4 py-3 mt-1">
            <div className="flex items-start gap-2 text-sm text-red-300">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <div>
                <div className="font-medium">Something went wrong</div>
                <div className="text-xs text-red-400/80 mt-0.5 break-words">{message.error}</div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ---- Chart visualisation chosen by `chart.kind`. -------------------------
// summary / trend / anomaly       → time-series area chart
// forecast                        → time-series area + dashed historical/predicted split
// ranking                         → bar chart over party names
// comparison / rca                → two-bar comparison with delta callout

const CHART_TOOLTIP_STYLE = {
  background: '#0a0a0a',
  border: '1px solid #27272a',
  borderRadius: 8,
  fontSize: 12,
} as const

const KIND_BADGE_LABEL: Record<ChartKind, string> = {
  summary:    'Summary',
  trend:      'Trend',
  ranking:    'Ranking',
  comparison: 'Comparison',
  rca:        'Root cause',
  forecast:   'Forecast',
  anomaly:    'Anomaly scan',
}

// Discriminate the simple W5 chart payload ({kind:bar|line|pie, points[]})
// emitted on the agent's `final` event from the richer legacy SalesChart
// (series/items/comparison). The W5 shape is the only one carrying a `points`
// array, so its presence is a safe discriminator.
function isChatChartPayload(
  chart: SalesChart | ChatChartPayload,
): chart is ChatChartPayload {
  return Array.isArray((chart as ChatChartPayload).points)
}

// Small inline note shown in place of a chart when the backend sent a chart
// object that carries no plottable data (empty/malformed payload). Without
// this the chart branch rendered nothing, leaving the user with no indication
// that a chart was attempted — the spinner/answer just appeared chart-less.
function ChartUnavailable({ title }: { title?: string }) {
  return (
    <div className="rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3">
      {title && (
        <div className="mb-1 text-sm font-medium tracking-tight text-zinc-100">
          {title}
        </div>
      )}
      <div className="flex items-center gap-2 text-xs text-zinc-500">
        <BarChart2 className="w-3.5 h-3.5 shrink-0 text-zinc-600" />
        <span>Chart unavailable — no data to display.</span>
      </div>
    </div>
  )
}

// W5 chat chart. Renders the agent-supplied {kind, title, x_label, y_label,
// points[]} as a compact, dark-themed recharts figure: line→LineChart,
// bar→BarChart, pie→PieChart. When there are no points it shows a small
// "Chart unavailable" note (rather than nothing) so an empty payload is visible.
function PointsChart({ chart }: { chart: ChatChartPayload }) {
  const points = chart.points ?? []
  if (points.length === 0) return <ChartUnavailable title={chart.title} />

  const kind = chart.chart_type
  // Long category labels (party names, products) only fit when rotated; short
  // ones (dates, single words) stay horizontal. Mirrors the dashboard heuristic.
  const longLabels = points.some((p) => (p.label ?? '').length > 6)
  const angle = points.length > 4 && longLabels ? -28 : 0
  const xHeight = angle === 0 ? 28 : 60
  const chartHeight = 180 + Math.max(0, xHeight - 28)

  const fmtValue = (v: number): string =>
    (v ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })
  const fmtCompact = (v: number): string => {
    if (Math.abs(v) >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`
    if (Math.abs(v) >= 1_000) return `${(v / 1_000).toFixed(1)}k`
    return `${v}`
  }
  const fmtTick = (l: string): string =>
    l && l.length > 12 ? `${l.slice(0, 11)}…` : l
  const yName = chart.y_label || 'Value'

  return (
    <div className="rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3">
      {chart.title && (
        <div className="mb-2 text-sm font-medium tracking-tight text-zinc-100">
          {chart.title}
        </div>
      )}
      <div className="-ml-2" style={{ height: kind === 'pie' ? 240 : chartHeight }}>
        <ResponsiveContainer>
          {kind === 'pie' ? (
            <PieChart>
              <Pie
                data={points}
                dataKey="value"
                nameKey="label"
                cx="50%"
                cy="50%"
                innerRadius={50}
                outerRadius={92}
                paddingAngle={points.length > 1 ? 2 : 0}
                stroke="#0a0a0a"
                strokeWidth={2}
              >
                {points.map((entry, idx) => (
                  <Cell
                    key={`${entry.label}-${idx}`}
                    fill={PIE_PALETTE[idx % PIE_PALETTE.length]}
                  />
                ))}
              </Pie>
              <Tooltip
                contentStyle={CHART_TOOLTIP_STYLE}
                formatter={(v: number, _n, ctx) => {
                  const label =
                    (ctx?.payload as { label?: string } | undefined)?.label ?? ''
                  return [fmtValue(v), label]
                }}
              />
              <Legend
                verticalAlign="middle"
                align="right"
                layout="vertical"
                iconType="circle"
                wrapperStyle={{ fontSize: 11, color: '#a1a1aa', paddingLeft: 12 }}
                formatter={(value: string) => (
                  <span style={{ color: '#d4d4d8' }}>{fmtTick(value)}</span>
                )}
              />
            </PieChart>
          ) : kind === 'line' ? (
            <LineChart data={points} margin={{ top: 4, right: 6, left: 0, bottom: 4 }}>
              <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="label"
                stroke="#71717a"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                interval="preserveStartEnd"
                minTickGap={16}
                angle={angle}
                textAnchor={angle === 0 ? 'middle' : 'end'}
                height={xHeight}
                tickFormatter={fmtTick}
                label={
                  chart.x_label
                    ? { value: chart.x_label, position: 'insideBottom', offset: -2, fill: '#71717a', fontSize: 11 }
                    : undefined
                }
              />
              <YAxis
                stroke="#71717a"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                width={48}
                tickFormatter={fmtCompact}
              />
              <Tooltip
                contentStyle={CHART_TOOLTIP_STYLE}
                cursor={{ stroke: '#3f3f46', strokeDasharray: '3 3' }}
                formatter={(v: number) => [fmtValue(v), yName]}
              />
              <Line
                type="monotone"
                dataKey="value"
                stroke="#10b981"
                strokeWidth={2}
                dot={false}
                activeDot={{ r: 4 }}
              />
            </LineChart>
          ) : (
            <BarChart data={points} margin={{ top: 4, right: 6, left: 0, bottom: 4 }}>
              <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="label"
                stroke="#71717a"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                tickMargin={8}
                interval="preserveStartEnd"
                angle={angle}
                textAnchor={angle === 0 ? 'middle' : 'end'}
                height={xHeight}
                tickFormatter={fmtTick}
                label={
                  chart.x_label
                    ? { value: chart.x_label, position: 'insideBottom', offset: -2, fill: '#71717a', fontSize: 11 }
                    : undefined
                }
              />
              <YAxis
                stroke="#71717a"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                width={48}
                tickFormatter={fmtCompact}
              />
              <Tooltip
                contentStyle={CHART_TOOLTIP_STYLE}
                cursor={{ fill: 'rgba(63, 63, 70, 0.3)' }}
                formatter={(v: number) => [fmtValue(v), yName]}
              />
              <Bar dataKey="value" fill="#10b981" radius={[4, 4, 0, 0]} />
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
    </div>
  )
}

function ChatChart({ chart }: { chart: SalesChart }) {
  const series = chart.series ?? []
  const totals = chart.totals ?? { total_sales: 0, orders: 0, customers: 0 }
  const hasSales = totals.total_sales > 0 || totals.orders > 0
  const items = chart.items ?? []
  const comparison = chart.comparison
  const kind: ChartKind = chart.kind ?? 'summary'
  const yLabel = chart.y_label
  const isCurrency = (yLabel ?? 'sales').toLowerCase() === 'sales'

  if (
    !hasSales
    && series.length === 0
    && items.length === 0
    && !comparison
  ) return <ChartUnavailable />

  return (
    <div className="rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3">
      <div className="flex items-baseline gap-3 mb-2">
        <div className="text-2xl font-semibold tracking-tight text-zinc-50">
          {formatChartValue(totals.total_sales, yLabel)}
        </div>
        {/* Hide the "orders" sub-label for non-currency charts — for a
            customers-by-purchase-count chart, "10 orders" was nonsense.
            Show the y_label name + row count instead. */}
        {isCurrency && totals.orders > 0 && (
          <div className="text-xs text-zinc-500">
            {totals.orders.toLocaleString('en-IN')} orders
          </div>
        )}
        {!isCurrency && (
          <div className="text-xs text-zinc-500">
            {chartValueLabel(yLabel)}
            {totals.orders > 0 ? ` · ${totals.orders.toLocaleString('en-IN')} rows` : ''}
          </div>
        )}
        <div className="text-[11px] text-zinc-600 ml-auto">
          {KIND_BADGE_LABEL[kind] ?? kind}
        </div>
      </div>

      {kind === 'ranking' ? (
        <RankingChart items={items.length ? items : seriesToItems(series)} yLabel={yLabel} />
      ) : (kind === 'comparison' || kind === 'rca') && comparison ? (
        <ComparisonChart comparison={comparison} />
      ) : (
        <TimeSeriesChart chart={chart} />
      )}
    </div>
  )
}

function seriesToItems(
  series: ReadonlyArray<{ bucket: string; sales: number; orders: number }>,
): RankingItem[] {
  return series.map((s) => ({ name: s.bucket, sales: s.sales, orders: s.orders }))
}

function TimeSeriesChart({ chart }: { chart: SalesChart }) {
  // Unique per-instance gradient id. Multiple chat answers can each render a
  // TimeSeriesChart at once; a hardcoded id="chat-fill" produced duplicate DOM
  // ids across instances. useId() keeps each <linearGradient> + its url(#…)
  // reference unique. Must run before any early return (Rules of Hooks).
  const gradientId = `chat-fill-${useId()}`
  const fillUrl = `url(#${gradientId})`
  const series = chart.series ?? []
  // Validate the backend-supplied granularity — "auto" or unknown values
  // fall through to inferGranularity so the x-axis always shows correct labels.
  const _VALID_GRAN = new Set<string>(['daily', 'weekly', 'monthly', 'yearly'])
  const granularity: Granularity = (
    chart.granularity && _VALID_GRAN.has(chart.granularity)
      ? chart.granularity as Granularity
      : inferGranularity(series)
  )
  const tickConfig = getXAxisTickConfig(series.length, granularity)
  const baseHeight = 176
  const chartHeight = baseHeight + Math.max(0, tickConfig.height - 32)
  const horizon = chart.forecast_horizon_days ?? 0
  const hasSplit = horizon > 0 && series.some((s) => (s as { predicted?: boolean }).predicted)
  const yLabel = chart.y_label
  const tooltipLabel = chartValueLabel(yLabel)

  if (series.length < 2) {
    return (
      <div className="text-[11px] text-zinc-500">
        {series.length === 1
          ? `One bucket: ${formatBucketTooltip(series[0].bucket, granularity)}`
          : 'Single-period summary.'}
      </div>
    )
  }

  return (
    <div className="-ml-2" style={{ height: chartHeight }}>
      <ResponsiveContainer>
        <AreaChart data={series} margin={{ top: 4, right: 6, left: 0, bottom: 4 }}>
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="bucket"
            stroke="#71717a"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            interval={tickConfig.interval}
            minTickGap={tickConfig.minTickGap}
            angle={tickConfig.angle}
            textAnchor={tickConfig.textAnchor}
            height={tickConfig.height}
            tickFormatter={(b: string) => formatBucketTick(b, granularity)}
          />
          <YAxis
            stroke="#71717a"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            width={48}
            tickFormatter={(v: number) => formatChartCompact(v, yLabel)}
          />
          <Tooltip
            contentStyle={CHART_TOOLTIP_STYLE}
            cursor={{ stroke: '#3f3f46', strokeDasharray: '3 3' }}
            formatter={(v: number, _n: unknown, p: { payload?: { predicted?: boolean } }) => [
              formatChartValue(v, yLabel),
              p?.payload?.predicted ? 'Forecast' : tooltipLabel,
            ]}
            labelFormatter={(l: string) => formatBucketTooltip(l, granularity)}
          />
          {hasSplit ? (
            <>
              <Area
                type="monotone"
                dataKey={(d: { predicted?: boolean; sales: number }) =>
                  d.predicted ? null : d.sales}
                stroke="#10b981"
                strokeWidth={2}
                fill={fillUrl}
                isAnimationActive={false}
                connectNulls={false}
              />
              <Area
                type="monotone"
                dataKey={(d: { predicted?: boolean; sales: number }) =>
                  d.predicted ? d.sales : null}
                stroke="#10b981"
                strokeDasharray="4 3"
                strokeWidth={2}
                fill={fillUrl}
                fillOpacity={0.3}
                isAnimationActive={false}
                connectNulls={false}
              />
            </>
          ) : (
            <Area
              type="monotone"
              dataKey="sales"
              stroke="#10b981"
              strokeWidth={2}
              fill={fillUrl}
            />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function RankingChart({
  items,
  yLabel,
}: {
  items: RankingItem[]
  yLabel?: string
}) {
  if (items.length === 0) {
    return <div className="text-[11px] text-zinc-500">No items to rank.</div>
  }
  const trimmed = items.slice(0, 10)
  const angle = trimmed.length > 4 ? -28 : 0
  const height = angle === 0 ? 200 : 240
  const tooltipLabel = chartValueLabel(yLabel)
  return (
    <div className="-ml-2" style={{ height }}>
      <ResponsiveContainer>
        <BarChart data={trimmed} margin={{ top: 4, right: 6, left: 0, bottom: 4 }}>
          <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="name"
            stroke="#71717a"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            interval={0}
            angle={angle}
            textAnchor={angle === 0 ? 'middle' : 'end'}
            height={angle === 0 ? 32 : 64}
            tickFormatter={(v: string) =>
              v && v.length > 14 ? `${v.slice(0, 12)}…` : v}
          />
          <YAxis
            stroke="#71717a"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(v: number) => formatChartCompact(v, yLabel)}
          />
          <Tooltip
            contentStyle={CHART_TOOLTIP_STYLE}
            cursor={{ fill: 'rgba(63, 63, 70, 0.3)' }}
            formatter={(v: number) => [formatChartValue(v, yLabel), tooltipLabel]}
          />
          <Bar dataKey="sales" fill="#10b981" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function ComparisonChart({ comparison }: { comparison: NonNullable<SalesChart['comparison']> }) {
  const data = [
    { label: 'Previous', sales: comparison.previous.sales, period: comparison.previous.period },
    { label: 'Current',  sales: comparison.current.sales,  period: comparison.current.period },
  ]
  const deltaPct = comparison.delta_pct
  const deltaAbs = comparison.delta_abs ?? 0
  const direction = deltaAbs > 0 ? 'up' : deltaAbs < 0 ? 'down' : 'flat'
  const deltaColor =
    direction === 'up' ? 'text-emerald-300'
    : direction === 'down' ? 'text-red-300'
    : 'text-zinc-400'
  const arrow = direction === 'up' ? '▲' : direction === 'down' ? '▼' : '·'

  return (
    <div>
      <div className="-ml-2" style={{ height: 200 }}>
        <ResponsiveContainer>
          <BarChart data={data} margin={{ top: 8, right: 6, left: 0, bottom: 4 }}>
            <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="#71717a"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              tickMargin={8}
            />
            <YAxis
              stroke="#71717a"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              width={52}
              tickFormatter={(v: number) => formatCompact(v)}
            />
            <Tooltip
              contentStyle={CHART_TOOLTIP_STYLE}
              cursor={{ fill: 'rgba(63, 63, 70, 0.3)' }}
              formatter={(v: number) => [formatINR(v), 'Sales']}
              labelFormatter={(_l: string, payload: Array<{ payload?: { period?: string } }>) => {
                const p = payload?.[0]?.payload
                return p?.period ?? ''
              }}
            />
            <Bar dataKey="sales" fill="#10b981" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-2 flex items-center justify-between text-[11px] text-zinc-500">
        <PeriodLabel period={comparison.previous} />
        <span className={cn('font-medium', deltaColor)}>
          {arrow} {formatINR(Math.abs(deltaAbs))}
          {deltaPct != null && ` (${Math.abs(deltaPct).toFixed(1)}%)`}
        </span>
        <PeriodLabel period={comparison.current} />
      </div>
    </div>
  )
}

function PeriodLabel({ period }: { period: PeriodTotals }) {
  return (
    <div className="flex flex-col">
      <span className="text-zinc-300">{formatINR(period.sales)}</span>
      <span className="text-[10px] text-zinc-600">{period.start} → {period.end}</span>
    </div>
  )
}

function ThinkingBlock({ toolEvents }: { toolEvents: ToolEvent[] }) {
  const current = toolEvents.find((t) => t.status === 'running')
  const completed = toolEvents.filter((t) => t.status !== 'running')
  const label = current ? TOOL_LABELS[current.tool] ?? current.tool : 'Thinking…'

  return (
    <div className="rounded-2xl rounded-tl-md bg-zinc-900/40 border border-zinc-800 px-4 py-3 mb-1">
      <div className="flex items-center gap-2 text-sm text-zinc-300">
        <Loader2 className="w-3.5 h-3.5 animate-spin text-emerald-400" />
        <span>{label}</span>
      </div>
      {current?.reasoning && (
        <div className="mt-1 text-xs text-zinc-500 italic">{current.reasoning}</div>
      )}
      {completed.length > 0 && (
        <ul className="mt-2 space-y-1">
          {completed.map((t, i) => (
            <li
              key={`${t.tool}-${i}`}
              className={cn(
                'text-xs',
                t.status === 'failed' ? 'text-red-400' : 'text-zinc-500',
              )}
            >
              <div className="flex items-center gap-2">
                <span
                  className={cn(
                    'w-1 h-1 rounded-full',
                    t.status === 'failed' ? 'bg-red-400' : 'bg-emerald-400',
                  )}
                />
                <span className="text-zinc-400">{TOOL_LABELS[t.tool] ?? t.tool}</span>
                {t.durationMs != null && (
                  <span className="text-zinc-600">· {Math.round(t.durationMs)}ms</span>
                )}
              </div>
              {t.reasoning && (
                <div className="ml-3 text-zinc-600 italic">{t.reasoning}</div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ===========================================================================
// UploadData (file + Drive) + UploadsTable subcomponent
// ===========================================================================

const ACCEPT =
  '.csv,.xls,.xlsx,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const MAX_BYTES = 1024 * 1024 * 1024 // 1 GB

export function UploadData() {
  const dataset = useAppStore((s) => s.dataset)
  const setDataset = useAppStore((s) => s.setDataset)

  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // Live progress of an async workbook upload (W4). Null when no upload is in
  // flight; otherwise the latest poll of GET /upload_status drives a progress
  // bar. The interval id is held in a ref so we can stop polling on unmount or
  // when the job finishes — preventing a setState-after-unmount leak.
  const [uploadProgress, setUploadProgress] = useState<UploadJobStatus | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // Tracks whether the component is still mounted. clearInterval stops *future*
  // ticks, but a poll IIFE already awaiting getUploadStatus when the user
  // navigates away would still call setState on resolve — a no-op-after-unmount
  // warning + leak. The poll body checks this ref before every state write.
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (pollRef.current !== null) clearInterval(pollRef.current)
    }
  }, [])

  // After a successful workbook upload the backend's LLM proposes how to read
  // the data (which column is the sales date, revenue, ...). We hold the
  // proposal here and render a confirm panel; clearing it (Confirm or Skip)
  // dismisses the panel.
  const [schemaProposal, setSchemaProposal] = useState<SchemaProposal | null>(null)

  const [auth, setAuth] = useState<AuthMe | null>(null)
  const [driveSyncing, setDriveSyncing] = useState(false)
  const [uploadsRefreshKey, setUploadsRefreshKey] = useState(0)
  const refreshUploadsList = () => setUploadsRefreshKey((k) => k + 1)
  const [driveNote, setDriveNote] = useState<string | null>(null)
  const [driveError, setDriveError] = useState<string | null>(null)
  const [driveFiles, setDriveFiles] = useState<DriveFile[]>([])
  const [driveLoading, setDriveLoading] = useState(false)
  const [selectedFileIds, setSelectedFileIds] = useState<Set<string>>(new Set())
  const [driveTarget, setDriveTarget] = useState<'sales' | 'purchase'>('sales')

  // Load the user's Drive files for the picker (also refreshes connected state).
  const loadDriveFiles = async () => {
    setDriveLoading(true)
    setDriveError(null)
    try {
      const status = await fetchDriveStatus()
      setDriveFiles(status.files)
      // Fix 1a: also carry through google_configured so the panel knows
      // OAuth is wired even when not yet connected.
      setAuth((prev) => ({ ...(prev ?? {}), authenticated: status.connected, google_configured: status.configured }))
    } catch (e) {
      setDriveError(e instanceof Error ? e.message : 'Could not list Drive files.')
    } finally {
      setDriveLoading(false)
    }
  }

  // Load Google auth status on mount. Independent of app login.
  useEffect(() => {
    void (async () => {
      try {
        const me = await fetchAuthMe()
        setAuth(me)
        // Fix 1b: load Drive files when connected OR when OAuth is configured
        // (so the panel reflects "configured but not connected" state too).
        if (me.authenticated || me.google_configured) void loadDriveFiles()
      } catch {
        setAuth({ authenticated: false })
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // (Fix 1c) OAuth return detection moved to App.tsx (always mounted).
  // UploadData's own mount effect already calls loadDriveFiles, so when
  // App.tsx switches the view to 'upload' after a connect the file picker
  // refreshes automatically.

  const toggleFile = (id: string) => {
    setSelectedFileIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const doSync = async () => {
    const ids = Array.from(selectedFileIds)
    if (ids.length === 0) {
      setDriveError('Select at least one file to import.')
      return
    }
    setDriveSyncing(true)
    setDriveError(null)
    setDriveNote(null)
    try {
      const result = await syncDrive(ids, driveTarget)
      const ok = result.results.filter((r) => r.ok)
      const failed = result.results.filter((r) => !r.ok)
      setDriveNote(
        `Imported ${ok.length} file${ok.length === 1 ? '' : 's'} ` +
          `(${result.rows_inserted_total.toLocaleString('en-IN')} rows into ${result.target}).` +
          (failed.length ? ` ${failed.length} failed.` : ''),
      )
      const lastOk = [...ok].reverse().find((r) => (r.rows_inserted ?? 0) >= 0)
      if (lastOk) {
        setDataset({
          name: lastOk.filename ?? 'Drive file',
          rows: lastOk.rows_inserted ?? 0,
          uploadedAt: new Date().toISOString(),
          source: 'drive',
        })
      }
      if (failed.length) {
        setDriveError(failed.map((f) => `${f.file_id}: ${f.error}`).join('; '))
      }
      setSelectedFileIds(new Set())
      refreshUploadsList()
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.detail as { detail?: string; error?: string } | undefined
        setDriveError(body?.detail ?? body?.error ?? e.message)
      } else if (e instanceof Error) {
        setDriveError(e.message)
      } else {
        setDriveError('Drive sync failed.')
      }
    } finally {
      setDriveSyncing(false)
    }
  }

  const doLogout = async () => {
    if (!confirm('Sign out of Google? Stored Drive tokens will be removed.')) return
    try {
      await logout()
      setAuth({ authenticated: false })
      setDriveNote(null)
      setDriveFiles([])
      setSelectedFileIds(new Set())
    } catch {
      /* noop */
    }
  }

  const onPick = (f: File) => {
    setError(null)
    if (!/\.(csv|xlsx?|xls)$/i.test(f.name)) {
      setError('Choose a CSV or Excel file (.csv, .xls, .xlsx).')
      return
    }
    if (f.size > MAX_BYTES) {
      setError('File exceeds 1 GB. Split it and upload sequentially.')
      return
    }
    setFile(f)
  }

  const describeError = (e: unknown, fallback: string): string => {
    if (e instanceof ApiError) {
      const detail = (e.detail as { detail?: string } | undefined)?.detail
      return detail ?? e.message
    }
    if (e instanceof Error) return e.message
    return fallback
  }

  // Shared "ingest finished" tail used by both the CSV (blocking) and workbook
  // (async-polled) paths: record the dataset pill, clear the picker, refresh the
  // uploads table, then drop into the EXISTING propose→confirm mapping step.
  // Best-effort: a propose failure must never block the upload, so it's swallowed.
  const finishIngest = async (name: string, rows: number) => {
    setDataset({
      name,
      rows,
      uploadedAt: new Date().toISOString(),
      source: 'upload',
    })
    setFile(null)
    if (inputRef.current) inputRef.current.value = ''
    refreshUploadsList()
    try {
      const proposal = await proposeSchema()
      // proposeSchema can resolve after unmount (esp. via the async poll path);
      // don't surface the confirm panel on a torn-down component.
      if (mountedRef.current) setSchemaProposal(proposal)
    } catch (proposeErr) {
      // eslint-disable-next-line no-console
      console.warn('[schema] propose failed; skipping confirm step', proposeErr)
    }
  }

  // Poll GET /upload_status every 1.5s until the async workbook job finishes.
  // Keeps the UI responsive (no 90s blocking request): each tick updates the
  // progress panel; `done` drops into finishIngest, `error` surfaces the error.
  const startPolling = (jobId: string, fileName: string) => {
    if (pollRef.current !== null) clearInterval(pollRef.current)
    const stop = () => {
      if (pollRef.current !== null) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
    pollRef.current = setInterval(() => {
      void (async () => {
        try {
          const status = await getUploadStatus(jobId)
          // The await above can resolve after the user navigated away; bail so
          // we never setState (or run finishIngest's setState) post-unmount.
          if (!mountedRef.current) { stop(); return }
          setUploadProgress(status)
          if (status.status === 'done') {
            stop()
            setUploadProgress(null)
            setBusy(false)
            const rows = Number(
              (status.summary as { rows_inserted?: number; rows?: number } | undefined)
                ?.rows_inserted ??
                (status.summary as { rows?: number } | undefined)?.rows ??
                0,
            )
            await finishIngest(fileName, Number.isFinite(rows) ? rows : 0)
          } else if (status.status === 'error') {
            stop()
            setUploadProgress(null)
            setBusy(false)
            setError(status.error || status.message || 'Upload failed during processing.')
            refreshUploadsList()
          }
        } catch (e) {
          stop()
          if (!mountedRef.current) return
          setUploadProgress(null)
          setBusy(false)
          setError(describeError(e, 'Lost contact while processing the upload.'))
        }
      })()
    }, 1500)
  }

  const onSubmit = async () => {
    if (!file) return
    setBusy(true)
    setError(null)
    setUploadProgress(null)
    const isWorkbook = /\.xlsx?$/i.test(file.name)
    try {
      if (isWorkbook) {
        // Async path: returns immediately with a job id, then we poll. busy stays
        // true (and is cleared by the poller) so the button can't re-fire.
        const { job_id } = await uploadWorkbookAsync(file)
        setUploadProgress({
          status: 'running',
          done_sheets: 0,
          total_sheets: 0,
          message: 'Starting…',
        })
        startPolling(job_id, file.name)
      } else {
        // CSV path is a fast synchronous insert (/upload) — keep it blocking.
        const resp = await uploadSales(file)
        await finishIngest(resp.filename, resp.rows_inserted)
        setBusy(false)
      }
    } catch (e) {
      setError(describeError(e, 'Upload failed.'))
      // Even errors are recorded server-side — refresh so the user sees them.
      refreshUploadsList()
      setUploadProgress(null)
      setBusy(false)
    }
  }

  return (
    <div className="animate-fade-in">
      <PageHeader
        icon={<Upload className="w-5 h-5 text-emerald-400" />}
        title="Upload Data"
        subtitle="Bring in your transactions. CSV/Excel from disk, or connect a Google Drive folder for continuous sync."
      />

      <div className="mt-8 grid md:grid-cols-2 gap-5">
        {/* Option A: file from device */}
        <section className="card p-6">
          <div className="flex items-center gap-2 mb-1">
            <FileSpreadsheet className="w-4 h-4 text-zinc-400" />
            <h2 className="font-medium text-sm">From device</h2>
          </div>
          <p className="text-xs text-zinc-500 mb-5">CSV or Excel (.xls / .xlsx). Up to 1 GB per upload — split larger datasets and upload sequentially.</p>

          <label
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              const f = e.dataTransfer.files[0]
              if (f) onPick(f)
            }}
            className="block border border-dashed border-zinc-700 rounded-lg p-8 text-center cursor-pointer hover:border-emerald-500/40 hover:bg-zinc-900/40 transition-colors"
          >
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={(e) => e.target.files?.[0] && onPick(e.target.files[0])}
            />
            <Upload className="w-6 h-6 text-zinc-500 mx-auto mb-2" />
            <div className="text-sm text-zinc-300">Drop a file here or click to browse</div>
            <div className="text-xs text-zinc-500 mt-1">CSV, XLS, XLSX</div>
          </label>

          {file && (
            <div className="mt-4 flex items-center justify-between bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2">
              <div className="flex items-center gap-2 truncate">
                <FileSpreadsheet className="w-4 h-4 text-emerald-400 shrink-0" />
                <span className="text-sm truncate">{file.name}</span>
                <span className="text-xs text-zinc-500 shrink-0">{(file.size / 1024).toFixed(1)} KB</span>
              </div>
              <button
                onClick={() => setFile(null)}
                className="p-1 text-zinc-500 hover:text-zinc-200 rounded-md"
                aria-label="Remove selected file"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          )}

          {error && <p className="mt-3 text-xs text-red-400">{error}</p>}

          {uploadProgress && <UploadProgress progress={uploadProgress} />}

          <button
            onClick={onSubmit}
            disabled={!file || busy}
            className="btn btn-primary mt-5 w-full"
          >
            {uploadProgress
              ? 'Processing…'
              : busy
                ? 'Uploading…'
                : 'Upload'}
          </button>
        </section>

        {/* Option B: Google Drive */}
        <section className="card p-6">
          <div className="flex items-center gap-2 mb-1">
            <Cloud className="w-4 h-4 text-zinc-400" />
            <h2 className="font-medium text-sm">Connect Google Drive</h2>
          </div>
          <p className="text-xs text-zinc-500 mb-5">
            Sign in, pick CSV / Excel / Sheets files from your Drive, and import
            them straight into your dataset.
          </p>

          {!auth?.authenticated ? (
            <>
              {auth && auth.google_configured === false ? (
                <p className="text-[11px] text-amber-400/80 leading-relaxed">
                  Google Drive is not configured on the server. Set
                  GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in backend/.env.
                </p>
              ) : (
                <a
                  href={googleLoginUrl()}
                  className="w-full flex items-center justify-center gap-3 bg-white text-zinc-900 hover:bg-zinc-100 font-medium rounded-lg px-4 py-2.5 text-sm transition-colors"
                >
                  <GoogleGlyph className="w-4 h-4" />
                  <span>Continue with Google</span>
                </a>
              )}
            </>
          ) : (
            <>
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-zinc-500">Import into</span>
                  <select
                    value={driveTarget}
                    onChange={(e) =>
                      setDriveTarget(e.target.value as 'sales' | 'purchase')
                    }
                    className="bg-zinc-900 border border-zinc-800 rounded-md text-xs px-2 py-1"
                  >
                    <option value="sales">sales</option>
                    <option value="purchase">purchase</option>
                  </select>
                </div>
                <button
                  type="button"
                  onClick={loadDriveFiles}
                  disabled={driveLoading}
                  className="text-[11px] text-zinc-400 hover:text-zinc-200 flex items-center gap-1"
                >
                  <RefreshCw
                    className={driveLoading ? 'w-3 h-3 animate-spin' : 'w-3 h-3'}
                  />
                  Refresh
                </button>
              </div>

              <div className="border border-zinc-800 rounded-lg max-h-56 overflow-y-auto divide-y divide-zinc-800/60">
                {driveLoading && driveFiles.length === 0 ? (
                  <p className="text-xs text-zinc-500 px-3 py-4">Loading files…</p>
                ) : driveFiles.length === 0 ? (
                  <p className="text-xs text-zinc-500 px-3 py-4">
                    No CSV / Excel / Sheets files found in your Drive.
                  </p>
                ) : (
                  driveFiles.map((f) => (
                    <label
                      key={f.id}
                      className="flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-zinc-900/50"
                    >
                      <input
                        type="checkbox"
                        checked={selectedFileIds.has(f.id)}
                        onChange={() => toggleFile(f.id)}
                        className="shrink-0"
                      />
                      <span className="text-xs truncate flex-1">{f.name}</span>
                    </label>
                  ))
                )}
              </div>

              <button
                type="button"
                onClick={doSync}
                disabled={driveSyncing || selectedFileIds.size === 0}
                className="btn btn-primary w-full mt-4"
              >
                <RefreshCw
                  className={driveSyncing ? 'w-4 h-4 animate-spin' : 'w-4 h-4'}
                />
                {driveSyncing
                  ? 'Importing…'
                  : `Import ${selectedFileIds.size || ''} selected`}
              </button>
            </>
          )}

          {driveNote && (
            <p className="text-[11px] text-zinc-500 mt-4 leading-relaxed">{driveNote}</p>
          )}
          {driveError && (
            <p className="text-[11px] text-red-400 mt-4 leading-relaxed">{driveError}</p>
          )}
          {auth?.authenticated && (
            <p className="text-[11px] text-zinc-500 mt-4 leading-relaxed">
              {auth.email ? `Signed in as ${auth.email}. ` : ''}
              <button
                type="button"
                onClick={doLogout}
                className="text-zinc-400 hover:text-zinc-200 underline underline-offset-2"
              >
                Sign out
              </button>
            </p>
          )}
        </section>
      </div>

      {schemaProposal && (
        <SchemaConfirm
          proposal={schemaProposal}
          onDone={() => {
            setSchemaProposal(null)
            // Reuse the existing post-upload refresh path: bump the dashboard
            // (fetchDashboard stamps the store's data_version) and the uploads
            // list so confirmed numbers show up without a manual reload.
            void fetchDashboard().catch(() => {})
            refreshUploadsList()
          }}
        />
      )}

      {dataset && (
        <section className="mt-8 card p-5 flex items-center gap-4 animate-slide-up">
          <CheckCircle2 className="w-5 h-5 text-emerald-400 shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium">Latest upload</div>
            <div className="text-xs text-zinc-400 truncate">
              {dataset.name} · {dataset.rows.toLocaleString()} rows · uploaded{' '}
              {new Date(dataset.uploadedAt).toLocaleString()}
              <span className="text-zinc-600"> · source: {dataset.source}</span>
            </div>
          </div>
          <button onClick={() => setDataset(null)} className="btn btn-secondary">
            Hide pill
          </button>
        </section>
      )}

      <div className="mt-8">
        <UploadsTable
          refreshKey={uploadsRefreshKey}
          onDisconnected={() => setDataset(null)}
        />
      </div>
    </div>
  )
}

// Live progress panel for an async workbook upload (W4). Renders a themed
// progress bar driven by done_sheets/total_sheets plus the backend's status
// message. Before the first sheet count arrives (total_sheets === 0) it shows a
// slim indeterminate-style bar so the user sees immediate feedback.
function UploadProgress({ progress }: { progress: UploadJobStatus }) {
  const { done_sheets, total_sheets, message } = progress
  const known = total_sheets > 0
  const pct = known
    ? Math.min(100, Math.round((done_sheets / total_sheets) * 100))
    : 8
  return (
    <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-900/60 px-3.5 py-3">
      <div className="flex items-center gap-2 text-sm text-zinc-200">
        <Loader2 className="w-3.5 h-3.5 animate-spin text-emerald-400 shrink-0" />
        <span className="flex-1 min-w-0 truncate">
          {known
            ? `Processing sheet ${done_sheets}/${total_sheets}`
            : 'Preparing your workbook'}
          {message ? ` · ${message}` : '…'}
        </span>
        {known && (
          <span className="text-xs text-zinc-500 tabular-nums shrink-0">{pct}%</span>
        )}
      </div>
      <div className="mt-2.5 h-1.5 w-full overflow-hidden rounded-full bg-zinc-800">
        <div
          className={cn(
            'h-full rounded-full bg-emerald-500 transition-all duration-500',
            !known && 'animate-pulse',
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

// Concepts shown in the confirm panel, in display order. transaction_date and
// revenue are guaranteed by the backend contract; the rest render only when the
// proposal includes them.
const SCHEMA_CONCEPTS: Array<{ key: string; label: string }> = [
  { key: 'transaction_date', label: 'Sales date' },
  { key: 'revenue',          label: 'Revenue / sales amount' },
  { key: 'customer',         label: 'Customer' },
  { key: 'invoice_id',       label: 'Invoice #' },
  { key: 'quantity',         label: 'Quantity' },
]

interface SchemaConfirmProps {
  proposal: SchemaProposal
  onDone: () => void
}

/**
 * "Confirm how we read your data" panel. Renders after a successful workbook
 * upload so the user can verify (and tweak) the LLM's proposed column mapping
 * before it drives dashboard numbers. Each detected concept gets an editable
 * <select>; the column universe is derived from every table.column the
 * proposal already references (grouped by table), since the backend contract
 * exposes no separate column list. Confirm posts the mapping; Skip dismisses.
 */
function SchemaConfirm({ proposal, onDone }: SchemaConfirmProps) {
  // Editable working copy of the concept→table.column map. Seeded from the
  // proposal; mutated by the per-concept selects.
  const [mapping, setMapping] = useState<SchemaMapping>(proposal.mapping)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Column universe grouped by table, harvested from every concept the
  // proposal references. Lets each select offer the columns known for its own
  // table (the detected value is always present, so the select can never be
  // empty for a detected concept).
  const columnsByTable = useMemo(() => {
    const acc: Record<string, Set<string>> = {}
    for (const ref of Object.values(proposal.mapping.concepts)) {
      if (!ref) continue
      ;(acc[ref.table] ??= new Set<string>()).add(ref.column)
    }
    return acc
  }, [proposal])

  const present = SCHEMA_CONCEPTS.filter(({ key }) => mapping.concepts[key])
  const issues = proposal.validation?.issues ?? []
  const validationOk = proposal.validation?.ok !== false

  const onColumnChange = (key: string, column: string) => {
    setMapping((prev) => {
      const ref = prev.concepts[key]
      if (!ref) return prev
      return {
        ...prev,
        concepts: { ...prev.concepts, [key]: { ...ref, column } },
      }
    })
  }

  const onConfirm = async () => {
    setBusy(true)
    setError(null)
    try {
      await confirmSchema(mapping)
      onDone()
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.detail as { detail?: string; issues?: string[] } | undefined
        setError(
          body?.issues?.length
            ? body.issues.join('; ')
            : body?.detail ?? e.message,
        )
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Could not save your data mapping.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-8 card p-6 animate-slide-up">
      <div className="flex items-center gap-2 mb-1">
        <CheckCircle2 className="w-4 h-4 text-emerald-400" />
        <h2 className="font-medium text-sm">Confirm how we read your data</h2>
      </div>
      <p className="text-xs text-zinc-500 mb-5">
        We auto-detected which columns hold each value. Check these are right —
        they drive every number on your dashboard.
      </p>

      {!validationOk && issues.length > 0 && (
        <div className="mb-5 flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2.5">
          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
          <div className="text-[11px] text-amber-200/90 leading-relaxed">
            {issues.map((issue, i) => (
              <div key={i}>{issue}</div>
            ))}
          </div>
        </div>
      )}

      <div className="divide-y divide-zinc-800/60 border border-zinc-800 rounded-lg">
        {present.map(({ key, label }) => {
          const ref = mapping.concepts[key]!
          const cols = Array.from(
            new Set([...(columnsByTable[ref.table] ?? new Set<string>()), ref.column]),
          )
          return (
            <div
              key={key}
              className="flex items-center justify-between gap-3 px-3 py-2.5"
            >
              <div className="min-w-0">
                <div className="text-sm text-zinc-200">{label}</div>
                <div className="text-[11px] text-zinc-500 truncate">{ref.table}</div>
              </div>
              <select
                value={ref.column}
                onChange={(e) => onColumnChange(key, e.target.value)}
                className="bg-zinc-900 border border-zinc-800 rounded-md text-xs px-2 py-1 max-w-[55%]"
              >
                {cols.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
          )
        })}
      </div>

      {error && <p className="mt-3 text-xs text-red-400">{error}</p>}

      <div className="mt-5 flex items-center gap-3">
        <button onClick={onConfirm} disabled={busy} className="btn btn-primary">
          {busy ? 'Saving…' : 'Confirm'}
        </button>
        <button onClick={onDone} disabled={busy} className="btn btn-secondary">
          Skip for now
        </button>
      </div>
    </section>
  )
}

function GoogleGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden>
      <path
        fill="#4285F4"
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.75h3.57c2.08-1.92 3.28-4.74 3.28-8.07z"
      />
      <path
        fill="#34A853"
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.75c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
      />
      <path
        fill="#FBBC05"
        d="M5.84 14.12c-.22-.66-.35-1.36-.35-2.12s.13-1.46.35-2.12V7.04H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.96l3.66-2.84z"
      />
      <path
        fill="#EA4335"
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.04l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z"
      />
    </svg>
  )
}

interface UploadsTableProps {
  refreshKey?: number
  onDisconnected?: (batchId: string) => void
}

const STATUS_STYLES: Record<UploadStatus, { label: string; cls: string; Icon: typeof CheckCircle2 }> = {
  active:  { label: 'Active',  cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30', Icon: CheckCircle2 },
  error:   { label: 'Error',   cls: 'bg-red-500/15 text-red-300 border-red-500/30',         Icon: AlertTriangle },
  removed: { label: 'Removed', cls: 'bg-zinc-700/40 text-zinc-400 border-zinc-700',          Icon: Trash2 },
}

function UploadsTable({ refreshKey = 0, onDisconnected }: UploadsTableProps) {
  const [uploads, setUploads] = useState<UploadEntry[]>([])
  const [totals, setTotals] = useState<{ sales: number; purchase: number } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchUploadsList()
      setUploads(data.uploads ?? [])
      setTotals(data.total_rows ?? null)
    } catch (e) {
      if (e instanceof ApiError) {
        setError(`${e.message}`)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Failed to load uploads.')
      }
      setUploads([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const onDisconnect = async (entry: UploadEntry) => {
    if (entry.status !== 'active') return
    const ok = window.confirm(
      `Disconnect "${entry.filename}"? Its ${entry.rows_inserted.toLocaleString()} rows ` +
        `will be removed from queries and the dashboard.`,
    )
    if (!ok) return
    setPendingId(entry.batch_id)
    try {
      await disconnectUpload(entry.batch_id)
      onDisconnected?.(entry.batch_id)
      await load()
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e.message)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Disconnect failed.')
      }
    } finally {
      setPendingId(null)
    }
  }

  return (
    <section className="card p-6">
      <header className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <FileSpreadsheet className="w-4 h-4 text-zinc-400" />
          <h2 className="font-medium text-sm">Uploaded datasets</h2>
          {totals && (
            <span className="text-[11px] text-zinc-500 ml-2">
              · {totals.sales.toLocaleString()} sales rows · {totals.purchase.toLocaleString()} purchase rows
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="btn btn-secondary"
          title="Refresh list"
        >
          <RefreshCw className={cn('w-3.5 h-3.5', loading && 'animate-spin')} />
          <span className="hidden sm:inline">Refresh</span>
        </button>
      </header>

      {error && (
        <div className="mb-3 text-xs text-red-300 bg-red-950/30 border border-red-900/40 rounded-md px-3 py-2">
          {error}
        </div>
      )}

      {!loading && uploads.length === 0 ? (
        <div className="text-xs text-zinc-500 py-6 text-center">
          No uploads yet. Once you upload a file it'll appear here.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-zinc-500 border-b border-zinc-800">
                <th className="text-left font-medium py-2 pr-3">File</th>
                <th className="text-left font-medium py-2 pr-3">Target</th>
                <th className="text-left font-medium py-2 pr-3">Status</th>
                <th className="text-left font-medium py-2 pr-3">Rows</th>
                <th className="text-left font-medium py-2 pr-3">Date range</th>
                <th className="text-left font-medium py-2 pr-3">Uploaded</th>
                <th className="text-right font-medium py-2 pl-3">Action</th>
              </tr>
            </thead>
            <tbody>
              {uploads.map((u) => {
                const style = STATUS_STYLES[u.status] ?? STATUS_STYLES.error
                const StatusIcon = style.Icon
                const range =
                  u.min_date && u.max_date
                    ? `${u.min_date} → ${u.max_date}`
                    : '—'
                return (
                  <tr
                    key={u.batch_id}
                    className={cn(
                      'border-b border-zinc-900 last:border-0',
                      u.status === 'removed' && 'opacity-60',
                    )}
                  >
                    <td className="py-2.5 pr-3 max-w-[18rem]">
                      <div className="truncate text-zinc-100" title={u.filename}>
                        {u.filename}
                      </div>
                      {u.error_message && (
                        <div
                          className="text-[11px] text-red-400/80 truncate mt-0.5"
                          title={u.error_message}
                        >
                          {u.error_message}
                        </div>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-zinc-400 capitalize">{u.target}</td>
                    <td className="py-2.5 pr-3">
                      <span
                        className={cn(
                          'inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[11px]',
                          style.cls,
                        )}
                      >
                        <StatusIcon className="w-3 h-3" />
                        {style.label}
                      </span>
                    </td>
                    <td className="py-2.5 pr-3 text-zinc-300 tabular-nums">
                      {u.rows_inserted.toLocaleString()}
                      {u.rows_failed > 0 && (
                        <span className="text-[11px] text-amber-400/80 ml-1">
                          ({u.rows_failed} failed)
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-zinc-400 tabular-nums">{range}</td>
                    <td className="py-2.5 pr-3 text-zinc-400">
                      {(() => {
                        // Backend now emits ISO with timezone offset
                        // (e.g. "2026-05-29T13:30:00+05:30"); blindly
                        // appending 'Z' produced an invalid duplicate
                        // and the string parsed as Invalid Date. Append
                        // 'Z' only when no timezone marker is present.
                        const raw = u.uploaded_at || ''
                        const hasTz = /Z$|[+-]\d{2}:?\d{2}$/.test(raw)
                        const parsed = new Date(hasTz ? raw : raw + 'Z')
                        return isNaN(parsed.getTime())
                          ? raw || '—'
                          : parsed.toLocaleString()
                      })()}
                    </td>
                    <td className="py-2.5 pl-3 text-right">
                      {u.status === 'active' ? (
                        <button
                          type="button"
                          onClick={() => void onDisconnect(u)}
                          disabled={pendingId === u.batch_id}
                          className="btn btn-secondary"
                          title="Disconnect this dataset"
                        >
                          {pendingId === u.batch_id ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : (
                            <XCircle className="w-3.5 h-3.5" />
                          )}
                          <span className="hidden sm:inline">Disconnect</span>
                        </button>
                      ) : (
                        <span className="text-[11px] text-zinc-600">—</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// ===========================================================================
// Shop Info
// ===========================================================================

export function ShopInfo() {
  const shop = useAppStore((s) => s.shop)
  const setShop = useAppStore((s) => s.setShop)
  const clearShop = useAppStore((s) => s.clearShop)
  const clearChat = useAppStore((s) => s.clearChat)
  // The sign-out control is always shown. `authEnabled` only shapes the
  // post-logout UX: with auth on, clearing the token drops the user on the
  // LoginGate; with auth off there's no gate, so onLogout reloads instead.
  const authEnabled = useAppStore((s) => s.auth.authEnabled)

  const [form, setForm] = useState<ShopInfoType>(shop)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const onLogout = () => {
    // Sign out instantly: clear the conversation + auth state in place. The old
    // window.location.reload() re-downloaded the whole app and re-ran every boot
    // probe (/health, /uploads, /conversations), which made sign-out feel slow.
    // App.tsx already renders the LoginGate reactively the moment the auth token
    // goes null, so no reload is needed — the gate appears immediately.
    clearChat()
    clearAuth()
    // With auth on, App swaps to the LoginGate the instant the token clears.
    // With auth off there's no gate to land on, so reload to a clean slate so
    // the sign-out still visibly takes effect.
    if (authEnabled !== true) window.location.reload()
  }

  const update = <K extends keyof ShopInfoType>(k: K, v: ShopInfoType[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const onSave = (e: React.FormEvent) => {
    e.preventDefault()
    setShop(form)
    setSavedAt(new Date().toLocaleTimeString())
  }

  const onClear = () => {
    if (!confirm('Clear all shop info?')) return
    clearShop()
    setForm({ shopName: '', ownerName: '' })
    setSavedAt(null)
  }

  const dirty = JSON.stringify(form) !== JSON.stringify(shop)

  return (
    <div className="animate-fade-in">
      <PageHeader
        icon={<Building2 className="w-5 h-5 text-emerald-400" />}
        title="Shop Info"
        subtitle="Your shop identity."
      />

      <form onSubmit={onSave} className="mt-8 max-w-2xl space-y-5">
        <Field
          label="Shop Name"
          value={form.shopName}
          onChange={(v) => update('shopName', v)}
          placeholder="e.g. Swiggy Mumbai HQ"
        />
        <Field
          label="Owner Name"
          value={form.ownerName}
          onChange={(v) => update('ownerName', v)}
          placeholder="Full legal name"
        />

        <div className="flex items-center gap-3 pt-3">
          <button type="submit" disabled={!dirty} className="btn btn-primary">
            <Save className="w-4 h-4" />
            Save
          </button>
          <button type="button" onClick={onClear} className="btn btn-secondary">
            <Trash2 className="w-4 h-4" />
            Clear
          </button>
          {savedAt && <span className="text-xs text-zinc-500">Saved at {savedAt}</span>}
        </div>
      </form>

      <div className="mt-10 max-w-2xl rounded-xl border border-zinc-800 bg-zinc-900/40 p-5">
          <h3 className="text-sm font-medium text-zinc-200">Account</h3>
          <p className="mt-1 text-xs text-zinc-500">
            Sign out of this device. You will need to log in again to continue.
          </p>
          <button
            type="button"
            onClick={onLogout}
            className="mt-4 flex items-center gap-2 px-3 py-2 rounded-lg bg-zinc-900/40 border border-zinc-800 text-zinc-400 hover:text-zinc-100 hover:border-zinc-700 transition-colors"
            title="Sign out"
          >
            <LogOut className="w-3.5 h-3.5 shrink-0" />
            <span className="text-xs">Sign out</span>
          </button>
      </div>
    </div>
  )
}

interface FieldProps {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
}

function Field({ label, value, onChange, placeholder }: FieldProps) {
  return (
    <div>
      <label className="label">{label}</label>
      <input
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete="off"
      />
    </div>
  )
}

