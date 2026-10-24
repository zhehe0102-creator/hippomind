"""Automatic distillation of session evidence into structured claims.

The ledger stores raw evidence and mutable projections.  This module turns
bounded batches of recent projections into typed, evidence-linked claims:

* a capable local agent CLI (Claude Code's print mode by default) extracts
  subject/predicate/slot/value claims from the batch;
* a deterministic fallback extracts task context fields and decision-like
  sentences when no model is available;
* every distilled memory points back to its source memory ids;
* conflicts created by the new claims are adjudicated automatically only when
  the winner is clearly stronger; ambiguous cases stay for review.

It deliberately reuses the existing memories/memory_claims tables, so there is
no schema migration and every distilled fact remains auditable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import llm_client
from .ledger import LedgerError, SQLiteLedger, utc_now
from .textutil import content_fingerprint

CURSOR_PREFIX = "distill:"
#: Number of candidates sent to one model call.
#:
#: Measured on production data with the real backend (2026-10-24), see
#: docs/DISTILL.md for the full tables.  Two measurements matter here.
#:
#: 1. Aggregate sweep, 3 blocks per size, distinct item sets per size:
#:
#:        size  s/call  s/item  item-coverage  claims/item
#:           8    13.6    1.70         45.8%        0.83
#:          16    18.4    1.15         47.9%        0.71
#:          24    22.8    0.95         41.7%        0.62
#:          32    31.7    0.99         34.4%        0.47
#:          48    35.1    0.73         27.8%        0.42
#:
#:    This sweep alone would favour 16.  It is confounded: each size saw a
#:    different item set, and items differ in how much extractable material they
#:    contain, so "coverage" partly measures the items, not the batch.
#:
#: 2. Matched experiment, the one that decides: for each 16-item block, compare
#:    one size-16 call against two size-8 calls *on the same items*, and count
#:    items that received at least one valid claim (9 blocks, 144 items):
#:
#:        size-16 wall 173.0s   size-8 wall 289.9s   speedup 1.68x
#:        items covered: size-16 64 / size-8 72
#:        recall of size-16 vs size-8: 88.9%
#:        items size-8 found that size-16 DROPPED: 18 (gained only 10)
#:
#:    So a batch of 16 silently loses ~11% of the items the smaller batch would
#:    have distilled -- it does not fail, it just marks them "nothing to extract"
#:    and moves on.  That is the exact failure this knob must not introduce:
#:    speed bought by quietly doing less work.
#:
#: Therefore the default stays at 8.  The throughput win this module ships comes
#: from content dedupe (4,019 rows -> 1,337 model calls) and from raising the
#: per-pass limit, neither of which trades coverage for speed.  16 remains
#: available via HIPPOMIND_DISTILL_BATCH for operators who measure their own
#: corpus and accept the trade.
DEFAULT_BATCH_SIZE = 8
#: Hard ceiling for a caller-supplied batch size.  Above this the prompt alone
#: exceeds the useful context window and coverage collapses.
MAX_BATCH = 48
MAX_CLAIMS_PER_SOURCE = 8
MAX_SOURCE_CHARS = 1_200
#: Output budget for one extraction call.
#:
#: Measured, not guessed: at 2,048 the answer is cut mid-object and the whole
#: batch is lost to a JSON parse error (the model emits up to 8 claims per item
#: with Chinese values, and 8 items can need ~2,600 tokens).  The CLI transport
#: this replaced had no explicit cap, so a low cap here would have silently made
#: the API path *worse* than the CLI on exactly the densest batches.  8,192
#: covers the measured worst case with headroom while still bounding a runaway
#: completion.
EXTRACT_MAX_TOKENS = 8_192
#: Which transports the extractor may use (HIPPOMIND_DISTILL_TRANSPORT).
#:
#:   auto (default)  direct API, then the claude CLI, then the heuristic
#:   api             direct API only -- the CLI on this host hangs (measured:
#:                   180s with no output, then ECONNRESET), so an operator whose
#:                   endpoint is healthy may want to rule it out entirely
#:   cli             the legacy CLI only, for hosts where the API is not
#:                   configured but the CLI works
#:
#: Anything unrecognised means "auto": a typo must not silently disable
#: extraction.
TRANSPORTS = ("auto", "api", "cli")


def default_transport() -> str:
    """Transport policy for new extractors (HIPPOMIND_DISTILL_TRANSPORT)."""
    raw = os.environ.get("HIPPOMIND_DISTILL_TRANSPORT", "").strip().lower()
    return raw if raw in TRANSPORTS else "auto"
DEFAULT_CONFIDENCE = 0.55
#: How many candidates one run() pass may claim.  The daemon calls run() on a
#: timer, so this only bounds one tick's work, not the whole backlog.
#:
#: This is where the safe speedup lives.  It changes *how many* items a pass
#: handles, never *how many share one model call*, so per-item quality is
#: identical to before; it just stops the pass from idling between ticks.
#: 64 rows at batch 8 is 8 model calls per pass (~110s at the measured 13.6s
#: per call), comfortably inside the daemon tick.
DEFAULT_RUN_LIMIT = 64
#: Cap on identical-content rows retired per pass (see _candidates).
DEFAULT_DEDUPE_MARKS = 4_000
DECISION_MARKERS = ("决定", "采用", "结论", "必须", "不能", "不要", "偏好",
                    "选择", "修复", "原因", "部署", "配置", "使用")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Read a bounded integer tuning knob from the environment.

    Every throughput knob is overridable so an operator can retune a live
    daemon without editing code, but never to a value the measurement above
    showed to be harmful: out-of-range input falls back to the default instead
    of silently producing a batch the model cannot handle.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum or value > maximum:
        return default
    return value


def default_batch_size() -> int:
    """Batch size for one extraction call (HIPPOMIND_DISTILL_BATCH)."""
    return _env_int("HIPPOMIND_DISTILL_BATCH", DEFAULT_BATCH_SIZE,
                    minimum=1, maximum=MAX_BATCH)


def default_run_limit() -> int:
    """Candidates claimed per run() pass (HIPPOMIND_DISTILL_LIMIT)."""
    return _env_int("HIPPOMIND_DISTILL_LIMIT", DEFAULT_RUN_LIMIT,
                    minimum=1, maximum=200)


def _clean(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _json_value(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class Candidate:
    memory_id: str
    kind: str
    content: str
    source_agent: str
    project: str
    task_id: str
    session_id: str
    created_at: str
    metadata: Mapping[str, Any]


class ClaudeExtractor:
    """Extraction model, reached by direct API first and the CLI only as a fallback.

    History matters here.  This class used to shell out to ``claude -p``, and on
    this machine that CLI is broken (its default model is absent from the
    installed version's catalogue, so it hangs or fails).  Every pass therefore
    tripped the breaker and production distilled with the heuristic -- visible as
    ``extractor=heuristic`` in /stats.  The endpoint behind the CLI was fine all
    along, so the transport is now a plain HTTP call
    (:mod:`hippomind.llm_client`); the CLI is kept as a second chance for hosts
    where it does work.

    What deliberately did *not* change: the prompt, the 8-items-per-call batching,
    the claim schema and the source-number semantics.  Swapping only the
    transport is what makes the heuristic-vs-api comparison in docs/DISTILL.md
    a measurement of the model rather than of a rewritten prompt.

    ``name`` reports the transport that actually answered, so a caller can tell
    ``api`` from ``cli`` from the ``heuristic`` fallback in the ledger.
    """

    def __init__(self, timeout: float = 45.0, *, use_api: bool = True,
                 use_cli: bool = True, complete: Any = None,
                 transport: str = ""):
        candidates = [shutil.which("claude"),
                      str(Path.home() / ".local" / "bin" / "claude"),
                      "/usr/local/bin/claude", "/opt/homebrew/bin/claude"]
        self.binary = next((path for path in candidates
                            if path and Path(path).is_file() and os.access(path, os.X_OK)), "")
        self.timeout = timeout
        self._failures = 0
        self._cooldown_until = 0.0
        self.last_error = ""
        #: Which transports this instance may use, in order.  Tests pin these to
        #: isolate one path; the default is "API if configured, else the CLI".
        policy = str(transport or default_transport()).strip().lower()
        if policy not in TRANSPORTS:
            # An unrecognised policy means "auto", never "nothing": a typo must
            # not silently disable extraction.
            policy = "auto"
        self.transport_policy = policy
        self.use_api = (bool(use_api) and policy in ("auto", "api")
                        and llm_client.available())
        self.use_cli = bool(use_cli) and policy in ("auto", "cli")
        #: Injection seam: a callable(prompt, timeout=...) -> Optional[str].
        #: When set, no socket and no subprocess is touched.
        self._complete = complete
        #: Transport that produced the most recent successful extraction.
        self.transport = ""

    @property
    def name(self) -> str:
        """The transport that answered last: api, cli, or claude (unresolved)."""
        if self.transport:
            return self.transport
        if self.use_api:
            return "api"
        return "cli" if self.binary else "claude"

    @property
    def available(self) -> bool:
        if time.time() < self._cooldown_until:
            return False
        return bool(self.use_api or (self.use_cli and self.binary))

    def _record_failure(self, exc: Exception) -> None:
        self._failures += 1
        self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        if self._failures >= 2:
            self._cooldown_until = time.time() + 600.0
            self._failures = 0

    def _ask(self, prompt: str, max_tokens: int) -> Optional[str]:
        """Send one prompt through the first transport that answers.

        Returning ``None` here means "no transport produced text"; the caller
        then falls back to the heuristic.  A transport failure is recorded on
        the breaker but never raised, so one dead path cannot abort a pass.
        """
        if self.use_api:
            caller = self._complete or llm_client.complete
            try:
                answer = caller(prompt, max_tokens=max_tokens, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 - a transport must not escape
                self._record_failure(exc)
                answer = None
            if answer:
                self.transport = "api"
                return answer
            self._record_failure(RuntimeError("api returned no usable answer"))
        if not (self.use_cli and self.binary):
            return None
        try:
            answer = self._run_cli(prompt)
        except (OSError, subprocess.SubprocessError) as exc:
            self._record_failure(exc)
            return None
        if answer:
            self.transport = "cli"
        return answer

    def _run_cli(self, prompt: str) -> Optional[str]:
        """The legacy transport: ``claude -p`` in print mode."""
        env = os.environ.copy()
        extra_paths = [
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / ".local" / "share" / "pi-node"
                / "node-v22.23.2-darwin-arm64" / "bin"),
            "/usr/local/bin", "/opt/homebrew/bin",
        ]
        env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "/usr/bin:/bin")])
        completed = subprocess.run(
            [self.binary, "-p", "--output-format", "text"],
            input=prompt, text=True, capture_output=True,
            timeout=self.timeout, cwd=str(Path.home()), env=env,
        )
        if completed.returncode != 0:
            self._record_failure(RuntimeError(completed.stderr[-300:]))
            return None
        return completed.stdout.strip()

    def extract(self, candidates: Sequence[Candidate]) -> Optional[List[Dict[str, Any]]]:
        if not self.available or not candidates:
            return None
        snippets = []
        for index, item in enumerate(candidates, 1):
            snippets.append("[%d] agent=%s project=%s task=%s kind=%s\n%s" % (
                index, item.source_agent or "unknown", item.project or "",
                item.task_id or "", item.kind, item.content[:MAX_SOURCE_CHARS]))
        prompt = """你是 HippoMind 的记忆提炼器。只输出 JSON，不要解释。
从下面的会话片段中提取稳定、可复核的事实 claims。每条 claim 必须有：
subject（实体/项目/主题）、predicate（关系/属性）、slot_key（槽位，稳定小写）、
value（值）、confidence（0-1）、source（对应片段编号）。
只提取：项目决定、稳定事实、用户偏好、任务状态、可复用步骤。
不要提取：临时命令输出、密钥、密码、猜测、寒暄、一次性文件路径。
如果片段没有可提炼内容，claims 返回空数组。
输出格式：{"claims":[{"subject":"...","predicate":"...","slot_key":"...","value":"...","confidence":0.9,"source":1}]}

片段：
%s
""" % "\n\n".join(snippets)
        text = self._ask(prompt, EXTRACT_MAX_TOKENS)
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(text[start:end + 1])
        except ValueError:
            return None
        claims = payload.get("claims") if isinstance(payload, dict) else None
        if not isinstance(claims, list):
            return None
        result: List[Dict[str, Any]] = []
        for item in claims[:64]:
            if not isinstance(item, Mapping):
                continue
            subject = _clean(item.get("subject"), 200)
            predicate = _clean(item.get("predicate"), 200)
            slot = _clean(item.get("slot_key"), 200)
            value = _clean(item.get("value"), 1_000)
            if not subject or not predicate or not slot or not value:
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence", 0.7))))
            except (TypeError, ValueError):
                confidence = 0.7
            try:
                source = int(item.get("source", 0))
            except (TypeError, ValueError):
                source = 0
            result.append({"subject": subject, "predicate": predicate,
                           "slot_key": slot, "value": value,
                           "confidence": round(confidence, 3), "source": source})
        self._failures = 0
        self._cooldown_until = 0.0
        return result


class DistillEngine:
    """Bounded, idempotent distillation worker."""

    _DEFAULT = object()

    def __init__(self, ledger: SQLiteLedger, *, extractor: Any = _DEFAULT):
        self.ledger = ledger
        if extractor is not self._DEFAULT:
            self.extractor = extractor
            return
        provider = os.environ.get("HIPPOMIND_DISTILL_PROVIDER", "auto").strip().lower()
        self.extractor = None if provider in {"heuristic", "off"} else ClaudeExtractor()

    def _candidates(self, limit: int, *, include_raw: bool = True,
                    window: Optional[int] = None) -> List[Candidate]:
        """Fetch pending candidates, newest first, split between the two queues.

        window deliberately over-fetches relative to limit: identical content is
        common (raw chat re-captured across sessions), so the caller needs a
        wider row window than the number of distinct items it wants to hand to
        the model.  Fetching is cheap; a model call is not.
        """
        limit = max(1, min(200, int(limit)))
        if window is not None:
            limit = max(limit, min(1_000, int(window)))
        # Keep both queues moving: half the batch from structured projections
        # (task/progress/decision), half from newest raw chat.  A pure
        # created_at sort would let 250k raw rows starve structured memory; a
        # pure structured-first sort would delay new sessions for days.
        structured_limit = limit if not include_raw else max(1, limit // 2)
        raw_limit = 0 if not include_raw else max(1, limit - structured_limit)
        base = ("SELECT m.memory_id,m.kind,m.content,m.source_agent,m.project,m.task_id,"
                "m.session_id,m.created_at,m.metadata_json FROM memories m "
                "LEFT JOIN source_cursors c ON c.source_id='distill:' || m.memory_id "
                "WHERE c.source_id IS NULL AND m.quality_state='accepted' "
                "AND m.lifecycle='active' AND m.source_agent!='hippomind-distill' "
                "AND m.kind IN ('task','progress','decision','semantic','fact',"
                "'preference','procedural','raw_chat') ")
        with self.ledger._reading() as conn:
            structured = conn.execute(base + "AND m.raw_chat=0 ORDER BY m.created_at DESC LIMIT ?",
                                      (structured_limit,)).fetchall()
            raw = (conn.execute(base + "AND m.raw_chat=1 ORDER BY m.created_at DESC LIMIT ?",
                                (raw_limit,)).fetchall() if raw_limit else [])
        rows = list(structured) + list(raw)
        seen = set()
        output: List[Candidate] = []
        for row in rows:
            memory_id = str(row["memory_id"])
            if memory_id in seen:
                continue
            seen.add(memory_id)
            output.append(Candidate(memory_id, str(row["kind"]), str(row["content"] or ""),
                                    str(row["source_agent"] or ""), str(row["project"] or ""),
                                    str(row["task_id"] or ""), str(row["session_id"] or ""),
                                    str(row["created_at"] or ""),
                                    _json_value(row["metadata_json"], {})))
        return output

    @staticmethod
    def _heuristic(candidate: Candidate) -> List[Dict[str, Any]]:
        claims: List[Dict[str, Any]] = []
        # Prefer the concrete task id; a shared project name or a synthetic
        # "outside-of-project" bucket would make unrelated sessions collide in
        # the same slot and manufacture conflicts.
        subject = candidate.task_id or candidate.project or candidate.memory_id
        metadata = candidate.metadata if isinstance(candidate.metadata, Mapping) else {}
        context = metadata.get("context") if isinstance(metadata.get("context"), Mapping) else {}
        for field in ("goal", "progress", "key_decisions", "next_steps", "blockers", "preferences"):
            values = context.get(field) if isinstance(context, Mapping) else None
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                continue
            for value in values[:3]:
                text = _clean(value, 500)
                if text:
                    # Slot keys are memory-scoped: deterministic extraction has
                    # no stable global slot, so two different sessions must not
                    # be treated as contradicting each other.
                    claims.append({"subject": subject, "predicate": field,
                                   "slot_key": candidate.memory_id + ":" + field,
                                   "value": text, "confidence": DEFAULT_CONFIDENCE,
                                   "source": 0})
        if claims:
            return claims[:MAX_CLAIMS_PER_SOURCE]
        # Raw / unstructured fallback: only decision-like sentences.
        for sentence in re.split(r"[。！？\n]+", candidate.content):
            text = _clean(sentence, 300)
            if len(text) < 8 or not any(marker in text for marker in DECISION_MARKERS):
                continue
            claims.append({"subject": subject, "predicate": "statement",
                           "slot_key": candidate.memory_id + ":s_"
                                       + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
                           "value": text, "confidence": 0.5, "source": 0})
            if len(claims) >= 3:
                break
        return claims

    def _claims_for(self, candidates: Sequence[Candidate]) -> Tuple[List[Dict[str, Any]], str]:
        extracted = None
        if getattr(self.extractor, "available", False):
            extracted = self.extractor.extract(candidates)
        if extracted:
            return extracted, getattr(self.extractor, "name", "llm")
        claims: List[Dict[str, Any]] = []
        for candidate in candidates:
            claims.extend(self._heuristic(candidate))
        return claims, "heuristic"

    def _record(self, candidate: Candidate, claims: Sequence[Mapping[str, Any]],
                extractor_name: str, parent_memory_id: str = "") -> Dict[str, Any]:
        # The ledger has a unique (memory_id, subject, predicate, slot_key)
        # constraint.  LLM output may repeat a slot; keep the strongest one
        # instead of failing the whole batch.
        deduped: Dict[Tuple[str, str, str], Mapping[str, Any]] = {}
        for item in claims:
            key = (str(item.get("subject", "")), str(item.get("predicate", "")),
                   str(item.get("slot_key", "")))
            previous = deduped.get(key)
            if previous is None or float(item.get("confidence", 0)) > float(previous.get("confidence", 0)):
                deduped[key] = item
        claims = list(deduped.values())
        values = ["%s %s=%s" % (item.get("subject", ""), item.get("predicate", ""),
                                item.get("value", "")) for item in claims]
        source_text = _clean(candidate.content, 700)
        summary = "；".join(value for value in values if value)[:1_200]
        content = ("提炼（%s）：%s" % (candidate.kind, source_text)) + chr(10) + summary
        record = {
            "content": content[:2_000],
            "kind": "semantic",
            "source_agent": "hippomind-distill",
            "scope": "shared",
            "task_id": candidate.task_id,
            "project": candidate.project,
            "session_id": candidate.session_id,
            "evidence": ["memory:" + candidate.memory_id],
            "timestamp": utc_now(),
            "confidence": round(sum(float(item.get("confidence", 0.55)) for item in claims) / max(1, len(claims)), 3),
            "metadata": {"distilled": True, "extractor": extractor_name,
                         "source_memory_ids": [candidate.memory_id],
                         "source_kind": candidate.kind},
            "claims": [{"subject": item["subject"], "predicate": item["predicate"],
                        "slot_key": item["slot_key"], "value": item["value"],
                        "confidence": item.get("confidence", 0.55)} for item in claims],
        }
        if parent_memory_id:
            record["causal_parents"] = [{"memory_id": parent_memory_id,
                                          "relation_type": "related_to",
                                          "confidence": 0.6}]
        return record

    def _mark(self, candidate: Candidate, claims: int) -> None:
        self.ledger.set_cursor(CURSOR_PREFIX + candidate.memory_id, "1",
                               metadata={"claims": claims, "at": utc_now()})

    def associate(self, *, limit: int = 24, per_memory: int = 3) -> Dict[str, int]:
        """Link distilled memories that share a claim subject.

        Distillation already links consecutive same-subject memories inside one
        batch, but that map resets every run, so cross-batch associations were
        never created: production had 1,847 claims and only 41 relations.  This
        pass walks recent distilled memories and joins each to a few older
        memories on the same subject, which is what makes the graph and the
        associative retrieval channel useful.
        """
        limit = max(1, min(200, int(limit)))
        per_memory = max(1, min(8, int(per_memory)))
        created = 0
        scanned = 0
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT DISTINCT m.memory_id FROM memories m "
                "JOIN memory_claims c ON c.memory_id = m.memory_id "
                "WHERE m.source_agent = 'hippomind-distill' AND m.lifecycle = 'active' "
                "ORDER BY m.created_at DESC LIMIT ?", (limit,)).fetchall()
        for row in rows:
            memory_id = str(row["memory_id"])
            scanned += 1
            with self.ledger._reading() as conn:
                subjects = [str(item["subject"]) for item in conn.execute(
                    "SELECT DISTINCT subject FROM memory_claims "
                    "WHERE memory_id=? AND state IN ('active','conflict') LIMIT 4",
                    (memory_id,)).fetchall() if str(item["subject"] or "")]
                if not subjects:
                    continue
                marks = ",".join("?" for _ in subjects)
                related = conn.execute(
                    "SELECT memory_id, MAX(confidence) score FROM memory_claims "
                    "WHERE subject IN (" + marks + ") AND memory_id != ? "
                    "AND state IN ('active','conflict') "
                    "GROUP BY memory_id ORDER BY score DESC LIMIT ?",
                    tuple(subjects) + (memory_id, per_memory)).fetchall()
            parents = [{"memory_id": str(item["memory_id"]),
                        "relation_type": "related_to",
                        "confidence": round(float(item["score"] or 0.5), 3)}
                       for item in related]
            if not parents:
                continue
            try:
                result = self.ledger.add_relations(memory_id, parents)
            except (LedgerError, ValueError):
                continue
            created += int(result.get("created", 0) or 0)
        return {"scanned": scanned, "created": created}

    def resolve_conflicts(self, *, limit: int = 200) -> int:
        if hasattr(self.ledger, "repair_claim_states"):
            self.ledger.repair_claim_states()
        claims = self.ledger.list_claims(scope="shared", state="conflict", limit=limit)
        groups: Dict[Tuple[str, str, str], List[Mapping[str, Any]]] = {}
        for item in claims:
            key = (str(item.get("subject", "")), str(item.get("predicate", "")),
                   str(item.get("slot_key", "")))
            groups.setdefault(key, []).append(item)
        resolved = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            ranked = sorted(group, key=lambda item: (
                float(item.get("confidence", 0) or 0) * float(item.get("source_trust", 0.5) or 0.5),
                str(item.get("asserted_at", ""))), reverse=True)
            top = ranked[0]
            second = ranked[1]
            top_score = float(top.get("confidence", 0) or 0) * float(top.get("source_trust", 0.5) or 0.5)
            second_score = float(second.get("confidence", 0) or 0) * float(second.get("source_trust", 0.5) or 0.5)
            if top_score < second_score + 0.10 and float(top.get("confidence", 0) or 0) < 0.90:
                # Confidence cannot decide.  Two equally strong (>=0.9)
                # contradictory claims are a genuine disagreement that needs a
                # human; anything weaker is treated as an evolving fact, where
                # the newest assertion wins.  The loser stays as superseded with
                # superseded_by pointing at the winner, so the decision remains
                # auditable and reversible.
                strong = [item for item in group if float(item.get("confidence", 0) or 0) >= 0.90]
                if len({str(item.get("value_hash", item.get("value_json", ""))) for item in strong}) > 1:
                    continue
                top = max(group, key=lambda item: str(item.get("asserted_at", "")))
            try:
                self.ledger.resolve_claim(str(top.get("claim_id", "")),
                                          str(top.get("memory_id", "")),
                                          source_agent="hippomind-distill")
                resolved += 1
            except (LedgerError, ValueError):
                continue
        return resolved

    @staticmethod
    def _dedupe_groups(candidates: Sequence[Candidate]) -> List[Tuple[Candidate, List[Candidate]]]:
        """Group candidates that carry the same normalised content.

        Raw chat is re-captured across sessions, so the pending queue is mostly
        repeats: on the 2026-10-24 production backlog 4,067 pending rows held
        only 1,367 distinct contents.  Paying a model call per row means paying
        about 3x for the same answer.

        The first (newest) member of each group is the representative that goes
        to the model.  The rest are kept, not discarded: each one still gets its
        own distilled record with its own evidence link, so ledger provenance is
        unchanged.  Only the model call is shared.
        """
        groups: Dict[str, List[Candidate]] = {}
        order: List[str] = []
        for candidate in candidates:
            # Fingerprint the *normalised* body, not the raw one: two rows that
            # differ only by captured whitespace or a runner wrapper are one
            # message.  An empty body fingerprints to the digest of "" -- a
            # constant -- so it has to be excluded explicitly or every blank row
            # in the queue would collapse into a single group.
            key = content_fingerprint(candidate.content) if candidate.content.strip() else ""
            if not key:
                # An empty/unfingerprintable body must never collapse with
                # another one; give it a unique key so it stands alone.
                key = chr(0) + candidate.memory_id
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(candidate)
        return [(groups[key][0], groups[key][1:]) for key in order]

    def _mark_duplicate(self, candidate: Candidate, representative_id: str) -> None:
        """Retire a repeat row without a model call, keeping the audit trail.

        The cursor records which representative answered for this row, so the
        decision is reversible: deleting the cursor puts the row back in the
        queue if the shared extraction is ever judged wrong for it.
        """
        self.ledger.set_cursor(CURSOR_PREFIX + candidate.memory_id, "1",
                               metadata={"claims": 0, "at": utc_now(),
                                         "dedupe_of": representative_id})

    def run(self, *, limit: Optional[int] = None, dry_run: bool = False,
            include_raw: bool = True, batch_size: Optional[int] = None,
            dedupe: bool = True) -> Dict[str, Any]:
        """Distil one bounded pass of pending evidence into claims.

        batch_size (default 8, env HIPPOMIND_DISTILL_BATCH) is the number of
        *distinct* items per model call.  limit (default 64, env
        HIPPOMIND_DISTILL_LIMIT) bounds how many pending rows one pass claims.
        dedupe collapses identical content to one model call while still
        distilling every row.
        """
        started = time.time()
        limit = default_run_limit() if limit is None else max(1, min(200, int(limit)))
        size = default_batch_size() if batch_size is None else max(1, min(MAX_BATCH, int(batch_size)))
        # Over-fetch so dedupe still yields limit distinct representatives.
        window = limit if not dedupe else min(1_000, limit * 4)
        candidates = self._candidates(limit, include_raw=include_raw, window=window)
        groups = (self._dedupe_groups(candidates) if dedupe
                  else [(candidate, []) for candidate in candidates])
        groups = groups[:limit]
        representatives = [group[0] for group in groups]
        duplicates = sum(len(group[1]) for group in groups)
        # Report what this pass will actually handle, not how wide the fetch
        # window was: dedupe reads limit*4 rows to find limit distinct ones, so
        # the raw fetch count would overstate the pass by ~4x and make any
        # throughput computed from it wrong.  "fetched" keeps the raw number
        # visible for diagnosing dedupe efficiency.
        summary = {"candidates": len(representatives) + duplicates,
                   "distinct": len(representatives),
                   "duplicates": duplicates,
                   "fetched": len(candidates),
                   "batch_size": size, "batches": 0, "claims": 0,
                   "distilled": 0, "skipped": 0, "errors": 0,
                   "extractor": "none", "extractors": {},
                   "conflicts_resolved": 0, "elapsed_ms": 0.0}
        last_by_subject: Dict[str, str] = {}
        for start in range(0, len(representatives), size):
            batch = representatives[start:start + size]
            if not batch:
                continue
            summary["batches"] += 1
            claims, extractor_name = self._claims_for(batch)
            # "extractor" is the last transport that ran (kept for compatibility
            # with existing dashboards); "extractors" counts them, because a
            # single pass can genuinely mix them -- a batch whose model answer is
            # an empty claims array legitimately falls back for that batch only.
            # Reporting just the last one made a mostly-API run look entirely
            # heuristic, which is exactly the number an operator would trust.
            summary["extractor"] = extractor_name
            counts = summary["extractors"]
            counts[extractor_name] = counts.get(extractor_name, 0) + 1
            by_source: Dict[int, List[Dict[str, Any]]] = {}
            for claim in claims:
                source = int(claim.get("source", 0) or 0)
                if source < 1 or source > len(batch):
                    source = 1
                by_source.setdefault(source, []).append(claim)
            for index, candidate in enumerate(batch, 1):
                source_claims = by_source.get(index, [])
                # Repeats share this group's claims; they are retired together
                # with it so a batch never half-finishes.
                repeats = groups[start + index - 1][1]
                if not source_claims:
                    summary["skipped"] += 1 + len(repeats)
                    if not dry_run:
                        self._mark(candidate, 0)
                        for repeat in repeats:
                            self._mark_duplicate(repeat, candidate.memory_id)
                    continue
                summary["claims"] += len(source_claims)
                if dry_run:
                    continue
                try:
                    subject = str(source_claims[0].get("subject", "")).strip().lower()
                    parent = last_by_subject.get(subject, "")
                    result = self.ledger.ingest(self._record(candidate, source_claims,
                                                             extractor_name, parent))
                    if subject:
                        last_by_subject[subject] = result.memory_id
                    self._mark(candidate, len(source_claims))
                    summary["distilled"] += 1
                except (LedgerError, ValueError):
                    summary["errors"] += 1
                    continue
                for repeat in repeats:
                    try:
                        repeat_parent = last_by_subject.get(subject, "") or result.memory_id
                        self.ledger.ingest(self._record(repeat, source_claims,
                                                        extractor_name, repeat_parent))
                        self._mark_duplicate(repeat, candidate.memory_id)
                        summary["distilled"] += 1
                    except (LedgerError, ValueError):
                        summary["errors"] += 1
        if not dry_run:
            summary["conflicts_resolved"] = self.resolve_conflicts()
            try:
                summary["associated"] = self.associate()
            except Exception:
                summary["associated"] = {"scanned": 0, "created": 0}
            # resolve_claim can leave a losing memory with claims that are no
            # longer consistent with its lifecycle; converge once per cycle.
            if hasattr(self.ledger, "repair_claim_states"):
                self.ledger.repair_claim_states()
        summary["elapsed_ms"] = round((time.time() - started) * 1000.0, 1)
        return summary

    def status(self) -> Dict[str, Any]:
        with self.ledger._reading() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            distilled = int(conn.execute(
                "SELECT COUNT(*) FROM memories WHERE source_agent='hippomind-distill'").fetchone()[0])
            claims = int(conn.execute("SELECT COUNT(*) FROM memory_claims").fetchone()[0])
            conflicts = int(conn.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE state='conflict'").fetchone()[0])
        return {"memories": total, "distilled": distilled, "claims": claims,
                "conflicts": conflicts, "extractor": getattr(self.extractor, "name", "heuristic"),
                "available": bool(getattr(self.extractor, "available", False)),
                "transport": getattr(self.extractor, "transport", "") or ""}
