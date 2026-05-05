"""Intent router — classifies a query as DIRECT CHAT or AGENTIC TOOL mode.

Runs BEFORE any sub-agent dispatch. Deterministic, keyword + regex based,
zero LLM cost. The goal is to keep small talk off the analytics pipeline
while never silently downgrading a real data question.

Decision precedence (highest first):
  1. Empty input            → chat (safe default)
  2. Exact-match small talk → chat ("hi", "thanks", "good morning", …)
  3. Any AGENTIC keyword    → agentic (data words always win, even after a
                              greeting prefix like "hi, show me sales")
  4. Chat regex pattern     → chat ("how are you", "who are you", …)
  5. Short low-signal input → chat (≤ 4 tokens, ≤ 30 chars, no data words)
  6. Fallback               → agentic (never accidentally skip data path)
"""
from __future__ import annotations

import re
from typing import Literal

Mode = Literal["chat", "agentic"]


# --- AGENTIC indicators ----------------------------------------------------
# If any of these substrings appears in the (lowercased) question, the
# request is treated as a data query — no matter what greeting prefix
# the user added.
_AGENTIC_KEYWORDS: tuple[str, ...] = (
    # Domain nouns
    "sale", "sales", "purchase", "purchases", "revenue", "income",
    "earning", "earnings", "turnover", "transaction", "transactions",
    "order", "orders", "customer", "customers", "buyer", "buyers",
    "vendor", "vendors", "supplier", "suppliers", "party",
    "invoice", "bill", "report", "dashboard", "kpi", "kpis",
    "analytics", "analysis", "trend", "growth",
    "forecast", "predict", "projection",
    "rca", "root cause",
    # Money / currency markers
    "amount", "₹", "rupee", "rupees", "rs.", "rs ", "dollar", "$",
    # Time references that imply a query window
    "today", "yesterday", "this week", "last week",
    "this month", "last month", "this year", "last year",
    "this quarter", "last quarter", "ytd",
    # Operations on data
    "upload", "import", "csv", "xlsx", "excel", "drive",
    "fetch", "show me", "give me", "list ", "find ",
    # Trigger phrases
    "why did", "what caused", "compare", " vs ", "versus",
    "best seller", "top selling", "top ",
    "how much", "how many", "total ",
)

# --- CHAT indicators -------------------------------------------------------
_CHAT_EXACT: frozenset[str] = frozenset({
    "hi", "hii", "hiii", "hello", "helo", "hey", "yo", "sup",
    "hi there", "hey there", "hello there",
    "good morning", "good afternoon", "good evening", "good night",
    "morning", "evening", "afternoon",
    "thanks", "thank you", "thx", "ty",
    "ok", "okay", "cool", "nice", "great", "awesome", "alright", "got it",
    "bye", "goodbye", "see you", "see ya", "cya",
})

_CHAT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^(hi|hii|hello|hey|yo|sup)[\s,!.?]*$", re.I),
    re.compile(r"^how\s+are\s+(you|u|ya|things)\b", re.I),
    re.compile(r"^who\s+are\s+you\b", re.I),
    re.compile(r"^what(?:'s|\s+is)\s+your\s+name\b", re.I),
    re.compile(r"^what\s+can\s+you\s+do\b", re.I),
    re.compile(r"^help\s*$", re.I),
    re.compile(r"^(thank\s*you|thanks|thx|ty)\b", re.I),
    re.compile(r"^(good|nice|great|happy)\s+(morning|afternoon|evening|night|day)\b", re.I),
    re.compile(r"^are\s+(you|u)\s+(there|online|alive|ok|good)\b", re.I),
    re.compile(r"^(bye|goodbye|see\s+(you|ya)|cya)\b", re.I),
)


def _normalize(question: str) -> tuple[str, str]:
    """Returns (lowercased_full, normalized_for_exact_match)."""
    q = (question or "").strip()
    lower = q.lower()
    normalized = re.sub(r"\s+", " ", lower).strip(" .,!?;:'\"")
    return lower, normalized


def classify(question: str) -> tuple[Mode, str]:
    """Decide CHAT vs AGENTIC. Returns (mode, human-readable reason)."""
    if not question or not question.strip():
        return "chat", "empty input — handled as chat (safe default)"

    lower, normal = _normalize(question)

    # 1. Exact small-talk match
    if normal in _CHAT_EXACT:
        return "chat", f"exact-match small talk: {normal!r}"

    # 2. Any agentic keyword takes precedence — protects the data path
    for kw in _AGENTIC_KEYWORDS:
        if kw in lower:
            return "agentic", f"agentic keyword present: {kw!r}"

    # 3. Chat regex patterns
    for pat in _CHAT_PATTERNS:
        if pat.search(lower):
            return "chat", f"matches chat pattern: {pat.pattern!r}"

    # 4. Short, low-signal input → chat
    tokens = normal.split()
    if len(tokens) <= 4 and len(normal) <= 30:
        return "chat", f"short low-signal question ({len(tokens)} tokens)"

    # 5. Default: agentic (never silently skip the data path)
    return "agentic", "no chat indicators detected; defaulting to agentic"
