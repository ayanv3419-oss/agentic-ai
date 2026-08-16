/**
 * App shell + entry — Sidebar, TopBar, view switcher, bootstrap.
 *
 * Single-user local-first MVP. Lightweight client-side login gate
 * (hardcoded credentials, localStorage persistence). NOT a real auth
 * system — just a startup gate for the MVP.
 *
 * Pages live in `ui_system.tsx`. Data and state live in `client_core.ts`.
 */
import React, { useEffect, useState, type ComponentType, type ReactNode } from 'react'
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
  Check,
  LayoutDashboard,
  MessageSquare,
  Sparkles,
  MessageSquarePlus,
  Trash2,
  Upload,
} from 'lucide-react'

import {
  clearAuth,
  cn,
  fetchAuthMe,
  onAccessBlocked,
  fetchUploadsList,
  forgotPassword,
  loginWith,
  probeAuthEnabled,
  resetPassword,
  signupWith,
  useAppStore,
} from '@/client_core'
import type { NavKey } from '@/client_core'
import { AiAssistant, Dashboard, ShopInfo, UploadData } from '@/ui_system'
import { Logo } from '@/logo'
import { UPI_QR_DATA_URI } from '@/upi_qr'
import { SplashScreen } from '@/splash'

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
  { key: 'shop',      label: 'Shop Info',     icon: Building2,        hint: 'Identity' },
  { key: 'upload',    label: 'Upload Data',   icon: Upload,           hint: 'CSV / Excel' },
  { key: 'dashboard', label: 'Dashboard',     icon: LayoutDashboard,  hint: 'KPIs & charts' },
  { key: 'ai',        label: 'AI Assistant',  icon: MessageSquare,    hint: 'Ask in plain English' },
]

interface SidebarProps {
  active: NavKey
  onSelect: (key: NavKey) => void
  /** Drawer open state (all screen sizes). */
  open: boolean
  /** Toggle the drawer — wired to the header logo so clicking it closes. */
  onToggle: () => void
  /** Days remaining in the free trial, or null when not on a trial. */
  trialDaysLeft?: number | null
  /** Open the plans / upgrade page. */
  onUpgrade?: () => void
}

function Sidebar({ active, onSelect, open, onToggle, trialDaysLeft, onUpgrade }: SidebarProps) {
  const sessions = useAppStore((s) => s.sessions)
  const viewingSessionId = useAppStore((s) => s.viewingSessionId)
  const refreshSessions = useAppStore((s) => s.refreshSessions)
  const openSession = useAppStore((s) => s.openSession)
  const closeSession = useAppStore((s) => s.closeSession)
  const removeSession = useAppStore((s) => s.removeSession)
  const clearChat = useAppStore((s) => s.clearChat)

  // Keep Recents fresh: pull on mount. AiAssistant also refreshes after each
  // turn (same store), so newly-created sessions surface here automatically.
  useEffect(() => {
    void refreshSessions()
  }, [refreshSessions])

  // "New chat" and opening a past session both jump into the AI Assistant view.
  const startNewChat = () => {
    clearChat()
    closeSession()
    onSelect('ai')
  }
  const openChat = (id: string) => {
    void openSession(id)
    onSelect('ai')
  }

  return (
    <aside
      className={cn(
        // Slide-in overlay drawer on ALL screen sizes — toggled by the logo.
        'fixed inset-y-0 left-0 z-50 w-64 bg-zinc-950 flex flex-col min-h-0',
        'transform transition-transform duration-200 ease-in-out motion-reduce:transition-none',
        'shadow-2xl shadow-black/40',
        open ? 'translate-x-0' : '-translate-x-full',
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-label="Close menu"
        title="Close menu"
        className="px-5 h-16 flex items-center gap-2 shrink-0 text-left hover:bg-zinc-800/30 transition-colors"
      >
        <div className="w-7 h-7 rounded-md bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center">
          <Logo className="w-3.5 h-3.5 text-emerald-400" />
        </div>
        <div className="font-semibold tracking-tight">Metric AI</div>
      </button>

      <nav className="p-3 space-y-1 shrink-0">
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

      {/* Upgrade / plans entry point. Always available so a convinced trial
          user can pay on day 2 instead of waiting to be locked out. */}
      {onUpgrade && (
        <div className="px-3 pb-2 shrink-0">
          <button
            type="button"
            onClick={onUpgrade}
            className="w-full flex items-center gap-3 rounded-lg px-3 py-2.5 text-left border border-emerald-500/25 bg-emerald-500/5 hover:bg-emerald-500/10 transition-colors"
          >
            <Sparkles className="w-4 h-4 shrink-0 text-emerald-400" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium leading-none text-emerald-100">Upgrade to Pro</div>
              <div className="text-[11px] text-emerald-400/70 mt-1 leading-none">
                {typeof trialDaysLeft === 'number' && trialDaysLeft > 0
                  ? `${trialDaysLeft} ${trialDaysLeft === 1 ? 'day' : 'days'} left in trial`
                  : '₹1,500 / month'}
              </div>
            </div>
          </button>
        </div>
      )}

      {/* Chat sessions — New chat + recency-ordered history (ChatGPT-style). */}
      <div className="px-3 pt-2 pb-2 shrink-0 border-t border-zinc-800/70">
        <button
          type="button"
          onClick={startNewChat}
          className="btn btn-secondary w-full justify-center h-9"
          title="New chat"
        >
          <MessageSquarePlus className="w-3.5 h-3.5" />
          <span>New chat</span>
        </button>
      </div>

      <div className="px-3 pb-1 shrink-0">
        <p className="text-[10px] font-medium uppercase tracking-wide text-zinc-600">Recents</p>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-3 space-y-0.5">
        {sessions.length === 0 ? (
          <p className="px-2 py-3 text-xs text-zinc-600">No conversations yet.</p>
        ) : (
          sessions.map((s) => {
            const isOpen = active === 'ai' && s.id === viewingSessionId
            return (
              <div
                key={s.id}
                className={cn(
                  'group relative flex items-center rounded-lg transition-colors',
                  isOpen ? 'bg-zinc-800/80' : 'hover:bg-zinc-900',
                )}
              >
                <button
                  type="button"
                  onClick={() => openChat(s.id)}
                  title={s.title || 'Untitled conversation'}
                  className={cn(
                    'flex-1 min-w-0 flex items-center gap-2 px-2.5 py-2 text-left',
                    isOpen ? 'text-zinc-100' : 'text-zinc-400 group-hover:text-zinc-200',
                  )}
                >
                  <MessageSquare className="w-3.5 h-3.5 shrink-0 text-zinc-600" />
                  <span className="truncate text-sm">{s.title || 'Untitled conversation'}</span>
                </button>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    void removeSession(s.id)
                  }}
                  title="Delete conversation"
                  aria-label="Delete conversation"
                  className="absolute right-1 opacity-0 group-hover:opacity-100 focus:opacity-100 p-1.5 rounded-md text-zinc-500 hover:text-red-400 hover:bg-zinc-800 transition-opacity"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            )
          })
        )}
      </div>
    </aside>
  )
}

// ===========================================================================
// TopBar
// ===========================================================================

function TopBar({ onToggleSidebar }: { onToggleSidebar: () => void }) {
  const shop = useAppStore((s) => s.shop)
  const dataset = useAppStore((s) => s.dataset)

  return (
    <header className="h-16 shrink-0 px-4 md:px-8 flex items-center justify-between bg-zinc-950/80 backdrop-blur supports-[backdrop-filter]:bg-zinc-950/60">
      <div className="flex items-center gap-2 min-w-0">
        {/* Logo toggles the sidebar drawer. Visible on all screen sizes. */}
        <button
          type="button"
          onClick={onToggleSidebar}
          aria-label="Toggle navigation menu"
          title="Menu"
          className="-ml-1 shrink-0 flex items-center justify-center w-9 h-9 rounded-md bg-emerald-500/10 border border-emerald-500/30 hover:bg-emerald-500/20 transition-colors"
        >
          <Logo className="w-4 h-4 text-emerald-400" />
        </button>
        <div className="min-w-0">
          {shop.ownerName ? (
            <div className="text-xs text-zinc-500 truncate">{shop.ownerName}</div>
          ) : null}
          <div className="text-sm font-medium text-zinc-100 truncate">
            {shop.shopName || 'Metric AI'}
          </div>
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
          <span>Powered by Qwen</span>
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
type AuthMode = 'login' | 'signup'

function LoginGate({ onSuccess }: { onSuccess: () => void }) {
  const [mode, setMode] = useState<AuthMode>('login')
  // Whether the inline "forgot password" view is showing. Kept as a separate
  // boolean (rather than a third AuthMode) so the login/signup toggle and its
  // shared field state stay untouched while the request-reset view is open.
  const [forgot, setForgot] = useState(false)
  // Login accepts a username OR email (backend admin login is username-based);
  // signup is strictly email-based. One field backs both — its label changes
  // with the mode.
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isSignup = mode === 'signup'

  const switchMode = (next: AuthMode) => {
    if (next === mode) return
    setMode(next)
    setError(null)
  }

  // Render the inline "request reset" view instead of the login/signup form.
  // Returning from it (Back to sign in) restores the login form untouched.
  if (forgot) {
    return <ForgotPasswordView onBack={() => setForgot(false)} />
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!identifier || !password) {
      setError(
        isSignup
          ? 'Email and password are both required.'
          : 'Email and password are both required.',
      )
      return
    }
    if (isSignup && password.length < 6) {
      setError('Password must be at least 6 characters.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      if (isSignup) {
        await signupWith(identifier, password)
      } else {
        await loginWith(identifier, password)
      }
      onSuccess()
    } catch (e) {
      const fallback = isSignup ? 'Sign up failed' : 'Login failed'
      const msg = e instanceof Error ? e.message : fallback
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
            <Logo className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div>
            <div className="font-semibold tracking-tight">Metric AI</div>
            <div className="text-[11px] text-zinc-500">
              {isSignup ? 'Create an account to continue' : 'Sign in to continue'}
            </div>
          </div>
        </div>
        {/* Mode toggle */}
        <div className="grid grid-cols-2 gap-1 rounded-md bg-zinc-950 border border-zinc-800 p-1">
          <button
            type="button"
            onClick={() => switchMode('login')}
            disabled={submitting}
            className={cn(
              'py-1.5 rounded text-xs font-medium transition-colors disabled:opacity-50',
              !isSignup
                ? 'bg-emerald-500/80 text-zinc-950'
                : 'text-zinc-400 hover:text-zinc-200',
            )}
          >
            Log in
          </button>
          <button
            type="button"
            onClick={() => switchMode('signup')}
            disabled={submitting}
            className={cn(
              'py-1.5 rounded text-xs font-medium transition-colors disabled:opacity-50',
              isSignup
                ? 'bg-emerald-500/80 text-zinc-950'
                : 'text-zinc-400 hover:text-zinc-200',
            )}
          >
            Sign up
          </button>
        </div>
        <div className="space-y-2">
          <label className="block text-xs text-zinc-400">
            Email
          </label>
          <input
            type={isSignup ? 'email' : 'text'}
            autoFocus
            value={identifier}
            onChange={(e) => setIdentifier(e.target.value)}
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
          {!isSignup && (
            <div className="text-right">
              <button
                type="button"
                onClick={() => {
                  setError(null)
                  setForgot(true)
                }}
                disabled={submitting}
                className="text-[11px] text-zinc-400 hover:text-emerald-400 disabled:opacity-50"
              >
                Forgot password?
              </button>
            </div>
          )}
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
          {submitting
            ? isSignup
              ? 'Creating account…'
              : 'Signing in…'
            : isSignup
              ? 'Sign up'
              : 'Sign in'}
        </button>
      </form>
    </div>
  )
}


// Shared chrome for the auth cards (logo + heading) so the forgot/reset views
// match the LoginGate's framing exactly without duplicating the markup.
function AuthCardShell({
  subtitle,
  onSubmit,
  children,
}: {
  subtitle: string
  onSubmit: (e: React.FormEvent) => void
  children: React.ReactNode
}) {
  return (
    <div className="h-screen w-screen flex items-center justify-center bg-zinc-950 text-zinc-100">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-2xl bg-zinc-900/70 border border-zinc-800 p-6 space-y-4"
      >
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-md bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center">
            <Logo className="w-3.5 h-3.5 text-emerald-400" />
          </div>
          <div>
            <div className="font-semibold tracking-tight">Metric AI</div>
            <div className="text-[11px] text-zinc-500">{subtitle}</div>
          </div>
        </div>
        {children}
      </form>
    </div>
  )
}


// Inline "request a reset link" view, reached from the login form's
// "Forgot password?" link. Calls forgotPassword(email) — which always resolves
// — then shows a NEUTRAL confirmation that never reveals whether an account
// exists for that address (matching the backend's anti-enumeration contract).
function ForgotPasswordView({ onBack }: { onBack: () => void }) {
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [sent, setSent] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email.trim()) return
    setSubmitting(true)
    // forgotPassword never throws (neutral by design); the confirmation shows
    // regardless of outcome.
    await forgotPassword(email.trim())
    setSubmitting(false)
    setSent(true)
  }

  if (sent) {
    return (
      <AuthCardShell subtitle="Check your email" onSubmit={(e) => e.preventDefault()}>
        <div className="text-[13px] text-zinc-300 leading-relaxed">
          If an account exists for that email, a reset link has been sent.
          The link expires in 30 minutes.
        </div>
        <button
          type="button"
          onClick={onBack}
          className="w-full py-2 rounded-md bg-emerald-500/80 hover:bg-emerald-500 text-zinc-950 text-sm font-medium"
        >
          Back to sign in
        </button>
      </AuthCardShell>
    )
  }

  return (
    <AuthCardShell subtitle="Reset your password" onSubmit={submit}>
      <div className="text-[11px] text-zinc-500">
        Enter your account email and we'll send a link to set a new password.
      </div>
      <div className="space-y-2">
        <label className="block text-xs text-zinc-400">Email</label>
        <input
          type="email"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="w-full px-3 py-2 rounded-md bg-zinc-950 border border-zinc-800 text-sm text-zinc-100 focus:outline-none focus:border-emerald-500"
          disabled={submitting}
        />
      </div>
      <button
        type="submit"
        disabled={submitting || !email.trim()}
        className="w-full py-2 rounded-md bg-emerald-500/80 hover:bg-emerald-500 text-zinc-950 text-sm font-medium disabled:opacity-50"
      >
        {submitting ? 'Sending…' : 'Send reset link'}
      </button>
      <button
        type="button"
        onClick={onBack}
        disabled={submitting}
        className="w-full text-[11px] text-zinc-400 hover:text-zinc-200 disabled:opacity-50"
      >
        Back to sign in
      </button>
    </AuthCardShell>
  )
}


// Full-screen "set a new password" view, shown by App when the URL carries a
// reset token (?reset_token=…). Collects new-password + confirm, calls
// resetPassword(token, pw); on success shows "Password updated, please log in"
// and invokes onDone (which clears the token from the URL and returns to the
// login form). On a 400 (expired/invalid token, weak password) it shows the
// server's message.
function ResetPasswordView({
  token,
  onDone,
}: {
  token: string
  onDone: () => void
}) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (password.length < 6) {
      setError('Password must be at least 6 characters.')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await resetPassword(token, password)
      setDone(true)
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Password reset failed.'
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  if (done) {
    return (
      <AuthCardShell subtitle="Password updated" onSubmit={(e) => e.preventDefault()}>
        <div className="text-[13px] text-zinc-300 leading-relaxed">
          Password updated, please log in with your new password.
        </div>
        <button
          type="button"
          onClick={onDone}
          className="w-full py-2 rounded-md bg-emerald-500/80 hover:bg-emerald-500 text-zinc-950 text-sm font-medium"
        >
          Back to sign in
        </button>
      </AuthCardShell>
    )
  }

  return (
    <AuthCardShell subtitle="Choose a new password" onSubmit={submit}>
      <div className="space-y-2">
        <label className="block text-xs text-zinc-400">New password</label>
        <input
          type="password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full px-3 py-2 rounded-md bg-zinc-950 border border-zinc-800 text-sm text-zinc-100 focus:outline-none focus:border-emerald-500"
          disabled={submitting}
        />
      </div>
      <div className="space-y-2">
        <label className="block text-xs text-zinc-400">Confirm password</label>
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
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
        {submitting ? 'Updating…' : 'Update password'}
      </button>
      <button
        type="button"
        onClick={onDone}
        disabled={submitting}
        className="w-full text-[11px] text-zinc-400 hover:text-zinc-200 disabled:opacity-50"
      >
        Back to sign in
      </button>
    </AuthCardShell>
  )
}


// Shown for the brief window between first paint and the /health probe
// resolving, when there's no stored token. Avoids flashing the full (authed)
// shell on AUTH-enabled deployments before the gate decision is known. Matches
// the app's dark theme; intentionally minimal so it's invisible on a fast
// probe and unobtrusive on a slow one.
function AuthProbePending() {
  return (
    <div
      className="h-screen w-screen flex items-center justify-center bg-zinc-950 text-zinc-100"
      role="status"
      aria-live="polite"
    >
      <div className="flex flex-col items-center gap-3">
        <div className="w-10 h-10 rounded-lg bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center animate-pulse">
          <Logo className="w-5 h-5 text-emerald-400" />
        </div>
        <span className="text-xs text-zinc-500">Loading…</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Payment / access-blocked screen
// ---------------------------------------------------------------------------
// Shown full-page when enforcement is on and this account isn't 'allowed':
//   expired → 7-day trial over, show payment instructions
//   denied  → subscription lapsed, show payment instructions
//   pending → awaiting manual approval (no payment ask)
const PAY_UPI_ID = '9023505664@fam'
const PAY_PHONE = '9023505664'
const PAY_AMOUNT = 1500
const PAY_LINK =
  `upi://pay?pa=${encodeURIComponent(PAY_UPI_ID)}&pn=${encodeURIComponent('MetricAI')}` +
  `&am=${PAY_AMOUNT}&cu=INR&tn=${encodeURIComponent('MetricAI subscription')}`

const FREE_FEATURES = [
  'Full access for 7 days',
  'Upload CSV & Excel files',
  'Ask unlimited questions',
  'Charts & dashboard',
  'Answers in English & Hindi',
]

const PRO_FEATURES = [
  'Everything in the free trial',
  'Keeps working after 7 days',
  'Unlimited questions & uploads',
  'Priority WhatsApp support',
  'New features as they launch',
]

function FeatureItem({ children }: { children: ReactNode }) {
  return (
    <li className="flex items-start gap-2.5 text-sm text-zinc-300">
      <Check className="w-4 h-4 mt-0.5 shrink-0 text-emerald-400" />
      <span>{children}</span>
    </li>
  )
}

/**
 * Plans page. Doubles as the access-blocked screen (trial expired / denied /
 * pending) and as the voluntary "Upgrade" view — `onBack` is supplied only in
 * the latter case, which swaps the Sign-out button for a back-to-app one.
 */
function AccessBlocked({
  status,
  email,
  daysLeft,
  onBack,
}: {
  status: string
  email?: string
  daysLeft?: number | null
  onBack?: () => void
}) {
  const [copied, setCopied] = useState(false)
  const pending = status === 'pending'
  const expired = status === 'expired'
  const onTrial = status === 'trial' || status === 'allowed'

  const copyUpi = () => {
    void navigator.clipboard?.writeText(PAY_UPI_ID).then(
      () => { setCopied(true); window.setTimeout(() => setCopied(false), 1800) },
      () => {},
    )
  }

  const heading = pending
    ? 'Account being set up'
    : expired
      ? 'Your free trial has ended'
      : onTrial
        ? 'Your plan'
        : 'Subscription inactive'

  const subheading = pending
    ? 'Your MetricAI account is being activated — you’ll have access shortly.'
    : expired
      ? 'Upgrade to keep asking questions about your shop’s data.'
      : onTrial
        ? 'Upgrade any time to keep MetricAI after your trial.'
        : 'Your subscription is inactive. Upgrade to restore access.'

  return (
    <div className="min-h-screen w-screen overflow-y-auto bg-zinc-950 text-zinc-100">
      <div className="max-w-4xl mx-auto px-4 py-10 sm:px-6 sm:py-14">
        {/* Header */}
        <div className="text-center">
          <div className="mx-auto mb-5 w-12 h-12 rounded-2xl bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center">
            <Logo className="w-5 h-5 text-emerald-400" />
          </div>
          <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight">{heading}</h1>
          <p className="mt-2.5 text-sm text-zinc-400 max-w-md mx-auto leading-relaxed">{subheading}</p>
        </div>

        {/* Plan cards */}
        <div className="mt-9 grid gap-4 md:grid-cols-2 items-start">
          {/* Free trial */}
          <div className="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-6">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-medium text-zinc-400">Free Trial</div>
              {onTrial && typeof daysLeft === 'number' && daysLeft > 0 && (
                <span className="text-[11px] font-semibold uppercase tracking-wider text-emerald-400 bg-emerald-500/10 border border-emerald-500/25 rounded-full px-2.5 py-1">
                  {daysLeft} {daysLeft === 1 ? 'day' : 'days'} left
                </span>
              )}
              {expired && (
                <span className="text-[11px] font-semibold uppercase tracking-wider text-amber-400 bg-amber-500/10 border border-amber-500/25 rounded-full px-2.5 py-1">
                  Expired
                </span>
              )}
            </div>
            <h2 className="mt-3 text-xl font-semibold tracking-tight">Try MetricAI</h2>
            <p className="mt-1.5 text-sm text-zinc-400 leading-relaxed">
              See your shop’s numbers answered in plain language, on your own data.
            </p>
            <div className="mt-5 flex items-baseline gap-1.5">
              <span className="text-4xl font-bold tracking-tight">₹0</span>
              <span className="text-sm text-zinc-500">/ 7 days</span>
            </div>
            <div className="mt-5 h-11 rounded-lg border border-zinc-800 bg-zinc-950/40 flex items-center justify-center text-sm text-zinc-500">
              {expired ? 'Trial ended' : 'Your current plan'}
            </div>
            <ul className="mt-6 space-y-3">
              {FREE_FEATURES.map((f) => <FeatureItem key={f}>{f}</FeatureItem>)}
            </ul>
          </div>

          {/* Pro */}
          <div className="rounded-2xl border border-emerald-500/40 bg-gradient-to-b from-emerald-950/30 to-zinc-900/40 p-6 shadow-lg shadow-emerald-950/30">
            <div className="flex items-center justify-between gap-3">
              <div className="text-sm font-medium text-emerald-300">MetricAI Pro</div>
              <span className="text-[11px] font-semibold uppercase tracking-wider text-emerald-300 bg-emerald-500/15 border border-emerald-500/30 rounded-full px-2.5 py-1">
                Recommended
              </span>
            </div>
            <h2 className="mt-3 text-xl font-semibold tracking-tight">Your shop’s analyst</h2>
            <p className="mt-1.5 text-sm text-zinc-400 leading-relaxed">
              Keep asking about profit, stock and trends — every day, all year.
            </p>
            <div className="mt-5 flex items-baseline gap-1.5">
              <span className="text-4xl font-bold tracking-tight text-emerald-50">₹1,500</span>
              <span className="text-sm text-zinc-500">/ month</span>
            </div>
            <a
              href={PAY_LINK}
              className="btn btn-primary w-full justify-center mt-5 h-11 text-sm font-semibold"
            >
              Upgrade to Pro — ₹1,500
            </a>
            <ul className="mt-6 space-y-3">
              {PRO_FEATURES.map((f) => <FeatureItem key={f}>{f}</FeatureItem>)}
            </ul>
          </div>
        </div>

        {/* Payment details */}
        {!pending && (
          <div className="mt-8 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-6">
            <h3 className="text-sm font-semibold text-zinc-200">How to pay</h3>
            <div className="mt-5 grid gap-6 sm:grid-cols-[auto_1fr] sm:items-start">
              <div className="flex flex-col items-center sm:items-start">
                <img
                  src={UPI_QR_DATA_URI}
                  alt="UPI QR code to pay ₹1,500 to MetricAI"
                  width={148}
                  height={148}
                  className="rounded-lg bg-white p-2"
                />
                <div className="mt-2 text-[11px] text-zinc-500">Scan with any UPI app</div>
              </div>

              <div className="min-w-0">
                <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1.5">Or pay to this UPI ID</div>
                <button
                  type="button"
                  onClick={copyUpi}
                  className="w-full flex items-center justify-between gap-3 rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-2.5 text-left hover:border-zinc-700 transition-colors"
                  title="Copy UPI ID"
                >
                  <span className="font-mono text-sm text-zinc-200 truncate">{PAY_UPI_ID}</span>
                  <span className="text-[11px] font-medium text-emerald-400 shrink-0">
                    {copied ? 'Copied' : 'Copy'}
                  </span>
                </button>

                <div className="mt-4 rounded-lg border border-zinc-800 bg-zinc-950/40 p-4">
                  <div className="text-xs font-semibold text-zinc-300 mb-1.5">After paying</div>
                  <p className="text-xs text-zinc-400 leading-relaxed">
                    Call or WhatsApp{' '}
                    <a href={`tel:${PAY_PHONE}`} className="text-emerald-400 font-medium">{PAY_PHONE}</a>{' '}
                    and tell us your account email — we’ll activate it right away.
                  </p>
                  {email && (
                    <div className="mt-3">
                      <div className="text-[11px] uppercase tracking-wider text-zinc-500 mb-1">Your account email</div>
                      <div className="font-mono text-sm text-zinc-200 break-all">{email}</div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        {pending && (
          <div className="mt-8 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-6 text-center">
            <p className="text-sm text-zinc-400 leading-relaxed">
              Need it activated now? Call or WhatsApp{' '}
              <a href={`tel:${PAY_PHONE}`} className="text-emerald-400 font-medium">{PAY_PHONE}</a>
              {email ? <> and mention <span className="font-mono text-zinc-300 break-all">{email}</span></> : null}.
            </p>
          </div>
        )}

        <div className="mt-8 flex justify-center">
          {onBack ? (
            <button type="button" onClick={onBack} className="btn btn-secondary">
              Back to MetricAI
            </button>
          ) : (
            <button type="button" onClick={() => clearAuth()} className="btn btn-secondary">
              Sign out
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function App() {
  // Land in the chat by default (was 'dashboard'). The splash overlay below
  // covers the first paint and fades into this view.
  const [view, setView] = useState<NavKey>('ai')
  // Brand splash. It lives in the main-shell return below, so it only paints
  // once we're past the LoginGate (i.e. "after login" when auth is on). It
  // never blocks on the /health probe, so a slow/unreachable backend can't
  // hang the app on a black screen. Re-armed in the gate's onSuccess so the
  // post-login entry shows it too. Plays on every mount (no persisted flag).
  const [showSplash, setShowSplash] = useState(true)
  // Mobile nav drawer. Desktop (>=md) ignores this — the sidebar is static there.
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const authToken = useAppStore((s) => s.auth.token)
  const authEnabled = useAppStore((s) => s.auth.authEnabled)
  // Bump this to force a re-probe of /health (e.g. after login).
  const [authProbe, setAuthProbe] = useState(0)
  // Fix 1c: Drive OAuth callback — error message surfaced at shell level.
  const [driveCallbackError, setDriveCallbackError] = useState<string | null>(null)
  // Password-reset deep link. Captured once on mount from ?reset_token=… so the
  // reset view can render even on AUTH_ENABLED=false deployments and before the
  // /health probe resolves (the user arrives here from an email, unauthenticated).
  const [resetToken, setResetToken] = useState<string | null>(null)
  // Access control (Phase 2). Populated from /auth/me once logged in.
  // { enforced } is the backend rollout flag; { status } is this account's
  // access_status. The blocked screen shows only when enforced && != allowed.
  const [access, setAccess] = useState<
    { status?: string; enforced?: boolean; email?: string; daysLeft?: number | null } | null
  >(null)
  // Voluntary plans/upgrade view (opened from the sidebar).
  const [showPlans, setShowPlans] = useState(false)

  // Probe /health on boot to discover whether AUTH_ENABLED=true. Result
  // lives in store.auth.authEnabled — the gate below renders only when
  // we've confirmed it's true AND there's no token yet.
  useEffect(() => {
    void probeAuthEnabled()
  }, [authProbe])

  // Access re-check. On login, and then every couple of minutes (a background
  // re-validation — no re-login needed), pull /auth/me and note the access
  // status. A transient error never locks the app (we just keep the last
  // known state / stay open).
  useEffect(() => {
    if (!authToken) { setAccess(null); return }
    let alive = true
    const check = async () => {
      try {
        const me = await fetchAuthMe()
        // Days remaining in the free trial, rounded up (0.2 days left → "1 day
        // left"), so the badge never reads "0 days" while access still works.
        let daysLeft: number | null = null
        if (me.trial_ends_at) {
          const ms = new Date(me.trial_ends_at).getTime() - Date.now()
          if (!Number.isNaN(ms)) daysLeft = Math.max(0, Math.ceil(ms / 86400000))
        }
        if (alive) {
          setAccess({
            status: me.access_status,
            enforced: me.access_enforced,
            email: me.email,
            daysLeft,
          })
        }
      } catch { /* transient — do not lock the app on an error */ }
    }
    void check()
    const id = window.setInterval(check, 120000)
    // Instant reaction: if ANY request comes back access-blocked, swap to the
    // payment screen immediately rather than surfacing a raw error in whatever
    // view made the call (and without waiting for the next poll).
    onAccessBlocked((status) => {
      if (alive) setAccess((prev) => ({ ...(prev || {}), status, enforced: true }))
    })
    return () => { alive = false; window.clearInterval(id); onAccessBlocked(null) }
  }, [authToken])

  // Capture a ?reset_token=… password-reset deep link on first mount, then
  // strip it from the URL so a refresh / shared link doesn't re-trigger the
  // view and the token doesn't linger in the address bar / history.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const t = params.get('reset_token')
    if (!t) return
    setResetToken(t)
    params.delete('reset_token')
    const clean = window.location.pathname + (params.toString() ? `?${params}` : '')
    window.history.replaceState({}, '', clean)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Fix 1c: Detect ?drive=connected / ?drive=error from the OAuth callback.
  // This must live at the App level (always mounted) so the callback works
  // even when the user is on a non-Upload view at return time.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const driveParam = params.get('drive')
    if (!driveParam) return
    params.delete('drive')
    const clean = window.location.pathname + (params.toString() ? `?${params}` : '')
    window.history.replaceState({}, '', clean)
    if (driveParam === 'connected') {
      // Switch to the Upload view; UploadData's mount effect will call
      // loadDriveFiles automatically since it runs on every mount.
      setView('upload')
    } else if (driveParam === 'error') {
      setDriveCallbackError('Google Drive sign-in failed. Please try again.')
      setView('upload')
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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

  // Owner preview: ?preview=expired|denied|pending renders the corresponding
  // access screen without any account state or enforcement. Pure UI — reads no
  // data and changes nothing — so it's safe to open (and handy for demos).
  const previewMode = new URLSearchParams(window.location.search).get('preview')
  if (previewMode && ['expired', 'denied', 'pending', 'trial'].includes(previewMode)) {
    return <AccessBlocked status={previewMode} email="shop.owner@example.com" daysLeft={4} />
  }

  // Password-reset deep link takes precedence over every gate: the user
  // arrived from an email link, is unauthenticated, and may be on an
  // AUTH_ENABLED=false deployment. Render the reset view until they finish (or
  // bail), then drop back into the normal flow with the token cleared.
  if (resetToken) {
    return (
      <ResetPasswordView
        token={resetToken}
        onDone={() => setResetToken(null)}
      />
    )
  }

  // Gate the app on a real backend login when AUTH_ENABLED=true.
  // The boolean stays null until probeAuthEnabled resolves; treat null
  // as "not yet enabled" so we don't flash the LoginGate on every boot.
  if (authEnabled === true && !authToken) {
    return (
      <LoginGate
        onSuccess={() => {
          setShowSplash(true)
          setAuthProbe((n) => n + 1)
        }}
      />
    )
  }

  // Auth-gate flash guard: while we have no token AND the /health probe hasn't
  // resolved yet (authEnabled === null), we don't yet know whether the gate or
  // the full shell should show. Render a lightweight loading screen instead of
  // the shell so AUTH-on deployments don't briefly flash authed content before
  // the probe flips authEnabled to true. A returning user (token present) skips
  // this entirely and renders the shell immediately. Once the probe resolves to
  // false this falls through to the shell; to true the LoginGate above renders.
  if (authEnabled === null && !authToken) {
    return <AuthProbePending />
  }

  // Access enforcement (Phase 2): when the backend is enforcing and this
  // customer isn't 'allowed', show the blocked screen instead of the app.
  if (access?.enforced && access.status && access.status !== 'allowed') {
    return (
      <AccessBlocked
        status={access.status}
        email={access.email}
        daysLeft={access.daysLeft}
      />
    )
  }

  // Voluntary "Upgrade" view — same plans page, but with a way back into the app.
  if (showPlans) {
    return (
      <AccessBlocked
        status={access?.status === 'allowed' ? 'trial' : (access?.status || 'trial')}
        email={access?.email}
        daysLeft={access?.daysLeft}
        onBack={() => setShowPlans(false)}
      />
    )
  }

  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-100">
      {showSplash && <SplashScreen onDone={() => setShowSplash(false)} />}
      {driveCallbackError && (
        <div className="fixed top-4 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 rounded-lg bg-red-950/90 border border-red-700/60 px-4 py-2.5 text-sm text-red-200 shadow-lg">
          <AlertTriangle className="w-4 h-4 text-red-400 shrink-0" />
          <span>{driveCallbackError}</span>
          <button
            type="button"
            onClick={() => setDriveCallbackError(null)}
            className="ml-1 text-red-400 hover:text-red-200 text-xs font-medium"
          >
            Dismiss
          </button>
        </div>
      )}
      {/* Drawer backdrop — click anywhere to close. All screen sizes. */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-40"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}
      <Sidebar
        active={view}
        open={sidebarOpen}
        trialDaysLeft={access?.status === 'trial' ? access?.daysLeft ?? null : null}
        onUpgrade={() => { setShowPlans(true); setSidebarOpen(false) }}
        onToggle={() => setSidebarOpen((v) => !v)}
        onSelect={(key) => {
          setView(key)
          // Close the drawer on any nav / New chat / recent-chat selection.
          setSidebarOpen(false)
        }}
      />
      <main className="flex-1 flex flex-col min-w-0">
        <TopBar onToggleSidebar={() => setSidebarOpen((v) => !v)} />
        <ErrorBoundary key={view} fallbackTitle={`${view} view crashed.`}>
          {view === 'ai' ? (
            <AiAssistant key="ai" />
          ) : (
            <div className="flex-1 overflow-y-auto">
              <div className="max-w-6xl mx-auto px-4 md:px-10 py-6 md:py-10">
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
