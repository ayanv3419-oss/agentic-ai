/**
 * Logo — the Metric AI brand mark: a Greek sigma "Σ" (the sum of every metric).
 *
 * Drawn as an inline SVG so it inherits its size and colour from `className`
 * exactly like the lucide icons it replaced. Use it the same way, e.g.:
 *   <Logo className="w-3.5 h-3.5 text-emerald-400" />
 */
export function Logo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 48 48"
      fill="none"
      stroke="currentColor"
      strokeWidth={7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      style={{ filter: 'drop-shadow(0 0 2px rgba(16,185,129,0.95)) drop-shadow(0 0 6px rgba(16,185,129,0.5))' }}
    >
      <path d="M37 10H13l12 14-12 14h24" />
    </svg>
  )
}
