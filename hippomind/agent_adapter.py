"""Generic lifecycle adapter shared by Codex, Pi and other local agents.

It intentionally has no model SDK dependency. An agent calls ``begin_task``
to obtain a compact untrusted Context Pack, writes explicit checkpoints as it
works, and records external feedback only after a real result is known.

Beyond the plain lifecycle, this adapter is the *write path* for three
capabilities the design document advertises but that used to be dead code on a
live 1.5M-row ledger (``memory_claims`` = 0 rows, ``memory_procedural`` = 0
rows, ``memory_feedback`` = 0 rows):

* slot-value claims are derived from explicit decisions/preferences (and from
  explicit ``subject/predicate/slot/value`` arguments), so contradictory
  assertions can actually collide and be resolved;
* ``skill`` / ``distill_skill`` turn a successful result plus its causal
  ancestor chain into a declarative procedural card;
* ``credit_outcome`` (and ``record(kind="result", outcome=...)``) spread a
  bounded success/failure signal back onto the causal ancestors of the result.

Derivation is deliberately conservative: ambiguous, untrusted, low-confidence
or free-text material derives nothing, and credit is only ever assigned from a
real result — never from the fact that a memory happened to be recalled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .context_pack import DEFAULT_MAX_CHARS, ContextPackBuilder, ContextPackOptions, as_untrusted_prompt
from .ledger import IngestResult, LedgerError, SQLiteLedger

#: Only these kinds are assertions that can collide on a slot.  Progress,
#: result, blocker, next_step and procedural rows are流水记录 (event stream
#: entries), not knowledge that another record can contradict.
CLAIM_KINDS = frozenset({"decision", "preference", "fact"})
#: Context field that carries the single value for each derived claim kind.
CLAIM_CONTEXT_FIELDS = {"decision": "key_decisions", "preference": "preferences"}
#: Derived claim text longer than this is not asserted: a 400-character
#: sentence is prose, not a slot value, and would only create false conflicts.
MAX_DERIVED_CLAIM_CHARS = 400
#: Below this confidence a record must not manufacture a slot conflict.
MIN_DERIVED_CLAIM_CONFIDENCE = 0.3
#: Sentinel distinguishing "value not supplied" from an explicit ``None``.
_MISSING = object()

#: A single outcome may move a target's utility by at most this much.  The
#: ledger's ``feedback`` already uses 0.8/0.2 smoothing; the bound here keeps a
#: single success/failure from dominating accumulated history.
OUTCOME_SIGNAL = 0.3
#: Only records produced this recently can receive credit for a result.
OUTCOME_WINDOW_HOURS = 24
#: Hard cap on credited ancestors per result.
MAX_CREDIT_TARGETS = 8
#: Skill steps/assertions are declarative one-liners, not essays.
MAX_SKILL_TEXT_CHARS = 400

_SUCCESS_WORDS = frozenset({
    "success", "succeeded", "ok", "pass", "passed", "done",
    "成功", "完成", "通过",
})
_FAILURE_WORDS = frozenset({
    "failure", "failed", "error", "fail", "失败", "错误", "回滚",
})
_CLAIM_PUNCTUATION = "。.;；!！?？,，"
_OUTCOME_PUNCTUATION = "。.;；!！?？,，:：、 \t\r\n"

_SKILL_STEP_KINDS = frozenset({"progress", "decision", "next_step"})
_ANCESTOR_CREDIT_KINDS = frozenset({"progress", "decision", "blocker"})
_FALLBACK_CREDIT_KINDS = frozenset({"progress", "decision"})


# ---------------------------------------------------------------------------
# Small, local normalisation helpers.  The adapter must not import private
# ledger helpers: they are implementation details of the storage layer and
# this module is the only allowed write-path surface.
# ---------------------------------------------------------------------------
def _clip(value: object, limit: int = 256) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _normalise_kind(value: object) -> str:
    """Mirror ``ledger._safe_kind`` so derivation matches what gets stored."""
    text = _clip(value, 64).lower().replace(" ", "_")
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-"})
    return cleaned[:64] or "fact"


def _normalise_claim_text(value: object) -> str:
    """Collapse whitespace and strip trailing sentence punctuation.

    ``"采用 SQLite。"`` and ``"采用 SQLite"`` must hash to the same slot value,
    otherwise every re-statement looks like a conflict.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split()).strip()
    return text.strip(_CLAIM_PUNCTUATION).strip()


def _normalise_claim_value(value: object) -> object:
    """Only string values are normalised; JSON values pass through untouched."""
    if isinstance(value, str):
        return _normalise_claim_text(value)
    return value


def _outcome_signal(value: object) -> int:
    """Map an explicit success/failure marker to +1/-1; anything else is 0.

    Booleans are accepted because ``metadata={"success": True}`` is the natural
    way to report a result; numeric status codes are *not* guessed at (an HTTP
    200 must not silently become a success signal).
    """
    if isinstance(value, bool):
        return 1 if value else -1
    if not isinstance(value, str):
        return 0
    text = value.strip(_OUTCOME_PUNCTUATION).lower()
    if not text:
        return 0
    if text in _SUCCESS_WORDS:
        return 1
    if text in _FAILURE_WORDS:
        return -1
    return 0


def _metadata_outcome_signal(metadata: Mapping[str, Any]) -> int:
    for key in ("outcome", "success", "status"):
        if key not in metadata:
            continue
        signal = _outcome_signal(metadata.get(key))
        if signal:
            return signal
    return 0


def _parse_timestamp(value: object) -> Optional[datetime]:
    text = _clip(value, 80)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _coerce_int(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number


def _as_text_list(value: object) -> List[Any]:
    """Preserve caller shape; the ledger validates item types."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)  # type: ignore[arg-type]
    except TypeError:
        return [value]


@dataclass
class AgentIdentity:
    source_agent: str
    session_id: str = ""
    task_id: str = ""
    project: str = ""
    scope: str = "shared"


class MemoryHubAgentAdapter:
    """Small, explicit lifecycle facade around the shared local ledger."""

    def __init__(self, source_agent: str, *, session_id: str = "", task_id: str = "",
                 project: str = "", scope: str = "shared", ledger: Optional[SQLiteLedger] = None,
                 ledger_path: str = ""):
        source_agent = str(source_agent or "").strip()
        if not source_agent:
            raise LedgerError("source_agent 不能为空")
        self.identity = AgentIdentity(source_agent=source_agent, session_id=str(session_id or "")[:256],
                                      task_id=str(task_id or "")[:256], project=str(project or "")[:512],
                                      scope=str(scope or "shared")[:128] or "shared")
        self.ledger = ledger or SQLiteLedger(ledger_path or None)
        self.contexts = ContextPackBuilder(self.ledger)
        #: Most recent record written through this adapter.  Used to link the
        #: next record as its causal child, which is what populates
        #: ``memory_relations`` — that table was measured at 0 rows on a live
        #: 1.5M-row ledger, so the whole causal-explanation feature was dead code.
        self._last_memory_id: str = ""

    def begin_task(self, query: str, *, max_items: int = 8, max_chars: int = DEFAULT_MAX_CHARS,
                   source_agents: Sequence[str] = (), debug: bool = False) -> Dict[str, Any]:
        """Read only the minimum evidence relevant to the current task."""
        return self.contexts.build(query, ContextPackOptions(
            max_items=max_items, max_chars=max_chars, raw_chat=False, require_evidence=True,
            scope=self.identity.scope, task_id=self.identity.task_id, project=self.identity.project,
            source_agents=source_agents, debug=debug,
        ))

    def untrusted_context_prompt(self, query: str) -> str:
        """Return a safe wrapper for a caller that explicitly feeds an LLM."""
        return as_untrusted_prompt(self.begin_task(query))

    # ------------------------------------------------------------------
    # Claim derivation (dead code no longer: memory_claims was 0 rows)
    # ------------------------------------------------------------------
    def _claim_subject(self, kind: str, explicit_subject: object) -> str:
        """Resolve the claim subject from the identity, most specific first.

        A decision is only asserted within its own task: without a task id the
        same project could hold two unrelated decisions that would then be
        reported as a conflict that never happened.
        """
        subject = _clip(explicit_subject, 256)
        if subject:
            return subject
        task_id = _clip(self.identity.task_id, 256)
        project = _clip(self.identity.project, 512)
        if kind == "decision":
            return task_id
        if kind == "preference":
            return project or task_id
        return task_id or project

    @staticmethod
    def _single_context_value(context: object, field: str) -> Optional[str]:
        """Return the one distinct value for ``field``.

        ``None`` means "ambiguous, do not derive"; ``""`` means "field absent,
        caller may fall back to the record content".
        """
        if not isinstance(context, Mapping):
            return ""
        if field not in context:
            return ""
        raw = context.get(field)
        if raw is None:
            return ""
        if isinstance(raw, str):
            candidates: List[object] = [raw]
        elif isinstance(raw, (list, tuple)):
            candidates = list(raw)
        else:
            # A nested object cannot be interpreted as one slot value.
            return None
        distinct: List[str] = []
        for item in candidates:
            if not isinstance(item, str):
                return None
            text = _normalise_claim_text(item)
            if text and text not in distinct:
                distinct.append(text)
        if len(distinct) > 1:
            return None
        return distinct[0] if distinct else ""

    def _derive_claims(self, *, kind: str, content: str, context: object,
                       metadata: Mapping[str, Any], confidence: float,
                       claims: Optional[List[Any]], subject: object,
                       predicate: object, slot: object, value: object,
                       derive_claims: bool) -> Optional[List[Any]]:
        """Build at most one conservative claim for this record.

        Ordering matters: explicit ``claims`` always win (pass-through), then
        the derivation gates, then the explicit slot/value path, then the
        kind-specific context/content path.  ``fact`` never auto-derives from
        free text — a sentence is not a single-slot assertion.
        """
        if claims is not None:
            return claims
        if not derive_claims:
            return None
        if metadata.get("derived_from_raw_chat") or metadata.get("untrusted_source"):
            return None
        if confidence < MIN_DERIVED_CLAIM_CONFIDENCE:
            return None
        if kind not in CLAIM_KINDS:
            return None

        explicit_slot = _clip(slot, 256)
        predicate_key = _clip(predicate, 256) or kind
        if explicit_slot and value is not _MISSING:
            claim_subject = self._claim_subject(kind, subject)
            if not claim_subject:
                # No subject means no addressable slot; the memory itself is
                # still written, we simply do not invent an assertion.
                return None
            return [{
                "subject": claim_subject,
                "predicate": predicate_key,
                "slot_key": explicit_slot,
                "value": _normalise_claim_value(value),
                "confidence": confidence,
            }]

        if kind == "fact":
            return None
        field = CLAIM_CONTEXT_FIELDS.get(kind, "")
        if not field:
            return None
        claim_subject = self._claim_subject(kind, subject)
        if not claim_subject:
            return None
        value_text = self._single_context_value(context, field)
        if value_text is None:
            return None
        if not value_text:
            value_text = _normalise_claim_text(content)
            if not value_text or len(value_text) > MAX_DERIVED_CLAIM_CHARS:
                return None
        return [{
            "subject": claim_subject,
            "predicate": predicate_key,
            "slot_key": explicit_slot or field,
            "value": value_text,
            "confidence": confidence,
        }]

    # ------------------------------------------------------------------
    # Lifecycle writes
    # ------------------------------------------------------------------
    def record(self, kind: str, content: str, *, evidence: Iterable[str],
               context: Optional[Mapping[str, Any]] = None, tags: Iterable[str] = (),
               role: str = "assistant", metadata: Optional[Mapping[str, Any]] = None,
               importance: int = 5, confidence: float = 0.5,
               claims: Optional[Iterable[Mapping[str, Any]]] = None,
               subject: str = "", predicate: str = "", slot: str = "",
               value: Any = _MISSING, procedural: Optional[Mapping[str, Any]] = None,
               derive_claims: bool = True, outcome: str = "") -> IngestResult:
        """Write one lifecycle record, optionally deriving a slot-value claim.

        ``claims`` is passed straight through to the ledger (explicit wins and
        disables derivation).  ``subject/predicate/slot/value`` address a single
        explicit assertion for claim kinds.  ``procedural`` carries a skill card
        for ``kind="procedural"``.  ``outcome`` on a ``result`` record both marks
        the result and triggers bounded credit assignment to its ancestors.
        """
        data = dict(metadata or {})
        data["lifecycle_adapter"] = self.identity.source_agent
        if outcome:
            data["outcome"] = outcome
        kind_key = _normalise_kind(kind)
        explicit_claims: Optional[List[Any]] = None
        if claims is not None:
            if isinstance(claims, Mapping):
                explicit_claims = [claims]
            else:
                try:
                    explicit_claims = list(claims)  # type: ignore[arg-type]
                except TypeError as exc:
                    raise LedgerError("claims 必须是对象或对象列表") from exc
        confidence_value = _coerce_float(confidence, 0.5)
        derived = self._derive_claims(
            kind=kind_key, content=content, context=context, metadata=data,
            confidence=confidence_value, claims=explicit_claims, subject=subject,
            predicate=predicate, slot=slot, value=value, derive_claims=derive_claims,
        )
        payload: Dict[str, Any] = {
            "kind": kind, "content": content, "source_agent": self.identity.source_agent,
            "session_id": self.identity.session_id, "task_id": self.identity.task_id,
            "project": self.identity.project, "scope": self.identity.scope, "role": role,
            "evidence": list(evidence), "context": dict(context or {}), "tags": list(tags),
            "metadata": data, "importance": importance, "confidence": confidence,
        }
        if derived:
            payload["claims"] = derived
        if procedural is not None:
            payload["procedural"] = procedural
        # Link the new record to the previous one from this adapter, so a
        # progress -> decision -> result sequence carries an explainable causal
        # chain instead of being a bag of unrelated rows.  Bounded to a single
        # parent: a long chain is reconstructible by walking it, and inventing
        # extra edges would make ``causal_ancestors`` expensive for no gain.
        previous = self._last_memory_id
        if previous and not data.get("causal_parents"):
            payload["causal_parents"] = [{"memory_id": previous,
                                           "relation_type": "follows"}]
        result = self.ledger.ingest(payload)
        if result.inserted:
            self._last_memory_id = result.memory_id
        if kind_key == "result":
            signal = (_outcome_signal(outcome) if outcome
                      else _metadata_outcome_signal(data))
            if signal:
                # Credit assignment is an optimisation over already-written
                # evidence: a failure here must never lose the result record.
                try:
                    self.credit_outcome(result.memory_id,
                                        signal=OUTCOME_SIGNAL * signal)
                except LedgerError:
                    pass
        return result

    def checkpoint(self, progress: str, *, evidence: Iterable[str], next_steps: Iterable[str] = ()) -> IngestResult:
        return self.record("progress", progress, evidence=evidence,
                           context={"progress": [progress], "next_steps": list(next_steps)}, importance=6)

    def decision(self, decision: str, *, evidence: Iterable[str]) -> IngestResult:
        return self.record("decision", decision, evidence=evidence,
                           context={"key_decisions": [decision]}, importance=7)

    def preference(self, preference: str, *, evidence: Iterable[str]) -> IngestResult:
        return self.record("preference", preference, evidence=evidence,
                           context={"preferences": [preference]}, importance=6)

    def blocker(self, blocker: str, *, evidence: Iterable[str], next_steps: Iterable[str] = ()) -> IngestResult:
        return self.record("blocker", blocker, evidence=evidence,
                           context={"blockers": [blocker], "next_steps": list(next_steps)}, importance=8)

    def complete(self, result: str, *, evidence: Iterable[str], next_steps: Iterable[str] = (),
                 outcome: str = "") -> IngestResult:
        return self.record("result", result, evidence=evidence,
                           context={"progress": [result], "next_steps": list(next_steps)},
                           importance=7, outcome=outcome)

    def feedback(self, memory_id: str, signal: float, note: str = "") -> bool:
        return self.ledger.feedback(memory_id, signal, note, self.identity.source_agent)

    # ------------------------------------------------------------------
    # Procedural skills (memory_procedural was 0 rows on the live ledger)
    # ------------------------------------------------------------------
    def skill(self, content: str, *, evidence: Iterable[str], trigger_pattern: Any,
              preconditions: Any = (), execution_pipeline: Any = (),
              verification_assertion: Any = (),
              calibration: Optional[Mapping[str, Any]] = None,
              tags: Iterable[str] = (), causal_parents: Optional[Iterable[Any]] = None,
              importance: int = 8, confidence: float = 0.6) -> IngestResult:
        """Write one declarative skill card.  It is stored, never executed.

        Missing ``trigger_pattern`` / ``preconditions`` / ``execution_pipeline``
        / ``verification_assertion`` is rejected by the ledger with
        ``LedgerError`` — that is the intended contract, not a bug to hide.
        """
        procedural = {
            "trigger_pattern": _as_text_list(trigger_pattern),
            "preconditions": _as_text_list(preconditions),
            "execution_pipeline": _as_text_list(execution_pipeline),
            "verification_assertion": _as_text_list(verification_assertion),
            "calibration_score": dict(calibration or {}),
        }
        metadata: Dict[str, Any] = {}
        if causal_parents:
            metadata["causal_parents"] = list(causal_parents)
        return self.record("procedural", content, evidence=evidence, tags=tags,
                           metadata=metadata, procedural=procedural,
                           importance=importance, confidence=confidence)

    def distill_skill(self, result_memory_id: str = "", *, outcome: str = "",
                      title: str = "", max_ancestors: int = 8,
                      tags: Iterable[str] = (), importance: int = 8,
                      confidence: float = 0.6) -> IngestResult:
        """Distil a skill from one successful result and its causal ancestors.

        The card is declarative only: this method never executes any step.  It
        refuses failed results, non-result memories and results whose causal
        chain contains no progress/decision/next_step ancestor (a result with
        no walkable history teaches nothing).
        """
        result_id = _clip(result_memory_id, 80) or self._last_memory_id
        if not result_id:
            raise LedgerError("distill_skill 需要一条 result 记忆")
        result = self.ledger.get_memory(result_id)
        if result is None:
            raise LedgerError(f"未找到结果记忆: {result_id}")
        if _normalise_kind(result.get("kind")) != "result":
            raise LedgerError("distill_skill 只能从 kind='result' 的记忆提炼技能")
        result_metadata = result.get("metadata")
        if not isinstance(result_metadata, Mapping):
            result_metadata = {}
        outcome_value: object = outcome if outcome else result_metadata.get("outcome")
        if _outcome_signal(outcome_value) != 1:
            raise LedgerError("distill_skill 只从成功结果提炼技能（outcome 必须为 success）")

        ancestor_limit = max(1, min(_coerce_int(max_ancestors, 8), 32))
        scope = _clip(result.get("scope"), 128) or self.identity.scope
        ancestors = self.ledger.causal_ancestors(
            [result_id], max_depth=2, limit=ancestor_limit, scope=scope)
        steps = sorted(
            (row for row in ancestors
             if _normalise_kind(row.get("kind")) in _SKILL_STEP_KINDS),
            key=lambda row: (_clip(row.get("created_at"), 80),
                             _clip(row.get("memory_id"), 80)),
        )
        blockers = [row for row in ancestors
                    if _normalise_kind(row.get("kind")) == "blocker"]
        if not steps:
            raise LedgerError("没有可提炼的因果步骤：result 的祖先链里没有 progress/decision/next_step")

        task_id = _clip(result.get("task_id"), 256) or self.identity.task_id
        first_step = _clip(steps[0].get("content"), MAX_SKILL_TEXT_CHARS)
        trigger = (f"任务 {task_id}：{first_step}" if task_id
                   else f"再次出现：{first_step}")
        preconditions = [_clip(row.get("content"), MAX_SKILL_TEXT_CHARS)
                         for row in blockers]
        preconditions = [item for item in preconditions if item]
        preconditions.append("来源记忆与因果链仍可在账本中检索到")
        pipeline = [{"action": _clip(row.get("content"), MAX_SKILL_TEXT_CHARS),
                     "verify": ""} for row in steps]
        assertions = [_clip(result.get("content"), MAX_SKILL_TEXT_CHARS)]
        parents: List[Dict[str, Any]] = [
            {"memory_id": result_id, "relation_type": "derived_from"}]
        parents.extend(
            {"memory_id": _clip(row.get("memory_id"), 80),
             "relation_type": "derived_from"}
            for row in steps[:ancestor_limit - 1]
            if _clip(row.get("memory_id"), 80)
        )
        evidence = [f"memory:{result_id}"]
        evidence.extend(f"memory:{_clip(row.get('memory_id'), 80)}"
                        for row in steps[:3] if _clip(row.get("memory_id"), 80))
        label = _clip(title, 2_000) or _clip(result.get("content"), 200)
        content = (f"技能（任务 {task_id}）：{label}" if task_id
                   else f"技能：{label}")
        return self.skill(
            content, evidence=evidence, trigger_pattern=[trigger],
            preconditions=preconditions, execution_pipeline=pipeline,
            verification_assertion=assertions,
            calibration={"attempts": 1, "successes": 1, "rate": 1.0},
            tags=tags, causal_parents=parents,
            importance=importance, confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Result-driven credit assignment (memory_feedback was 0 rows)
    # ------------------------------------------------------------------
    @staticmethod
    def _credit_magnitude(signal: Optional[float], outcome: str) -> float:
        if signal is not None:
            if isinstance(signal, bool):
                raise LedgerError("signal 必须是数字")
            magnitude = _coerce_float(signal, float("nan"))
            if not math.isfinite(magnitude):
                raise LedgerError("signal 必须是有限数字")
            return max(-OUTCOME_SIGNAL, min(OUTCOME_SIGNAL, magnitude))
        if outcome:
            direction = _outcome_signal(outcome)
            if not direction:
                raise LedgerError(f"无法从 outcome 判定信用方向: {outcome!r}")
            return OUTCOME_SIGNAL * direction
        raise LedgerError("credit_outcome 需要显式 signal 或 success/failure outcome")

    def _credit_candidates(self, result: Mapping[str, Any], *, limit: int,
                           window_hours: float) -> List[str]:
        result_id = _clip(result.get("memory_id"), 80)
        task_id = _clip(result.get("task_id"), 256) or _clip(self.identity.task_id, 256)
        if not task_id:
            # Without a task boundary the "same task" contract cannot hold;
            # crediting anything here would leak credit across unrelated work.
            return []
        scope = _clip(result.get("scope"), 128) or self.identity.scope
        rows = [row for row in self.ledger.causal_ancestors(
            [result_id], max_depth=2, limit=max(limit * 2, 16), scope=scope)
            if _normalise_kind(row.get("kind")) in _ANCESTOR_CREDIT_KINDS]
        if not rows:
            # No causal chain (e.g. the result was the first record): fall back
            # to recent progress/decision rows of the *same* task only.
            rows = [row for row in self.ledger.search(
                "", scope=scope, task_id=task_id, limit=max(limit * 4, 16))
                if _normalise_kind(row.get("kind")) in _FALLBACK_CREDIT_KINDS]
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        credited: List[str] = []
        seen: set = set()
        for row in rows:
            memory_id = _clip(row.get("memory_id"), 80)
            if not memory_id or memory_id == result_id or memory_id in seen:
                continue
            if _clip(row.get("task_id"), 256) != task_id:
                continue
            created = _parse_timestamp(row.get("created_at", ""))
            if created is None or created < cutoff:
                continue
            seen.add(memory_id)
            credited.append(memory_id)
            if len(credited) >= limit:
                break
        return credited

    def credit_outcome(self, result_memory_id: str = "", *, outcome: str = "",
                       signal: Optional[float] = None,
                       window_hours: float = OUTCOME_WINDOW_HOURS,
                       limit: int = MAX_CREDIT_TARGETS,
                       note: str = "") -> Dict[str, Any]:
        """Spread one bounded success/failure signal onto a result's ancestors.

        ``feedback_id`` is derived deterministically from result, direction and
        target, so calling this twice is idempotent: the ledger de-duplicates by
        content hash and the second call reports ``applied=0``.  Only real
        results can assign credit — being recalled is never a signal.
        """
        result_id = _clip(result_memory_id, 80) or self._last_memory_id
        if not result_id:
            raise LedgerError("credit_outcome 需要一条 result 记忆")
        result = self.ledger.get_memory(result_id)
        if result is None:
            raise LedgerError(f"未找到结果记忆: {result_id}")
        if _normalise_kind(result.get("kind")) != "result":
            raise LedgerError("只有 kind='result' 的记忆可以分配信用")
        magnitude = self._credit_magnitude(signal, outcome)
        target_limit = max(1, min(_coerce_int(limit, MAX_CREDIT_TARGETS),
                                  MAX_CREDIT_TARGETS))
        window = max(0.0, min(_coerce_float(window_hours, OUTCOME_WINDOW_HOURS),
                              24.0 * 365.0))
        credited: List[str] = []
        skipped = 0
        if magnitude:
            note_text = _clip(note, 4_000) or f"auto-credit:{result_id}"
            direction = "up" if magnitude > 0 else "down"
            for candidate_id in self._credit_candidates(
                    result, limit=target_limit, window_hours=window):
                applied = self.ledger.feedback(
                    candidate_id, magnitude, note_text, self.identity.source_agent,
                    feedback_id=f"auto-{direction}-{result_id}-{candidate_id}")
                if applied:
                    credited.append(candidate_id)
                else:
                    skipped += 1
        return {"result": result_id, "signal": magnitude,
                "applied": len(credited), "skipped": skipped,
                "credited": credited}
