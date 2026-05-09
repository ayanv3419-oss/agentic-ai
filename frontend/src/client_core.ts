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

export interface MonthlySalesSlice {
  month: string
  sales: number
}

export interface DashboardData {
  month: string | null
  kpis: KpiSet
  series: Series[]
  monthly_sales_pie?: MonthlySalesSlice[]
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

export interface UserPrincipal {
  id: string
  email: string
  tenant_id: string
  workspace_id: string
  role: 'owner' | 'admin' | 'viewer'
}

export interface WorkspaceSummary {
  id: string
  name: string
}

export interface AuthMe {
  authenticated: boolean
  username?: string
  email?: string
  name?: string
  picture?: string
  google_configured?: boolean
  // New JWT-aware fields. Optional for back-compat with the legacy
  // single-admin /auth/me response.
  user_id?: string
  tenant_id?: string
  workspace_id?: string
  role?: 'owner' | 'admin' | 'viewer'
  workspace?: WorkspaceSummary | null
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  token: string
  username: string
  expires_at: string
  // Present on JWT logins (real users); absent for legacy admin path.
  user?: UserPrincipal
}

export interface RegisterRequest {
  email: string
  password: string
  workspace_name?: string
}

export interface RegisterResponse {
  token: string
  expires_at: number    // unix ts (the register endpoint returns int, not ISO)
  user: UserPrincipal
  workspace: WorkspaceSummary
}

export interface AuthState {
  token: string | null
  username: string | null
  expiresAt: string | null
  // Optional — populated for JWT logins (real users), null for legacy
  // single-admin sessions. Allows role-aware UI + workspace display.
  userId: string | null
  tenantId: string | null
  workspaceId: string | null
  workspaceName: string | null
  role: 'owner' | 'admin' | 'viewer' | null
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
  const { shop, auth, conversationId } = useAppStore.getState()
  const res = await safeFetch(`${BASE_URL}/query_stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(shop.groqApiKey ? { 'X-Groq-Api-Key': shop.groqApiKey } : {}),
      ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}),
    },
    body: JSON.stringify({ question, conversation_id: conversationId }),
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

/** Login. Accepts either email (real users — JWT path) or username
 *  (legacy single-admin path). The backend routes by '@' presence.
 *  Throws ApiError on bad credentials / network failure. */
export async function login(username: string, password: string): Promise<LoginResponse> {
  const res = await safeFetch(`${BASE_URL}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  return handle<LoginResponse>(res, 'POST /auth/login')
}

/** Self-service registration — creates (tenant, workspace, user) trio
 *  and returns a JWT immediately so the user lands signed-in. */
export async function register(req: RegisterRequest): Promise<RegisterResponse> {
  const res = await safeFetch(`${BASE_URL}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  return handle<RegisterResponse>(res, 'POST /auth/register')
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
  opts?: { apiKey?: string; signal?: AbortSignal; maxRetries?: number },
): Promise<void> {
  const apiKey = opts?.apiKey ?? useAppStore.getState().shop.groqApiKey
  if (!apiKey) {
    throw new ApiError('Groq API key not configured. Set it in Shop Info.', 401)
  }
  return streamQueryWithRetry(message, onEvent, {
    signal:     opts?.signal,
    maxRetries: opts?.maxRetries ?? 2,
  })
}

/**
 * Reconnect-aware wrapper around `streamQuery`. Retries on transient
 * network failures (TypeError from fetch, dropped connection mid-stream)
 * with exponential backoff: 0.5s, 1s, 2s. Does NOT retry:
 *
 *   - 4xx HTTP responses (auth, validation, rate-limit)   — caller's job
 *   - explicit AbortError                                 — user cancelled
 *   - ApiError with status set                            — server said no
 *
 * Each retry starts a NEW stream from the server (the existing turn is
 * lost) — that's an acceptable degradation given the alternative is a
 * blank chat bubble. When the SSE backend learns to resume from a
 * `turn_id`, this wrapper will get a "resume from event N" path.
 */
async function streamQueryWithRetry(
  message: string,
  onEvent: (e: SseEvent) => void,
  opts: { signal?: AbortSignal; maxRetries: number },
): Promise<void> {
  let attempt = 0
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      await streamQuery(message, onEvent, opts.signal)
      return
    } catch (err) {
      // Honour explicit aborts immediately.
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw err
      }
      // Server-side rejections (4xx with parsed envelope) are not
      // retryable — propagate so the caller can render the error.
      if (err instanceof ApiError && err.status && err.status < 500) {
        throw err
      }
      attempt += 1
      if (attempt > opts.maxRetries) {
        throw err
      }
      const delay = Math.min(2000, 500 * 2 ** (attempt - 1))
      // eslint-disable-next-line no-console
      console.warn(`[sse] retry ${attempt}/${opts.maxRetries} in ${delay}ms`, err)
      // Tell the consumer we're reconnecting so the chat bubble can
      // surface a hint instead of looking frozen.
      try {
        onEvent({ event: 'reconnecting', data: { attempt, delay_ms: delay } })
      } catch { /* never let UI signal block retry */ }
      await new Promise((r) => setTimeout(r, delay))
      if (opts.signal?.aborted) {
        const e = new DOMException('aborted', 'AbortError')
        throw e
      }
    }
  }
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
  // Per-tab conversation identifier — sent with every /query_stream so the
  // backend's per-(user, conversation) memory store can isolate continuity.
  // Rotated when the user clears chat (so a fresh chat is a fresh window).
  conversationId: string

  auth: AuthState

  setShop: (info: Partial<ShopInfo>) => void
  setDataset: (d: DatasetMeta | null) => void
  setFilters: (f: Partial<DashboardFilters>) => void
  clearShop: () => void

  appendMessage: (m: ChatMessage) => void
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void
  clearChat: () => void
  setStreaming: (b: boolean) => void
  rotateConversation: () => void

  setAuth: (
    token: string,
    username: string,
    expiresAt: string,
    extras?: {
      userId?: string
      tenantId?: string
      workspaceId?: string
      workspaceName?: string | null
      role?: 'owner' | 'admin' | 'viewer'
    },
  ) => void
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
  userId: null,
  tenantId: null,
  workspaceId: null,
  workspaceName: null,
  role: null,
}

const defaultMonth = (): string => new Date().toISOString().slice(0, 7)

const MAX_PERSISTED_MESSAGES = 50

// Per-tab conversation id. We use crypto.randomUUID when available, else
// fall back to a Math.random-based hex string (older browsers / SSR).
const newConversationId = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'c-' + Math.random().toString(16).slice(2) + Date.now().toString(16)
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      shop: emptyShop,
      dataset: null,
      filters: { month: defaultMonth() },

      chatHistory: [],
      isStreaming: false,
      conversationId: newConversationId(),

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
      clearChat: () =>
        set({
          chatHistory: [],
          isStreaming: false,
          // Rotate so the next turn starts a fresh continuity window.
          conversationId: newConversationId(),
        }),
      setStreaming: (b) => set({ isStreaming: b }),
      rotateConversation: () => set({ conversationId: newConversationId() }),

      setAuth: (token, username, expiresAt, extras) =>
        set({
          auth: {
            token,
            username,
            expiresAt,
            userId:        extras?.userId ?? null,
            tenantId:      extras?.tenantId ?? null,
            workspaceId:   extras?.workspaceId ?? null,
            workspaceName: extras?.workspaceName ?? null,
            role:          extras?.role ?? null,
          },
        }),
      clearAuth: () =>
        set({
          auth: emptyAuth,
          // Sign-out also rotates conversation so a different user landing on
          // the same browser doesn't inherit the previous conversation key.
          conversationId: newConversationId(),
        }),
    }),
    {
      name: 'agentic-ai:v1',
      partialize: (s) => ({
        shop: s.shop,
        filters: s.filters,
        dataset: s.dataset,
        chatHistory: s.chatHistory.slice(-MAX_PERSISTED_MESSAGES),
        conversationId: s.conversationId,
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
