/**
 * Data layer — types, API client, and global state store.
 *
 * Single consolidated module for everything frontend code needs in order to
 * talk to the backend or read/write user-visible state. Other UI modules
 * import from here only — no fan-out into sub-folders.
 *
 * Sections:
 *   1. Shared types (UI + DTO contracts with the backend)
 *   2. Class-name helper (cn)
 *   3. Backend URL resolution + low-level fetch helpers (apiGet, apiPost,
 *      uploadFile, streamQuery)
 *   4. Public API functions (login, fetchDashboard, syncDrive, …)
 *   5. Zustand store (`useAppStore`) + `selectIsAuthed` selector
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

// ---------------------------------------------------------------------------
// 1. Types
// ---------------------------------------------------------------------------

export type NavKey = 'shop' | 'upload' | 'dashboard' | 'ai'

export type ChatRole = 'user' | 'assistant'
export type ChatStatus = 'thinking' | 'streaming' | 'done' | 'error'

export interface ToolEvent {
  tool: string
  status: 'running' | 'done' | 'failed'
  durationMs?: number
  error?: string
}

export interface ChatMessage {
  id: string
  role: ChatRole
  content: string
  status: ChatStatus
  toolEvents: ToolEvent[]
  chart?: SalesChart | null
  error?: string
  timestamp: string
}

export interface ShopInfo {
  shopName: string
  ownerName: string
  groqApiKey: string
}

export interface DatasetMeta {
  name: string
  rows: number
  uploadedAt: string
  source: 'upload' | 'drive'
}

export interface DashboardFilters {
  month: string // YYYY-MM, or '' for "all time"
}

export interface KpiSet {
  total_sales: number
  orders: number
  customers: number
}

export interface Series {
  bucket: string
  sales: number
  orders: number
}

export interface DashboardData {
  month: string | null
  kpis: KpiSet
  series: Series[]
}

export interface UploadSummary {
  total_sales: number
  min_date: string
  max_date: string
}

export interface UploadResponse {
  batch_id: string
  filename: string
  rows_inserted: number
  summary: UploadSummary
  target?: 'sales' | 'purchase'
  rows_failed?: number
  errors?: Array<{ row: number; reason: string }>
  unmatched_headers?: string[]
  sheet_name?: string | null
  header_row_used?: string[]
  validation?: { batch_rows: number; min_date: string; max_date: string }
  bytes_received?: number
  table_total?: number
}

export type UploadStatus = 'active' | 'error' | 'removed'

export interface UploadEntry {
  batch_id: string
  filename: string
  target: 'sales' | 'purchase'
  rows_inserted: number
  rows_failed: number
  source: 'upload' | 'google_drive'
  status: UploadStatus
  min_date?: string | null
  max_date?: string | null
  error_message?: string | null
  uploaded_at: string
}

export interface UploadsListResponse {
  uploads: UploadEntry[]
  total_rows: { sales: number; purchase: number }
}

export interface DisconnectResponse {
  batch_id: string
  rows_removed: number
  table: 'sales' | 'purchase'
  already_removed: boolean
  status: 'removed'
}

export interface AuthMe {
  authenticated: boolean
  username?: string
  email?: string
  name?: string
  picture?: string
  google_configured?: boolean
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  token: string
  username: string
  expires_at: string
}

export interface AuthState {
  token: string | null
  username: string | null
  expiresAt: string | null
}

export interface DriveImportDetail {
  file: string
  status: string
  rows?: number
  batch_id?: string
  file_id?: string
  mime_type?: string
  error?: string
}

export interface DriveSyncResult {
  ok: boolean
  discovered: number
  imported: number
  rows_inserted: number
  skipped_already: number
  skipped_too_large: number
  failed: number
  details: DriveImportDetail[]
  error?: string
  kind?: string
}

export type ChartGranularity = 'daily' | 'weekly' | 'monthly' | 'yearly'

/** Drives the chat-bubble chart selection.
 *  - summary / trend / forecast → AreaChart over a date series
 *  - ranking                    → BarChart with categorical X (party names)
 *  - comparison / rca           → 2-bar BarChart (current vs previous period) */
export type ChartKind =
  | 'summary'
  | 'trend'
  | 'ranking'
  | 'comparison'
  | 'rca'
  | 'forecast'
  | 'anomaly'

export interface ChartTotals {
  total_sales: number
  orders: number
  customers: number
}

export interface RankingItem {
  name: string
  sales: number
  orders: number
}

export interface PeriodTotals {
  period: string
  start: string
  end: string
  sales: number
  orders: number
  customers: number
}

export interface ChartComparison {
  current: PeriodTotals
  previous: PeriodTotals
  delta_abs: number
  delta_pct: number | null
}

export interface SalesChart {
  granularity: ChartGranularity
  totals: ChartTotals
  series: Series[]
  // Optional intent-driven extras (present when the backend produces
  // a non-summary aggregate). All are read-only on the frontend.
  kind?: ChartKind
  intent_type?: string
  items?: RankingItem[]
  comparison?: ChartComparison
  forecast_horizon_days?: number
}

export interface SseEvent {
  event: string
  data: unknown
}

// ---------------------------------------------------------------------------
// 2. Class-name helper
// ---------------------------------------------------------------------------

export function cn(...inputs: Array<string | false | null | undefined>): string {
  return inputs.filter(Boolean).join(' ')
}

// ---------------------------------------------------------------------------
// 3. Backend URL resolution + low-level fetch helpers
// ---------------------------------------------------------------------------

/** Hardcoded production backend — last-resort fallback when no env var is set.
 *  This MUST point to the real deployed Render service. Override per-environment
 *  with VITE_BACKEND_URL if your service is named differently. */
const PRODUCTION_FALLBACK = 'https://agentic-ai-anet.onrender.com'

function resolveBackendUrl(): string {
  const candidate =
    (import.meta.env.VITE_BACKEND_URL as string | undefined) ??
    (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
    PRODUCTION_FALLBACK
  const trimmed = (candidate || '').trim().replace(/\/+$/, '')
  return trimmed || PRODUCTION_FALLBACK
}

const BASE_URL = resolveBackendUrl()

export class ApiError extends Error {
  status: number
  detail?: unknown
  constructor(message: string, status: number, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

function buildHeaders(extra?: HeadersInit): Headers {
  const { shop, auth } = useAppStore.getState()
  const h = new Headers(extra)
  if (!h.has('Content-Type')) h.set('Content-Type', 'application/json')
  if (shop.groqApiKey) h.set('X-Groq-Api-Key', shop.groqApiKey)
  if (auth.token) h.set('Authorization', `Bearer ${auth.token}`)
  return h
}

async function handle<T>(res: Response, label: string): Promise<T> {
  if (!res.ok) {
    const detail = await res.json().catch(() => undefined)
    if (res.status === 401 && !label.includes('/auth/login')) {
      // eslint-disable-next-line no-console
      console.warn(`[api] ${label} → 401, clearing auth`)
      useAppStore.getState().clearAuth()
    }
    // eslint-disable-next-line no-console
    console.error(`[api] ${label} → HTTP ${res.status}`, detail)
    throw new ApiError(`${label} ${res.status}`, res.status, detail)
  }
  return (await res.json()) as T
}

/** fetch wrapper that turns network-level failures into a logged ApiError
 * (status=0). Browsers translate every CORS / DNS / connection-refused
 * failure into the opaque `TypeError: Failed to fetch`; this catches and
 * re-emits with the URL + cause for easier diagnosis. */
async function safeFetch(input: RequestInfo, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init)
  } catch (e) {
    const url = typeof input === 'string' ? input : (input as Request).url
    const cause = e instanceof Error ? `${e.name}: ${e.message}` : String(e)
    // eslint-disable-next-line no-console
    console.error(`[api] network error reaching ${url}`, e)
    throw new ApiError(
      `Network error reaching ${url} (${cause}). Backend down, wrong port, or CORS blocked.`,
      0,
      { cause, url },
    )
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await safeFetch(`${BASE_URL}${path}`, {
    headers: buildHeaders(),
    credentials: 'include',
  })
  return handle<T>(res, `GET ${path}`)
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await safeFetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers: buildHeaders(),
    credentials: 'include',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return handle<T>(res, `POST ${path}`)
}

export async function uploadFile<T>(
  path: string,
  file: File,
  extra?: Record<string, string>,
): Promise<T> {
  const { shop, auth } = useAppStore.getState()
  const fd = new FormData()
  fd.append('file', file)
  Object.entries(extra ?? {}).forEach(([k, v]) => fd.append(k, v))
  const headers: Record<string, string> = {}
  if (shop.groqApiKey) headers['X-Groq-Api-Key'] = shop.groqApiKey
  if (auth.token) headers['Authorization'] = `Bearer ${auth.token}`
  // eslint-disable-next-line no-console
  console.info(`[api] UPLOAD ${path} file=${file.name} bytes=${file.size}`)
  const res = await safeFetch(`${BASE_URL}${path}`, {
    method: 'POST',
    headers,
    body: fd,
    credentials: 'include',
  })
  return handle<T>(res, `UPLOAD ${path}`)
}

/**
 * Stream SSE events from POST /query_stream.
 * Calls onEvent for each parsed `event:`/`data:` block. The data field is
 * JSON-parsed when possible.
 */
export async function streamQuery(
  question: string,
  onEvent: (e: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const { shop, auth } = useAppStore.getState()
  const res = await safeFetch(`${BASE_URL}/query_stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(shop.groqApiKey ? { 'X-Groq-Api-Key': shop.groqApiKey } : {}),
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
    },
    body: JSON.stringify({ question }),
    signal,
  })
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => undefined)
    if (res.status === 401) {
      useAppStore.getState().clearAuth()
    }
    // eslint-disable-next-line no-console
    console.error(`[api] POST /query_stream → HTTP ${res.status}`, detail)
    throw new ApiError(`POST /query_stream ${res.status}`, res.status, detail)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const blocks = buf.split('\n\n')
    buf = blocks.pop() ?? ''
    for (const block of blocks) {
      if (!block.trim()) continue
      let event = 'message'
      let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (!data) continue
      try {
        onEvent({ event, data: JSON.parse(data) })
      } catch {
        onEvent({ event, data })
      }
    }
  }
}

// ---------------------------------------------------------------------------
// 4. Public API surface
// ---------------------------------------------------------------------------

export async function uploadSales(
  file: File,
  target: 'sales' | 'purchase' = 'sales',
): Promise<UploadResponse> {
  return uploadFile<UploadResponse>('/upload', file, { target })
}

export async function fetchUploadsList(): Promise<UploadsListResponse> {
  return apiGet<UploadsListResponse>('/uploads')
}

export async function disconnectUpload(batchId: string): Promise<DisconnectResponse> {
  return apiPost<DisconnectResponse>(`/uploads/${encodeURIComponent(batchId)}/disconnect`)
}

export async function fetchDashboard(month?: string): Promise<DashboardData> {
  const q = month ? `?month=${encodeURIComponent(month)}` : ''
  return apiGet<DashboardData>(`/dashboard${q}`)
}

/** Admin login. Throws ApiError on bad credentials / network failure. */
export async function login(username: string, password: string): Promise<LoginResponse> {
  const res = await safeFetch(`${BASE_URL}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  return handle<LoginResponse>(res, 'POST /auth/login')
}

export async function logoutBackend(): Promise<{ ok: boolean }> {
  return apiPost<{ ok: boolean }>('/auth/logout')
}

export const googleLoginUrl = (): string => `${BASE_URL}/auth/google/login`

export async function fetchAuthMe(): Promise<AuthMe> {
  return apiGet<AuthMe>('/auth/me')
}

export async function logout(): Promise<{ ok: boolean }> {
  return apiPost<{ ok: boolean }>('/auth/logout')
}

export async function syncDrive(): Promise<DriveSyncResult> {
  return apiPost<DriveSyncResult>('/drive/sync')
}

/**
 * AI Assistant streaming wrapper. Sends `message` to /query_stream and forwards
 * each parsed SSE event to `onEvent`. The Groq API key is pulled from the store
 * but can be overridden via opts.apiKey.
 */
export async function streamAIQuery(
  message: string,
  onEvent: (e: SseEvent) => void,
  opts?: { apiKey?: string; signal?: AbortSignal },
): Promise<void> {
  const apiKey = opts?.apiKey ?? useAppStore.getState().shop.groqApiKey
  if (!apiKey) {
    throw new ApiError('Groq API key not configured. Set it in Shop Info.', 401)
  }
  return streamQuery(message, onEvent, opts?.signal)
}

// ---------------------------------------------------------------------------
// 5. Global state store (Zustand + persist)
// ---------------------------------------------------------------------------

interface AppState {
  shop: ShopInfo
  dataset: DatasetMeta | null
  filters: DashboardFilters

  chatHistory: ChatMessage[]
  isStreaming: boolean

  auth: AuthState

  setShop: (info: Partial<ShopInfo>) => void
  setDataset: (d: DatasetMeta | null) => void
  setFilters: (f: Partial<DashboardFilters>) => void
  clearShop: () => void

  appendMessage: (m: ChatMessage) => void
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void
  clearChat: () => void
  setStreaming: (b: boolean) => void

  setAuth: (token: string, username: string, expiresAt: string) => void
  clearAuth: () => void
}

const emptyShop: ShopInfo = {
  shopName: '',
  ownerName: '',
  groqApiKey: '',
}

const emptyAuth: AuthState = {
  token: null,
  username: null,
  expiresAt: null,
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

      auth: emptyAuth,

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

      setAuth: (token, username, expiresAt) =>
        set({ auth: { token, username, expiresAt } }),
      clearAuth: () => set({ auth: emptyAuth }),
    }),
    {
      name: 'agentic-ai:v1',
      partialize: (s) => ({
        shop: s.shop,
        filters: s.filters,
        dataset: s.dataset,
        chatHistory: s.chatHistory.slice(-MAX_PERSISTED_MESSAGES),
        auth: s.auth,
      }),
    },
  ),
)

/** Derived selector — true iff a non-expired bearer token is on hand. */
export function selectIsAuthed(s: AppState): boolean {
  const a = s.auth
  if (!a.token || !a.expiresAt) return false
  const exp = new Date(a.expiresAt).getTime()
  return Number.isFinite(exp) && exp > Date.now()
}
