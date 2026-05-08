/**
 * App shell + entry — Sidebar, TopBar, Login screen, view switcher, bootstrap.
 *
 * Consolidates the former App.tsx (Sidebar / TopBar / Login / App component)
 * and main.tsx (ReactDOM.createRoot bootstrap). PageHeader (used by every
 * page) lives in ui_system.tsx alongside the pages that consume it.
 *
 * Pages live in `ui_system.tsx`. Data and state live in `client_core.ts`.
 * This module imports from those — never the other way around.
 */
import React, { useState, type ComponentType } from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'
import {
  Activity,
  AlertTriangle,
  Building2,
  LayoutDashboard,
  Loader2,
  Lock,
  LogOut,
  MessageSquare,
  Sparkles,
  Upload,
} from 'lucide-react'

import {
  ApiError,
  cn,
  login as apiLogin,
  logoutBackend,
  selectIsAuthed,
  useAppStore,
} from '@/client_core'
import type { NavKey } from '@/client_core'
import { AiAssistant, Dashboard, ShopInfo, UploadData } from '@/ui_system'

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
  { key: 'upload',    label: 'Upload Data',   icon: Upload,           hint: 'CSV / Excel / Drive' },
  { key: 'dashboard', label: 'Dashboard',     icon: LayoutDashboard,  hint: 'KPIs & charts' },
  { key: 'ai',        label: 'AI Assistant',  icon: MessageSquare,    hint: 'Ask in plain English' },
]

interface SidebarProps {
  active: NavKey
  onSelect: (key: NavKey) => void
}

function Sidebar({ active, onSelect }: SidebarProps) {
  const apiKeySet = useAppStore((s) => Boolean(s.shop.groqApiKey))
  const username = useAppStore((s) => s.auth.username)
  const clearAuth = useAppStore((s) => s.clearAuth)

  const onSignOut = async () => {
    try {
      await logoutBackend()
    } catch {
      /* server logout is a no-op for stateless tokens — clear locally either way */
    }
    clearAuth()
  }

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
          onClick={() => void onSignOut()}
          className="w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-zinc-900/40 border border-zinc-800 text-zinc-400 hover:text-zinc-100 hover:border-zinc-700 transition-colors"
          title="Sign out"
        >
          <div className="flex items-center gap-2 min-w-0">
            <LogOut className="w-3.5 h-3.5 shrink-0" />
            <span className="text-xs truncate">
              {username ? `Sign out (${username})` : 'Sign out'}
            </span>
          </div>
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
// Login screen
// ===========================================================================

function Login() {
  const setAuth = useAppStore((s) => s.setAuth)

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const res = await apiLogin(username.trim(), password)
      setAuth(res.token, res.username, res.expires_at)
    } catch (e) {
      if (e instanceof ApiError) {
        const detail = (e.detail as { detail?: { detail?: string; error?: string } } | undefined)?.detail
        setError(detail?.detail ?? detail?.error ?? e.message)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Login failed.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-zinc-950 text-zinc-100 px-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center justify-center mb-6">
          <div className="w-12 h-12 rounded-xl bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center">
            <Sparkles className="w-5 h-5 text-emerald-400" />
          </div>
        </div>
        <h1 className="text-center text-xl font-semibold tracking-tight">
          Agentic AI
        </h1>
        <p className="text-center text-sm text-zinc-500 mt-1">
          Sign in to continue.
        </p>

        <form onSubmit={onSubmit} className="mt-8 card p-6 space-y-4">
          <div>
            <label className="label" htmlFor="login-username">
              Username
            </label>
            <input
              id="login-username"
              className="input"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="username"
              autoComplete="username"
              autoFocus
              required
            />
          </div>

          <div>
            <label className="label" htmlFor="login-password">
              Password
            </label>
            <input
              id="login-password"
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••"
              autoComplete="current-password"
              required
            />
          </div>

          {error && (
            <div className="flex items-start gap-2 text-xs text-red-300 bg-red-950/30 border border-red-900/40 rounded-md px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={busy || !username || !password}
            className="btn btn-primary w-full"
          >
            {busy ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                Signing in…
              </>
            ) : (
              <>
                <Lock className="w-4 h-4" />
                Sign in
              </>
            )}
          </button>
        </form>

        <p className="text-center text-[11px] text-zinc-600 mt-6">
          Authorized users only. Sessions persist locally until they expire.
        </p>
      </div>
    </div>
  )
}

// ===========================================================================
// App root — auth gate + view switcher
// ===========================================================================

export default function App() {
  const [view, setView] = useState<NavKey>('dashboard')

  const isAuthed = useAppStore(selectIsAuthed)
  if (!isAuthed) {
    return <Login />
  }

  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-100">
      <Sidebar active={view} onSelect={setView} />
      <main className="flex-1 flex flex-col min-w-0">
        <TopBar />
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
      </main>
    </div>
  )
}

// ===========================================================================
// Bootstrap (was main.tsx) — mounts the App into #root.
// ===========================================================================

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
