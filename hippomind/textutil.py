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
from functools import lru_cache
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
#: Blocks whose *entire content* is volatile runtime state.  These are removed
#: with their contents: keeping them would make two captures of the same request
#: hash differently because the timestamp inside changed.
_VOLATILE_BLOCK_PATTERNS = (
    re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.S | re.I),
    re.compile(r"<environment_context>.*?</environment_context>", re.S | re.I),
    re.compile(r"<environment-context>.*?</environment-context>", re.S | re.I),
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I),
    re.compile(r"<system_reminder>.*?</system_reminder>", re.S | re.I),
    re.compile(r"<current_time>.*?</current_time>", re.S | re.I),
    re.compile(r"<local_time>.*?</local_time>", re.S | re.I),
)

#: Tags that merely *wrap* real payload.  Only the tag itself is dropped so the
#: user's actual sentence survives de-duplication.
_WRAPPER_TAG_RE = re.compile(
    r"</?(?:USER_REQUEST|user_request|USER_MESSAGE|HUMAN_MESSAGE)\s*/?>",
    re.I,
)

#: Backwards-compatible aggregate: everything that is not payload.
_WRAPPER_PATTERNS = _VOLATILE_BLOCK_PATTERNS + (_WRAPPER_TAG_RE,)
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



@lru_cache(maxsize=512)
def _tokenize_cached(text: str, limit: int) -> frozenset:
    return frozenset(_tokenize_uncached(text, limit))


def tokenize_multilingual(text: str, limit: int = MAX_TEXT_CHARS) -> Set[str]:
    """Cached wrapper: the pack near-duplicate scan calls this O(n^2) times."""
    return set(_tokenize_cached(str(text or ""), int(limit)))
def _tokenize_uncached(text: str, limit: int = MAX_TEXT_CHARS) -> Set[str]:
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
    """Remove harness chrome while preserving the actual payload.

    Volatile blocks are dropped entirely (their contents change every run);
    wrapping tags are dropped but their contents are kept.
    """
    cleaned = text or ""
    for pattern in _VOLATILE_BLOCK_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _WRAPPER_TAG_RE.sub(" ", cleaned)
    return cleaned


def normalise_for_dedup(text: str, *, keep_wrappers: bool = False) -> str:
    """Return a stable canonical form used for content de-duplication.

    The result intentionally drops volatile data: runner wrapper blocks, absolute
    timestamps, and whitespace differences.  Two messages that a user would call
    "the same message" therefore collapse to the same string even when they were
    captured at different times.

    Standalone ``YYYY-MM-DD`` dates inside the payload are preserved so facts
    such as release windows or milestone dates on different days never collide.

    ``keep_wrappers`` retains the wrapper blocks while still normalising
    whitespace and timestamps; callers that must preserve structural markers can
    opt in.
    """
    cleaned = text or ""
    if not keep_wrappers:
        cleaned = strip_wrappers(cleaned)
    cleaned = _TIMESTAMP_RE.sub(" ", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip().lower()
    return cleaned


def content_fingerprint(text: str, *, keep_wrappers: bool = False) -> str:
    """Short hex digest of :func:`normalise_for_dedup`, suitable as a dedup key."""
    canonical = normalise_for_dedup(text, keep_wrappers=keep_wrappers)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Noise classification
# ---------------------------------------------------------------------------
# Raw chat is the dominant volume in a real hub (1.47M of 1.54M rows), and most of
# it carries no reusable knowledge: harness banners, tool banners, slash-command
# echoes, one-word acknowledgements.  These heuristics let the storage layer and
# the retriever agree on what "noise" means without either importing the other.

#: Strings a harness injects around the real payload.  Matched case-insensitively
#: against the *stripped* text; a message that is only a banner is pure noise.
_BANNER_PREFIXES = (
    "# agents.md instructions",
    "<user_request>",
    "</user_request>",
    "<additional_metadata>",
    "</additional_metadata>",
    "<environment_context>",
    "</environment_context>",
    "<system-reminder>",
    "</system-reminder>",
    "the current local time is:",
    "filesystem sandboxing defines",
    "approval policy is currently",
    "you are an ai assistant accessed via",
)

#: One-word acknowledgements that neither carry facts nor aid retrieval.
_FILLER_EXACT = frozenset({
    "ok", "okay", "好", "好的", "嗯", "嗯嗯", "可以", "行", "继续", "对", "是的",
    "谢谢", "多谢", "thanks", "thank you", "thx", "yes", "no", "y", "n", "k",
    "go", "go on", "continue", "下一步", "然后呢",
})

#: Command echoes (`/compact`, `!ls`) carry no semantic content.
_COMMAND_ECHO_RE = re.compile(r"^[/!][a-zA-Z][\w-]{0,48}\s*$")

#: Tool-result banners such as `[exit 0]`, `(tool result)`; not knowledge.
_TOOL_BANNER_RE = re.compile(
    r"^(?:\[[^\]]{0,40}\]|\([^)]{0,40}\))\s*$"
)


def _strip_banners(text: str) -> str:
    """Drop leading harness banner lines so the payload can be inspected."""
    lines = [line for line in (text or "").splitlines()]
    kept = []
    for line in lines:
        probe = line.strip().lower()
        if any(probe.startswith(prefix) for prefix in _BANNER_PREFIXES):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def is_noise_text(text: str, *, min_informative_chars: int = 12) -> bool:
    """Return ``True`` when ``text`` is very unlikely to be useful memory.

    The test is deliberately conservative: it only rejects content that is
    *provably* structural (banners, command echoes, tool banners) or has almost
    no informative characters after normalisation.  Anything ambiguous is kept,
    because a false negative (we store noise) is cheaper than a false positive
    (we discard a real lesson).
    """
    payload = _strip_banners(text or "")
    if not payload:
        return True
    probe = payload.strip().lower()
    if not probe:
        return True
    if probe in _FILLER_EXACT:
        return True
    if _COMMAND_ECHO_RE.match(probe) or _TOOL_BANNER_RE.match(probe):
        return True
    informative = [ch for ch in payload if not ch.isspace()]
    if len(informative) < min_informative_chars:
        # Short but substantive Chinese answers ("用 Redis 因为要持久化") still pass
        # as long as they contain letters/digits; punctuation-only does not.
        if not any(ch.isalnum() for ch in payload):
            return True
        cjk = sum(1 for ch in payload if "\u3400" <= ch <= "\u9fff")
        latin = sum(1 for ch in payload if ch.isascii() and ch.isalnum())
        return (cjk + latin) < 4
    return False


def informative_ratio(text: str) -> float:
    """Fraction of characters that carry signal, for ranking/filtering.

    Returns ``0.0`` for empty input and ``1.0`` for dense prose.  Used to bias
    retrieval away from boilerplate without hard-dropping anything.
    """
    text = text or ""
    if not text:
        return 0.0
    payload = _strip_banners(text)
    if not payload:
        return 0.0
    informative = sum(1 for ch in payload if ch.isalnum() or "\u3400" <= ch <= "\u9fff")
    return informative / max(1, len(payload))


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


def coerce_random_seed(seed):
    """Return a seed that random.Random accepts on Python 3.9 and 3.11+.

    Before Python 3.11 random.seed() fell back to hash() for arbitrary
    objects, so passing e.g. a Hippocampus instance happened to work.
    Python 3.13 raises TypeError for anything that is not None, int, float,
    str, bytes or bytearray. Coerce index-able values (numpy integers) to int
    and fall back to None for everything else, so the demo half keeps
    working instead of crashing on a newer interpreter.
    """
    if seed is None or isinstance(seed, (int, float, str, bytes, bytearray)):
        return seed
    try:
        return int(seed.__index__())
    except (AttributeError, TypeError, ValueError):
        return None
