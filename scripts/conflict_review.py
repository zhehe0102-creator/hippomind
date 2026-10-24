#!/usr/bin/env python3
"""Conflict review: adjudicate ``memory_claims`` conflicts with an audit trail.

Why this exists
---------------
``ingest`` parks two contradictory claims on a slot in ``state='conflict'``
whenever neither side wins by confidence margin (see
``identity._insert_claims``).  Those rows are invisible to recall: neither side
is ``accepted``+``active``, so the fact they disagree about disappears from
Context Pack.  On the production ledger that left 36 claims parked with no way
out except a hand-typed ``hippomind resolve``.

What the queue actually contains
--------------------------------
Inspecting the production queue showed that a "conflict" is almost never a
real disagreement.  The inventory stage therefore classifies each group by
root cause *before* any decider runs, because the correct action differs:

  ``stale``        the competing memory is already superseded/expired - an
                   earlier ruling settled it, the claim just never converged.
  ``orphan``       the slot has no live competitor at all.
  ``duplicate``    both sides carry the same ``value_hash``; the conflict is an
                   artifact of ingest scoring, not of meaning.
  ``truncation``   one value is a truncated prefix of the other, so the
                   "disagreement" is an extraction defect.
  ``genuine``      two different values that are not prefix-related.

Only ``genuine`` groups are worth asking a model about.  That keeps the model
call count small, which matters because the local CLI is slow and rate-limited.

Three separable stages, so each can be inspected and reverted:

1. **inventory** - group conflict claims by
   ``(scope, subject, predicate, slot_key)``, classify the root cause, and
   render both sides with their evidence.  ``--json`` emits the same data.
2. **adjudicate** - decide ``supersede_a`` / ``supersede_b`` / ``keep_both`` /
   ``needs_human``.  Two deciders, and every verdict records which one spoke:

   * ``--decider llm`` - the model endpoint, reached directly over HTTP via
     ``hippomind/llm_client.py`` (the same transport ``hippomind/distill.py``
     uses; the local Claude Code CLI remains a fallback), given only the two
     evidence records.
   * ``--decider rules`` - a deterministic, offline policy (the fallback, and
     what the tests exercise).

   A decider never writes; it only returns a verdict.
3. **apply** - execute through the slot-scoped ledger methods and persist the
   reasoning into ``memory_claim_reviews``.

Why not just call ``ledger.resolve_claim``?
-------------------------------------------
``resolve_claim`` is *memory-scoped*: it demotes every claim of the losing
memory.  Measured on the production ledger, that would have silently removed up
to five unrelated active facts per ruling (one memory held 4 other facts,
another 5).  So apply uses the slot-scoped helpers added to
``hippomind/ledger/lifecycle.py``:

  ``settle_claim_slot``   crown a winner for one slot only.
  ``release_claim_slot``  keep both sides (nothing retired, both stay recallable).

``resolve_claim`` and ``repair_claim_states`` are unchanged.

Auditability
------------
Nothing is deleted.  Every verdict is one row in ``memory_claim_reviews``: the
slot, both claim ids, both value hashes, the verdict, the confidence, the
free-text reason, the decider, the model identity, and a digest of the exact
evidence the decider saw.  The ledger additionally writes a ``resolve`` /
``release`` audit event, so the two trails cross-check.

``--dry-run`` is the default: it prints every verdict and writes nothing.
``--apply`` is required to mutate the ledger.

Usage
-----
    python3 scripts/conflict_review.py                     # inventory + dry run
    python3 scripts/conflict_review.py --json              # machine readable
    python3 scripts/conflict_review.py --decider rules --apply
    python3 scripts/conflict_review.py --decider llm --apply --limit 10
    python3 scripts/conflict_review.py --show-reviews      # audit trail

Exit codes: 0 success (including "nothing to do"), 2 on an unreadable ledger or
a write that failed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import llm_client  # noqa: E402
from hippomind.ledger import LedgerError, SQLiteLedger, utc_now  # noqa: E402

DEFAULT_LEDGER = os.path.expanduser(
    "~/Library/Application Support/HippoMind/hippomind.sqlite")

#: The four verdicts the prompt and the rules engine may emit.
VERDICTS = ("supersede_a", "supersede_b", "keep_both", "needs_human")

#: Root-cause classes, most certain first.  Ordering is also the resolution
#: order: a stale or duplicate group needs no model opinion.
CAUSES = ("stale", "orphan", "duplicate", "truncation", "genuine")

#: Only ``genuine`` groups are sent to a model.
MODEL_WORTHY = frozenset({"genuine"})

#: A supersede verdict must reach this confidence to be applied automatically.
#: Below it the group is escalated to needs_human instead of silently changing
#: recall.  Deliberately high: a wrong supersede removes a fact from recall,
#: which is worse than leaving a conflict visible for review.
DEFAULT_MIN_CONFIDENCE = 0.6

#: How many characters of source memory a decider sees.
MAX_EVIDENCE_CHARS = 700

#: Suffixes distill appends when it truncated an extraction.  Stripped before
#: the prefix test so a truncated value is recognised as the same fact.
TRUNCATION_MARKERS = ("（片段截断，完整范围未知）", "（片段截断）",
                      " (truncated)", "...(truncated)")

#: Circuit breaker, modelled on ``distill.ClaudeExtractor``: after this many
#: consecutive failures the decider stops calling out for ``COOLDOWN_SECONDS``
#: and every remaining group falls through to the rules engine.  Without it a
#: dead API turns a review run into one timeout per group.
BREAKER_THRESHOLD = 2
COOLDOWN_SECONDS = 600.0
DEFAULT_TIMEOUT = 60.0
#: Output budget for one verdict.  The answer is a four-field JSON object with a
#: one-sentence reason, so this is deliberately generous: a truncated object is
#: an unparseable verdict, which costs a model call *and* falls back to rules.
DECIDE_MAX_TOKENS = 1_024

REVIEW_TABLE = "memory_claim_reviews"

REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_claim_reviews (
    review_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    cause TEXT NOT NULL DEFAULT '',
    claim_a TEXT NOT NULL,
    claim_b TEXT NOT NULL,
    memory_a TEXT NOT NULL,
    memory_b TEXT NOT NULL,
    value_hash_a TEXT NOT NULL,
    value_hash_b TEXT NOT NULL,
    verdict TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    decider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    evidence_digest TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_reviews_slot
    ON memory_claim_reviews(scope, subject, predicate, slot_key, created_at);
"""


# ---------------------------------------------------------------------------
# Evidence model
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Side:
    """One claim in a conflict, plus the memory that asserted it."""

    claim_id: str
    memory_id: str
    value: Any
    value_hash: str
    confidence: float
    source_trust: float
    asserted_at: str
    state: str
    memory_quality: str
    memory_lifecycle: str
    memory_created_at: str
    source_agent: str
    content: str

    @property
    def score(self) -> float:
        return float(self.confidence) * float(self.source_trust)

    @property
    def recallable(self) -> bool:
        return (self.memory_lifecycle == "active"
                and self.memory_quality in ("accepted", "conflict"))

    def as_evidence(self) -> Dict[str, Any]:
        """The exact projection handed to a decider (and digested)."""
        return {
            "claim_id": self.claim_id,
            "value": self.value,
            "confidence": round(float(self.confidence), 4),
            "source_trust": round(float(self.source_trust), 4),
            "asserted_at": self.asserted_at,
            "claim_state": self.state,
            "memory_state": "%s/%s" % (self.memory_quality, self.memory_lifecycle),
            "source_agent": self.source_agent,
            "excerpt": _excerpt(self.content, MAX_EVIDENCE_CHARS),
        }


@dataclass
class Group:
    """All live claims competing for one slot."""

    scope: str
    subject: str
    predicate: str
    slot_key: str
    sides: List[Side] = field(default_factory=list)
    cause: str = ""

    @property
    def conflict_sides(self) -> List[Side]:
        return [s for s in self.sides if s.state == "conflict"]

    @property
    def label(self) -> str:
        return "%s | %s | %s" % (self.subject, self.predicate, self.slot_key)

    def ranked(self) -> List[Side]:
        """Deterministic order: strongest evidence first, then newest."""
        return sorted(self.sides, key=lambda s: (s.score, s.asserted_at),
                      reverse=True)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "scope": self.scope,
            "subject": self.subject,
            "predicate": self.predicate,
            "slot_key": self.slot_key,
            "label": self.label,
            "cause": self.cause,
            "sides": [s.as_evidence() for s in self.ranked()],
        }


@dataclass
class Verdict:
    """A decider answer, before anything is written."""

    verdict: str
    confidence: float
    reason: str
    decider: str
    model: str = ""
    #: claim id the verdict would crown (empty when nothing should change).
    winner: str = ""
    #: claim ids the verdict would retire (empty for keep_both/needs_human).
    retire: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "confidence": round(self.confidence, 4),
                "reason": self.reason, "decider": self.decider,
                "model": self.model, "winner": self.winner,
                "retire": list(self.retire)}


def _excerpt(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def _evidence_digest(payload: Mapping[str, Any]) -> str:
    """Stable digest of exactly what a decider was shown."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _strip_truncation(value: Any) -> Any:
    """Remove distill's truncation suffix so prefixes compare cleanly."""
    if not isinstance(value, str):
        return value
    out = value
    for marker in TRUNCATION_MARKERS:
        out = out.replace(marker, "")
    return out.strip()


def classify(group: Group) -> str:
    """Assign a root cause to a group.  Pure, deterministic, no I/O.

    The order matters: a stale side is dead regardless of its value, and a
    duplicate pair never needs a semantic judgement.
    """
    live = [s for s in group.sides if s.state in ("active", "conflict")]
    if not live:
        return "orphan"
    if all(not s.recallable for s in live):
        # Every side is out of recall: an earlier ruling already settled this,
        # only the claim state failed to converge.
        return "stale"
    if len(live) < 2:
        return "orphan"
    if len({s.value_hash for s in live}) == 1:
        return "duplicate"
    # Compare *distinct* values, not the top two sides.  A slot can hold three
    # or more sides, and the strongest two are frequently identical (two
    # memories asserting the same thing while a third differs) - looking only
    # at the top two classified such a group as a real disagreement when the
    # disagreement is entirely between a duplicate pair and one outlier.
    distinct: List[Any] = []
    for side in sorted(live, key=lambda s: (s.score, s.asserted_at),
                       reverse=True):
        value = _strip_truncation(side.value)
        if value not in distinct:
            distinct.append(value)
    if len(distinct) == 1:
        return "duplicate"
    if len(distinct) == 2:
        a, b = distinct
        if isinstance(a, str) and isinstance(b, str) and a and b:
            if a in b or b in a:
                return "truncation"
    return "genuine"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
_SIDE_QUERY = """
SELECT c.claim_id, c.memory_id, c.scope, c.subject, c.predicate, c.slot_key,
       c.value_json, c.value_hash, c.confidence, c.source_trust, c.asserted_at,
       c.state, m.quality_state, m.lifecycle,
       m.created_at AS memory_created_at, m.source_agent, m.content
FROM memory_claims c
JOIN memories m ON m.memory_id = c.memory_id
WHERE c.scope = ?
"""


def _row_to_side(row: Mapping[str, Any]) -> Side:
    try:
        value = json.loads(row["value_json"] or "null")
    except (TypeError, ValueError):
        value = row["value_json"]
    return Side(
        claim_id=str(row["claim_id"]), memory_id=str(row["memory_id"]),
        value=value, value_hash=str(row["value_hash"]),
        confidence=float(row["confidence"]), source_trust=float(row["source_trust"]),
        asserted_at=str(row["asserted_at"]), state=str(row["state"]),
        memory_quality=str(row["quality_state"]),
        memory_lifecycle=str(row["lifecycle"]),
        memory_created_at=str(row["memory_created_at"]),
        source_agent=str(row["source_agent"]), content=str(row["content"]),
    )


def collect_groups(ledger: SQLiteLedger, *, scope: str = "shared",
                   include_superseded: bool = False) -> List[Group]:
    """Return every slot holding a ``conflict`` claim, classified by cause.

    A group holds the live competition only (``active`` + ``conflict``);
    superseded rows are history and would only mislead a decider.
    ``include_superseded`` adds them back for display.  A group with a single
    live side is still returned: it is an orphaned conflict and needs to be
    released, not adjudicated.
    """
    with ledger._reading() as conn:  # noqa: SLF001 - read-only audit helper
        rows = conn.execute(_SIDE_QUERY, (scope,)).fetchall()
    buckets: Dict[Tuple[str, str, str, str], Group] = {}
    for row in rows:
        key = (str(row["scope"]), str(row["subject"]), str(row["predicate"]),
               str(row["slot_key"]))
        group = buckets.get(key)
        if group is None:
            group = Group(scope=key[0], subject=key[1], predicate=key[2],
                          slot_key=key[3])
            buckets[key] = group
        group.sides.append(_row_to_side(row))
    groups: List[Group] = []
    for group in buckets.values():
        if not group.conflict_sides:
            continue
        if not include_superseded:
            group.sides = [s for s in group.sides
                           if s.state in ("active", "conflict")]
        group.cause = classify(group)
        groups.append(group)
    groups.sort(key=lambda g: (CAUSES.index(g.cause), g.subject, g.predicate,
                               g.slot_key))
    return groups


# ---------------------------------------------------------------------------
# Decider 1: deterministic rules
# ---------------------------------------------------------------------------
def rules_decide(group: Group) -> Verdict:
    """Offline policy.  Every branch states the evidence it used.

    The branches follow the root-cause classification, so the common cases
    never need a model:

    1. ``duplicate`` - the two memories agree (same ``value_hash``); the
       conflict is an ingest-scoring artifact.  Retiring the weaker side loses
       nothing, so crown the stronger/newer one.
    2. ``stale`` / ``orphan`` - at most one side is still recallable, so the
       surviving side wins by default; with no survivor at all the slot is
       retired.  Either way an earlier ruling already decided it.
    3. ``truncation`` - the shorter value is a prefix of the longer one, so the
       longer extraction is the complete fact; crown it.
    4. ``genuine`` - two different values.  Keep both: deterministic code
       cannot settle a real-world disagreement, and guessing would remove a
       fact from recall.  Escalate to a human/model instead.
    """
    ranked = group.ranked()
    recallable = [s for s in ranked if s.recallable]

    if group.cause == "duplicate":
        top, second = ranked[0], ranked[1]
        return Verdict(
            "supersede_a", 0.95,
            "两造 value_hash 相同（%s），分歧来自入库打分而非事实冲突；"
            "保留证据更强的一方（confidence x trust=%0.3f vs %0.3f）"
            % (top.value_hash[:12], top.score, second.score),
            "rules", winner=top.claim_id, retire=[second.claim_id])

    if group.cause == "truncation":
        longer = max(recallable or ranked,
                     key=lambda s: len(str(_strip_truncation(s.value))))
        return Verdict(
            "supersede_a", 0.85,
            "取值是截断关系（短的那条是长条的前缀），保留完整提取的一方 "
            "memory=%s" % longer.memory_id,
            "rules", winner=longer.claim_id,
            retire=[s.claim_id for s in ranked if s.claim_id != longer.claim_id])

    if group.cause in ("stale", "orphan"):
        if len(recallable) == 1:
            survivor = recallable[0]
            return Verdict(
                "supersede_a", 0.9,
                "只有一方仍可召回（memory=%s），其余对手已下线或不存在；"
                "这是历史裁决的收敛，不是新判断" % survivor.memory_id,
                "rules", winner=survivor.claim_id,
                retire=[s.claim_id for s in ranked
                        if s.claim_id != survivor.claim_id])
        if not recallable:
            return Verdict(
                "keep_both", 0.9,
                "该 slot 两侧都已不可召回（记忆被 superseded/expired），"
                "这类 claim 本来就不可能被召回；账本会在决策时把它们记为"
                "superseded 以清空队列，不新建任何召回",
                "rules")
        # Several sides still recallable but the cause was not duplicate: fall
        # through to the genuine branch rather than guessing.
        group.cause = "genuine"

    if len(ranked) < 2:
        only = ranked[0] if ranked else None
        if only is None:
            return Verdict("needs_human", 0.0, "该 slot 已无活跃 claim", "rules")
        return Verdict(
            "keep_both", 0.9,
            "唯一活跃 claim，释放回 active 即可；memory=%s" % only.memory_id,
            "rules", winner=only.claim_id)

    top, second = ranked[0], ranked[1]
    return Verdict(
        "needs_human", 0.5,
        "两造取值不同（%s vs %s）且证据接近（score %0.3f vs %0.3f），"
        "确定性规则无法裁决，保留冲突等待人工/强模型判断"
        % (_excerpt(json.dumps(top.value, ensure_ascii=False), 60),
           _excerpt(json.dumps(second.value, ensure_ascii=False), 60),
           top.score, second.score),
        "rules")


# ---------------------------------------------------------------------------
# Decider 2: local Claude CLI
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = """你是 HippoMind 记忆系统的冲突裁决器。只依据下面给出的两条证据判断，不要猜测证据之外的事实。

同一个记忆槽位（subject=%s, predicate=%s, slot_key=%s）上有两条互相冲突的断言：

[证据 A]
claim_id: %s
取值: %s
置信度: %s
来源可信度: %s
断言时间: %s
来源记忆摘录: %s

[证据 B]
claim_id: %s
取值: %s
置信度: %s
来源可信度: %s
断言时间: %s
来源记忆摘录: %s

请从下列四个裁决中选择一个：
- supersede_a：A 成立，B 作废（两者不可能同时为真，且 A 更可信）
- supersede_b：B 成立，A 作废
- keep_both：两者可以同时为真（例如不同项目/不同时间的并列事实），都应保留
- needs_human：证据不足以判断，需要人工复核

判断准则：
1. 取值实质相同（措辞差异不算不同）→ 保留更可信的一条，另一条作废。
2. 取值互斥（同一事物的两种不同说法）→ 选更可信/更晚的一条；若无法区分则 needs_human。
3. 两条描述不同方面或不同对象（不是同一事物的两种说法）→ keep_both。
4. 只判断本槽位，不要因为「看起来像同一个项目」就合并其它槽位的语义。

只输出 JSON，不要任何解释或 Markdown 代码块：
{"verdict":"supersede_a|supersede_b|keep_both|needs_human","confidence":0.0,"reason":"一句话理由，必须引用上面的证据"}"""


class ClaudeDecider:
    """Model decider: direct API first, the local CLI as a fallback.

    The transport order mirrors :class:`hippomind.distill.ClaudeExtractor`, and
    for the same measured reason: on this host ``claude -p`` hangs (its default
    model is missing from the installed version's catalogue) while the endpoint
    behind it answers in seconds.  Asking the API first is what makes
    ``--decider llm` actually consult a model instead of silently degrading to
    the rules engine.

    ``available`` turns False after ``BREAKER_THRESHOLD`` consecutive failures
    and stays False for ``COOLDOWN_SECONDS``; callers then fall back to
    ``rules_decide`` for the rest of the run.  ``timeout`` bounds each call so a
    hung transport cannot stall a review session (measured: the local CLI can
    hang indefinitely when its upstream API is unreachable, which is exactly
    what the breaker is for).

    A ``runner`` is an explicit transport: when one is injected it is the *only*
    thing used, so a test that supplies a runner never reaches a socket.
    """

    name = "llm"

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT,
                 binary: str = "",
                 runner: Optional[Callable[[str], Any]] = None,
                 use_api: bool = True,
                 complete: Optional[Callable[..., Any]] = None):
        candidates = [binary, shutil.which("claude") or "",
                      str(Path.home() / ".local" / "bin" / "claude"),
                      "/usr/local/bin/claude", "/opt/homebrew/bin/claude"]
        self.binary = next((p for p in candidates
                            if p and Path(p).is_file()
                            and os.access(p, os.X_OK)), "")
        self.timeout = float(timeout)
        self._failures = 0
        self._cooldown_until = 0.0
        self.last_error = ""
        self.calls = 0
        self.model = ""
        #: Injection seam for tests: called with the prompt, returns raw
        #: stdout.  When set, no subprocess and no socket is ever touched.
        self.runner = runner
        #: Direct-API transport.  Enabled only when the caller did not inject a
        #: runner and a runner is not the point of this instance.
        self.use_api = bool(use_api) and runner is None
        self._complete = complete
        self.transport = ""

    @property
    def available(self) -> bool:
        if time.time() < self._cooldown_until:
            return False
        if self.runner is not None or self.binary:
            return True
        return bool(self.use_api and llm_client.available())

    @property
    def tripped(self) -> bool:
        """True while the breaker is holding the decider offline."""
        return time.time() < self._cooldown_until

    def _record_failure(self, exc: BaseException) -> None:
        self._failures += 1
        self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        if self._failures >= BREAKER_THRESHOLD:
            self._cooldown_until = time.time() + COOLDOWN_SECONDS
            self._failures = 0

    def _record_success(self) -> None:
        self._failures = 0
        self._cooldown_until = 0.0

    def _run(self, prompt: str) -> str:
        """Produce the model's raw answer, or raise (the caller records failure).

        Order: injected runner, then the direct API, then the CLI.  An API miss
        is not fatal on its own -- a host with a working CLI but no endpoint
        configuration still gets a model answer -- but every miss is recorded on
        the breaker so a dead transport stops costing calls.
        """
        if self.runner is not None:
            self.transport = "runner"
            return str(self.runner(prompt) or "")
        if self.use_api:
            caller = self._complete or llm_client.complete
            answer = caller(prompt, max_tokens=DECIDE_MAX_TOKENS,
                            timeout=self.timeout)
            if answer:
                self.transport = "api"
                return answer
            self._record_failure(RuntimeError("api returned no usable answer"))
            if not self.binary or self.tripped:
                raise RuntimeError("api returned no usable answer")
        env = os.environ.copy()
        extra = [str(Path.home() / ".local" / "bin"),
                 "/usr/local/bin", "/opt/homebrew/bin"]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "/usr/bin:/bin")])
        completed = subprocess.run(
            [self.binary, "-p", "--output-format", "text"],
            input=prompt, text=True, capture_output=True,
            timeout=self.timeout, cwd=str(Path.home()), env=env)
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or "")[-300:])
        self.transport = "cli"
        return completed.stdout

    def decide(self, group: Group) -> Optional[Verdict]:
        """Ask the model.  ``None`` means "no usable answer" (caller falls back)."""
        if not self.available:
            return None
        ranked = group.ranked()
        if len(ranked) < 2:
            return None
        a, b = ranked[0], ranked[1]
        prompt = PROMPT_TEMPLATE % (
            group.subject, group.predicate, group.slot_key,
            a.claim_id, _excerpt(json.dumps(a.value, ensure_ascii=False), 200),
            a.confidence, a.source_trust, a.asserted_at,
            _excerpt(a.content, MAX_EVIDENCE_CHARS),
            b.claim_id, _excerpt(json.dumps(b.value, ensure_ascii=False), 200),
            b.confidence, b.source_trust, b.asserted_at,
            _excerpt(b.content, MAX_EVIDENCE_CHARS),
        )
        self.calls += 1
        try:
            text = self._run(prompt)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            self._record_failure(exc)
            return None
        verdict = parse_verdict(text, a=a, b=b, decider=self.name)
        if verdict is None:
            self._record_failure(
                ValueError("unparseable model output: %s" % text[:200]))
            return None
        verdict.model = self.model
        self._record_success()
        return verdict


def parse_verdict(text: str, *, a: Side, b: Side,
                  decider: str = "llm") -> Optional[Verdict]:
    """Parse a decider JSON answer into a :class:`Verdict`.

    Tolerates prose or a Markdown fence around the object (models do both) but
    rejects anything that is not one of the four known verdicts: an
    unrecognised string must never be coerced into a data-destroying action.
    """
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = _excerpt(payload.get("reason", ""), 600)
    winner, retire = "", []
    if verdict == "supersede_a":
        winner, retire = a.claim_id, [b.claim_id]
    elif verdict == "supersede_b":
        winner, retire = b.claim_id, [a.claim_id]
    return Verdict(verdict, confidence, reason or "(模型未给出理由)", decider,
                   winner=winner, retire=retire)


# ---------------------------------------------------------------------------
# Applying a verdict
# ---------------------------------------------------------------------------
def _ensure_review_table(ledger: SQLiteLedger) -> None:
    """Create the audit table on first use.

    Deliberately *not* a schema migration: bumping ``SCHEMA_VERSION`` would make
    every older release refuse the ledger, and this table is optional metadata.
    ``CREATE TABLE IF NOT EXISTS`` is idempotent and cheap.
    """
    def write(conn):
        conn.executescript(REVIEW_SCHEMA)
        return True

    ledger._transaction(write)  # noqa: SLF001 - the audit table is ours


def record_review(ledger: SQLiteLedger, group: Group, verdict: Verdict,
                  *, evidence_digest: str, applied: bool,
                  outcome: str) -> str:
    """Persist one adjudication.  Returns the review id (deterministic).

    ``INSERT OR REPLACE`` on a deterministic id makes a repeated ``--apply``
    idempotent: re-deciding the same slot with the same evidence and the same
    verdict rewrites one row instead of appending a duplicate.
    """
    ranked = group.ranked()
    a = ranked[0]
    b = ranked[1] if len(ranked) > 1 else ranked[0]
    review_id = "cr_" + hashlib.sha256("\x1f".join((
        group.scope, group.subject, group.predicate, group.slot_key,
        a.claim_id, b.claim_id, verdict.verdict,
        evidence_digest)).encode("utf-8")).hexdigest()[:32]
    now = utc_now()

    def write(conn):
        conn.execute("""
            INSERT OR REPLACE INTO memory_claim_reviews(
                review_id, scope, subject, predicate, slot_key, cause,
                claim_a, claim_b, memory_a, memory_b, value_hash_a, value_hash_b,
                verdict, confidence, reason, decider, model, evidence_digest,
                applied, outcome, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (review_id, group.scope, group.subject, group.predicate,
              group.slot_key, group.cause, a.claim_id, b.claim_id,
              a.memory_id, b.memory_id, a.value_hash, b.value_hash,
              verdict.verdict, float(verdict.confidence), verdict.reason,
              verdict.decider, verdict.model, evidence_digest,
              1 if applied else 0, outcome, now))
        return review_id

    ledger._transaction(write)  # noqa: SLF001
    return review_id


def apply_verdict(ledger: SQLiteLedger, group: Group,
                  verdict: Verdict) -> Dict[str, Any]:
    """Execute one verdict.  Never deletes rows; never touches other slots.

    * ``supersede_a`` / ``supersede_b`` -> ``settle_claim_slot`` crowns the
      winner for this slot only, so the loser keeps its unrelated facts.
    * ``keep_both`` -> ``release_claim_slot`` returns both sides to ``active``;
      nothing is retired and both values stay recallable.
    * ``needs_human`` -> no change at all; the conflict stays visible.
    """
    ranked = group.ranked()
    if not ranked:
        return {"applied": False, "outcome": "该 slot 无活跃 claim，跳过"}
    if verdict.verdict in ("supersede_a", "supersede_b"):
        winner = verdict.winner or ranked[0].claim_id
        winner_side = next((s for s in ranked if s.claim_id == winner), None)
        if winner_side is None:
            return {"applied": False, "outcome": "winner_not_in_group"}
        result = ledger.settle_claim_slot(ranked[0].claim_id,
                                          winner_side.memory_id,
                                          source_agent="conflict-review")
        outcome = ("settle winner=%s retired=%s settled=%s"
                   % (result["winner"], ",".join(result["retired"]),
                      result["settled"]))
        return {"applied": bool(result["crowned"] or result["retired"]),
                "outcome": outcome, "settled": result["settled"]}
    if verdict.verdict == "keep_both":
        result = ledger.release_claim_slot(ranked[0].claim_id,
                                           source_agent="conflict-review")
        outcome = ("release released=%s blocked=%s"
                   % (",".join(result["released"]),
                      ",".join(result["blocked"])))
        return {"applied": bool(result["slot_cleared"]), "outcome": outcome}
    return {"applied": False, "outcome": "needs_human：保留冲突，未改动账本"}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass
class ReviewRun:
    """One full review pass: decide everything, then (maybe) write."""

    groups: List[Group] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True
    applied: int = 0
    skipped: int = 0
    errors: int = 0
    decider_used: str = "rules"
    llm_calls: int = 0
    llm_failures: int = 0
    llm_breaker: bool = False
    min_confidence: float = DEFAULT_MIN_CONFIDENCE

    def summary(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for item in self.results:
            counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
        causes: Dict[str, int] = {}
        for group in self.groups:
            causes[group.cause] = causes.get(group.cause, 0) + 1
        return {"groups": len(self.groups), "dry_run": self.dry_run,
                "causes": causes, "verdicts": counts, "applied": self.applied,
                "skipped": self.skipped, "errors": self.errors,
                "decider": self.decider_used, "llm_calls": self.llm_calls,
                "llm_failures": self.llm_failures,
                "llm_breaker": self.llm_breaker,
                "min_confidence": self.min_confidence,
                "results": self.results}


def review(ledger: SQLiteLedger, *, decider: str = "rules",
           apply: bool = False, limit: int = 0, scope: str = "shared",
           min_confidence: float = DEFAULT_MIN_CONFIDENCE,
           only_causes: Optional[Sequence[str]] = None,
           llm: Optional[ClaudeDecider] = None) -> ReviewRun:
    """Inventory -> adjudicate -> (optionally) apply.  Pure orchestration.

    The model is only consulted for ``genuine`` groups (see
    :data:`MODEL_WORTHY`): the other four causes are decided by evidence that
    is already conclusive, so spending a rate-limited model call on them would
    only add latency and a new way to be wrong.
    """
    run = ReviewRun(dry_run=not apply, min_confidence=min_confidence)
    run.groups = collect_groups(ledger, scope=scope)
    if only_causes:
        wanted = {str(c) for c in only_causes}
        run.groups = [g for g in run.groups if g.cause in wanted]
    if limit:
        run.groups = run.groups[:limit]
    if apply:
        _ensure_review_table(ledger)

    model = llm if llm is not None else (
        ClaudeDecider() if decider == "llm" else None)
    run.decider_used = "llm" if decider == "llm" else "rules"

    for group in run.groups:
        evidence = {"slot": group.as_dict(),
                    "sides": [s.as_evidence() for s in group.ranked()]}
        digest = _evidence_digest(evidence)
        verdict: Optional[Verdict] = None
        if decider == "llm" and model is not None and group.cause in MODEL_WORTHY:
            verdict = model.decide(group)
            run.llm_calls = model.calls
            if verdict is None:
                run.llm_failures += 1
        if verdict is None:
            verdict = rules_decide(group)
            if decider == "llm" and group.cause in MODEL_WORTHY:
                verdict.reason = "[LLM 不可用，回退规则] " + verdict.reason
        # A low-confidence supersede is never executed: escalate instead.  This
        # is the guard that keeps a hallucinating model from silently removing
        # a fact from recall.
        if verdict.verdict in ("supersede_a", "supersede_b") and \
                verdict.confidence < min_confidence:
            verdict = Verdict(
                "needs_human", verdict.confidence,
                "裁决置信度 %.2f 低于阈值 %.2f，转人工：%s"
                % (verdict.confidence, min_confidence, verdict.reason),
                verdict.decider, model=verdict.model)

        item = {"slot": group.label, "cause": group.cause,
                "verdict": verdict.verdict,
                "confidence": round(verdict.confidence, 3),
                "reason": verdict.reason, "decider": verdict.decider,
                "evidence_digest": digest,
                "sides": [{"claim_id": s.claim_id, "memory_id": s.memory_id,
                           "value": s.value, "confidence": s.confidence,
                           "source_trust": s.source_trust,
                           "asserted_at": s.asserted_at, "state": s.state,
                           "memory_state": "%s/%s" % (s.memory_quality,
                                                      s.memory_lifecycle)}
                          for s in group.ranked()]}
        if not apply:
            item["applied"] = False
            item["outcome"] = "dry-run：未写库"
            run.skipped += 1
            run.results.append(item)
            continue
        try:
            outcome = apply_verdict(ledger, group, verdict)
        except (LedgerError, ValueError) as exc:
            run.errors += 1
            item["applied"] = False
            item["outcome"] = "失败：%s: %s" % (type(exc).__name__, str(exc)[:200])
            run.results.append(item)
            # A failed write must still be auditable, but never as "applied".
            try:
                record_review(ledger, group, verdict, evidence_digest=digest,
                              applied=False, outcome=item["outcome"])
            except (LedgerError, ValueError):
                pass
            continue
        item["applied"] = bool(outcome.get("applied"))
        item["outcome"] = str(outcome.get("outcome", ""))
        if "settled" in outcome:
            item["settled"] = bool(outcome["settled"])
        run.applied += 1 if item["applied"] else 0
        if not item["applied"]:
            run.skipped += 1
        item["review_id"] = record_review(
            ledger, group, verdict, evidence_digest=digest,
            applied=item["applied"], outcome=item["outcome"])
        run.results.append(item)
    # Report the model's state once, after the loop: both facts describe the
    # whole run, and inside the loop a run with no groups never set them.
    if model is not None:
        run.llm_breaker = bool(getattr(model, "tripped", False))
        run.llm_transport = str(getattr(model, "transport", "") or "")
    return run


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _format_side(tag: str, side: Mapping[str, Any]) -> List[str]:
    return [
        "    %s claim=%s mem=%s conf=%.2f trust=%.2f state=%s mem_state=%s"
        % (tag, side["claim_id"], side["memory_id"], side["confidence"],
           side["source_trust"], side["state"], side["memory_state"]),
        "      value: %s"
        % _excerpt(json.dumps(side["value"], ensure_ascii=False, default=str),
                   160),
    ]


def format_run(run: ReviewRun) -> List[str]:
    """Render a run as plain text (separate so tests can assert on it)."""
    mode = "DRY-RUN（不写库）" if run.dry_run else "APPLY（写库）"
    lines = ["== 冲突裁决 %s · 决策器=%s · 阈值=%.2f =="
             % (mode, run.decider_used, run.min_confidence),
             "冲突组: %d" % len(run.groups), ""]
    for index, item in enumerate(run.results, 1):
        lines.append("[%d] %s  (%s)" % (index, item["slot"], item["cause"]))
        for tag, side in zip("AB", item["sides"][:2]):
            lines.extend(_format_side(tag, side))
        lines.append("    -> %s (%.2f) [%s]"
                     % (item["verdict"], item["confidence"], item["decider"]))
        lines.append("      理由: %s" % item["reason"])
        lines.append("      结果: %s" % item["outcome"])
        lines.append("")
    summary = run.summary()
    lines.append("== 汇总 ==")
    lines.append("  根因分布: %s"
                 % json.dumps(summary["causes"], ensure_ascii=False,
                              sort_keys=True))
    lines.append("  裁决分布: %s"
                 % json.dumps(summary["verdicts"], ensure_ascii=False,
                              sort_keys=True))
    lines.append("  已应用: %d · 未应用: %d · 错误: %d · LLM 调用: %d（失败 %d）"
                 % (run.applied, run.skipped, run.errors, run.llm_calls,
                    run.llm_failures))
    if run.llm_breaker:
        lines.append("  熔断器已触发：模型连续失败，本次余下裁决全部走规则")
    return lines


def show_reviews(ledger: SQLiteLedger, limit: int = 50) -> List[str]:
    """Render the audit trail (empty list when the table does not exist yet)."""
    with ledger._reading() as conn:  # noqa: SLF001
        try:
            rows = conn.execute(
                "SELECT * FROM memory_claim_reviews ORDER BY created_at DESC "
                "LIMIT ?", (limit,)).fetchall()
        except Exception:  # table absent: a ledger that never applied a review
            rows = []
    if not rows:
        return ["尚无裁决记录（表为空或未创建）"]
    lines = ["== 裁决审计（最近 %d 条）==" % len(rows)]
    for row in rows:
        lines.append("%s %s | %s | %s"
                     % (row["created_at"], row["verdict"], row["decider"],
                        row["subject"]))
        lines.append("  slot=%s/%s/%s cause=%s"
                     % (row["subject"], row["predicate"], row["slot_key"],
                        row["cause"]))
        if row["claim_a"] == row["claim_b"]:
            lines.append("  唯一活跃方=%s (%s)"
                         % (row["claim_a"], row["value_hash_a"][:12]))
        else:
            lines.append("  A=%s (%s)  B=%s (%s)"
                         % (row["claim_a"], row["value_hash_a"][:12],
                            row["claim_b"], row["value_hash_b"][:12]))
        lines.append("  conf=%.2f applied=%s model=%s"
                     % (row["confidence"], bool(row["applied"]),
                        row["model"] or "-"))
        lines.append("  reason=%s" % row["reason"])
        lines.append("  evidence=%s outcome=%s"
                     % (row["evidence_digest"][:26], row["outcome"]))
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="冲突裁决：列出 conflict claims，按根因裁决，并留审计证据")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER)
    parser.add_argument("--scope", default="shared")
    parser.add_argument("--decider", choices=("rules", "llm"), default="rules",
                        help="rules=确定性规则（离线）；llm=本机 claude CLI"
                             "（失败自动回退规则）")
    parser.add_argument("--apply", action="store_true",
                        help="真正写库；不加则只打印（dry-run 是默认）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式 dry-run（默认行为，便于脚本自证）")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少个冲突组")
    parser.add_argument("--cause", action="append", default=None,
                        choices=list(CAUSES),
                        help="只处理指定根因（可重复）")
    parser.add_argument("--min-confidence", type=float,
                        default=DEFAULT_MIN_CONFIDENCE,
                        help="低于该置信度的 supersede 一律转 needs_human")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="单次模型调用超时秒数")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--show-reviews", action="store_true",
                        help="打印裁决审计记录")
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        print("--apply 与 --dry-run 互斥", file=sys.stderr)
        return 2
    ledger = SQLiteLedger(args.ledger)
    try:
        if args.show_reviews:
            print("\n".join(show_reviews(ledger, args.limit or 50)))
            return 0
        llm = ClaudeDecider(timeout=args.timeout) if args.decider == "llm" else None
        if args.decider == "llm" and llm is not None and not llm.available:
            print("提示: 未找到可执行的 claude CLI，全部回退规则裁决",
                  file=sys.stderr)
        run = review(ledger, decider=args.decider, apply=args.apply,
                     limit=args.limit, scope=args.scope,
                     min_confidence=args.min_confidence,
                     only_causes=args.cause, llm=llm)
        if args.json:
            print(json.dumps(run.summary(), ensure_ascii=False, indent=2,
                             default=str))
        else:
            print("\n".join(format_run(run)))
        return 2 if run.errors else 0
    except LedgerError as exc:
        print("账本错误: %s" % str(exc)[:300], file=sys.stderr)
        return 2
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
