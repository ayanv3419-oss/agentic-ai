/**
 * App shell + entry — Sidebar, TopBar, view switcher, bootstrap.
 *
 * Single-user local-first MVP. Lightweight client-side login gate
 * (hardcoded credentials, localStorage persistence). NOT a real auth
 * system — just a startup gate for the MVP.
 *
 * Pages live in `ui_system.tsx`. Data and state live in `client_core.ts`.
 */
import React, { useEffect, useState, type ComponentType } from 'react'
import ReactDOM from 'react-dom/client'
import * as Sentry from '@sentry/react'
import './index.css'

// ----------------------------------------------------------------------------
// Sentry — no-op when VITE_SENTRY_DSN is unset.
// ----------------------------------------------------------------------------
const SENTRY_DSN = import.meta.env.VITE_SENTRY_DSN as string | undefined
if (SENTRY_DSN) {
  Sentry.init({
    dsn: SENTRY_DSN,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT ?? 'development',
    tracesSampleRate: Number(import.meta.env.VITE_SENTRY_TRACES_SAMPLE_RATE ?? 0.1),
    integrations: [Sentry.browserTracingIntegration()],
    sendDefaultPii: import.meta.env.VITE_SENTRY_SEND_PII === '1',
  })
}
import {
  Activity,
  AlertTriangle,
  Building2,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  Sparkles,
  Upload,
} from 'lucide-react'

import {
  cn,
  clearAuth,
  fetchUploadsList,
  loginWith,
  probeAuthEnabled,
  useAppStore,
} from '@/client_core'
import type { NavKey } from '@/client_core'
import { AiAssistant, Dashboard, ShopInfo, UploadData } from '@/ui_system'

// ===========================================================================
// Startup gate removed
// ===========================================================================
//
// Previous versions had a client-side login screen (hardcoded creds in
// localStorage). It was cosmetic — anyone with browser dev tools could
// bypass it — so it has been dropped. Users land directly in the app.
// The backend's POST /auth/login still exists for forward-compatible
// auth, but the frontend no longer calls it.

// --- Legacy gate helpers removed. The block below stays as a stub
//     reference so a real future auth flow can re-introduce them.

// Remove any stale gate flag from prior frontend builds. Safe no-op
// when localStorage is unavailable (private mode, embedded view).
try {
  if (typeof window !== 'undefined') {
    try { window.localStorage?.removeItem('agentic-ai:gate') } catch {}
    try { window.sessionStorage?.removeItem('agentic-ai:gate') } catch {}
  }
} catch { /* fall through */ }

// ===========================================================================
// Sidebar
// ===========================================================================

interface NavItem {
  key: NavKey
  label: string
  icon: ComponentType<{ className?: string }>
  hint: string
}

const ITEMS: NavItem[] = [
  { key: 'shop',      label: 'Shop Info',     icon: Building2,        hint: 'Identity & API key' },
  { key: 'upload',    label: 'Upload Data',   icon: Upload,           hint: 'CSV / Excel' },
  { key: 'dashboard', label: 'Dashboard',     icon: LayoutDashboard,  hint: 'KPIs & charts' },
  { key: 'ai',        label: 'AI Assistant',  icon: MessageSquare,    hint: 'Ask in plain English' },
]

interface SidebarProps {
  active: NavKey
  onSelect: (key: NavKey) => void
  onLogout: () => void
}

function Sidebar({ active, onSelect, onLogout }: SidebarProps) {
  const apiKeySet = useAppStore((s) => Boolean(s.shop.groqApiKey))

  return (
    <aside className="w-64 shrink-0 bg-zinc-950 border-r border-zinc-800 flex flex-col">
      <div className="px-5 h-16 flex items-center gap-2 border-b border-zinc-800">
        <div className="w-7 h-7 rounded-md bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center">
          <Sparkles className="w-3.5 h-3.5 text-emerald-400" />
        </div>
        <div className="font-semibold tracking-tight">Agentic AI</div>
      </div>

      <nav className="flex-1 p-3 space-y-1">
        {ITEMS.map((item) => {
          const Icon = item.icon
          const isActive = active === item.key
          return (
            <button
              key={item.key}
              onClick={() => onSelect(item.key)}
              className={cn(
                'group w-full flex items-center gap-3 rounded-lg px-3 py-2.5 transition-colors text-left',
                isActive
                  ? 'bg-zinc-800/80 text-zinc-50'
                  : 'text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/40',
              )}
            >
              <Icon
                className={cn(
                  'w-4 h-4 shrink-0',
                  isActive ? 'text-emerald-400' : 'text-zinc-500 group-hover:text-zinc-300',
                )}
              />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium leading-none">{item.label}</div>
                <div className="text-[11px] text-zinc-500 mt-1 leading-none">{item.hint}</div>
              </div>
            </button>
          )
        })}
      </nav>

      <div className="p-3 border-t border-zinc-800 space-y-2">
        <div className="px-3 py-2.5 rounded-lg bg-zinc-900/60 border border-zinc-800">
          <div className="flex items-center justify-between">
            <div className="text-[11px] uppercase tracking-wider text-zinc-500">Groq API</div>
            <span
              className={cn(
                'inline-block w-1.5 h-1.5 rounded-full',
                apiKeySet ? 'bg-emerald-400' : 'bg-amber-400',
              )}
            />
          </div>
          <div
            className={cn(
              'text-sm font-medium mt-1',
              apiKeySet ? 'text-emerald-400' : 'text-amber-400',
            )}
          >
            {apiKeySet ? 'Connected' : 'Not configured'}
          </div>
        </div>

        <button
          type="button"
          onClick={onLogout}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-zinc-900/40 border border-zinc-800 text-zinc-400 hover:text-zinc-100 hover:border-zinc-700 transition-colors"
          title="Sign out"
        >
          <LogOut className="w-3.5 h-3.5 shrink-0" />
          <span className="text-xs">Sign out</span>
        </button>
      </div>
    </aside>
  )
}

// ===========================================================================
// TopBar
// ===========================================================================

function TopBar() {
  const shop = useAppStore((s) => s.shop)
  const dataset = useAppStore((s) => s.dataset)

  return (
    <header className="h-16 shrink-0 border-b border-zinc-800 px-8 flex items-center justify-between bg-zinc-950/80 backdrop-blur supports-[backdrop-filter]:bg-zinc-950/60">
      <div className="min-w-0">
        {shop.ownerName ? (
          <div className="text-xs text-zinc-500 truncate">{shop.ownerName}</div>
        ) : null}
        <div className="text-sm font-medium text-zinc-100 truncate">
          {shop.shopName || 'Agentic AI'}
        </div>
      </div>
      <div className="flex items-center gap-4 text-xs text-zinc-500">
        {dataset && (
          <div className="hidden md:flex items-center gap-2">
            <span className="text-zinc-600">Active dataset</span>
            <span className="text-zinc-300 font-medium">{dataset.name}</span>
            <span className="text-zinc-600">·</span>
            <span>{dataset.rows.toLocaleString()} rows</span>
          </div>
        )}
        <div className="flex items-center gap-1.5">
          <Activity className="w-3.5 h-3.5 text-emerald-400" />
          <span>Powered by Groq</span>
        </div>
      </div>
    </header>
  )
}

// ===========================================================================
// Error Boundary
// ===========================================================================

interface ErrorBoundaryState { error: Error | null }

class ErrorBoundary extends React.Component<
  { children: React.ReactNode; fallbackTitle?: string },
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary] page crashed:', error, info)
    try {
      Sentry.captureException(error, { extra: { componentStack: info.componentStack } })
    } catch {
      /* never let monitoring break recovery */
    }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="card p-6 m-6 border-red-900/40 bg-red-950/20 text-sm text-red-200">
          <div className="flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 text-red-400 shrink-0 mt-0.5" />
            <div className="min-w-0">
              <div className="font-semibold text-red-100 mb-1">
                {this.props.fallbackTitle ?? 'This view crashed.'}
              </div>
              <div className="text-xs font-mono break-words text-red-300/90">
                {this.state.error.message}
              </div>
              <button
                onClick={() => this.setState({ error: null })}
                className="mt-3 btn btn-secondary text-xs"
              >
                Reset view
              </button>
            </div>
          </div>
        </div>
      )
    }
    return this.props.children as React.ReactElement
  }
}

// ===========================================================================
// App root — login gate removed; users land directly in the app
// ===========================================================================
//
// The previous client-side login gate (Mansuri / 182012) was a cosmetic
// flag in localStorage and offered no real security — the backend has
// always been the actual enforcement point. Removing the gate so the
// app loads straight to the dashboard. A future real-auth flow can
// re-introduce LoginGate by reading from the backend's POST /auth/login
// endpoint, which the backend still exposes.

// ===========================================================================
// LoginGate — only renders when the backend reports AUTH_ENABLED=true
// AND we have no token in the store. In the default (AUTH_ENABLED=false)
// configuration the gate is invisible and the app loads straight to the
// dashboard, matching today's behaviour. Phase-3 wiring; the actual
// credentials live in ADMIN_USERNAME / ADMIN_PASSWORD env vars.
// ===========================================================================
function LoginGate({ onSuccess }: { onSuccess: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username || !password) {
      setError('Username and password are both required.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await loginWith(username, password)
      onSuccess()
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Login failed'
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="h-screen w-screen flex items-center justify-center bg-zinc-950 text-zinc-100">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl bg-zinc-900/70 border border-zinc-800 p-6 space-y-4"
      >
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-md bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center">
            <Sparkles className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div>
            <div className="font-semibold tracking-tight">Agentic AI</div>
            <div className="text-[11px] text-zinc-500">Sign in to continue</div>
          </div>
        </div>
        <div className="space-y-2">
          <label className="block text-xs text-zinc-400">Username</label>
          <input
            type="text"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full px-3 py-2 rounded-md bg-zinc-950 border border-zinc-800 text-sm text-zinc-100 focus:outline-none focus:border-emerald-500"
            disabled={submitting}
          />
        </div>
        <div className="space-y-2">
          <label className="block text-xs text-zinc-400">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full px-3 py-2 rounded-md bg-zinc-950 border border-zinc-800 text-sm text-zinc-100 focus:outline-none focus:border-emerald-500"
            disabled={submitting}
          />
        </div>
        {error && (
          <div className="text-[11px] text-red-300 bg-red-500/10 border border-red-500/30 rounded-md px-2 py-1.5">
            {error}
          </div>
        )}
        <button
          type="submit"
          disabled={submitting}
          className="w-full py-2 rounded-md bg-emerald-500/80 hover:bg-emerald-500 text-zinc-950 text-sm font-medium disabled:opacity-50"
        >
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}


export default function App() {
  const [view, setView] = useState<NavKey>('dashboard')
  const authToken = useAppStore((s) => s.auth.token)
  const authEnabled = useAppStore((s) => s.auth.authEnabled)
  // Bump this to force a re-probe of /health (e.g. after login).
  const [authProbe, setAuthProbe] = useState(0)

  // Probe /health on boot to discover whether AUTH_ENABLED=true. Result
  // lives in store.auth.authEnabled — the gate below renders only when
  // we've confirmed it's true AND there's no token yet.
  useEffect(() => {
    void probeAuthEnabled()
  }, [authProbe])

  // On every mount, ask the backend which datasets are active and reconcile
  // Zustand so the TopBar always shows the right file name — even after the
  // browser tab was closed and reopened, or localStorage was cleared.
  useEffect(() => {
    fetchUploadsList()
      .then(({ uploads }) => {
        const active = uploads
          .filter((u) => u.status === 'active')
          .sort((a, b) => b.uploaded_at.localeCompare(a.uploaded_at))
        const { setDataset } = useAppStore.getState()
        if (active.length > 0) {
          const u = active[0]
          setDataset({
            name: u.filename,
            rows: u.rows_inserted,
            uploadedAt: u.uploaded_at,
            source: u.source === 'google_drive' ? 'drive' : 'upload',
          })
        } else {
          setDataset(null)
        }
      })
      .catch(() => { /* backend not ready yet — keep whatever localStorage has */ })
  }, [authToken])

  // Gate the app on a real backend login when AUTH_ENABLED=true.
  // The boolean stays null until probeAuthEnabled resolves; treat null
  // as "not yet enabled" so we don't flash the LoginGate on every boot.
  if (authEnabled === true && !authToken) {
    return <LoginGate onSuccess={() => setAuthProbe((n) => n + 1)} />
  }

  const onLogout = () => {
    clearAuth()
    // Force the auth probe + uploads fetch to re-run after the token is gone.
    setAuthProbe((n) => n + 1)
    window.location.reload()
  }

  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-100">
      <Sidebar active={view} onSelect={setView} onLogout={onLogout} />
      <main className="flex-1 flex flex-col min-w-0">
        <TopBar />
        <ErrorBoundary key={view} fallbackTitle={`${view} view crashed.`}>
          {view === 'ai' ? (
            <AiAssistant key="ai" />
          ) : (
            <div className="flex-1 overflow-y-auto">
              <div className="max-w-6xl mx-auto px-6 md:px-10 py-10">
                {view === 'shop' && <ShopInfo key="shop" />}
                {view === 'upload' && <UploadData key="upload" />}
                {view === 'dashboard' && <Dashboard key="dashboard" />}
              </div>
            </div>
          )}
        </ErrorBoundary>
      </main>
    </div>
  )
}

// ===========================================================================
// Bootstrap
// ===========================================================================

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
