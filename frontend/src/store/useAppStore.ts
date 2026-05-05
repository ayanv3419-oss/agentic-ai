import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type {
  ChatMessage,
  DashboardFilters,
  DatasetMeta,
  ShopInfo,
} from '@/types'

interface AppState {
  shop: ShopInfo
  dataset: DatasetMeta | null
  filters: DashboardFilters

  chatHistory: ChatMessage[]
  isStreaming: boolean

  setShop: (info: Partial<ShopInfo>) => void
  setDataset: (d: DatasetMeta | null) => void
  setFilters: (f: Partial<DashboardFilters>) => void
  clearShop: () => void

  appendMessage: (m: ChatMessage) => void
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void
  clearChat: () => void
  setStreaming: (b: boolean) => void
}

const emptyShop: ShopInfo = {
  shopName: '',
  businessName: '',
  ownerName: '',
  groqApiKey: '',
}

const defaultMonth = (): string => new Date().toISOString().slice(0, 7)

const MAX_PERSISTED_MESSAGES = 50

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      shop: emptyShop,
      dataset: null,
      filters: { month: defaultMonth() },

      chatHistory: [],
      isStreaming: false,

      setShop: (info) => set((s) => ({ shop: { ...s.shop, ...info } })),
      setDataset: (d) => set({ dataset: d }),
      setFilters: (f) => set((s) => ({ filters: { ...s.filters, ...f } })),
      clearShop: () => set({ shop: emptyShop }),

      appendMessage: (m) =>
        set((s) => ({ chatHistory: [...s.chatHistory, m] })),
      updateMessage: (id, patch) =>
        set((s) => ({
          chatHistory: s.chatHistory.map((m) =>
            m.id === id ? { ...m, ...patch } : m,
          ),
        })),
      clearChat: () => set({ chatHistory: [], isStreaming: false }),
      setStreaming: (b) => set({ isStreaming: b }),
    }),
    {
      name: 'agentic-ai:v1',
      partialize: (s) => ({
        shop: s.shop,
        filters: s.filters,
        dataset: s.dataset,
        chatHistory: s.chatHistory.slice(-MAX_PERSISTED_MESSAGES),
      }),
    },
  ),
)
