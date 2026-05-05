import type { ReactNode } from 'react'

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
