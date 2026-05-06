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
  businessName: string
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
  month: string // YYYY-MM
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
  // Optional fields the backend now returns
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

export interface ChartTotals {
  total_sales: number
  orders: number
  customers: number
}

export interface SalesChart {
  granularity: ChartGranularity
  totals: ChartTotals
  series: Series[]
}

export interface SseEvent {
  event: string
  data: unknown
}
