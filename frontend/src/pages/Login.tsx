import { useState } from 'react'
import { Lock, Sparkles, AlertTriangle, Loader2 } from 'lucide-react'
import { ApiError, login as apiLogin } from '@/lib/api'
import { useAppStore } from '@/store/useAppStore'

export function Login() {
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
