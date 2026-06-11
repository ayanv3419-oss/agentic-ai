# ADR-0001 — LLM provider strategy

**Status:** Superseded (original: Groq 2025-05-15 → Qwen+Gemini 2026-06-11)  
**Date:** 2026-06-11

## Context

The system calls an LLM on every agentic turn for tool selection, SQL writing, and
insight narration. Latency, cost, and reliability all matter for UX. The original
decision chose Groq with a user-supplied API key; this was replaced twice:
- Migrated off Groq → Together.ai (Qwen/Qwen3-8B) when Groq rate limits became an issue
- Migrated off Together.ai → Alibaba Qwen direct endpoint with Gemini as fallback
  (local commits e119fc3/9838c9f, shipped to Render prod)

## Decision

**Primary:** Alibaba Qwen, served via `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`
env vars (OpenAI-compatible endpoint).

**Fallback:** Google Gemini, served via `FALLBACK_LLM_BASE_URL` /
`FALLBACK_LLM_MODEL` / `FALLBACK_LLM_API_KEY`.

Fallback logic lives in `coordinator/llm.py`: on primary failure the fallback
runs `_complete_with_tools_one_shot()` — a single attempt, no retry cascade, to
avoid doubling the tail latency.

**Local dev:** Ollama (`http://localhost:11434/v1`) — any ≤4B Q4 model.
Config is the same env-var interface (just point `LLM_BASE_URL` at Ollama).

The API key is now stored server-side (never sent from the browser).

## Consequences

- ✅ Backend holds the key — no user-side key management
- ✅ Fallback prevents total outage when primary is down
- ✅ OpenAI-compatible interface — swapping providers requires only env-var changes
- ✅ Local dev works on CPU-only hardware (Ollama)
- ⚠️ Two provider dependencies instead of one — both must be monitored
- ⚠️ Fallback is one-shot (no retry) — Gemini transient errors still surface as
  turn failures; a full retry cascade on the fallback would double tail latency
