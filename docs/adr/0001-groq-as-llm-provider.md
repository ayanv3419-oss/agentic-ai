# ADR-0001 — Groq as the LLM provider

**Status:** Accepted  
**Date:** 2025-05-15

## Context

The system needs an LLM for three tasks: sub-agent dispatch, SQL planning, and insight narration. The LLM is called on every agentic turn, so latency and cost matter significantly for UX.

## Decision

Use **Groq** (`/openai/v1/chat/completions`) as the sole LLM provider, with `llama-3.3-70b-versatile` as the default model. The Groq API key is supplied per-request by the browser in `X-Groq-Api-Key`, scoped via `contextvars` so keys never leak between concurrent requests.

## Consequences

- ✅ Very low latency (Groq's LPU inference is significantly faster than GPU-based providers)
- ✅ No backend API key storage — the user brings their own key
- ✅ OpenAI-compatible interface makes swapping providers easy
- ⚠️ Single provider dependency — if Groq is down, the agentic pipeline is unavailable
- ⚠️ Model availability tied to what Groq offers
