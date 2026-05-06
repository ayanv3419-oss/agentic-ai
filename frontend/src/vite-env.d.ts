/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Preferred — full Render backend URL, e.g. https://agentic-ai-anet.onrender.com */
  readonly VITE_BACKEND_URL?: string
  /** Legacy — kept as fallback for backwards compatibility with older deploys. */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
