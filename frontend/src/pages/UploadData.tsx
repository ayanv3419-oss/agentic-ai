import { useEffect, useRef, useState } from 'react'
import { Upload, FileSpreadsheet, Cloud, X, CheckCircle2, RefreshCw } from 'lucide-react'
import { PageHeader } from '@/components/ui/PageHeader'
import { UploadsTable } from '@/components/UploadsTable'
import { useAppStore } from '@/store/useAppStore'
import {
  ApiError,
  fetchAuthMe,
  googleLoginUrl,
  logout,
  syncDrive,
  uploadSales,
} from '@/lib/api'
import type { AuthMe } from '@/types'

const ACCEPT =
  '.csv,.xls,.xlsx,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const MAX_BYTES = 1024 * 1024 * 1024 // 1 GB

export function UploadData() {
  const dataset = useAppStore((s) => s.dataset)
  const setDataset = useAppStore((s) => s.setDataset)
  const apiKeySet = useAppStore((s) => Boolean(s.shop.groqApiKey))

  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const [auth, setAuth] = useState<AuthMe | null>(null)
  const [driveSyncing, setDriveSyncing] = useState(false)
  const [uploadsRefreshKey, setUploadsRefreshKey] = useState(0)
  const refreshUploadsList = () => setUploadsRefreshKey((k) => k + 1)
  const [driveNote, setDriveNote] = useState<string | null>(null)
  const [driveError, setDriveError] = useState<string | null>(null)
  const autoSyncRanRef = useRef(false)

  // Load auth status on mount.
  useEffect(() => {
    void (async () => {
      try {
        setAuth(await fetchAuthMe())
      } catch {
        setAuth({ authenticated: false })
      }
    })()
  }, [])

  // Pick up the OAuth-callback signal and auto-sync once.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const status = params.get('drive')
    const err = params.get('drive_error')
    if (err) setDriveError(`Google sign-in failed: ${err}`)
    if (status === 'connected' && !autoSyncRanRef.current) {
      autoSyncRanRef.current = true
      void doSync()
    }
    if (err || status) {
      params.delete('drive')
      params.delete('drive_error')
      params.delete('detail')
      const clean = window.location.pathname + (params.toString() ? `?${params}` : '')
      window.history.replaceState({}, '', clean)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const doSync = async () => {
    setDriveSyncing(true)
    setDriveError(null)
    setDriveNote(null)
    try {
      const result = await syncDrive()
      setDriveNote(
        `Imported ${result.imported} file${result.imported === 1 ? '' : 's'} (${result.rows_inserted.toLocaleString('en-IN')} rows).` +
          (result.skipped_already ? ` ${result.skipped_already} already in DB.` : '') +
          (result.failed ? ` ${result.failed} failed.` : ''),
      )
      // Surface the most-recent file via the existing dataset pill.
      const lastImported = result.details.find((d) => d.status === 'imported')
      if (lastImported && typeof lastImported.rows === 'number') {
        setDataset({
          name: lastImported.file,
          rows: lastImported.rows,
          uploadedAt: new Date().toISOString(),
          source: 'drive',
        })
      }
      setAuth(await fetchAuthMe())
      refreshUploadsList()
    } catch (e) {
      if (e instanceof ApiError) {
        const body = e.detail as { detail?: string; error?: string } | undefined
        setDriveError(body?.detail ?? body?.error ?? e.message)
      } else if (e instanceof Error) {
        setDriveError(e.message)
      } else {
        setDriveError('Drive sync failed.')
      }
    } finally {
      setDriveSyncing(false)
    }
  }

  const doLogout = async () => {
    if (!confirm('Sign out of Google? Stored Drive tokens will be removed.')) return
    try {
      await logout()
      setAuth({ authenticated: false })
      setDriveNote(null)
    } catch {
      /* noop */
    }
  }

  const onPick = (f: File) => {
    setError(null)
    if (!/\.(csv|xlsx?|xls)$/i.test(f.name)) {
      setError('Choose a CSV or Excel file (.csv, .xls, .xlsx).')
      return
    }
    if (f.size > MAX_BYTES) {
      setError('File exceeds 1 GB. Split it and upload sequentially.')
      return
    }
    setFile(f)
  }

  const onSubmit = async () => {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const resp = await uploadSales(file)
      setDataset({
        name: resp.filename,
        rows: resp.rows_inserted,
        uploadedAt: new Date().toISOString(),
        source: 'upload',
      })
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
      // New upload landed — refresh the uploads table.
      refreshUploadsList()
    } catch (e) {
      if (e instanceof ApiError) {
        const detail = (e.detail as { detail?: string } | undefined)?.detail
        setError(detail ?? e.message)
      } else if (e instanceof Error) {
        setError(e.message)
      } else {
        setError('Upload failed.')
      }
      // Even errors are recorded server-side — refresh so the user sees them.
      refreshUploadsList()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="animate-fade-in">
      <PageHeader
        icon={<Upload className="w-5 h-5 text-emerald-400" />}
        title="Upload Data"
        subtitle="Bring in your transactions. CSV/Excel from disk, or connect a Google Drive folder for continuous sync."
      />

      <div className="mt-8 grid md:grid-cols-2 gap-5">
        {/* Option A: file from device */}
        <section className="card p-6">
          <div className="flex items-center gap-2 mb-1">
            <FileSpreadsheet className="w-4 h-4 text-zinc-400" />
            <h2 className="font-medium text-sm">From device</h2>
          </div>
          <p className="text-xs text-zinc-500 mb-5">CSV or Excel (.xls / .xlsx). Up to 1 GB per upload — split larger datasets and upload sequentially.</p>

          <label
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              const f = e.dataTransfer.files[0]
              if (f) onPick(f)
            }}
            className="block border border-dashed border-zinc-700 rounded-lg p-8 text-center cursor-pointer hover:border-emerald-500/40 hover:bg-zinc-900/40 transition-colors"
          >
            <input
              ref={inputRef}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={(e) => e.target.files?.[0] && onPick(e.target.files[0])}
            />
            <Upload className="w-6 h-6 text-zinc-500 mx-auto mb-2" />
            <div className="text-sm text-zinc-300">Drop a file here or click to browse</div>
            <div className="text-xs text-zinc-500 mt-1">CSV, XLS, XLSX</div>
          </label>

          {file && (
            <div className="mt-4 flex items-center justify-between bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2">
              <div className="flex items-center gap-2 truncate">
                <FileSpreadsheet className="w-4 h-4 text-emerald-400 shrink-0" />
                <span className="text-sm truncate">{file.name}</span>
                <span className="text-xs text-zinc-500 shrink-0">{(file.size / 1024).toFixed(1)} KB</span>
              </div>
              <button
                onClick={() => setFile(null)}
                className="p-1 text-zinc-500 hover:text-zinc-200 rounded-md"
                aria-label="Remove selected file"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          )}

          {error && <p className="mt-3 text-xs text-red-400">{error}</p>}

          <button
            onClick={onSubmit}
            disabled={!file || busy}
            className="btn btn-primary mt-5 w-full"
          >
            {busy ? 'Uploading…' : 'Upload'}
          </button>
        </section>

        {/* Option B: Google Drive */}
        <section className="card p-6">
          <div className="flex items-center gap-2 mb-1">
            <Cloud className="w-4 h-4 text-zinc-400" />
            <h2 className="font-medium text-sm">Connect Google Drive</h2>
          </div>
          <p className="text-xs text-zinc-500 mb-5">
            Pick a folder; new files in it will be synced automatically.
          </p>

          {!auth?.authenticated ? (
            <a
              href={googleLoginUrl()}
              className="w-full flex items-center justify-center gap-3 bg-white text-zinc-900 hover:bg-zinc-100 font-medium rounded-lg px-4 py-2.5 text-sm transition-colors"
            >
              <GoogleGlyph className="w-4 h-4" />
              <span>Continue with Google</span>
            </a>
          ) : (
            <button
              type="button"
              onClick={doSync}
              disabled={driveSyncing}
              className="btn btn-primary w-full"
            >
              <RefreshCw className={driveSyncing ? 'w-4 h-4 animate-spin' : 'w-4 h-4'} />
              {driveSyncing ? 'Syncing…' : 'Sync Drive now'}
            </button>
          )}

          {driveNote && (
            <p className="text-[11px] text-zinc-500 mt-4 leading-relaxed">{driveNote}</p>
          )}
          {driveError && (
            <p className="text-[11px] text-red-400 mt-4 leading-relaxed">{driveError}</p>
          )}
          {!driveNote && !driveError && (
            <p className="text-[11px] text-zinc-500 mt-4 leading-relaxed">
              {auth?.authenticated ? (
                <>
                  Signed in as {auth.email}.{' '}
                  <button
                    type="button"
                    onClick={doLogout}
                    className="text-zinc-400 hover:text-zinc-200 underline underline-offset-2"
                  >
                    Sign out
                  </button>
                </>
              ) : (
                'Read-only access to drive.readonly. Already-imported files are skipped.'
              )}
            </p>
          )}
        </section>
      </div>

      {!apiKeySet && (
        <div className="mt-6 text-xs text-amber-400/80 flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
          Set your Groq API key in Shop Info before running analytics on this dataset.
        </div>
      )}

      {dataset && (
        <section className="mt-8 card p-5 flex items-center gap-4 animate-slide-up">
          <CheckCircle2 className="w-5 h-5 text-emerald-400 shrink-0" />
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium">Latest upload</div>
            <div className="text-xs text-zinc-400 truncate">
              {dataset.name} · {dataset.rows.toLocaleString()} rows · uploaded{' '}
              {new Date(dataset.uploadedAt).toLocaleString()}
              <span className="text-zinc-600"> · source: {dataset.source}</span>
            </div>
          </div>
          <button onClick={() => setDataset(null)} className="btn btn-secondary">
            Hide pill
          </button>
        </section>
      )}

      <div className="mt-8">
        <UploadsTable
          refreshKey={uploadsRefreshKey}
          onDisconnected={() => {
            // The table reloads itself; clear the local pill if it pointed to
            // a now-removed file.
            setDataset(null)
          }}
        />
      </div>
    </div>
  )
}

function GoogleGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden>
      <path
        fill="#4285F4"
        d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.75h3.57c2.08-1.92 3.28-4.74 3.28-8.07z"
      />
      <path
        fill="#34A853"
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.75c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
      />
      <path
        fill="#FBBC05"
        d="M5.84 14.12c-.22-.66-.35-1.36-.35-2.12s.13-1.46.35-2.12V7.04H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.96l3.66-2.84z"
      />
      <path
        fill="#EA4335"
        d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.04l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z"
      />
    </svg>
  )
}
