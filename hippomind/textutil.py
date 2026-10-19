"""Shared text primitives with no memory-model dependencies.

Historically the hub modules (``ledger``, ``context_pack``, ``commons``) imported
``token_sim`` from :mod:`hippomind.hippocampus`.  That pulled the 1.4k-line
grid-world demonstration package into every import of the storage layer and made
the ``__init__`` module graph circular.  These helpers are pure text utilities, so
they live here and have zero imports from the memory model.

Nothing in this module mutates state, touches the filesystem, or depends on
SQLite; it is safe to import from anywhere in the package.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, List, Set

#: Hard bound for a persisted free-text field.  Callers pass tighter limits for
#: individual columns; this is only the fallback ceiling.
MAX_TEXT_CHARS = 8_000

_CJK = "\u3400-\u9fff"
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[" + _CJK + r"]")

# Wrapper markers emitted by agent harnesses around otherwise identical user
# text.  They differ on every replay (timestamps, metadata blocks), which used to
# make semantically identical messages hash differently and defeat de-duplication.
_WRAPPER_PATTERNS = (
    re.compile(r"<USER_REQUEST>.*?</USER_REQUEST>", re.S | re.I),
    re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.S | re.I),
    re.compile(r"<environment_context>.*?</environment_context>", re.S | re.I),
    re.compile(r"<environment-context>.*?</environment-context>", re.S | re.I),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I),
    re.compile(r"<system_reminder>.*?</system_reminder>", re.S | re.I),
    re.compile(r"<current_time>.*?</current_time>", re.S | re.I),
    re.compile(r"<local_time>.*?</local_time>", re.S | re.I),
)
#: Inline timestamps and clock-time phrases that carry no memory signal.
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_WS_RE = re.compile(r"\s+")


def short_text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    """Convert arbitrary persisted/user data into a bounded text field."""
    if value is None:
        return ""
    try:
        return str(value).strip()[:limit]
    except Exception:  # pragma: no cover - defensive, objects may raise on str()
        return ""


# Backwards-compatible private alias used by the demonstration package.
_short_text = short_text


def tokenize_multilingual(text: str, limit: int = MAX_TEXT_CHARS) -> Set[str]:
    """Return word tokens for Latin text plus unigrams/bigrams for CJK.

    No external tokenizer is used: Latin/digit runs are kept whole, every CJK
    character is a unigram, and adjacent CJK characters add a bigram.  Bigrams
    are what make Chinese phrase matching work without a dictionary.
    """
    parts = _TOKEN_RE.findall(short_text(text, limit).lower())
    tokens: Set[str] = set(parts)
    cjk = [part for part in parts if len(part) == 1 and "\u3400" <= part <= "\u9fff"]
    tokens.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


def token_sim(a: str, b: str) -> float:
    """Multilingual word/character-bigram Jaccard similarity in ``[0, 1]``."""
    sa = tokenize_multilingual(a)
    sb = tokenize_multilingual(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def strip_wrappers(text: str) -> str:
    """Remove harness wrapper elements that carry volatile per-run metadata."""
    cleaned = text or ""
    for pattern in _WRAPPER_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def normalise_for_dedup(text: str, *, keep_wrappers: bool = False) -> str:
    """Return a stable canonical form used for content de-duplication.

    The result intentionally drops volatile data: runner wrapper blocks, absolute
    timestamps, and whitespace differences.  Two messages that a user would call
    "the same message" therefore collapse to the same string even when they were
    captured at different times.

    ``keep_wrappers`` retains the wrapper blocks while still normalising
    whitespace and timestamps; callers that must preserve structural markers can
    opt in.
    """
    cleaned = text or ""
    if not keep_wrappers:
        cleaned = strip_wrappers(cleaned)
    cleaned = _TIMESTAMP_RE.sub(" ", cleaned)
    cleaned = _ISO_DATE_RE.sub(" ", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip().lower()
    return cleaned


def content_fingerprint(text: str, *, keep_wrappers: bool = False) -> str:
    """Short hex digest of :func:`normalise_for_dedup`, suitable as a dedup key."""
    canonical = normalise_for_dedup(text, keep_wrappers=keep_wrappers)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:32]


def truncate_middle(text: str, limit: int, marker: str = "\n…\n") -> str:
    """Truncate ``text`` to ``limit`` characters keeping both ends readable."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if len(marker) >= limit:
        return text[:limit]
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def dedupe_preserving_order(values: Iterable[str]) -> List[str]:
    """De-duplicate strings case-insensitively while keeping first occurrence."""
    seen: Set[str] = set()
    ordered: List[str] = []
    for value in values:
        key = (value or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered
