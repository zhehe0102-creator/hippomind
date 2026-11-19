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
    scope: str = "shared"


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
    @staticmethod
    def _parse_cursor(raw: Any) -> Dict[str, Any]:
        """Best-effort decode of a digest cursor.

        Current shape: {"messages": n, "last_at": iso, "memory_id": id}.  Empty
        or malformed values mean "never digested"; a legacy object without
        last_at is still honoured (freshness falls back to the message count).
        """
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _digest_stale(row: Mapping[str, Any]) -> bool:
        """True when the session has no digest, or its digest is outdated.

        pending = no cursor OR n > cursor.messages OR last_at > cursor.last_at.
        Comparing the message count catches appends whose timestamp ties the
        previous last_at; comparing last_at catches histories whose newest row
        was rewritten.  Legacy cursors without last_at fall back to the count.
        """
        cursor = row.get("cursor") or {}
        if not cursor:
            return True
        try:
            seen = max(0, int(cursor.get("messages") or 0))
        except (TypeError, ValueError):
            seen = 0
        try:
            total = max(0, int(row.get("n") or 0))
        except (TypeError, ValueError):
            total = 0
        if total > seen:
            return True
        cursor_last = str(cursor.get("last_at") or "")
        return bool(cursor_last) and str(row.get("last_at") or "") > cursor_last

    def pending_sessions(self, limit: int = 4, *, min_messages: int = 4) -> List[Dict[str, Any]]:
        """Return sessions with no digest or with a stale digest, newest first."""
        limit = max(1, min(500, int(limit)))
        minimum = max(1, int(min_messages))
        # Over-fetch: staleness needs the cursor parsed in Python (it is JSON),
        # so the SQL LIMIT would otherwise drop eligible sessions.
        window = min(2_000, max(64, limit * 8))
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT m.session_id, m.scope, m.source_agent, COUNT(*) n, MIN(m.created_at) first_at, "
                "MAX(m.created_at) last_at, MAX(m.project) project, MAX(m.task_id) task_id, "
                "COALESCE(c.cursor, CASE WHEN cl.metadata_json IS NULL OR "
                "COALESCE(json_extract(cl.metadata_json, '$.scope'), m.scope) = m.scope "
                "THEN cl.cursor ELSE NULL END) cursor "
                "FROM memories m "
                "LEFT JOIN source_cursors c ON c.source_id = 'digest:' || m.scope || ':' || m.session_id "
                "LEFT JOIN source_cursors cl ON cl.source_id = 'digest:' || m.session_id "
                "WHERE m.raw_chat=1 AND m.session_id!='' "
                "GROUP BY m.session_id, m.scope HAVING n >= ? "
                "ORDER BY last_at DESC LIMIT ?", (minimum, window)).fetchall()
        pending: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["cursor"] = self._parse_cursor(item.pop("cursor", ""))
            if not self._digest_stale(item):
                continue
            pending.append(item)
            if len(pending) >= limit:
                break
        return pending

    def _messages(self, session_id: str, *, scope: str = "") -> List[Dict[str, Any]]:
        """Newest MAX_MESSAGES_PER_SESSION messages, returned oldest first.

        The cap must select the *tail*, not the head: a long session that keeps
        growing would otherwise digest only its first messages forever, and the
        new message that made the session stale would never enter the summary.
        """
        scope_filter = " AND scope=?" if scope else ""
        params: Tuple[Any, ...] = (session_id, scope, MAX_MESSAGES_PER_SESSION) if scope else (session_id, MAX_MESSAGES_PER_SESSION)
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT memory_id, role, content, created_at FROM ("
                " SELECT memory_id, role, content, created_at FROM memories"
                " WHERE raw_chat=1 AND session_id=?" + scope_filter + " AND content!=''"
                " ORDER BY created_at DESC, memory_id DESC LIMIT ?"
                ") ORDER BY created_at ASC, memory_id ASC",
                params).fetchall()
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
        from .distill import Candidate, DistillEngine  # local import keeps the module graph flat

        source_scope = str(getattr(summary, "scope", "") or "shared")
        candidate = Candidate("", "task", summary.digest, summary.agent,
                              summary.project, summary.task_id, summary.session_id,
                              summary.last_at, {}, source_scope)
        if getattr(self.extractor, "available", False):
            helper = DistillEngine(self.ledger, extractor=None)
            if isinstance(self.extractor, ClaudeExtractor):
                claims = self.extractor.extract(
                    [candidate], known_slots=helper._active_slots([candidate])
                )
            else:
                claims = self.extractor.extract([candidate])
            if claims:
                filtered = [c for c in claims if str(c.get("op") or "").strip().upper() != "NOOP"]
                if filtered:
                    subject_map = helper._active_subject_map()
                    return (
                        helper._canonicalize_claims(filtered, source_scope, subject_map),
                        getattr(self.extractor, "name", "llm"),
                    )
        # Deterministic fallback: keep the digest itself as one factual claim.
        return ([{"subject": summary.task_id or summary.session_id,
                  "predicate": "会话摘要",
                  "slot_key": summary.session_id + ":digest",
                  "value": _clean(summary.digest, 1_500),
                  "confidence": 0.5, "source": 1}], "heuristic")

    def _record(self, summary: SessionSummary, claims: Sequence[Mapping[str, Any]],
                extractor_name: str) -> Dict[str, Any]:
        from .distill import classify_claim_value

        source_scope = str(getattr(summary, "scope", "") or "").strip() or "shared"
        deduped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for item in claims:
            subject = str(item.get("subject", "") or "").strip()
            predicate = str(item.get("predicate", "") or "").strip()
            slot_key = str(item.get("slot_key", "") or "").strip()
            value = str(item.get("value", "") or "").strip()
            if not subject or not predicate or not slot_key or not value:
                continue
            if classify_claim_value(slot_key, predicate, value):
                continue
            confidence = float(item.get("confidence", 0.6) or 0.6)
            key = (subject, predicate, slot_key)
            prev = deduped.get(key)
            if prev is None or confidence > float(prev.get("confidence", 0.0)):
                deduped[key] = {
                    "subject": subject,
                    "predicate": predicate,
                    "slot_key": slot_key,
                    "value": value,
                    "confidence": confidence,
                }
        gated_claims = list(deduped.values())
        values = ["%s %s=%s" % (item["subject"], item["predicate"], item["value"])
                  for item in gated_claims]
        body = "会话摘要（%s，%s 条消息）：%s" % (
            summary.agent or "unknown", summary.messages, _clean(summary.digest, 1_600))
        return {
            "content": (body + ("\n" + "；".join(v for v in values if v) if values else ""))[:2_400],
            "kind": "semantic",
            "source_agent": "hippomind-digest",
            "scope": source_scope,
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
                         "first_at": summary.first_at, "last_at": summary.last_at,
                         "source_scope": source_scope,
                         "derived_from_raw_chat": True,
                         "untrusted_source": True},
            "claims": gated_claims,
        }

    def _mark(self, session_id: str, messages: int, memory_id: str = "",
              last_at: str = "", error: str = "", *, scope: str = "shared") -> None:
        payload = {"messages": int(messages), "last_at": str(last_at or ""),
                   "memory_id": str(memory_id or ""), "scope": str(scope or "shared")}
        norm_scope = str(scope or "shared").strip() or "shared"
        use_legacy = (norm_scope == "shared")
        if not use_legacy:
            with self.ledger._reading() as conn:
                scope_cnt = int(conn.execute(
                    "SELECT COUNT(DISTINCT scope) FROM memories WHERE raw_chat=1 AND session_id=?",
                    (session_id,),
                ).fetchone()[0])
                existing_scoped = conn.execute(
                    "SELECT 1 FROM source_cursors WHERE source_id=?",
                    (CURSOR_PREFIX + norm_scope + ":" + session_id,),
                ).fetchone()
                existing_legacy = conn.execute(
                    "SELECT metadata_json FROM source_cursors WHERE source_id=?",
                    (CURSOR_PREFIX + session_id,),
                ).fetchone()
            if scope_cnt <= 1 and existing_scoped is None:
                if existing_legacy is None:
                    use_legacy = True
                else:
                    try:
                        legacy_meta = json.loads(str(existing_legacy["metadata_json"] or "{}"))
                    except (TypeError, ValueError):
                        legacy_meta = {}
                    legacy_scope = str(legacy_meta.get("scope") or "").strip() if isinstance(legacy_meta, dict) else ""
                    if not legacy_scope or legacy_scope == norm_scope:
                        use_legacy = True
        cursor_id = (CURSOR_PREFIX + session_id if use_legacy
                     else CURSOR_PREFIX + norm_scope + ":" + session_id)
        self.ledger.set_cursor(cursor_id,
                               json.dumps(payload, separators=(",", ":")),
                               error=error,
                               metadata={"kind": "session-digest", "scope": norm_scope, "at": utc_now()})

    def _supersede_previous(self, old_memory_id: str, new_memory_id: str) -> bool:
        """Retire the digest a newer summary replaces.

        A re-digested session must not leave two active summaries describing
        the same conversation: the old row is superseded so recall cannot
        return stale progress as current.
        """
        if not old_memory_id or not new_memory_id or old_memory_id == new_memory_id:
            return False
        with self.ledger._reading() as conn:
            row = conn.execute("SELECT lifecycle FROM memories WHERE memory_id=?",
                               (old_memory_id,)).fetchone()
        if row is None or str(row["lifecycle"]) != "active":
            return False
        try:
            return bool(self.ledger.supersede(old_memory_id, new_memory_id,
                                              source_agent="hippomind-digest"))
        except (LedgerError, ValueError):
            return False

    def run(self, *, limit: int = 4, dry_run: bool = False,
            min_messages: int = 4) -> Dict[str, Any]:
        started = time.time()
        sessions = self.pending_sessions(limit, min_messages=min_messages)
        summary = {"sessions": len(sessions), "digested": 0, "superseded": 0,
                   "skipped": 0, "errors": 0, "claims": 0, "messages": 0,
                   "extractor": "none", "elapsed_ms": 0.0}
        for row in sessions:
            session_id = str(row.get("session_id") or "")
            if not session_id:
                continue
            scope = str(row.get("scope") or "shared").strip() or "shared"
            cursor = row.get("cursor") if isinstance(row.get("cursor"), Mapping) else {}
            previous_id = str(cursor.get("memory_id") or "")
            messages = self._messages(session_id, scope=scope)
            # Freshness is measured on the whole session, not on the bounded
            # digest window: recording len(messages) would re-queue any session
            # longer than MAX_MESSAGES_PER_SESSION on every pass.
            total = max(len(messages), int(row.get("n") or 0))
            last_at = str(row.get("last_at") or "")
            summary["messages"] += len(messages)
            digest = self.build_digest(messages)
            if not digest or not _informative(digest):
                summary["skipped"] += 1
                if not dry_run:
                    self._mark(session_id, total, memory_id=previous_id,
                               last_at=last_at,
                               error="empty digest" if not digest else "not informative",
                               scope=scope)
                continue
            item = SessionSummary(
                session_id=session_id,
                agent=str(row.get("source_agent") or ""),
                project=str(row.get("project") or ""),
                task_id=str(row.get("task_id") or ""),
                messages=len(messages),
                digest=digest,
                first_at=str(row.get("first_at") or ""),
                last_at=last_at,
                scope=scope,
            )
            claims, extractor_name = self._claims(item)
            summary["extractor"] = extractor_name
            if dry_run:
                summary["claims"] += len(claims)
                summary["digested"] += 1
                continue
            try:
                record = self._record(item, claims, extractor_name)
                if not record.get("claims"):
                    summary["skipped"] += 1
                    self._mark(session_id, total, memory_id=previous_id,
                               last_at=last_at, error="no valid claims", scope=scope)
                    continue
                summary["claims"] += len(record["claims"])
                result = self.ledger.ingest(record)
                if self._supersede_previous(previous_id, result.memory_id):
                    summary["superseded"] += 1
                self._mark(session_id, total, memory_id=result.memory_id,
                           last_at=last_at, scope=scope)
                summary["digested"] += 1
            except (LedgerError, ValueError) as exc:
                summary["errors"] += 1
                self._mark(session_id, total, memory_id=previous_id, last_at=last_at,
                           error=str(exc)[:300], scope=scope)
        summary["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
        return summary

    def status(self) -> Dict[str, Any]:
        with self.ledger._reading() as conn:
            total = int(conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM memories "
                "WHERE raw_chat=1 AND session_id!=''").fetchone()[0])
            digested = int(conn.execute(
                "SELECT COUNT(*) FROM source_cursors WHERE source_id LIKE 'digest:%'").fetchone()[0])
            pending = len(self.pending_sessions(limit=500))
        return {"sessions": total, "digested": digested, "pending": pending,
                "extractor": getattr(self.extractor, "name", "heuristic"),
                "available": bool(getattr(self.extractor, "available", False))}
