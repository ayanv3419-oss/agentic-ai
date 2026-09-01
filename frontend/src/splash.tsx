/**
 * SplashScreen — full-screen brand intro shown on app boot.
 *
 * Sequence (≈1.8s total): a soft emerald glow + the Σ brand tile scale in,
 * the sigma strokes itself in, then "Metric AI" and the tagline rise in a
 * stagger, a brief hold, and the whole overlay fades out — `onDone()` fires
 * exactly once.
 *
 * Self-contained: all @keyframes live in a scoped <style> block below so this
 * works with zero additions to index.css. The brand tile mirrors the sidebar
 * + chat-empty-state mark (emerald-500/10 fill, emerald-500/30 border) so the
 * intro feels of-a-piece with the app. Clicking the overlay or pressing any
 * key skips to the fade-out. Honours prefers-reduced-motion with a static,
 * no-drawing intro.
 */
import React, { useEffect, useRef, useState } from 'react'

type Phase = 'in' | 'out'

// Timing (ms). FADE_MS must match the overlay fade-out duration so the
// finish timeout lines up with the visual fade.
const HOLD_MS = 1350 // hold-then-fade start (tile + draw + staggered text settle)
const FADE_MS = 500 // overlay fade-out duration → also the onDone delay after skip

export function SplashScreen({ onDone }: { onDone: () => void }) {
  const [phase, setPhase] = useState<Phase>('in')

  // True once the user has reduced-motion enabled. Read once on mount.
  const [reduced, setReduced] = useState(false)

  // Guard so onDone() can never fire twice (timer path vs skip path).
  const doneRef = useRef(false)
  // Every pending timer id, cleared on unmount.
  const timersRef = useRef<ReturnType<typeof setTimeout>[]>([])

  const finish = () => {
    if (doneRef.current) return
    doneRef.current = true
    onDone()
  }

  // Move to the fade-out phase (idempotent — repeated skips are harmless).
  // Typed as a click handler so it drops straight onto the overlay's onClick.
  const startFadeOut = (_e?: React.MouseEvent<HTMLDivElement>) => {
    setPhase('out')
  }

  // --- Mount: detect reduced motion, schedule the timeline, wire up skip ---
  useEffect(() => {
    const prefersReduced =
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    setReduced(prefersReduced)

    const timers = timersRef.current
    const schedule = (fn: () => void, ms: number) => {
      timers.push(setTimeout(fn, ms))
    }

    // Reduced motion: brief static hold, then fade out. Otherwise run the
    // full tile → draw → wordmark → tagline timeline before fading.
    schedule(startFadeOut, prefersReduced ? 550 : HOLD_MS)

    const onKeyDown = () => startFadeOut()
    window.addEventListener('keydown', onKeyDown)

    return () => {
      window.removeEventListener('keydown', onKeyDown)
      for (const id of timers) clearTimeout(id)
      timers.length = 0
    }
  }, [])

  // --- When we enter the 'out' phase, fade for FADE_MS then finish once. ---
  useEffect(() => {
    if (phase !== 'out') return
    const id = setTimeout(finish, FADE_MS)
    timersRef.current.push(id)
    return () => clearTimeout(id)
    // finish/onDone are stable enough via the doneRef guard; phase is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase])

  const tileClass = reduced ? 'splash-tile-static' : 'splash-tile-in'
  const wordClass = reduced ? 'splash-rise-static' : 'splash-word-in'
  const tagClass = reduced ? 'splash-rise-static' : 'splash-tag-in'

  return (
    <div
      role="status"
      aria-label="Loading Metric AI"
      onClick={startFadeOut}
      className="splash-overlay"
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 50,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '24px',
        // Subtle emerald bloom behind the mark on a near-black base.
        background: '#09090b',
        opacity: phase === 'out' ? 0 : 1,
        transition: `opacity ${FADE_MS}ms ease`,
        cursor: 'pointer',
        userSelect: 'none',
      }}
    >
      {/* Brand tile — mirrors the sidebar / chat-empty-state mark, scaled up. */}
      <div
        className={tileClass}
        style={{
          width: 104,
          height: 104,
          borderRadius: 28,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'rgba(16,185,129,0.10)',
          border: '1px solid rgba(16,185,129,0.30)',
          // Soft radial glow around the mark — the premium-pass bloom, done as
          // a contained halo (a wide faint ring + a tighter brighter one)
          // rather than a full-viewport gradient.
          boxShadow:
            '0 0 90px 10px rgba(16,185,129,0.18), 0 0 34px rgba(16,185,129,0.32), inset 0 0 22px rgba(16,185,129,0.07)',
        }}
      >
        <img
          src="/logo-crystal.png"
          alt="Metric AI"
          style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 'inherit' }}
        />
      </div>

      {/* Wordmark + tagline, staggered. */}
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <div
          className={wordClass}
          style={{
            fontSize: 24,
            fontWeight: 600,
            letterSpacing: '-0.02em',
            color: '#fafafa',
          }}
        >
          Metric AI
        </div>
        <div
          className={tagClass}
          style={{
            fontSize: 13.5,
            fontWeight: 400,
            letterSpacing: '0.01em',
            color: '#71717a',
          }}
        >
          Ask your data anything
        </div>
      </div>

      <style>{`
        .splash-tile-in {
          opacity: 0;
          transform: scale(0.85);
          animation: splash-tile 0.62s cubic-bezier(0.22, 1, 0.36, 1) forwards;
        }
        .splash-tile-static { opacity: 1; transform: none; }
        @keyframes splash-tile {
          from { opacity: 0; transform: scale(0.85); }
          60%  { opacity: 1; }
          to   { opacity: 1; transform: scale(1); }
        }

        .splash-sigma-draw {
          stroke-dasharray: 100;
          stroke-dashoffset: 100;
          animation: splash-draw 0.85s cubic-bezier(0.65, 0, 0.35, 1) 0.15s forwards;
        }
        .splash-sigma-static {
          stroke-dasharray: 100;
          stroke-dashoffset: 0;
        }
        @keyframes splash-draw {
          from { stroke-dashoffset: 100; }
          to   { stroke-dashoffset: 0; }
        }

        .splash-word-in {
          opacity: 0;
          animation: splash-rise 0.5s cubic-bezier(0.22, 1, 0.36, 1) 0.55s forwards;
        }
        .splash-tag-in {
          opacity: 0;
          animation: splash-rise 0.5s cubic-bezier(0.22, 1, 0.36, 1) 0.8s forwards;
        }
        .splash-rise-static { opacity: 1; }
        @keyframes splash-rise {
          from { opacity: 0; transform: translateY(9px); }
          to   { opacity: 1; transform: translateY(0); }
        }
      `}</style>
    </div>
  )
}
