/**
 * Logo — the MetricAi brand mark: the emerald crystal.
 *
 * Rendered as an <img> that fills and covers its parent tile, so it drops into
 * the existing rounded logo tiles exactly like the old mark did, e.g.:
 *   <div className="w-7 h-7 rounded-md ..."><Logo className="..." /></div>
 * `className` is accepted for compatibility; the size comes from the parent tile.
 */
export function Logo({ className }: { className?: string }) {
  return (
    <img
      src="/logo-crystal.png"
      alt="MetricAi"
      className={className}
      style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 'inherit' }}
    />
  )
}
