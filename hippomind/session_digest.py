"""Session-level digest: turn a whole raw session into one durable memory.

Raw chat is stored per message, so a 9,000-message Codex session would need
9,000 distillation rounds to be summarised.  This module groups raw messages by
session, builds a bounded digest (the opening request, the strongest decision
and progress lines, and the final state), distils it once, and records a
``digest:<session>`` cursor so the session is never digested twice.

The digest is deliberately extractive before it is generative: the model sees
the most decision-dense lines rather than a 40,000-character transcript, which
keeps latency and cost bounded and makes the evidence chain easy to audit.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .distill import ClaudeExtractor
from .ledger import LedgerError, SQLiteLedger, utc_now

CURSOR_PREFIX = "digest:"
MAX_DIGEST_CHARS = 6_000
MAX_MESSAGES_PER_SESSION = 400
DECISION_HINTS = ("决定", "采用", "结论", "必须", "不能", "不要", "偏好", "选择",
                  "修复", "原因", "部署", "配置", "完成", "已实现", "验收", "通过")


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    agent: str
    project: str
    task_id: str
    messages: int
    digest: str
    first_at: str
    last_at: str


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


#: Inline harness tags that carry no durable meaning.
BOILERPLATE_RE = re.compile(
    r"</?(USER_REQUEST|ADDITIONAL_METADATA|USER_SETTINGS_CHANGE|SYSTEM_REMINDER|"
    r"environment_context|MEMORY_DATA_UNTRUSTED|MEMORY_DATA)[^>]*>", re.IGNORECASE)

#: Harness sentences that pollute a digest.  These are injected by the host on
#: every turn, so they repeat in every session and would otherwise dominate the
#: summary of a long one.  Matched per sentence, not per line: a sentence that
#: merely mentions the time still carries the user's actual request.
HARNESS_SENTENCE_RE = re.compile(
    r"(The current local time is|The user changed setting|"
    r"No need to comment on this change|If reporting what model|"
    r"Today.s date is|knowledge cutoff is|^\[工具结果[:：]|^\[tool result|"
    r"^\[user\]|^\[assistant\])",
    re.IGNORECASE)

#: Sessions whose text is pure harness traffic carry no durable memory.
NOISE_PATTERNS = (
    "return only json",
    "output-format text",
    chr(123) + chr(34) + "ok" + chr(34) + chr(58) + "true" + chr(125),
    "<MEMORY_DATA",
    "system-reminder",
    "HippoMind 的记忆提炼器",
    "output_format",
)


def _strip_boilerplate(text: str) -> str:
    """Remove host-injected tags and per-turn harness sentences.

    Filtering is per sentence: dropping only the matched words would leave
    fragments like "2026-10-23T21:45:44-07:00." in every digest, which is noise
    of the same kind, just shorter.
    """
    cleaned = BOILERPLATE_RE.sub(" ", str(text or ""))
    sentences = re.split(r"(?<=[。！？.!?])" + chr(92) + "s+|" + chr(92) + "n+", cleaned)
    kept = [part.strip() for part in sentences
            if part.strip() and not HARNESS_SENTENCE_RE.search(part)]
    return " ".join(kept)


CONTEXT_ONLY_RE = re.compile(
    r"(HippoMind 自动上下文|不可信参考数据|MEMORY_DATA_UNTRUSTED|"
    r"<MEMORY_DATA|未找到有证据且相关的记忆)",
    re.IGNORECASE)


def _context_only(text: str) -> bool:
    """True when the text is dominated by injected memory context."""
    matches = CONTEXT_ONLY_RE.findall(str(text or ""))
    if not matches:
        return False
    stripped = CONTEXT_ONLY_RE.sub(" ", str(text or ""))
    return len(" ".join(stripped.split())) < 80


def _informative(text: str) -> bool:
    lowered = text.lower()
    if any(pattern.lower() in lowered for pattern in NOISE_PATTERNS):
        return False
    if _context_only(text):
        return False
    # A digest of only tags/JSON has almost no letters or CJK left.
    letters = sum(1 for ch in text if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return letters >= 20


def _score(text: str) -> int:
    return sum(1 for hint in DECISION_HINTS if hint in text)


class SessionDigester:
    """Bounded, idempotent session summariser."""

    _DEFAULT = object()

    def __init__(self, ledger: SQLiteLedger, *, extractor: Any = _DEFAULT,
                 provider: str = ""):
        self.ledger = ledger
        if extractor is not self._DEFAULT:
            # Explicit None means "deterministic only"; that is what tests and
            # the HIPPOMIND_DIGEST_PROVIDER=heuristic path ask for.
            self.extractor = extractor
        else:
            configured = (provider or os.environ.get("HIPPOMIND_DIGEST_PROVIDER",
                          os.environ.get("HIPPOMIND_DISTILL_PROVIDER", "auto"))).strip().lower()
            self.extractor = None if configured in {"heuristic", "off"} else ClaudeExtractor()

    # -- collection -------------------------------------------------------
    def pending_sessions(self, limit: int = 4, *, min_messages: int = 4) -> List[Dict[str, Any]]:
        """Return sessions with no digest cursor, newest first."""
        limit = max(1, min(500, int(limit)))
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT m.session_id, m.source_agent, COUNT(*) n, MIN(m.created_at) first_at, "
                "MAX(m.created_at) last_at, MAX(m.project) project, MAX(m.task_id) task_id "
                "FROM memories m LEFT JOIN source_cursors c "
                "ON c.source_id = 'digest:' || m.session_id "
                "WHERE m.raw_chat=1 AND m.session_id!='' AND c.source_id IS NULL "
                "GROUP BY m.session_id HAVING n >= ? "
                "ORDER BY last_at DESC LIMIT ?", (max(1, int(min_messages)), limit)).fetchall()
        return [dict(row) for row in rows]

    def _messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT memory_id, role, content, created_at FROM memories "
                "WHERE raw_chat=1 AND session_id=? AND content!='' "
                "ORDER BY created_at ASC LIMIT ?",
                (session_id, MAX_MESSAGES_PER_SESSION)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def build_digest(messages: Sequence[Mapping[str, Any]]) -> str:
        """Extractive digest: opening request, decision-dense lines, final state."""
        if not messages:
            return ""
        parts: List[str] = []
        opening = _clean(_strip_boilerplate(messages[0].get("content")), 900)
        if opening:
            parts.append("开场请求：" + opening)
        scored = sorted(
            ((_score(str(item.get("content", ""))), index, item)
             for index, item in enumerate(messages)),
            key=lambda value: (-value[0], value[1]))
        picked: List[Tuple[int, Mapping[str, Any]]] = []
        for score, index, item in scored:
            if score <= 0:
                break
            picked.append((index, item))
            if len(picked) >= 14:
                break
        picked.sort(key=lambda value: value[0])
        for index, item in picked:
            text = _clean(_strip_boilerplate(item.get("content")), 420)
            if not text or text == opening:
                continue
            role = str(item.get("role") or "?")
            parts.append("[%s] %s" % (role, text))
        tail = _clean(_strip_boilerplate(messages[-1].get("content")), 700)
        if tail:
            parts.append("收尾：" + tail)
        digest = "\n".join(parts)
        return digest[:MAX_DIGEST_CHARS]

    # -- extraction -------------------------------------------------------
    def _claims(self, summary: SessionSummary) -> Tuple[List[Dict[str, Any]], str]:
        from .distill import Candidate  # local import keeps the module graph flat

        candidate = Candidate("", "task", summary.digest, summary.agent,
                              summary.project, summary.task_id, summary.session_id,
                              summary.last_at, {})
        if getattr(self.extractor, "available", False):
            claims = self.extractor.extract([candidate])
            if claims:
                return claims, getattr(self.extractor, "name", "llm")
        # Deterministic fallback: keep the digest itself as one factual claim.
        return ([{"subject": summary.task_id or summary.session_id,
                  "predicate": "会话摘要",
                  "slot_key": summary.session_id + ":digest",
                  "value": _clean(summary.digest, 1_500),
                  "confidence": 0.5, "source": 1}], "heuristic")

    def _record(self, summary: SessionSummary, claims: Sequence[Mapping[str, Any]],
                extractor_name: str) -> Dict[str, Any]:
        values = ["%s %s=%s" % (item.get("subject", ""), item.get("predicate", ""),
                                item.get("value", "")) for item in claims]
        body = "会话摘要（%s，%s 条消息）：%s" % (
            summary.agent or "unknown", summary.messages, _clean(summary.digest, 1_600))
        return {
            "content": (body + "\n" + "；".join(v for v in values if v))[:2_400],
            "kind": "semantic",
            "source_agent": "hippomind-digest",
            "scope": "shared",
            "task_id": summary.task_id or summary.session_id,
            "project": summary.project,
            "session_id": summary.session_id,
            "evidence": ["session:%s" % summary.session_id],
            "timestamp": summary.last_at or utc_now(),
            "confidence": 0.6,
            # A session digest is a handoff summary: it must outrank the
            # individual raw messages it came from, otherwise a 2-message
            # harness session can win the slot with recency alone.
            "importance": 7,
            "metadata": {"digested": True, "extractor": extractor_name,
                         "messages": summary.messages,
                         "first_at": summary.first_at, "last_at": summary.last_at},
            "claims": [{"subject": str(item.get("subject", "")),
                        "predicate": str(item.get("predicate", "")),
                        "slot_key": str(item.get("slot_key", "")),
                        "value": str(item.get("value", "")),
                        "confidence": float(item.get("confidence", 0.6) or 0.6)}
                       for item in claims],
        }

    def _mark(self, session_id: str, messages: int, memory_id: str = "",
              error: str = "") -> None:
        self.ledger.set_cursor(CURSOR_PREFIX + session_id,
                               json.dumps({"memory_id": memory_id, "messages": messages},
                                          separators=(",", ":")),
                               error=error,
                               metadata={"kind": "session-digest", "at": utc_now()})

    def run(self, *, limit: int = 4, dry_run: bool = False,
            min_messages: int = 4) -> Dict[str, Any]:
        started = time.time()
        sessions = self.pending_sessions(limit, min_messages=min_messages)
        summary = {"sessions": len(sessions), "digested": 0, "skipped": 0,
                   "errors": 0, "claims": 0, "messages": 0,
                   "extractor": "none", "elapsed_ms": 0.0}
        for row in sessions:
            session_id = str(row.get("session_id") or "")
            if not session_id:
                continue
            messages = self._messages(session_id)
            summary["messages"] += len(messages)
            digest = self.build_digest(messages)
            if not digest or not _informative(digest):
                summary["skipped"] += 1
                if not dry_run:
                    self._mark(session_id, len(messages),
                               error="empty digest" if not digest else "not informative")
                continue
            item = SessionSummary(
                session_id=session_id,
                agent=str(row.get("source_agent") or ""),
                project=str(row.get("project") or ""),
                task_id=str(row.get("task_id") or ""),
                messages=len(messages),
                digest=digest,
                first_at=str(row.get("first_at") or ""),
                last_at=str(row.get("last_at") or ""),
            )
            claims, extractor_name = self._claims(item)
            summary["extractor"] = extractor_name
            summary["claims"] += len(claims)
            if dry_run:
                summary["digested"] += 1
                continue
            try:
                result = self.ledger.ingest(self._record(item, claims, extractor_name))
                self._mark(session_id, len(messages), memory_id=result.memory_id)
                summary["digested"] += 1
            except (LedgerError, ValueError) as exc:
                summary["errors"] += 1
                self._mark(session_id, len(messages), error=str(exc)[:300])
        summary["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
        return summary

    def status(self) -> Dict[str, Any]:
        with self.ledger._reading() as conn:
            total = int(conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM memories "
                "WHERE raw_chat=1 AND session_id!=''").fetchone()[0])
            digested = int(conn.execute(
                "SELECT COUNT(*) FROM source_cursors WHERE source_id LIKE 'digest:%'").fetchone()[0])
            pending = max(0, total - digested)
        return {"sessions": total, "digested": digested, "pending": pending,
                "extractor": getattr(self.extractor, "name", "heuristic"),
                "available": bool(getattr(self.extractor, "available", False))}
