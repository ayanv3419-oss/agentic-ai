/**
 * App shell + entry — Sidebar, TopBar, view switcher, bootstrap.
 *
 * Single-user local-first MVP. Lightweight client-side login gate
 * (hardcoded credentials, localStorage persistence). NOT a real auth
 * system — just a startup gate for the MVP.
 *
 * Pages live in `ui_system.tsx`. Data and state live in `client_core.ts`.
 */
import React, { useState, type ComponentType } from 'react'
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
  Eye,
  EyeOff,
  LayoutDashboard,
  Lock,
  LogOut,
  MessageSquare,
  Sparkles,
  Upload,
} from 'lucide-react'

import { cn, useAppStore } from '@/client_core'
import type { NavKey } from '@/client_core'
import { AiAssistant, Dashboard, ShopInfo, UploadData } from '@/ui_system'

// ===========================================================================
// Lightweight startup gate — frontend-only, hardcoded credentials
// ===========================================================================
//
// This is NOT real authentication. It's a client-side flag in localStorage
// (with a sessionStorage fallback for private-browsing mode) so the app can
// ask for a password on first open. Anyone with browser dev tools can bypass
// it by running `localStorage.setItem('agentic-ai:gate','1')` and refreshing.
//
// Comparison is whitespace-trimmed; username is case-insensitive, password
// stays case-sensitive. These tolerance rules eliminate the most common
// "I typed the right thing but it says wrong password" failure modes:
//   - mobile keyboards adding trailing spaces
//   - autofill inserting invisible characters
//   - "mansuri" vs "Mansuri" mistypes

const AUTH_USERNAME = 'Mansuri'
const AUTH_PASSWORD = '182012'
const AUTH_KEY = 'agentic-ai:gate'

// Sanitize input — strip whitespace + zero-width / BOM chars some keyboards add.
function clean(s: string): string {
  return (s ?? '').replace(/[​-‍﻿]/g, '').trim()
}

function readGate(): boolean {
  try {
    if (typeof window !== 'undefined' && window.localStorage) {
      if (localStorage.getItem(AUTH_KEY) === '1') return true
    }
  } catch { /* private mode */ }
  try {
    if (typeof window !== 'undefined' && window.sessionStorage) {
      if (sessionStorage.getItem(AUTH_KEY) === '1') return true
    }
  } catch { /* same-origin block */ }
  return false
}

function setGate(open: boolean): { persisted: boolean; storage: string } {
  let persisted = false
  let storage = 'memory'
  try {
    if (open) localStorage.setItem(AUTH_KEY, '1')
    else localStorage.removeItem(AUTH_KEY)
    persisted = true
    storage = 'localStorage'
  } catch {
    try {
      if (open) sessionStorage.setItem(AUTH_KEY, '1')
      else sessionStorage.removeItem(AUTH_KEY)
      persisted = true
      storage = 'sessionStorage'
    } catch { /* fall through to in-memory */ }
  }
  return { persisted, storage }
}

function LoginGate({ onUnlock }: { onUnlock: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [attempts, setAttempts] = useState(0)

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const u = clean(username)
    const p = clean(password)

    // Username is case-insensitive; password is exact (case-sensitive).
    const usernameOk = u.toLowerCase() === AUTH_USERNAME.toLowerCase()
    const passwordOk = p === AUTH_PASSWORD

    // Diagnostic logging — visible in the browser console.
    // eslint-disable-next-line no-console
    console.info('[login] attempt', {
      username_entered_len: u.length,
      password_entered_len: p.length,
      username_match: usernameOk,
      password_match: passwordOk,
      storage_available: typeof window !== 'undefined' && !!window.localStorage,
    })

    if (usernameOk && passwordOk) {
      const persist = setGate(true)
      // eslint-disable-next-line no-console
      console.info('[login] success', persist)
      if (!persist.persisted) {
        // Storage blocked — proceed but warn user the session won't persist.
        // We still unlock so they can use the app this session.
        setError('Logged in — but storage is blocked, so refresh will require login again.')
      }
      onUnlock()
      return
    }

    // Produce a HELPFUL error so the user can self-diagnose.
    const next = attempts + 1
    setAttempts(next)
    if (!usernameOk && !passwordOk) {
      setError('Username and password are both incorrect. Check capitalization.')
    } else if (!usernameOk) {
      setError(`Username is incorrect. (Hint: it should start with capital "M".)`)
    } else {
      setError('Password is incorrect.')
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

        <form onSubmit={onSubmit} className="mt-6 card p-6 space-y-4">
          <div>
            <label className="label" htmlFor="login-username">Username</label>
            <input
              id="login-username"
              className="input"
              type="text"
              value={username}
              onChange={(e) => { setUsername(e.target.value); setError(null) }}
              autoComplete="username"
              autoCapitalize="off"
              spellCheck={false}
              autoFocus
              required
            />
          </div>

          <div>
            <label className="label" htmlFor="login-password">Password</label>
            <div className="relative">
              <input
                id="login-password"
                className="input pr-10"
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => { setPassword(e.target.value); setError(null) }}
                autoComplete="current-password"
                autoCapitalize="off"
                spellCheck={false}
                required
              />
              <button
                type="button"
                onClick={() => setShowPassword((s) => !s)}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-zinc-500 hover:text-zinc-200"
                tabIndex={-1}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword
                  ? <EyeOff className="w-3.5 h-3.5" />
                  : <Eye className="w-3.5 h-3.5" />}
              </button>
            </div>
          </div>

          {error && (
            <div className="flex items-start gap-2 text-xs text-red-300 bg-red-950/30 border border-red-900/40 rounded-md px-3 py-2">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>{error}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={!username.trim() || !password.trim()}
            className="btn btn-primary w-full"
          >
            <Lock className="w-4 h-4" />
            Sign in
          </button>
        </form>

        {attempts >= 2 && (
          <div className="mt-4 text-[11px] text-zinc-500 leading-relaxed text-center">
            Still locked out? Open browser DevTools (F12) → Console, paste
            <code className="mx-1 px-1 py-0.5 rounded bg-zinc-900 text-zinc-300">
              localStorage.setItem('agentic-ai:gate','1')
            </code>
            and refresh.
          </div>
        )}

        <p className="text-center text-[11px] text-zinc-600 mt-6">
          Local single-user gate. No accounts, no signup.
        </p>
      </div>
    </div>
  )
}

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
// App root — gate first, then app
// ===========================================================================

export default function App() {
  const [view, setView] = useState<NavKey>('dashboard')
  const [unlocked, setUnlocked] = useState<boolean>(readGate)

  if (!unlocked) {
    return (
      <ErrorBoundary fallbackTitle="Login screen crashed.">
        <LoginGate onUnlock={() => setUnlocked(true)} />
      </ErrorBoundary>
    )
  }

  const onLogout = () => {
    setGate(false)
    setUnlocked(false)
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
