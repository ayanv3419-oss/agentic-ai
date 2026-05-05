import { useState } from 'react'
import { Sidebar } from '@/components/Sidebar'
import { TopBar } from '@/components/TopBar'
import { ShopInfo } from '@/pages/ShopInfo'
import { UploadData } from '@/pages/UploadData'
import { Dashboard } from '@/pages/Dashboard'
import { AiAssistant } from '@/pages/AiAssistant'
import type { NavKey } from '@/types'

export default function App() {
  const [view, setView] = useState<NavKey>('dashboard')

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
