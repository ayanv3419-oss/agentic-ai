import { useEffect, useState } from 'react'
import {
  RefreshCw,
  XCircle,
  CheckCircle2,
  AlertTriangle,
  Trash2,
  Loader2,
  FileSpreadsheet,
} from 'lucide-react'
import { ApiError, disconnectUpload, fetchUploadsList } from '@/lib/api'
import type { UploadEntry, UploadStatus } from '@/types'
import { cn } from '@/lib/cn'

interface UploadsTableProps {
  /** Increment this to force a refetch (e.g. after a new upload). */
  refreshKey?: number
  /** Called after a row is successfully disconnected (so the parent can refresh
   *  any sibling state — the table already refreshes itself). */
  onDisconnected?: (batchId: string) => void
}

const STATUS_STYLES: Record<UploadStatus, { label: string; cls: string; Icon: typeof CheckCircle2 }> = {
  active:  { label: 'Active',  cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30', Icon: CheckCircle2 },
  error:   { label: 'Error',   cls: 'bg-red-500/15 text-red-300 border-red-500/30',         Icon: AlertTriangle },
  removed: { label: 'Removed', cls: 'bg-zinc-700/40 text-zinc-400 border-zinc-700',          Icon: Trash2 },
}


export function UploadsTable({ refreshKey = 0, onDisconnected }: UploadsTableProps) {
  const [uploads, setUploads] = useState<UploadEntry[]>([])
  const [totals, setTotals] = useState<{ sales: number; purchase: number } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchUploadsList()
      setUploads(data.uploads ?? [])
      setTotals(data.total_rows ?? null)
    } catch (e) {
      if (e instanceof ApiError) {
        setError(`${e.message}`)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Failed to load uploads.')
      }
      setUploads([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const onDisconnect = async (entry: UploadEntry) => {
    if (entry.status !== 'active') return
    const ok = window.confirm(
      `Disconnect "${entry.filename}"? Its ${entry.rows_inserted.toLocaleString()} rows ` +
        `will be removed from queries and the dashboard.`,
    )
    if (!ok) return
    setPendingId(entry.batch_id)
    try {
      await disconnectUpload(entry.batch_id)
      onDisconnected?.(entry.batch_id)
      await load()
    } catch (e) {
      if (e instanceof ApiError) {
        setError(e.message)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Disconnect failed.')
      }
    } finally {
      setPendingId(null)
    }
  }

  return (
    <section className="card p-6">
      <header className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <FileSpreadsheet className="w-4 h-4 text-zinc-400" />
          <h2 className="font-medium text-sm">Uploaded datasets</h2>
          {totals && (
            <span className="text-[11px] text-zinc-500 ml-2">
              · {totals.sales.toLocaleString()} sales rows · {totals.purchase.toLocaleString()} purchase rows
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="btn btn-secondary"
          title="Refresh list"
        >
          <RefreshCw className={cn('w-3.5 h-3.5', loading && 'animate-spin')} />
          <span className="hidden sm:inline">Refresh</span>
        </button>
      </header>

      {error && (
        <div className="mb-3 text-xs text-red-300 bg-red-950/30 border border-red-900/40 rounded-md px-3 py-2">
          {error}
        </div>
      )}

      {!loading && uploads.length === 0 ? (
        <div className="text-xs text-zinc-500 py-6 text-center">
          No uploads yet. Once you upload a file it'll appear here.
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-zinc-500 border-b border-zinc-800">
                <th className="text-left font-medium py-2 pr-3">File</th>
                <th className="text-left font-medium py-2 pr-3">Target</th>
                <th className="text-left font-medium py-2 pr-3">Status</th>
                <th className="text-left font-medium py-2 pr-3">Rows</th>
                <th className="text-left font-medium py-2 pr-3">Date range</th>
                <th className="text-left font-medium py-2 pr-3">Uploaded</th>
                <th className="text-right font-medium py-2 pl-3">Action</th>
              </tr>
            </thead>
            <tbody>
              {uploads.map((u) => {
                const style = STATUS_STYLES[u.status] ?? STATUS_STYLES.error
                const StatusIcon = style.Icon
                const range =
                  u.min_date && u.max_date
                    ? `${u.min_date} → ${u.max_date}`
                    : '—'
                return (
                  <tr
                    key={u.batch_id}
                    className={cn(
                      'border-b border-zinc-900 last:border-0',
                      u.status === 'removed' && 'opacity-60',
                    )}
                  >
                    <td className="py-2.5 pr-3 max-w-[18rem]">
                      <div className="truncate text-zinc-100" title={u.filename}>
                        {u.filename}
                      </div>
                      {u.error_message && (
                        <div
                          className="text-[11px] text-red-400/80 truncate mt-0.5"
                          title={u.error_message}
                        >
                          {u.error_message}
                        </div>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-zinc-400 capitalize">{u.target}</td>
                    <td className="py-2.5 pr-3">
                      <span
                        className={cn(
                          'inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-[11px]',
                          style.cls,
                        )}
                      >
                        <StatusIcon className="w-3 h-3" />
                        {style.label}
                      </span>
                    </td>
                    <td className="py-2.5 pr-3 text-zinc-300 tabular-nums">
                      {u.rows_inserted.toLocaleString()}
                      {u.rows_failed > 0 && (
                        <span className="text-[11px] text-amber-400/80 ml-1">
                          ({u.rows_failed} failed)
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-zinc-400 tabular-nums">{range}</td>
                    <td className="py-2.5 pr-3 text-zinc-400">
                      {new Date(u.uploaded_at + 'Z').toLocaleString()}
                    </td>
                    <td className="py-2.5 pl-3 text-right">
                      {u.status === 'active' ? (
                        <button
                          type="button"
                          onClick={() => void onDisconnect(u)}
                          disabled={pendingId === u.batch_id}
                          className="btn btn-secondary"
                          title="Disconnect this dataset"
                        >
                          {pendingId === u.batch_id ? (
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                          ) : (
                            <XCircle className="w-3.5 h-3.5" />
                          )}
                          <span className="hidden sm:inline">Disconnect</span>
                        </button>
                      ) : (
                        <span className="text-[11px] text-zinc-600">—</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
