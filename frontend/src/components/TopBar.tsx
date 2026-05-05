import { Activity } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'

export function TopBar() {
  const shop = useAppStore((s) => s.shop)
  const dataset = useAppStore((s) => s.dataset)

  return (
    <header className="h-16 shrink-0 border-b border-zinc-800 px-8 flex items-center justify-between bg-zinc-950/80 backdrop-blur supports-[backdrop-filter]:bg-zinc-950/60">
      <div className="min-w-0">
        <div className="text-xs text-zinc-500 truncate">
          {shop.businessName || 'No business configured'}
        </div>
        <div className="text-sm font-medium text-zinc-100 truncate">
          {shop.shopName || 'Welcome'}
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
