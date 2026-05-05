import { useState } from 'react'
import { Building2, Eye, EyeOff, Save, Trash2, ShieldCheck, AlertTriangle } from 'lucide-react'
import { PageHeader } from '@/components/ui/PageHeader'
import { useAppStore } from '@/store/useAppStore'
import type { ShopInfo as ShopInfoType } from '@/types'

export function ShopInfo() {
  const shop = useAppStore((s) => s.shop)
  const setShop = useAppStore((s) => s.setShop)
  const clearShop = useAppStore((s) => s.clearShop)

  const [form, setForm] = useState<ShopInfoType>(shop)
  const [showKey, setShowKey] = useState(false)
  const [savedAt, setSavedAt] = useState<string | null>(null)

  const update = <K extends keyof ShopInfoType>(k: K, v: ShopInfoType[K]) =>
    setForm((f) => ({ ...f, [k]: v }))

  const onSave = (e: React.FormEvent) => {
    e.preventDefault()
    setShop(form)
    setSavedAt(new Date().toLocaleTimeString())
  }

  const onClear = () => {
    if (!confirm('Clear all shop info, including the API key?')) return
    clearShop()
    setForm({ shopName: '', businessName: '', ownerName: '', groqApiKey: '' })
    setSavedAt(null)
  }

  const dirty = JSON.stringify(form) !== JSON.stringify(shop)

  return (
    <div className="animate-fade-in">
      <PageHeader
        icon={<Building2 className="w-5 h-5 text-emerald-400" />}
        title="Shop Info"
        subtitle="Identity and credentials. The Groq API key is used for every analytics request."
      />

      <form onSubmit={onSave} className="mt-8 max-w-2xl space-y-5">
        <Field
          label="Shop Name"
          value={form.shopName}
          onChange={(v) => update('shopName', v)}
          placeholder="e.g. Swiggy Mumbai HQ"
        />
        <Field
          label="Business Name"
          value={form.businessName}
          onChange={(v) => update('businessName', v)}
          placeholder="e.g. Bundl Technologies Pvt Ltd"
        />
        <Field
          label="Owner Name"
          value={form.ownerName}
          onChange={(v) => update('ownerName', v)}
          placeholder="Full legal name"
        />

        <div>
          <label className="label">Groq API Key</label>
          <div className="relative">
            <input
              type={showKey ? 'text' : 'password'}
              value={form.groqApiKey}
              onChange={(e) => update('groqApiKey', e.target.value)}
              placeholder="gsk_..."
              autoComplete="off"
              spellCheck={false}
              data-1p-ignore
              data-lpignore="true"
              className="input pr-10 font-mono text-sm"
            />
            <button
              type="button"
              onClick={() => setShowKey((s) => !s)}
              className="absolute right-1 top-1/2 -translate-y-1/2 p-2 text-zinc-500 hover:text-zinc-200 rounded-md"
              aria-label={showKey ? 'Hide API key' : 'Show API key'}
            >
              {showKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
          <KeyNotice hasKey={Boolean(form.groqApiKey)} />
        </div>

        <div className="flex items-center gap-3 pt-3">
          <button type="submit" disabled={!dirty} className="btn btn-primary">
            <Save className="w-4 h-4" />
            Save
          </button>
          <button type="button" onClick={onClear} className="btn btn-secondary">
            <Trash2 className="w-4 h-4" />
            Clear
          </button>
          {savedAt && <span className="text-xs text-zinc-500">Saved at {savedAt}</span>}
        </div>
      </form>
    </div>
  )
}

interface FieldProps {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
}

function Field({ label, value, onChange, placeholder }: FieldProps) {
  return (
    <div>
      <label className="label">{label}</label>
      <input
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete="off"
      />
    </div>
  )
}

function KeyNotice({ hasKey }: { hasKey: boolean }) {
  if (hasKey) {
    return (
      <p className="mt-2 flex items-start gap-2 text-xs text-zinc-500">
        <ShieldCheck className="w-3.5 h-3.5 text-emerald-400 shrink-0 mt-0.5" />
        <span>
          Stored locally in your browser. Sent as <code className="text-zinc-400 font-mono">X-Groq-Api-Key</code> on every API call. Never bundled into source.
        </span>
      </p>
    )
  }
  return (
    <p className="mt-2 flex items-start gap-2 text-xs text-amber-400/80">
      <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
      <span>API key required to run analytics queries. Get one at console.groq.com.</span>
    </p>
  )
}
