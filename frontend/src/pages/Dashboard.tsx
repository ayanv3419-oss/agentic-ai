import { useEffect, useState } from 'react'
import {
  LayoutDashboard,
  ShoppingBag,
  TrendingUp,
  Users,
  RefreshCw,
} from 'lucide-react'
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { PageHeader } from '@/components/ui/PageHeader'
import { useAppStore } from '@/store/useAppStore'
import { ApiError, fetchDashboard } from '@/lib/api'
import type { DashboardData } from '@/types'

const PALETTE = {
  primary: '#10b981',
  grid: '#27272a',
  tick: '#a1a1aa',
}

export function Dashboard() {
  const filters = useAppStore((s) => s.filters)
  const setFilters = useAppStore((s) => s.setFilters)

  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await fetchDashboard(filters.month))
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
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.month])

  return (
    <div className="animate-fade-in">
      <PageHeader
        icon={<LayoutDashboard className="w-5 h-5 text-emerald-400" />}
        title="Dashboard"
        subtitle="Your business at a glance. Pick a month to scope the numbers."
        trailing={
          <div className="flex items-center gap-2">
            <input
              type="month"
              value={filters.month}
              onChange={(e) => setFilters({ month: e.target.value })}
              className="input w-auto"
              aria-label="Select month"
            />
            <button
              onClick={load}
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
          period={formatMonth(filters.month)}
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
          title="Monthly Performance"
          hint={`Daily sales for ${formatMonth(filters.month)}`}
        />
        <div className="h-80 mt-4 -ml-2">
          <ResponsiveContainer>
            <BarChart data={data?.series ?? []} margin={{ top: 12, right: 12, left: 0, bottom: 4 }}>
              <CartesianGrid stroke={PALETTE.grid} strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="bucket" stroke={PALETTE.tick} fontSize={13} tickLine={false} axisLine={false} tickMargin={8} />
              <YAxis
                stroke={PALETTE.tick} fontSize={13} tickLine={false} axisLine={false}
                width={56} tickFormatter={(v: number) => formatCompact(v)}
              />
              <Tooltip
                contentStyle={{ background: '#0a0a0a', border: '1px solid #27272a', borderRadius: 10, fontSize: 13, padding: '10px 12px' }}
                cursor={{ fill: 'rgba(63, 63, 70, 0.3)' }}
                formatter={(v: number) => [formatCurrency(v), 'Sales']}
                labelFormatter={(l) => `Day ${l}`}
              />
              <Bar dataKey="sales" fill={PALETTE.primary} radius={[6, 6, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="mt-5 card p-7">
        <SectionHeader title="Revenue Trend" hint={`How sales moved through ${formatMonth(filters.month)}`} />
        <div className="h-72 mt-4 -ml-2">
          <ResponsiveContainer>
            <AreaChart data={data?.series ?? []} margin={{ top: 12, right: 12, left: 0, bottom: 4 }}>
              <defs>
                <linearGradient id="trend-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={PALETTE.primary} stopOpacity={0.35} />
                  <stop offset="100%" stopColor={PALETTE.primary} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke={PALETTE.grid} strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="bucket" stroke={PALETTE.tick} fontSize={13} tickLine={false} axisLine={false} tickMargin={8} />
              <YAxis
                stroke={PALETTE.tick} fontSize={13} tickLine={false} axisLine={false}
                width={56} tickFormatter={(v: number) => formatCompact(v)}
              />
              <Tooltip
                contentStyle={{ background: '#0a0a0a', border: '1px solid #27272a', borderRadius: 10, fontSize: 13, padding: '10px 12px' }}
                cursor={{ stroke: '#3f3f46', strokeDasharray: '3 3' }}
                formatter={(v: number) => [formatCurrency(v), 'Revenue']}
                labelFormatter={(l) => `Day ${l}`}
              />
              <Area type="monotone" dataKey="sales" stroke={PALETTE.primary} strokeWidth={2.5} fill="url(#trend-fill)" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </section>
    </div>
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

function formatMonth(month: string): string {
  const [y, m] = month.split('-').map(Number)
  if (!y || !m) return month
  const d = new Date(y, m - 1, 1)
  return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
}

function formatCurrency(n: number): string {
  return `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
}

function formatCompact(n: number): string {
  if (n >= 1_00_00_000) return `₹${(n / 1_00_00_000).toFixed(1)}Cr`
  if (n >= 1_00_000) return `₹${(n / 1_00_000).toFixed(1)}L`
  if (n >= 1_000) return `₹${(n / 1_000).toFixed(0)}k`
  return `₹${n}`
}
