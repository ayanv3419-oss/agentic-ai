/**
 * Data layer — types, API client, and global state store.
 *
 * Single-user local-first MVP — no authentication.
 *
 * Sections:
 *   1. Shared types
 *   2. Class-name helper (cn)
 *   3. Backend URL resolution + low-level fetch helpers
 *   4. Public API functions
 *   5. Zustand store
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
  // The LLM's one-line reasoning for picking this capability, carried by the
  // `loop.iteration` SSE event the AgenticLoop emits before each tool call.
  reasoning?: string
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
}

export interface DatasetMeta {
  name: string
  rows: number
  uploadedAt: string
  source: 'upload' | 'drive'
}

export interface DashboardFilters {
  month: string
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

export type ChartGranularity = 'daily' | 'weekly' | 'monthly' | 'yearly'

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
  kind?: ChartKind
  intent_type?: string
  items?: RankingItem[]
  comparison?: ChartComparison
  forecast_horizon_days?: number
  // Semantic label for the Y axis (sales / count / quantity / percent / ...)
  // Backend normalises LLM-supplied label via _normalize_y_label.
  // When absent, frontend treats the value as currency for back-compat
  // with legacy KPI / ResultAggregator charts.
  y_label?: string
}

export interface SseEvent {
  event: string
  data: unknown
}

// Google upload/Drive integration types (separate from app login — used
// only by the "Connect Google Drive" UI on the Upload page).
export interface AuthMe {
  authenticated: boolean
  username?: string
  email?: string
  name?: string
  picture?: string
  google_configured?: boolean
}

export interface DriveFile {
  id: string
  name: string
  mimeType: string
  modifiedTime?: string
  size?: string
}

export interface DriveStatus {
  connected: boolean
  configured: boolean
  files: DriveFile[]
}

export interface DriveSyncFileResult {
  ok: boolean
  file_id: string
  batch_id?: string
  filename?: string
  target?: string
  rows_inserted?: number
  rows_failed?: number
  rows_skipped_duplicate?: number
  rows_replaced?: number
  table_total?: number
  error?: string
}

export interface DriveSyncResult {
  target: string
  dedup_mode: string
  results: DriveSyncFileResult[]
  rows_inserted_total: number
}

// Chat session history (read-only "Recents"). Server-sourced metadata for one
// stored conversation. Lives in the backend DB (conversations table) and is
// listed recency-first by the Recents sidebar.
export interface SessionMeta {
  id: string
  title: string
  updated_at: string
  message_count: number
}

// Shape of a single server-stored message as returned by GET /conversations/{id}.
// Mapped into the frontend's ChatMessage by getConversation().
interface ServerConversationMessage {
  id: string
  role: ChatRole
  content: string
  chart?: SalesChart | null
  created_at: string
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

const PRODUCTION_FALLBACK = 'https://agentic-ai-anet.onrender.com'

function resolveBackendUrl(): string {
  const candidate =
    (import.meta.env.VITE_BACKEND_URL as string | undefined) ??
    (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
    PRODUCTION_FALLBACK
  const trimmed = (candidate || '').trim().replace(/\/+$/, '')
  const resolved = trimmed || PRODUCTION_FALLBACK
  // Loud warning in dev when nothing was configured and we silently
  // landed on the production Render app. This hides the most common
  // local-dev footgun: forgetting VITE_BACKEND_URL and then wondering
  // why local backend changes have no effect.
  if (import.meta.env.DEV && resolved === PRODUCTION_FALLBACK) {
    // eslint-disable-next-line no-console
    console.warn(
      `[client_core] VITE_BACKEND_URL not set; using production fallback ` +
      `${PRODUCTION_FALLBACK}. Set VITE_BACKEND_URL=http://localhost:8000 ` +
      `in .env.local to talk to your local backend.`,
    )
  }
  return resolved
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
  const h = new Headers(extra)
  if (!h.has('Content-Type')) h.set('Content-Type', 'application/json')
  return h
}

async function handle<T>(res: Response, label: string): Promise<T> {
  if (!res.ok) {
    const detail = await res.json().catch(() => undefined)
    // eslint-disable-next-line no-console
    console.error(`[api] ${label} → HTTP ${res.status}`, detail)
    throw new ApiError(`${label} ${res.status}`, res.status, detail)
  }
  return (await res.json()) as T
}

// Inject the Authorization: Bearer header on every backend request when
// the store has a token. The backend's auth middleware ignores it when
// AUTH_ENABLED=false, so this is safe to always do — no flag plumbing.
function withAuthHeader(init?: RequestInit): RequestInit {
  try {
    const token = useAppStore.getState().auth.token
    if (!token) return init ?? {}
    const headers = new Headers(init?.headers ?? undefined)
    if (!headers.has('Authorization')) {
      headers.set('Authorization', `Bearer ${token}`)
    }
    return { ...(init ?? {}), headers }
  } catch {
    return init ?? {}
  }
}

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
  const res = await safeFetch(`${BASE_URL}${path}`, withAuthHeader({
    headers: buildHeaders(),
  }))
  return handle<T>(res, `GET ${path}`)
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await safeFetch(`${BASE_URL}${path}`, withAuthHeader({
    method: 'POST',
    headers: buildHeaders(),
    body: body === undefined ? undefined : JSON.stringify(body),
  }))
  return handle<T>(res, `POST ${path}`)
}

export async function uploadFile<T>(
  path: string,
  file: File,
  extra?: Record<string, string>,
): Promise<T> {
  const fd = new FormData()
  fd.append('file', file)
  Object.entries(extra ?? {}).forEach(([k, v]) => fd.append(k, v))
  const headers: Record<string, string> = {}
  // eslint-disable-next-line no-console
  console.info(`[api] UPLOAD ${path} file=${file.name} bytes=${file.size}`)
  const res = await safeFetch(`${BASE_URL}${path}`, withAuthHeader({
    method: 'POST',
    headers,
    body: fd,
  }))
  return handle<T>(res, `UPLOAD ${path}`)
}

/**
 * Stream SSE events from POST /query_stream.
 */
export async function streamQuery(
  question: string,
  onEvent: (e: SseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const { conversationId } = useAppStore.getState()
  const res = await safeFetch(`${BASE_URL}/query_stream`, withAuthHeader({
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify({ question, conversation_id: conversationId }),
    signal,
  }))
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => undefined)
    // eslint-disable-next-line no-console
    console.error(`[api] POST /query_stream → HTTP ${res.status}`, detail)
    throw new ApiError(`POST /query_stream ${res.status}`, res.status, detail)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  // The finally block releases the underlying ReadableStream lock so the
  // network connection can be reclaimed when the user navigates away
  // mid-stream. Previously the reader was leaked: the AbortController
  // stopped *new* reads but the reader reference (and its buffered data)
  // sat in memory until GC, doubled in React StrictMode.
  try {
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
          else if (line.startsWith('data:')) {
            // SSE spec: strip one leading space, join multi-line data fields with \n.
            // The old `+= .trim()` silently glued JSON tokens together when a
            // payload happened to contain a newline.
            data += (data ? '\n' : '') + line.slice(5).replace(/^ /, '')
          }
        }
        if (!data) continue
        try {
          const parsed = JSON.parse(data)
          // Stamp serverDataVersion from any SSE payload that carries it
          // (turn.end + final from the backend's emit boundary). Powers the
          // Dashboard auto-refetch and the chat "data updated" banner.
          if (parsed && typeof parsed === 'object' && 'data_version' in parsed) {
            useAppStore.getState().observeServerDataVersion(parsed.data_version)
          }
          onEvent({ event, data: parsed })
        } catch {
          onEvent({ event, data })
        }
      }
    }
  } finally {
    try { await reader.cancel() } catch { /* already done / aborted */ }
  }
}

// ---------------------------------------------------------------------------
// 4. Public API surface
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Auth helpers — call /auth/login, store the signed token, sign the user
// out. Used by the LoginGate component. The backend's auth middleware
// validates the token on every subsequent request via the Authorization
// header that withAuthHeader() injects.
// ---------------------------------------------------------------------------

export interface LoginResult {
  ok: boolean
  token: string
  username: string
  token_ttl_hours?: number
  auth_enabled?: boolean
}

export async function loginWith(
  username: string,
  password: string,
): Promise<LoginResult> {
  const res = await safeFetch(`${BASE_URL}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => undefined)
    throw new ApiError(
      (detail as { detail?: string } | undefined)?.detail
        ?? `Login failed (HTTP ${res.status})`,
      res.status,
      detail,
    )
  }
  const result = (await res.json()) as LoginResult
  useAppStore.getState().setAuthToken(result.token, result.username)
  if (typeof result.auth_enabled === 'boolean') {
    useAppStore.getState().setAuthEnabled(result.auth_enabled)
  }
  return result
}

// Create a new account. POST /auth/signup { email, password } -> { token }.
// On success the backend mints a tenant-bearing token; we store it through the
// exact same path loginWith uses (setAuthToken) so the app becomes
// authenticated identically. The username slot is set to the email so the
// shell has something to display. Reuses safeFetch / BASE_URL / ApiError.
export async function signupWith(email: string, password: string): Promise<void> {
  const res = await safeFetch(`${BASE_URL}/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => undefined)
    throw new ApiError(
      (detail as { detail?: string } | undefined)?.detail
        ?? `Sign up failed (HTTP ${res.status})`,
      res.status,
      detail,
    )
  }
  const result = (await res.json()) as { token: string }
  useAppStore.getState().setAuthToken(result.token, email)
}

// Clear the local auth state. Renamed from `logout` because the codebase
// already has a Google-Drive `logout()` that hits POST /auth/logout —
// keeping the names distinct prevents a TS duplicate-declaration error
// and keeps the call sites readable.
export function clearAuth(): void {
  useAppStore.getState().setAuthToken(null, null)
}

// Probe /health for the backend's AUTH_ENABLED flag. Called once on app
// boot so the LoginGate knows whether to render. Never throws — silently
// leaves authEnabled as null on network error.
export async function probeAuthEnabled(): Promise<boolean | null> {
  try {
    const res = await safeFetch(`${BASE_URL}/health`)
    if (!res.ok) return null
    const json = await res.json().catch(() => ({})) as { auth_enabled?: boolean }
    const flag = typeof json.auth_enabled === 'boolean' ? json.auth_enabled : false
    useAppStore.getState().setAuthEnabled(flag)
    return flag
  } catch {
    return null
  }
}

export async function uploadSales(
  file: File,
  target: 'sales' | 'purchase' = 'sales',
): Promise<UploadResponse> {
  const result = await uploadFile<UploadResponse>('/upload', file, { target })
  // Backend stamps `data_version` on the upload response (Phase A wiring).
  // Pulling it into the store here means a successful upload flows
  // through to the Dashboard's version-watching effect with no extra
  // glue in the UploadData component.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useAppStore.getState().observeServerDataVersion((result as any)?.data_version)
  return result
}

export async function uploadWorkbook(file: File): Promise<UploadResponse> {
  const result = await uploadFile<UploadResponse>('/upload_workbook', file, {})
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useAppStore.getState().observeServerDataVersion((result as any)?.data_version)
  return result
}

export async function fetchUploadsList(): Promise<UploadsListResponse> {
  return apiGet<UploadsListResponse>('/uploads')
}

export async function disconnectUpload(batchId: string): Promise<DisconnectResponse> {
  const result = await apiPost<DisconnectResponse>(`/uploads/${encodeURIComponent(batchId)}/disconnect`)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useAppStore.getState().observeServerDataVersion((result as any)?.data_version)
  return result
}

export async function fetchDashboard(month?: string): Promise<DashboardData> {
  const q = month ? `?month=${encodeURIComponent(month)}` : ''
  const result = await apiGet<DashboardData>(`/dashboard${q}`)
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  useAppStore.getState().observeServerDataVersion((result as any)?.data_version)
  return result
}

/**
 * Helper invoked after any client-initiated change that mutates server
 * data (upload, unarchive, ...) so the Dashboard + Uploads list reload
 * without each call site having to remember both refetches. Runs both
 * fetches in parallel and swallows individual failures so a slow
 * /uploads doesn't block the dashboard reload (or vice-versa).
 */
export async function refreshAfterChange(month?: string): Promise<void> {
  await Promise.allSettled([
    fetchDashboard(month),
    fetchUploadsList(),
  ])
}

// Google upload/Drive helpers — only used by the Upload page's "Connect
// Google Drive" card. These are independent of any app-startup login.
export const googleLoginUrl = (): string => `${BASE_URL}/auth/google/login`

export async function fetchAuthMe(): Promise<AuthMe> {
  return apiGet<AuthMe>('/auth/me')
}

export async function logout(): Promise<{ ok: boolean }> {
  return apiPost<{ ok: boolean }>('/auth/logout')
}

export async function fetchDriveStatus(): Promise<DriveStatus> {
  return apiGet<DriveStatus>('/drive/status')
}

export async function syncDrive(
  fileIds: string[],
  target: 'sales' | 'purchase' = 'sales',
  dedupMode = 'skip',
): Promise<DriveSyncResult> {
  return apiPost<DriveSyncResult>('/drive/sync', {
    file_ids: fileIds,
    target,
    dedup_mode: dedupMode,
  })
}

// ---------------------------------------------------------------------------
// Chat session history (read-only "Recents"). All three reuse the same
// BASE_URL + auth-header + error-handling primitives as apiGet/apiPost, so
// the backend origin is never hardcoded here.
// ---------------------------------------------------------------------------

export async function listConversations(): Promise<SessionMeta[]> {
  const { conversations } = await apiGet<{ conversations: SessionMeta[] }>('/conversations')
  return conversations ?? []
}

export async function getConversation(
  id: string,
): Promise<{ id: string; title: string; messages: ChatMessage[] }> {
  const data = await apiGet<{
    id: string
    title: string
    messages: ServerConversationMessage[]
  }>(`/conversations/${encodeURIComponent(id)}`)
  const messages: ChatMessage[] = (data.messages ?? []).map((m) => ({
    id: m.id,
    role: m.role,
    content: m.content,
    chart: m.chart ?? null,
    status: 'done' as ChatStatus,
    toolEvents: [],
    timestamp: m.created_at,
  }))
  return { id: data.id, title: data.title, messages }
}

export async function deleteConversation(id: string): Promise<void> {
  const res = await safeFetch(`${BASE_URL}/conversations/${encodeURIComponent(id)}`, withAuthHeader({
    method: 'DELETE',
    headers: buildHeaders(),
  }))
  await handle<{ deleted: boolean }>(res, `DELETE /conversations/${id}`)
}

export async function streamAIQuery(
  message: string,
  onEvent: (e: SseEvent) => void,
  opts?: { signal?: AbortSignal; maxRetries?: number },
): Promise<void> {
  return streamQueryWithRetry(message, onEvent, {
    signal:     opts?.signal,
    maxRetries: opts?.maxRetries ?? 2,
  })
}

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
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw err
      }
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
// 5. Global state store (Zustand + persist) — no auth
// ---------------------------------------------------------------------------

interface AppState {
  shop: ShopInfo
  dataset: DatasetMeta | null
  filters: DashboardFilters

  chatHistory: ChatMessage[]
  isStreaming: boolean
  conversationId: string

  // Read-only "Recents" history. Transient + server-sourced: never persisted
  // (not in the persist partialize) — re-fetched from the backend on mount and
  // after each turn. `viewingSessionId` is the id of the session currently being
  // viewed read-only, or null when the live chat is active. `viewedMessages`
  // holds the fetched read-only transcript so the LIVE `chatHistory` is left
  // completely untouched while a session is open.
  sessions: SessionMeta[]
  viewingSessionId: string | null
  viewedMessages: ChatMessage[]

  // Latest `data_version` observed from any server response. Used by the
  // Dashboard to auto-reload when the server's data has changed.
  serverDataVersion: number | null

  // Snapshot of `serverDataVersion` at the moment the current conversation
  // began. Used by the chat UI to show a "data updated since this
  // conversation started" banner when serverDataVersion has since moved
  // past it.
  conversationStartVersion: number | null

  // Phase 3 auth: token from /auth/login is sent as Authorization: Bearer
  // on every backend request. authEnabled is what the BACKEND told us via
  // /health — frontend uses it to decide whether to render the LoginGate.
  // When null we haven't probed yet; treat as enabled to be safe.
  auth: {
    token:       string | null
    username:    string | null
    authEnabled: boolean | null
  }

  setShop: (info: Partial<ShopInfo>) => void
  setDataset: (d: DatasetMeta | null) => void
  setFilters: (f: Partial<DashboardFilters>) => void
  clearShop: () => void

  appendMessage: (m: ChatMessage) => void
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void
  clearChat: () => void
  setStreaming: (b: boolean) => void
  rotateConversation: () => void

  // Recents actions. refreshSessions re-pulls the server list; openSession
  // fetches a transcript and shows it read-only; closeSession returns to the
  // live chat; removeSession deletes server-side then refreshes (and closes the
  // viewer if the deleted session was open).
  refreshSessions: () => Promise<void>
  openSession: (id: string) => Promise<void>
  closeSession: () => void
  removeSession: (id: string) => Promise<void>

  // Auth setters
  setAuthToken: (token: string | null, username?: string | null) => void
  setAuthEnabled: (enabled: boolean) => void

  // Monotonically updates serverDataVersion (ignores stale lower values).
  // First call within a conversation also stamps conversationStartVersion.
  observeServerDataVersion: (v: number | null | undefined) => void
}

const emptyShop: ShopInfo = {
  shopName: '',
  ownerName: '',
}

const defaultMonth = (): string => new Date().toISOString().slice(0, 7)

const MAX_PERSISTED_MESSAGES = 50

const newConversationId = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'c-' + Math.random().toString(16).slice(2) + Date.now().toString(16)
}

export const useAppStore = create<AppState>()(
  persist(
    (set, get) => ({
      shop: emptyShop,
      dataset: null,
      filters: { month: defaultMonth() },

      chatHistory: [],
      isStreaming: false,
      conversationId: newConversationId(),

      sessions: [],
      viewingSessionId: null,
      viewedMessages: [],

      serverDataVersion: null,
      conversationStartVersion: null,

      auth: { token: null, username: null, authEnabled: null },

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
          conversationId: newConversationId(),
          // Re-anchor the chat banner to "right now" so a stale conversation
          // doesn't carry a leftover "data updated" indicator into the new one.
          conversationStartVersion: null,
        }),
      setStreaming: (b) => set({ isStreaming: b }),
      rotateConversation: () =>
        set({
          conversationId: newConversationId(),
          conversationStartVersion: null,
        }),

      // --- Recents (read-only history) --------------------------------------
      // All defensive: a history-list/fetch/delete failure must never break the
      // live chat, so each swallows its error after logging.
      refreshSessions: async () => {
        try {
          const sessions = await listConversations()
          set({ sessions })
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error('[sessions] refresh failed', e)
        }
      },
      openSession: async (id) => {
        try {
          const convo = await getConversation(id)
          // Stash into viewedMessages — never the live chatHistory — so the
          // live conversation survives untouched behind the read-only view.
          set({ viewingSessionId: id, viewedMessages: convo.messages })
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error('[sessions] open failed', e)
        }
      },
      closeSession: () => set({ viewingSessionId: null, viewedMessages: [] }),
      removeSession: async (id) => {
        try {
          await deleteConversation(id)
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error('[sessions] delete failed', e)
        }
        if (get().viewingSessionId === id) set({ viewingSessionId: null, viewedMessages: [] })
        await get().refreshSessions()
      },

      setAuthToken: (token, username) =>
        set((s) => ({ auth: { ...s.auth, token, username: username ?? s.auth.username } })),
      setAuthEnabled: (enabled) =>
        set((s) => ({ auth: { ...s.auth, authEnabled: enabled } })),

      observeServerDataVersion: (v) => {
        if (v === null || v === undefined) return
        const n = typeof v === 'number' ? v : Number(v)
        if (!Number.isFinite(n)) return
        set((s) => {
          // Only move forward — late-arriving responses from before a bump
          // must not roll the known version backwards.
          const next = s.serverDataVersion === null || n > s.serverDataVersion
            ? n
            : s.serverDataVersion
          // First server response in a conversation anchors the chat banner.
          const start = s.conversationStartVersion === null ? next : s.conversationStartVersion
          return { serverDataVersion: next, conversationStartVersion: start }
        })
      },
    }),
    {
      name: 'agentic-ai:v1',
      partialize: (s) => ({
        shop: s.shop,
        filters: s.filters,
        dataset: s.dataset,
        chatHistory: s.chatHistory.slice(-MAX_PERSISTED_MESSAGES),
        conversationId: s.conversationId,
        // Persist auth so a page refresh doesn't kick the user out.
        // authEnabled stays in memory (re-probed via /health on boot).
        auth: { token: s.auth.token, username: s.auth.username, authEnabled: null },
      }),
    },
  ),
)
