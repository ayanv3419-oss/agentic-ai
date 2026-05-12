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
 *   - ShopInfo        (shop identity + Groq API key form)
 *
 * Subcomponents that are only used by one page (e.g. UploadsTable, ChatChart,
 * KPI cards, the Google glyph) live next to that page in this file. Anything
 * that's shared across pages — chart axis helpers, the API client, the global
 * store — lives in `charts.ts` / `api.ts`.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {

  AlertTriangle,
  Building2,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Cloud,
  Eye,
  EyeOff,
  FileSpreadsheet,
  LayoutDashboard,
  Loader2,
  RefreshCw,
  Save,
  ShieldCheck,
  ShoppingBag,
  Send,
  Sparkles,
  Square,
  Trash2,
  TrendingUp,
  Upload,
  User2,
  Users,
  X,
  XCircle,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
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
  disconnectUpload,
  fetchAuthMe,
  fetchDashboard,
  fetchUploadsList,
  googleLoginUrl,
  logout,
  streamAIQuery,
  syncDrive,
  uploadSales,
  useAppStore,
} from '@/client_core'
import type {
  AuthMe,
  ChartKind,
  ChatMessage,
  DashboardData,
  PeriodTotals,
  RankingItem,
  SalesChart,
  SseEvent,
  ShopInfo as ShopInfoType,
  ToolEvent,
  UploadEntry,
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

  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async (month: string) => {
    setLoading(true)
    setError(null)
    try {
      // Empty string means "all time" — fetchDashboard treats undefined as no filter.
      setData(await fetchDashboard(month || undefined))
    } catch (e) {
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
      setLoading(false)
    }
  }

  useEffect(() => {
    void load(filters.month)
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

      <div className="mt-10 grid lg:grid-cols-3 gap-5">
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
      </div>

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
                <linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1">
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
              <Area type="monotone" dataKey="sales" stroke={PALETTE.primary} strokeWidth={2.5} fill="url(#trend-fill)" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </section>

      <MonthlySalesPie data={data?.monthly_sales_pie ?? []} />
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
    <div className="card p-7 lg:col-span-2 animate-slide-up bg-gradient-to-br from-zinc-900/60 to-zinc-900/20">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-zinc-300 uppercase tracking-wide">{label}</span>
        <TrendingUp className="w-4 h-4 text-emerald-400" />
      </div>
      <div className="mt-3 text-4xl md:text-5xl font-semibold tracking-tight">{value}</div>
      <div className="mt-2 text-sm text-zinc-500">{period}</div>
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
}

const SUGGESTIONS = [
  'What are my last 2 days sales?',
  'Show this month revenue',
  'Why did sales drop?',
  'Top performing products this week',
]

const newId = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`

export function AiAssistant() {
  const messages = useAppStore((s) => s.chatHistory)
  const isStreaming = useAppStore((s) => s.isStreaming)
  const appendMessage = useAppStore((s) => s.appendMessage)
  const updateMessage = useAppStore((s) => s.updateMessage)
  const clearChat = useAppStore((s) => s.clearChat)
  const setStreaming = useAppStore((s) => s.setStreaming)
  const apiKeySet = useAppStore((s) => Boolean(s.shop.groqApiKey))

  const [input, setInput] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

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
    () => input.trim().length > 0 && !isStreaming && apiKeySet,
    [input, isStreaming, apiKeySet],
  )

  const stop = () => {
    abortRef.current?.abort()
    abortRef.current = null
  }

  const handleSend = async () => {
    const text = input.trim()
    if (!text || isStreaming) return
    if (!apiKeySet) return

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
    setStreaming(true)

    const ctrl = new AbortController()
    abortRef.current = ctrl

    let toolEvents: ToolEvent[] = []

    try {
      await streamAIQuery(
        text,
        (e: SseEvent) => {
          const data = e.data as Record<string, unknown> | undefined
          switch (e.event) {
            case 'tool.call': {
              const tool = String(data?.name ?? '')
              if (!tool) return
              toolEvents = [...toolEvents, { tool, status: 'running' }]
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
              toolEvents = toolEvents.map((te) =>
                te.tool === tool && te.status === 'running'
                  ? { ...te, status: ok ? 'done' : 'failed', durationMs: duration, error }
                  : te,
              )
              updateMessage(assistantMsg.id, { toolEvents })
              break
            }
            case 'final': {
              const answer = String(data?.answer ?? '')
              const chart = (data?.chart ?? null) as SalesChart | null
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

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <ChatHeader onClear={clearChat} hasMessages={messages.length > 0} />

      <div ref={scrollerRef} className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 md:px-10 py-8">
          {messages.length === 0 ? (
            <EmptyState onPick={(s) => setInput(s)} apiKeySet={apiKeySet} />
          ) : (
            <div className="space-y-6">
              {messages.map((m) => (
                <MessageRow key={m.id} message={m} />
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="border-t border-zinc-800 bg-zinc-950">
        <div className="max-w-3xl mx-auto px-6 md:px-10 py-4">
          {!apiKeySet && (
            <div className="mb-3 flex items-center gap-2 text-xs text-amber-400/80">
              <AlertTriangle className="w-3.5 h-3.5" />
              Set your Groq API key in Shop Info to enable the assistant.
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
                apiKeySet
                  ? 'Ask anything about your business…'
                  : 'API key required — open Shop Info to add yours'
              }
              disabled={!apiKeySet}
              className="flex-1 resize-none bg-transparent px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none disabled:opacity-60"
            />
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
          <p className="mt-2 text-[11px] text-zinc-600 text-center">
            Press Enter to send · Shift + Enter for newline
          </p>
        </div>
      </div>
    </div>
  )
}

function ChatHeader({ onClear, hasMessages }: { onClear: () => void; hasMessages: boolean }) {
  return (
    <div className="border-b border-zinc-800 px-6 md:px-10 py-4 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center">
          <Sparkles className="w-4 h-4 text-emerald-400" />
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

function EmptyState({
  onPick,
  apiKeySet,
}: {
  onPick: (s: string) => void
  apiKeySet: boolean
}) {
  return (
    <div className="py-16 animate-fade-in">
      <div className="text-center">
        <div className="inline-flex w-12 h-12 rounded-xl bg-emerald-500/10 border border-emerald-500/30 items-center justify-center mb-4">
          <Sparkles className="w-5 h-5 text-emerald-400" />
        </div>
        <h2 className="text-2xl font-semibold tracking-tight">How can I help today?</h2>
        <p className="mt-2 text-sm text-zinc-500">
          Ask in plain English. I'll pull the data, run the analysis, and explain the result.
        </p>
      </div>

      <div className="mt-10 grid sm:grid-cols-2 gap-3">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onPick(s)}
            disabled={!apiKeySet}
            className="text-left card px-4 py-3 hover:border-zinc-700 hover:bg-zinc-900/70 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <span className="text-sm text-zinc-200">{s}</span>
          </button>
        ))}
      </div>
    </div>
  )
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
        <Sparkles className="w-3.5 h-3.5 text-emerald-400" />
      </div>
      <div className="min-w-0 flex-1 max-w-[85%]">
        {showThinking && <ThinkingBlock toolEvents={message.toolEvents} />}

        {message.chart && <ChatChart chart={message.chart} />}

        {message.content && (
          <div
            className={cn(
              'rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3',
              message.chart && 'mt-2',
            )}
          >
            <p className="whitespace-pre-wrap text-sm text-zinc-100 leading-relaxed">
              {message.content}
            </p>
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

function ChatChart({ chart }: { chart: SalesChart }) {
  const series = chart.series ?? []
  const totals = chart.totals ?? { total_sales: 0, orders: 0, customers: 0 }
  const hasSales = totals.total_sales > 0 || totals.orders > 0
  const items = chart.items ?? []
  const comparison = chart.comparison
  const kind: ChartKind = chart.kind ?? 'summary'

  if (
    !hasSales
    && series.length === 0
    && items.length === 0
    && !comparison
  ) return null

  return (
    <div className="rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3">
      <div className="flex items-baseline gap-3 mb-2">
        <div className="text-2xl font-semibold tracking-tight text-zinc-50">
          {formatINR(totals.total_sales)}
        </div>
        {totals.orders > 0 && (
          <div className="text-xs text-zinc-500">
            {totals.orders.toLocaleString('en-IN')} orders
          </div>
        )}
        <div className="text-[11px] text-zinc-600 ml-auto">
          {KIND_BADGE_LABEL[kind] ?? kind}
        </div>
      </div>

      {kind === 'ranking' ? (
        <RankingChart items={items.length ? items : seriesToItems(series)} />
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
  const series = chart.series ?? []
  const granularity: Granularity = chart.granularity ?? inferGranularity(series)
  const tickConfig = getXAxisTickConfig(series.length, granularity)
  const baseHeight = 176
  const chartHeight = baseHeight + Math.max(0, tickConfig.height - 32)
  const horizon = chart.forecast_horizon_days ?? 0
  const hasSplit = horizon > 0 && series.some((s) => (s as { predicted?: boolean }).predicted)

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
            <linearGradient id="chat-fill" x1="0" y1="0" x2="0" y2="1">
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
            tickFormatter={(v: number) => formatCompact(v)}
          />
          <Tooltip
            contentStyle={CHART_TOOLTIP_STYLE}
            cursor={{ stroke: '#3f3f46', strokeDasharray: '3 3' }}
            formatter={(v: number, _n: unknown, p: { payload?: { predicted?: boolean } }) => [
              formatINR(v),
              p?.payload?.predicted ? 'Forecast' : 'Sales',
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
                fill="url(#chat-fill)"
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
                fill="url(#chat-fill)"
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
              fill="url(#chat-fill)"
            />
          )}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function RankingChart({ items }: { items: RankingItem[] }) {
  if (items.length === 0) {
    return <div className="text-[11px] text-zinc-500">No items to rank.</div>
  }
  const trimmed = items.slice(0, 10)
  const angle = trimmed.length > 4 ? -28 : 0
  const height = angle === 0 ? 200 : 240
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
            tickFormatter={(v: number) => formatCompact(v)}
          />
          <Tooltip
            contentStyle={CHART_TOOLTIP_STYLE}
            cursor={{ fill: 'rgba(63, 63, 70, 0.3)' }}
            formatter={(v: number) => [formatINR(v), 'Sales']}
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
      {completed.length > 0 && (
        <ul className="mt-2 space-y-1">
          {completed.map((t, i) => (
            <li
              key={`${t.tool}-${i}`}
              className={cn(
                'text-xs flex items-center gap-2',
                t.status === 'failed' ? 'text-red-400' : 'text-zinc-500',
              )}
            >
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
  const apiKeySet = useAppStore((s) => Boolean(s.shop.groqApiKey))

  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const [auth, setAuth] = useState<AuthMe | null>(null)
  const [driveSyncing, setDriveSyncing] = useState(false)
  const [uploadsRefreshKey, setUploadsRefreshKey] = useState(0)
  const refreshUploadsList = () => setUploadsRefreshKey((k) => k + 1)
  const [driveNote, setDriveNote] = useState<string | null>(null)
  const [driveError, setDriveError] = useState<string | null>(null)
  const autoSyncRanRef = useRef(false)

  // Load Google auth status on mount. Independent of app login.
  useEffect(() => {
    void (async () => {
      try {
        setAuth(await fetchAuthMe())
      } catch {
        setAuth({ authenticated: false })
      }
    })()
  }, [])

  // Pick up the OAuth-callback signal and auto-sync once.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const status = params.get('drive')
    const err = params.get('drive_error')
    if (err) setDriveError(`Google sign-in failed: ${err}`)
    if (status === 'connected' && !autoSyncRanRef.current) {
      autoSyncRanRef.current = true
      void doSync()
    }
    if (err || status) {
      params.delete('drive')
      params.delete('drive_error')
      params.delete('detail')
      const clean = window.location.pathname + (params.toString() ? `?${params}` : '')
      window.history.replaceState({}, '', clean)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const doSync = async () => {
    setDriveSyncing(true)
    setDriveError(null)
    setDriveNote(null)
    try {
      const result = await syncDrive()
      setDriveNote(
        `Imported ${result.imported} file${result.imported === 1 ? '' : 's'} (${result.rows_inserted.toLocaleString('en-IN')} rows).` +
          (result.skipped_already ? ` ${result.skipped_already} already in DB.` : '') +
          (result.failed ? ` ${result.failed} failed.` : ''),
      )
      const lastImported = result.details.find((d) => d.status === 'imported')
      if (lastImported && typeof lastImported.rows === 'number') {
        setDataset({
          name: lastImported.file,
          rows: lastImported.rows,
          uploadedAt: new Date().toISOString(),
          source: 'drive',
        })
      }
      setAuth(await fetchAuthMe())
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

  const onSubmit = async () => {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const resp = await uploadSales(file)
      setDataset({
        name: resp.filename,
        rows: resp.rows_inserted,
        uploadedAt: new Date().toISOString(),
        source: 'upload',
      })
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
      refreshUploadsList()
    } catch (e) {
      if (e instanceof ApiError) {
        const detail = (e.detail as { detail?: string } | undefined)?.detail
        setError(detail ?? e.message)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Upload failed.')
      }
      // Even errors are recorded server-side — refresh so the user sees them.
      refreshUploadsList()
    } finally {
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

          <button
            onClick={onSubmit}
            disabled={!file || busy}
            className="btn btn-primary mt-5 w-full"
          >
            {busy ? 'Uploading…' : 'Upload'}
          </button>
        </section>

        {/* Option B: Google Drive */}
        <section className="card p-6">
          <div className="flex items-center gap-2 mb-1">
            <Cloud className="w-4 h-4 text-zinc-400" />
            <h2 className="font-medium text-sm">Connect Google Drive</h2>
          </div>
          <p className="text-xs text-zinc-500 mb-5">
            Pick a folder; new files in it will be synced automatically.
          </p>

          {!auth?.authenticated ? (
            <a
              href={googleLoginUrl()}
              className="w-full flex items-center justify-center gap-3 bg-white text-zinc-900 hover:bg-zinc-100 font-medium rounded-lg px-4 py-2.5 text-sm transition-colors"
            >
              <GoogleGlyph className="w-4 h-4" />
              <span>Continue with Google</span>
            </a>
          ) : (
            <button
              type="button"
              onClick={doSync}
              disabled={driveSyncing}
              className="btn btn-primary w-full"
            >
              <RefreshCw className={driveSyncing ? 'w-4 h-4 animate-spin' : 'w-4 h-4'} />
              {driveSyncing ? 'Syncing…' : 'Sync Drive now'}
            </button>
          )}

          {driveNote && (
            <p className="text-[11px] text-zinc-500 mt-4 leading-relaxed">{driveNote}</p>
          )}
          {driveError && (
            <p className="text-[11px] text-red-400 mt-4 leading-relaxed">{driveError}</p>
          )}
          {!driveNote && !driveError && (
            <p className="text-[11px] text-zinc-500 mt-4 leading-relaxed">
              {auth?.authenticated ? (
                <>
                  Signed in as {auth.email}.{' '}
                  <button
                    type="button"
                    onClick={doLogout}
                    className="text-zinc-400 hover:text-zinc-200 underline underline-offset-2"
                  >
                    Sign out
                  </button>
                </>
              ) : (
                'Read-only access to drive.readonly. Already-imported files are skipped.'
              )}
            </p>
          )}
        </section>
      </div>

      {!apiKeySet && (
        <div className="mt-6 text-xs text-amber-400/80 flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
          Set your Groq API key in Shop Info before running analytics on this dataset.
        </div>
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
                      {new Date(u.uploaded_at + 'Z').toLocaleString()}
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

  const [form, setForm] = useState<ShopInfoType>(shop)
  const [showKey, setShowKey] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const update = <K extends keyof ShopInfoType>(k: K, v: ShopInfoType[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const onSave = (e: React.FormEvent) => {
    e.preventDefault()
    setShop(form)
    setSavedAt(new Date().toLocaleTimeString())
  }

  const onClear = () => {
    if (!confirm('Clear all shop info, including the API key?')) return
    clearShop()
    setForm({ shopName: '', ownerName: '', groqApiKey: '' })
    setSavedAt(null)
  }

  const dirty = JSON.stringify(form) !== JSON.stringify(shop)

  return (
    <div className="animate-fade-in">
      <PageHeader
        icon={<Building2 className="w-5 h-5 text-emerald-400" />}
        title="Shop Info"
        subtitle="Identity and credentials. The Groq API key is used for every analytics request."
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

        <div>
          <label className="label">Groq API Key</label>
          <div className="relative">
            <input
              type={showKey ? 'text' : 'password'}
              value={form.groqApiKey}
              onChange={(e) => update('groqApiKey', e.target.value)}
              placeholder="gsk_..."
              autoComplete="off"
              spellCheck={false}
              data-1p-ignore
              data-lpignore="true"
              className="input pr-10 font-mono text-sm"
            />
            <button
              type="button"
              onClick={() => setShowKey((s) => !s)}
              className="absolute right-1 top-1/2 -translate-y-1/2 p-2 text-zinc-500 hover:text-zinc-200 rounded-md"
              aria-label={showKey ? 'Hide API key' : 'Show API key'}
            >
              {showKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
          <KeyNotice hasKey={Boolean(form.groqApiKey)} />
        </div>

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

function KeyNotice({ hasKey }: { hasKey: boolean }) {
  if (hasKey) {
    return (
      <p className="mt-2 flex items-start gap-2 text-xs text-zinc-500">
        <ShieldCheck className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-0.5" />
        <span>
          Stored locally in your browser. Sent as <code className="text-zinc-400 font-mono">X-Groq-Api-Key</code> on every API call. Never bundled into source.
        </span>
      </p>
    )
  }
  return (
    <p className="mt-2 flex items-start gap-2 text-xs text-amber-400/80">
      <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
      <span>API key required to run analytics queries. Get one at console.groq.com.</span>
    </p>
  )
}
