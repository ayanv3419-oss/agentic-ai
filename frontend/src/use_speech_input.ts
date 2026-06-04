/**
 * useSpeechInput — voice dictation via the browser's Web Speech API.
 *
 * Wraps `SpeechRecognition` (Chrome/Edge: `webkitSpeechRecognition`) so a
 * component can offer a "tap-to-talk" mic that streams recognized text back
 * via `onResult`. The component owns the textarea state; this hook only does
 * recognition and reports the running transcript for the current session.
 *
 * Zero dependencies, no backend, no API key — transcription runs in the
 * browser. Unsupported browsers (e.g. Firefox) report `supported: false` so
 * the caller can hide the button.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

export interface UseSpeechInput {
  /** True when the browser exposes a SpeechRecognition implementation. */
  supported: boolean
  /** True while actively listening to the microphone. */
  listening: boolean
  /** Human-readable error from the last failed session, or null. */
  error: string | null
  start: () => void
  stop: () => void
  toggle: () => void
}

function getConstructor(): SpeechRecognitionConstructor | null {
  if (typeof window === 'undefined') return null
  return window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null
}

/**
 * @param onResult Called on every recognition update with the full transcript
 *   for the current listening session (interim + finalized). The caller
 *   decides how to merge it into its input (e.g. append to existing text).
 * @param opts.lang BCP-47 language tag. Defaults to en-IN (Indian English).
 */
export function useSpeechInput(
  onResult: (transcript: string) => void,
  opts?: { lang?: string },
): UseSpeechInput {
  const Ctor = useMemo(getConstructor, [])
  const supported = Ctor !== null
  const lang = opts?.lang ?? 'en-IN'

  const [listening, setListening] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const recognitionRef = useRef<SpeechRecognition | null>(null)

  // Hold the latest callback in a ref so recognition handlers stay stable and
  // we never re-create the recognition object just because the parent
  // re-rendered with a new closure.
  const onResultRef = useRef(onResult)
  useEffect(() => {
    onResultRef.current = onResult
  }, [onResult])

  const stop = useCallback(() => {
    const rec = recognitionRef.current
    if (!rec) return
    try {
      rec.stop()
    } catch {
      /* already stopped */
    }
  }, [])

  const start = useCallback(() => {
    if (!Ctor) return
    // Chrome throws InvalidStateError if start() is called while already
    // running — guard on our own ref.
    if (recognitionRef.current) return

    setError(null)
    const rec = new Ctor()
    rec.lang = lang
    rec.continuous = true
    rec.interimResults = true
    rec.maxAlternatives = 1

    rec.onresult = (event: SpeechRecognitionEvent) => {
      let transcript = ''
      for (let i = 0; i < event.results.length; i += 1) {
        transcript += event.results[i][0].transcript
      }
      onResultRef.current(transcript.trim())
    }
    rec.onerror = (event: SpeechRecognitionErrorEvent) => {
      // 'no-speech' (silence) and 'aborted' (user/programmatic stop) are
      // benign — don't surface them as errors.
      if (event.error === 'no-speech' || event.error === 'aborted') return
      setError(
        event.error === 'not-allowed' || event.error === 'service-not-allowed'
          ? 'Microphone access was blocked. Allow it in your browser to dictate.'
          : `Voice input error: ${event.error}`,
      )
    }
    rec.onend = () => {
      recognitionRef.current = null
      setListening(false)
    }

    recognitionRef.current = rec
    try {
      rec.start()
      setListening(true)
    } catch {
      recognitionRef.current = null
      setListening(false)
    }
  }, [Ctor, lang])

  const toggle = useCallback(() => {
    if (listening) stop()
    else start()
  }, [listening, start, stop])

  // Tear down any in-flight recognition when the component unmounts so the mic
  // is released if the user navigates away mid-dictation.
  useEffect(
    () => () => {
      const rec = recognitionRef.current
      if (!rec) return
      try {
        rec.abort()
      } catch {
        /* noop */
      }
      recognitionRef.current = null
    },
    [],
  )

  return { supported, listening, error, start, stop, toggle }
}
