/**
 * API client. Reads the Groq API key from the Zustand store at call time and
 * forwards it as `X-Groq-Api-Key`.
 *
 * Backend URL resolution (in order of preference):
 *   1. import.meta.env.VITE_BACKEND_URL    — preferred, full https URL of the
 *      Render backend service (e.g. https://agentic-ai-anet.onrender.com).
 *   2. import.meta.env.VITE_API_BASE_URL   — legacy alias kept for backward
 *      compatibility with previously-configured deployments.
 *   3. PRODUCTION_FALLBACK                 — hardcoded production backend so
 *      the build never produces requests against the frontend's own origin
 *      (which would 404 because the backend isn't there).
 */
import { useAppStore } from '@/store/useAppStore'
import type {
  AuthMe,
  DashboardData,
  DisconnectResponse,
  DriveSyncResult,
  LoginResponse,
  SseEvent,
  UploadResponse,
  UploadsListResponse,
} from '@/types'

/** Hardcoded production backend — last-resort fallback when no env var is set.
 *  This MUST point to the real deployed Render service. Override per-environment
 *  with VITE_BACKEND_URL if your service is named differently. */
const PRODUCTION_FALLBACK = 'https://agentic-ai-anet.onrender.com'

function resolveBackendUrl(): string {
  const candidate =
    (import.meta.env.VITE_BACKEND_URL as string | undefined) ??
    (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
    PRODUCTION_FALLBACK
  // Strip trailing slashes so `${BASE_URL}/upload` doesn't produce `//upload`.
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
    // 401 from any non-login endpoint => session expired or invalidated.
    // Drop the local auth state so the app falls back to the Login page.
    if (res.status === 401 && !label.includes('/auth/login')) {
      // eslint-disable-next-line no-console
      console.warn(`[api] ${label} → 401, clearing auth`)
      useAppStore.getState().clearAuth()
    }
    // Surface the structured backend error to DevTools so "Failed to fetch"
    // never hides what the server actually said.
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

export async function uploadFile<T>(path: string, file: File, extra?: Record<string, string>): Promise<T> {
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

export async function uploadSales(file: File, target: 'sales' | 'purchase' = 'sales'): Promise<UploadResponse> {
  return uploadFile<UploadResponse>('/upload', file, { target })
}

export async function fetchUploadsList(): Promise<UploadsListResponse> {
  return apiGet<UploadsListResponse>('/uploads')
}

export async function disconnectUpload(batchId: string): Promise<DisconnectResponse> {
  return apiPost<DisconnectResponse>(`/uploads/${encodeURIComponent(batchId)}/disconnect`)
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
    // 401 → drop the session so the app falls back to Login.
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

export async function fetchDashboard(month?: string): Promise<DashboardData> {
  const q = month ? `?month=${encodeURIComponent(month)}` : ''
  return apiGet<DashboardData>(`/dashboard${q}`)
}

// --- Auth + Drive --------------------------------------------------------

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
  // Stateless tokens — the server endpoint is a no-op, but call it anyway
  // so the frontend has one consistent place to invalidate the session.
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
