import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Send,
  Square,
  Sparkles,
  User2,
  AlertTriangle,
  Trash2,
  Loader2,
} from 'lucide-react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { useAppStore } from '@/store/useAppStore'
import { streamAIQuery, ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import type { ChatMessage, SalesChart, SseEvent, ToolEvent } from '@/types'

const TOOL_LABELS: Record<string, string> = {
  RouteClassifier: 'Understanding your question',
  TimeKPI: 'Resolving time window and metric',
  SchemaRetriever: 'Loading data schema',
  SqlWriter: 'Writing the query',
  SqlExecutor: 'Running the query',
  ResponseFormatter: 'Formatting the response',
  ResponseStored: 'Saving the response',
  Database: 'Storing data',
}

const SUGGESTIONS = [
  'What are my last 2 days sales?',
  'Show this month revenue',
  'Why did sales drop?',
  'Top performing products this week',
]

const newId = (): string =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`

export function AiAssistant() {
  const messages = useAppStore((s) => s.chatHistory)
  const isStreaming = useAppStore((s) => s.isStreaming)
  const appendMessage = useAppStore((s) => s.appendMessage)
  const updateMessage = useAppStore((s) => s.updateMessage)
  const clearChat = useAppStore((s) => s.clearChat)
  const setStreaming = useAppStore((s) => s.setStreaming)
  const apiKeySet = useAppStore((s) => Boolean(s.shop.groqApiKey))

  const [input, setInput] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // autoscroll on new content
  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages, isStreaming])

  // autosize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`
  }, [input])

  const canSend = useMemo(
    () => input.trim().length > 0 && !isStreaming && apiKeySet,
    [input, isStreaming, apiKeySet],
  )

  const stop = () => {
    abortRef.current?.abort()
    abortRef.current = null
  }

  const handleSend = async () => {
    const text = input.trim()
    if (!text || isStreaming) return
    if (!apiKeySet) return

    const userMsg: ChatMessage = {
      id: newId(),
      role: 'user',
      content: text,
      status: 'done',
      toolEvents: [],
      timestamp: new Date().toISOString(),
    }
    const assistantMsg: ChatMessage = {
      id: newId(),
      role: 'assistant',
      content: '',
      status: 'thinking',
      toolEvents: [],
      timestamp: new Date().toISOString(),
    }

    appendMessage(userMsg)
    appendMessage(assistantMsg)
    setInput('')
    setStreaming(true)

    const ctrl = new AbortController()
    abortRef.current = ctrl

    let toolEvents: ToolEvent[] = []

    try {
      await streamAIQuery(
        text,
        (e: SseEvent) => {
          const data = e.data as Record<string, unknown> | undefined
          switch (e.event) {
            case 'tool.call': {
              const tool = String(data?.name ?? '')
              if (!tool) return
              toolEvents = [...toolEvents, { tool, status: 'running' }]
              updateMessage(assistantMsg.id, {
                status: 'streaming',
                toolEvents,
              })
              break
            }
            case 'tool.result': {
              const tool = String(data?.name ?? '')
              const ok = Boolean(data?.ok)
              const duration = Number(data?.duration_ms ?? 0)
              const error = data?.error ? String(data.error) : undefined
              toolEvents = toolEvents.map((te) =>
                te.tool === tool && te.status === 'running'
                  ? { ...te, status: ok ? 'done' : 'failed', durationMs: duration, error }
                  : te,
              )
              updateMessage(assistantMsg.id, { toolEvents })
              break
            }
            case 'final': {
              const answer = String(data?.answer ?? '')
              const chart = (data?.chart ?? null) as SalesChart | null
              updateMessage(assistantMsg.id, {
                content: answer,
                chart,
                status: 'done',
                toolEvents,
              })
              break
            }
            case 'agent.result': {
              const err = data?.error
              if (err) {
                updateMessage(assistantMsg.id, {
                  status: 'error',
                  error: String(err),
                  toolEvents,
                })
              }
              break
            }
          }
        },
        { signal: ctrl.signal },
      )

      // If the server closed without a `final` event, fall back to a generic message.
      const cur = useAppStore.getState().chatHistory.find((m) => m.id === assistantMsg.id)
      if (cur && cur.status !== 'done' && cur.status !== 'error') {
        updateMessage(assistantMsg.id, {
          status: 'done',
          content: cur.content || 'No answer was produced.',
        })
      }
    } catch (e) {
      const isAbort = e instanceof DOMException && e.name === 'AbortError'
      if (isAbort) {
        updateMessage(assistantMsg.id, {
          status: 'done',
          content: 'Stopped.',
          toolEvents,
        })
      } else {
        const message =
          e instanceof ApiError
            ? `${e.message}${e.detail ? ` — ${JSON.stringify(e.detail)}` : ''}`
            : e instanceof Error
              ? e.message
              : 'Unknown error'
        updateMessage(assistantMsg.id, {
          status: 'error',
          error: message,
          toolEvents,
        })
      }
    } finally {
      abortRef.current = null
      setStreaming(false)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void handleSend()
    }
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <ChatHeader onClear={clearChat} hasMessages={messages.length > 0} />

      <div ref={scrollerRef} className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 md:px-10 py-8">
          {messages.length === 0 ? (
            <EmptyState onPick={(s) => setInput(s)} apiKeySet={apiKeySet} />
          ) : (
            <div className="space-y-6">
              {messages.map((m) => (
                <MessageRow key={m.id} message={m} />
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="border-t border-zinc-800 bg-zinc-950">
        <div className="max-w-3xl mx-auto px-6 md:px-10 py-4">
          {!apiKeySet && (
            <div className="mb-3 flex items-center gap-2 text-xs text-amber-400/80">
              <AlertTriangle className="w-3.5 h-3.5" />
              Set your Groq API key in Shop Info to enable the assistant.
            </div>
          )}
          <div className="relative flex items-end gap-2 rounded-2xl border border-zinc-800 bg-zinc-900/60 focus-within:border-emerald-500/50 transition-colors p-2">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              rows={1}
              placeholder={
                apiKeySet
                  ? 'Ask anything about your business…'
                  : 'API key required — open Shop Info to add yours'
              }
              disabled={!apiKeySet}
              className="flex-1 resize-none bg-transparent px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none disabled:opacity-60"
            />
            {isStreaming ? (
              <button
                type="button"
                onClick={stop}
                className="btn btn-secondary h-9 px-3"
                title="Stop"
              >
                <Square className="w-3.5 h-3.5" />
                <span className="hidden sm:inline">Stop</span>
              </button>
            ) : (
              <button
                type="button"
                onClick={() => void handleSend()}
                disabled={!canSend}
                className="btn btn-primary h-9 px-3"
                title="Send"
              >
                <Send className="w-3.5 h-3.5" />
                <span className="hidden sm:inline">Send</span>
              </button>
            )}
          </div>
          <p className="mt-2 text-[11px] text-zinc-600 text-center">
            Press Enter to send · Shift + Enter for newline
          </p>
        </div>
      </div>
    </div>
  )
}

function ChatHeader({ onClear, hasMessages }: { onClear: () => void; hasMessages: boolean }) {
  return (
    <div className="border-b border-zinc-800 px-6 md:px-10 py-4 flex items-center justify-between">
      <div className="flex items-center gap-3">
        <div className="w-9 h-9 rounded-lg bg-zinc-900 border border-zinc-800 flex items-center justify-center">
          <Sparkles className="w-4 h-4 text-emerald-400" />
        </div>
        <div>
          <h1 className="text-base font-semibold tracking-tight">AI Assistant</h1>
          <p className="text-xs text-zinc-500">Ask about sales, revenue, or trends in plain English.</p>
        </div>
      </div>
      <button
        type="button"
        onClick={() => {
          if (!hasMessages) return
          if (!confirm('Clear chat history?')) return
          onClear()
        }}
        disabled={!hasMessages}
        className="btn btn-ghost"
        title="Clear chat"
      >
        <Trash2 className="w-3.5 h-3.5" />
        <span className="hidden sm:inline">Clear</span>
      </button>
    </div>
  )
}

function EmptyState({
  onPick,
  apiKeySet,
}: {
  onPick: (s: string) => void
  apiKeySet: boolean
}) {
  return (
    <div className="py-16 animate-fade-in">
      <div className="text-center">
        <div className="inline-flex w-12 h-12 rounded-xl bg-emerald-500/10 border border-emerald-500/30 items-center justify-center mb-4">
          <Sparkles className="w-5 h-5 text-emerald-400" />
        </div>
        <h2 className="text-2xl font-semibold tracking-tight">How can I help today?</h2>
        <p className="mt-2 text-sm text-zinc-500">
          Ask in plain English. I'll pull the data, run the analysis, and explain the result.
        </p>
      </div>

      <div className="mt-10 grid sm:grid-cols-2 gap-3">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onPick(s)}
            disabled={!apiKeySet}
            className="text-left card px-4 py-3 hover:border-zinc-700 hover:bg-zinc-900/70 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <span className="text-sm text-zinc-200">{s}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function MessageRow({ message }: { message: ChatMessage }) {
  if (message.role === 'user') {
    return (
      <div className="flex items-start gap-3 justify-end animate-slide-up">
        <div className="max-w-[85%] rounded-2xl rounded-tr-md bg-emerald-500/15 border border-emerald-500/30 px-4 py-2.5">
          <p className="whitespace-pre-wrap text-sm text-zinc-50 leading-relaxed">
            {message.content}
          </p>
        </div>
        <div className="w-8 h-8 rounded-full bg-zinc-900 border border-zinc-800 flex items-center justify-center shrink-0">
          <User2 className="w-3.5 h-3.5 text-zinc-400" />
        </div>
      </div>
    )
  }

  const showThinking = message.status === 'thinking' || message.status === 'streaming'
  const isError = message.status === 'error'

  return (
    <div className="flex items-start gap-3 animate-slide-up">
      <div className="w-8 h-8 rounded-full bg-emerald-500/10 border border-emerald-500/30 flex items-center justify-center shrink-0">
        <Sparkles className="w-3.5 h-3.5 text-emerald-400" />
      </div>
      <div className="min-w-0 flex-1 max-w-[85%]">
        {showThinking && <ThinkingBlock toolEvents={message.toolEvents} />}

        {message.chart && <ChatChart chart={message.chart} />}

        {message.content && (
          <div
            className={cn(
              'rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3',
              message.chart && 'mt-2',
            )}
          >
            <p className="whitespace-pre-wrap text-sm text-zinc-100 leading-relaxed">
              {message.content}
            </p>
          </div>
        )}

        {isError && message.error && (
          <div className="rounded-2xl rounded-tl-md bg-red-950/40 border border-red-900/50 px-4 py-3 mt-1">
            <div className="flex items-start gap-2 text-sm text-red-300">
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
              <div>
                <div className="font-medium">Something went wrong</div>
                <div className="text-xs text-red-400/80 mt-0.5 break-words">{message.error}</div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function formatINR(n: number): string {
  return `₹${(n ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
}

function formatCompactINR(n: number): string {
  if (n >= 1_00_00_000) return `₹${(n / 1_00_00_000).toFixed(1)}Cr`
  if (n >= 1_00_000) return `₹${(n / 1_00_000).toFixed(1)}L`
  if (n >= 1_000) return `₹${(n / 1_000).toFixed(0)}k`
  return `₹${n}`
}

function ChatChart({ chart }: { chart: SalesChart }) {
  const series = chart.series ?? []
  const totals = chart.totals ?? { total_sales: 0, orders: 0, customers: 0 }
  const hasSales = totals.total_sales > 0 || totals.orders > 0
  if (!hasSales && series.length === 0) return null

  return (
    <div className="rounded-2xl rounded-tl-md bg-zinc-900/60 border border-zinc-800 px-4 py-3">
      <div className="flex items-baseline gap-3 mb-2">
        <div className="text-2xl font-semibold tracking-tight text-zinc-50">
          {formatINR(totals.total_sales)}
        </div>
        {totals.orders > 0 && (
          <div className="text-xs text-zinc-500">
            {totals.orders.toLocaleString('en-IN')} orders
          </div>
        )}
        <div className="text-[11px] text-zinc-600 ml-auto capitalize">
          {chart.granularity ?? 'daily'}
        </div>
      </div>
      {series.length >= 2 ? (
        <div className="h-44 -ml-2">
          <ResponsiveContainer>
            <AreaChart data={series} margin={{ top: 4, right: 6, left: 0, bottom: 4 }}>
              <defs>
                <linearGradient id="chat-fill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#10b981" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#10b981" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="bucket"
                stroke="#71717a"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                tickMargin={6}
              />
              <YAxis
                stroke="#71717a"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                width={48}
                tickFormatter={(v: number) => formatCompactINR(v)}
              />
              <Tooltip
                contentStyle={{
                  background: '#0a0a0a',
                  border: '1px solid #27272a',
                  borderRadius: 8,
                  fontSize: 12,
                }}
                cursor={{ stroke: '#3f3f46', strokeDasharray: '3 3' }}
                formatter={(v: number) => [formatINR(v), 'Sales']}
              />
              <Area
                type="monotone"
                dataKey="sales"
                stroke="#10b981"
                strokeWidth={2}
                fill="url(#chat-fill)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      ) : (
        <div className="text-[11px] text-zinc-500">
          {series.length === 1
            ? `One bucket: ${series[0].bucket}`
            : 'Single-period summary.'}
        </div>
      )}
    </div>
  )
}

function ThinkingBlock({ toolEvents }: { toolEvents: ToolEvent[] }) {
  const current = toolEvents.find((t) => t.status === 'running')
  const completed = toolEvents.filter((t) => t.status !== 'running')
  const label = current ? TOOL_LABELS[current.tool] ?? current.tool : 'Thinking…'

  return (
    <div className="rounded-2xl rounded-tl-md bg-zinc-900/40 border border-zinc-800 px-4 py-3 mb-1">
      <div className="flex items-center gap-2 text-sm text-zinc-300">
        <Loader2 className="w-3.5 h-3.5 animate-spin text-emerald-400" />
        <span>{label}</span>
      </div>
      {completed.length > 0 && (
        <ul className="mt-2 space-y-1">
          {completed.map((t, i) => (
            <li
              key={`${t.tool}-${i}`}
              className={cn(
                'text-xs flex items-center gap-2',
                t.status === 'failed' ? 'text-red-400' : 'text-zinc-500',
              )}
            >
              <span
                className={cn(
                  'w-1 h-1 rounded-full',
                  t.status === 'failed' ? 'bg-red-400' : 'bg-emerald-400',
                )}
              />
              <span className="text-zinc-400">{TOOL_LABELS[t.tool] ?? t.tool}</span>
              {t.durationMs != null && (
                <span className="text-zinc-600">· {Math.round(t.durationMs)}ms</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
