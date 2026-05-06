import type { ComponentType } from 'react'
import { Building2, Upload, LayoutDashboard, Sparkles, MessageSquare, LogOut } from 'lucide-react'
import { cn } from '@/lib/cn'
import { logoutBackend } from '@/lib/api'
import { useAppStore } from '@/store/useAppStore'
import type { NavKey } from '@/types'

interface NavItem {
  key: NavKey
  label: string
  icon: ComponentType<{ className?: string }>
  hint: string
}

const ITEMS: NavItem[] = [
  { key: 'shop', label: 'Shop Info', icon: Building2, hint: 'Identity & API key' },
  { key: 'upload', label: 'Upload Data', icon: Upload, hint: 'CSV / Excel / Drive' },
  { key: 'dashboard', label: 'Dashboard', icon: LayoutDashboard, hint: 'KPIs & charts' },
  { key: 'ai', label: 'AI Assistant', icon: MessageSquare, hint: 'Ask in plain English' },
]

interface SidebarProps {
  active: NavKey
  onSelect: (key: NavKey) => void
}

export function Sidebar({ active, onSelect }: SidebarProps) {
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
