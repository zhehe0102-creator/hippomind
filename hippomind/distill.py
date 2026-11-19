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
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import llm_client
from .ledger import LedgerError, SQLiteLedger, utc_now
# 关系类型词表只有一处定义（ledger/graph.py）；distill 只 import 它真正写
# 的类型，避免出现「同名词表两份、各自漂移」。
from .ledger.graph import RELATION_SAME_SUBJECT, RELATION_SAME_TASK
from .schema import fts_indexed_predicate
from .textutil import content_fingerprint, is_noise_text

CURSOR_PREFIX = "distill:"
#: Cost/latency keys added to every run() summary and returned by cost_stats().
#: They are observability only: nothing reads them to change extraction, write
#: back, ordering or the existing summary keys.  Semantics: extractor_calls are
#: extractor.extract() invocations (0 when no extractor is configured),
#: extractor_failures are attempts that returned no usable claims or raised,
#: items_in are candidate rows handed to the boundary, items_out are claims the
#: boundary returned, and deterministic_fallback_count counts batches served by
#: the heuristic (extractor unavailable, returned nothing, or not configured).
COST_FIELDS = ("extractor_calls", "extractor_failures", "extractor_ms",
               "batch_count", "items_in", "items_out",
               "deterministic_fallback_count")
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
#: Markers that identify the distiller's own prompt echoed back as content.
#:
#: These are deliberately *literal phrases from this module's prompt*, not
#: generic words like "记忆" or "JSON": a real memory is allowed to talk about
#: memory systems and about JSON, so a broad match would silently delete
#: legitimate knowledge.  Each marker must be long enough that it cannot appear
#: by accident in normal prose.
PROMPT_SELF_MARKERS = (
    "你是 HippoMind 的记忆提炼器",
    "你是 hippomind 的记忆提炼器",
    "只输出 JSON，不要解释",
    "只输出 json，不要解释",
    "从下面的会话片段中提取稳定、可复核的事实",
    "从下面的会话片段中提取",
    "subject（实体/项目/主题）",
    "slot_key（槽位，稳定小写）",
    "confidence（0-1）",
    "source（对应片段编号）",
    "如果片段没有可提炼内容，claims 返回空数组",
)

#: An empty extraction result stored as if it were a fact.
EMPTY_PAYLOAD_RE = re.compile(
    r'^\s*(?:\{\s*"claims"\s*:\s*\[\s*\]\s*\}|\[\s*\]|'
    r'只输出\s*JSON\s*[：:]\s*\{\s*"claims"\s*:\s*\[\s*\]\s*\})\s*$',
    re.IGNORECASE)

#: A distilled record whose summary is empty: 提炼（raw_chat）： and nothing else.
EMPTY_DISTILL_RE = re.compile(
    r"^\s*提炼(?:（[^）]*）|\([^)]*\))?\s*[：:]\s*$")


def classify_unusable(content: str) -> str:
    """Return a reason code when content must not become a memory.

    Returns "" when the content is acceptable.  The codes are stable so
    callers can count and report them instead of dropping content silently:

    empty
        Nothing but whitespace.
    empty_payload
        A structurally empty extraction result ({"claims":[]}).
    empty_distill
        A distilled record with an empty summary (提炼（x）：).
    self_prompt
        The distiller's own prompt captured as if it were conversation.
    noise
        Structurally provable noise (banners, bare command echoes) per
        :func:`hippomind.textutil.is_noise_text`.

    Why a function and not a boolean: the production ledger accumulated 476
    such rows precisely because nothing recorded *why* content was dropped, so
    nobody could tell "we filtered it" from "we never captured it".  Returning
    the reason makes the gate observable.
    """
    text = content or ""
    if not text.strip():
        return "empty"
    stripped = text.strip()
    if EMPTY_PAYLOAD_RE.match(stripped):
        return "empty_payload"
    if EMPTY_DISTILL_RE.match(stripped):
        return "empty_distill"
    # A distilled record whose body is only the empty payload, e.g.
    # 提炼（raw_chat）：{"claims":[]} -- the wrapper hides the emptiness from
    # the two checks above, so unwrap once and re-test the tail.
    if stripped.startswith("提炼") and len(stripped) < 200:
        parts = re.split(r"[：:]", stripped, 1)
        if len(parts) == 2 and EMPTY_PAYLOAD_RE.match(parts[1].strip()):
            return "empty_payload"
    lowered = stripped.lower()
    for marker in PROMPT_SELF_MARKERS:
        if marker.lower() in lowered:
            return "self_prompt"
    if is_noise_text(text):
        return "noise"
    return ""


def is_usable_memory(content: str) -> bool:
    """Convenience predicate for callers that only need a yes/no answer."""
    return not classify_unusable(content)


# ---------------------------------------------------------------------------
# Claim-value gate: is this *value* worth occupying a slot?
# ---------------------------------------------------------------------------
#: Slots whose name carries no meaning of its own, so a short value there is
#: almost certainly the model failing to say anything.  Measured on the
#: 2026-10-24 production ledger: 377 of 377 claims in these slots had a long
#: free-text value and none was a bare state word -- i.e. a short value in one
#: of these slots is anomalous by construction.
#:
#: The inverse is the important half of the rule.  In a *typed* slot
#: (default_token_budget, tech.database, device_codename) a short value is the
#: correct answer, and killing it would destroy real knowledge: production holds
#: "6000" (token budget), "pa3q" (device codename), "SQLite", "ebpf",
#: "/dev/tmp", "259", "v2.2" -- every one of them short and every one of them
#: load-bearing.  So the gate only ever looks at values in generalized slots.
GENERIC_SLOT_NAMES = frozenset({
    "statement", "note", "notes", "misc", "summary", "description", "detail",
    "details", "info", "information", "content", "text", "body", "remark",
    "comment", "other", "general", "memo", "observation", "fact", "insight",
    "takeaway", "finding", "point", "message", "msg", "value", "data",
})
#: Slot names distill itself synthesizes for heuristic claims: <memid>:s_ab12.
#: They are generalized by construction -- the heuristic has no stable global
#: slot -- so they belong in the same bucket as "statement".
GENERIC_SLOT_RE = re.compile(r"^s_[0-9a-f]{6,}$", re.IGNORECASE)
#: Values that carry no information on their own.  These are only ever rejected
#: in a generalized slot; in a typed slot "true" is a perfectly good answer to
#: bash_requires_approval and a state word is a good answer to "status".
BARE_VALUE_RE = re.compile(
    r"^(?:true|false|yes|no|on|off|none|null|nil|tbd|n/?a|"
    r"可用|不可用|是|否|有|无|成功|失败|正常|异常|完成|未完成|"
    r"已修复|未修复|通过|未通过|一致|不一致|存在|不存在)$",
    re.IGNORECASE)
PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")


def slot_tail(slot_key: str) -> str:
    """The meaningful part of a slot key.

    Distill emits two shapes: a plain key (task_status) and a memory-scoped one
    (mem_ab12...:s_9f8e7d).  Only the tail carries meaning, so classification
    looks at that and nothing else.
    """
    return str(slot_key or "").split(":")[-1].strip()


def is_generic_slot(slot_key: str) -> bool:
    """True when the slot name itself says nothing about the expected value."""
    tail = slot_tail(slot_key).lower()
    return tail in GENERIC_SLOT_NAMES or bool(GENERIC_SLOT_RE.match(tail))


def classify_claim_value(slot_key: str, predicate: str, value: str) -> str:
    """Return a reason code when a claim value must not occupy a slot.

    Returns "" when the value is acceptable.  Codes are stable so the caller can
    count them (same contract as classify_unusable):

    empty_value
        Nothing but whitespace.
    punct_only
        Punctuation or underscores only ("-", "--", "...").
    tautology
        The value merely restates the subject or the slot name, so the claim
        asserts nothing: slot=project_name value=relayomni-work on subject
        relayomni-work.  Measured at 0.07% of production claims (3/4,464), all
        three genuinely empty, so the rule is safe at that rate.
    generic_single_char / generic_bare_state / generic_bare_number
        A one-character value, a bare boolean/state word, or a bare number in a
        *generalized* slot -- the "3" and short-state-word cases.

    Why slot semantics and not just length: length alone cannot separate "6000"
    (a token budget worth remembering) from "3" (a count the model could not
    name).  Both are short numbers.  What separates them is whether the slot
    already tells you what the number means.  In default_token_budget it does;
    in statement it does not.  A pure length rule would have deleted the first
    to catch the second -- measured cost: 82 of 374 fresh claims are 8
    characters or shorter and nearly all are legitimate (SQLite, pa3q, v2.2).
    """
    text = str(value or "").strip()
    if not text:
        return "empty_value"
    if PUNCT_ONLY_RE.match(text):
        return "punct_only"
    lowered = text.lower()
    # A value that only restates the slot name asserts nothing.  Deliberately
    # NOT extended to "value equals the predicate": measured on 683 fresh claims,
    # all 3 such cases were real facts whose predicate merely repeated the value
    # (subject=审计脚本 predicate=未运行 value=未运行 says the script is not
    # running).  Dropping those would lose information to tidy up wording.
    if lowered == slot_tail(slot_key).lower():
        return "tautology"
    # Everything below is conditional on a generalized slot.  A typed slot is
    # allowed to hold a short value; that is the whole point of the split.
    if not is_generic_slot(slot_key):
        return ""
    if len(text) == 1:
        return "generic_single_char"
    if BARE_VALUE_RE.match(text):
        return "generic_bare_state"
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        return "generic_bare_number"
    return ""

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
    #: Ledger scope of the source memory (agent:claude, shared, ...).  A
    #: distilled projection is never more visible than the evidence it came
    #: from; unpinned/legacy sources fall back to shared.
    scope: str = "shared"


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

    def extract(self, candidates: Sequence[Candidate], *,
                known_slots: Optional[Sequence[Mapping[str, str]]] = None
                ) -> Optional[List[Dict[str, Any]]]:
        if not self.available or not candidates:
            return None
        snippets = []
        for index, item in enumerate(candidates, 1):
            snippets.append("[%d] agent=%s project=%s task=%s kind=%s\n%s" % (
                index, item.source_agent or "unknown", item.project or "",
                item.task_id or "", item.kind, item.content[:MAX_SOURCE_CHARS]))
        known_block = ""
        if known_slots:
            slot_lines = [
                "- %s | %s | %s = %s" % (
                    _clean(s.get("subject"), 80),
                    _clean(s.get("predicate"), 60),
                    _clean(s.get("slot_key"), 80),
                    _clean(s.get("value"), 120),
                )
                for s in list(known_slots)[:40]
                if s.get("subject") and s.get("slot_key")
            ]
            if slot_lines:
                known_block = (
                    "\n已知活跃实体槽位（请优先复用已有规范化 subject 与 slot_key；"
                    "若新事实与现有槽位完全一致请标 op=\"NOOP\"，更新旧值请标 op=\"SUPERSEDE\"，新增标 op=\"ADD\"）：\n"
                    + "\n".join(slot_lines) + "\n"
                )
        prompt = """你是 HippoMind 的记忆提炼器。只输出 JSON，不要解释。
从下面的会话片段中提取稳定、可复核的事实 claims。每条 claim 必须有：
subject（实体/项目/主题）、predicate（关系/属性）、slot_key（槽位，稳定小写）、
value（值）、confidence（0-1）、source（对应片段编号）；可选 op（ADD/SUPERSEDE/NOOP）。
只提取：项目决定、稳定事实、用户偏好、任务状态、可复用步骤。
不要提取：临时命令输出、密钥、密码、猜测、寒暄、一次性文件路径。
如果片段没有可提炼内容，claims 返回空数组。
输出格式：{"claims":[{"subject":"...","predicate":"...","slot_key":"...","value":"...","confidence":0.9,"source":1,"op":"ADD"}]}
%s
片段：
%s
""" % (known_block, "\n\n".join(snippets))
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
            op = str(item.get("op") or "ADD").strip().upper()
            if op not in {"ADD", "SUPERSEDE", "NOOP"}:
                op = "ADD"
            result.append({"subject": subject, "predicate": predicate,
                           "slot_key": slot, "value": value,
                           "confidence": round(confidence, 3), "source": source,
                           "op": op})
        self._failures = 0
        self._cooldown_until = 0.0
        return result


class DistillEngine:
    """Bounded, idempotent distillation worker."""

    _DEFAULT = object()

    def __init__(self, ledger: SQLiteLedger, *, extractor: Any = _DEFAULT):
        self.ledger = ledger
        #: Cost/latency counters.  run() is single-threaded today; the lock is
        #: there so a /stats reader on another thread never sees a half-updated
        #: cumulative dict, and it must not grow into a control path.
        self._cost_lock = threading.Lock()
        self._cost: Dict[str, Any] = {
            "runs": 0, "batches": 0, "batch_count": 0, "extractor_calls": 0,
            "extractor_failures": 0, "extractor_ms": 0.0,
            "items_in": 0, "items_out": 0,
            "deterministic_fallback_count": 0,
        }
        self._run_cost: Optional[Dict[str, Any]] = None
        self._last_run_cost: Dict[str, Any] = {}
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
                "m.session_id,m.created_at,m.metadata_json,m.scope FROM memories m "
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
                                    _json_value(row["metadata_json"], {}),
                                    str(row["scope"] or "shared")))
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
        if len(claims) < MAX_CLAIMS_PER_SOURCE and isinstance(context, Mapping):
            files = context.get("related_files")
            if isinstance(files, list):
                cleaned_files = [_clean(f, 200) for f in files[:6] if _clean(f, 200)]
                if cleaned_files:
                    claims.append({
                        "subject": subject,
                        "predicate": "related_files",
                        "slot_key": candidate.memory_id + ":related_files",
                        "value": ", ".join(cleaned_files)[:500],
                        "confidence": DEFAULT_CONFIDENCE,
                        "source": 0,
                    })
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

    # -- cost/latency observability (no control flow) ----------------------
    def _begin_run_cost(self) -> None:
        with self._cost_lock:
            self._cost["runs"] = int(self._cost.get("runs", 0)) + 1
            self._run_cost = {field: 0 for field in COST_FIELDS}
            self._run_cost["batches"] = 0

    def _record_batch_cost(self, *, extractor_attempted: bool,
                           extractor_failed: bool, extractor_ms: float,
                           items_in: int, items_out: int,
                           deterministic_fallback: bool) -> None:
        """Accumulate one batch outcome into the instance and current-run dicts.

        Called from _claims_for's finally block, so an extractor that raises is
        counted before the exception continues to propagate (behaviour
        unchanged); the batch is still classified as attempted+failed.
        """
        with self._cost_lock:
            buckets = [self._cost]
            if self._run_cost is not None:
                buckets.append(self._run_cost)
            for bucket in buckets:
                # batch_count is the per-run/summary name; batches stays the
                # historical cumulative name so both readers keep working.
                bucket["batch_count"] = int(bucket.get("batch_count", 0)) + 1
                bucket["batches"] = int(bucket.get("batches", 0)) + 1
                bucket["items_in"] = int(bucket.get("items_in", 0)) + int(items_in)
                bucket["items_out"] = int(bucket.get("items_out", 0)) + int(items_out)
                if extractor_attempted:
                    bucket["extractor_calls"] = int(bucket.get("extractor_calls", 0)) + 1
                    bucket["extractor_ms"] = (float(bucket.get("extractor_ms", 0.0))
                                              + float(extractor_ms))
                    if extractor_failed:
                        bucket["extractor_failures"] = (
                            int(bucket.get("extractor_failures", 0)) + 1)
                if deterministic_fallback:
                    bucket["deterministic_fallback_count"] = (
                        int(bucket.get("deterministic_fallback_count", 0)) + 1)

    def _finish_run_cost(self, elapsed_ms: float) -> Dict[str, Any]:
        with self._cost_lock:
            run_cost = dict(self._run_cost or {field: 0 for field in COST_FIELDS})
            run_cost["extractor_ms"] = round(float(run_cost.get("extractor_ms", 0.0)), 3)
            snapshot: Dict[str, Any] = {"at": utc_now(),
                                        "elapsed_ms": round(float(elapsed_ms), 1)}
            snapshot.update(run_cost)
            self._last_run_cost = snapshot
            self._run_cost = None
            return dict(snapshot)

    def cost_stats(self) -> Dict[str, Any]:
        """Cumulative extractor cost + the most recent completed run().

        Observability only.  The dict is a copy taken under the counter lock, so
        a caller (e.g. a future /stats endpoint) can serialise it without
        racing a running pass.
        """
        with self._cost_lock:
            cumulative = dict(self._cost)
            last_run = dict(self._last_run_cost)
        calls = int(cumulative.get("extractor_calls", 0) or 0)
        extractor_ms = round(float(cumulative.get("extractor_ms", 0.0) or 0.0), 3)
        cumulative["extractor_ms"] = extractor_ms
        cumulative["extractor_ms_avg"] = (
            round(extractor_ms / calls, 3) if calls else 0.0)
        return {"cumulative": cumulative, "last_run": last_run,
                "extractor": getattr(self.extractor, "name", "heuristic"),
                "available": bool(getattr(self.extractor, "available", False)),
                "transport": getattr(self.extractor, "transport", "") or ""}

    def _active_slots(self, candidates: Sequence[Candidate], limit: int = 40) -> List[Dict[str, str]]:
        """Mem0 式闭环槽位字典：提取当前批涉及 scope 下的活跃实体槽位供复用/去重。"""
        scopes = sorted({str(getattr(c, "scope", "") or "shared") for c in candidates})
        if not scopes:
            return []
        marks = ",".join("?" for _ in scopes)
        rows = []
        try:
            with self.ledger._reading() as conn:
                rows = conn.execute(
                    "SELECT scope, subject, predicate, slot_key, value_json "
                    "FROM memory_claims "
                    "WHERE scope IN (" + marks + ") AND state IN ('active','conflict') "
                    "AND subject != '' AND slot_key != '' "
                    "ORDER BY rowid DESC LIMIT ?",
                    tuple(scopes) + (max(1, int(limit)),),
                ).fetchall()
        except Exception:
            return []
        slots: List[Dict[str, str]] = []
        seen: set = set()
        for row in rows:
            subj = str(row["subject"] or "").strip()
            pred = str(row["predicate"] or "").strip()
            slot = str(row["slot_key"] or "").strip()
            if not subj or not slot or (subj.lower(), slot) in seen:
                continue
            seen.add((subj.lower(), slot))
            raw_val = str(row["value_json"] or "")
            try:
                val = str(json.loads(raw_val)) if raw_val else ""
            except (TypeError, ValueError):
                val = raw_val
            slots.append({
                "scope": str(row["scope"] or "shared"),
                "subject": subj,
                "predicate": pred,
                "slot_key": slot,
                "value": val[:160],
            })
        return slots

    def _claims_for(self, candidates: Sequence[Candidate]) -> Tuple[List[Dict[str, Any]], str]:
        """Extract one batch, recording calls/latency/fallback at the boundary."""
        attempted = bool(getattr(self.extractor, "available", False))
        extracted: Optional[List[Dict[str, Any]]] = None
        failed = False
        extract_ms = 0.0
        items_out = 0
        deterministic_fallback = False
        try:
            if attempted:
                extract_started = time.perf_counter()
                try:
                    if isinstance(self.extractor, ClaudeExtractor):
                        known_slots = self._active_slots(candidates)
                        extracted = self.extractor.extract(candidates, known_slots=known_slots)
                    else:
                        extracted = self.extractor.extract(candidates)
                except BaseException:  # noqa: BLE001 - count, then re-raise as before
                    failed = True
                    raise
                finally:
                    extract_ms = (time.perf_counter() - extract_started) * 1000.0
                if not extracted:
                    failed = True
            if extracted:
                claims = extracted
                extractor_name = getattr(self.extractor, "name", "llm")
            else:
                claims = []
                for candidate in candidates:
                    claims.extend(self._heuristic(candidate))
                extractor_name = "heuristic"
                deterministic_fallback = True
            items_out = len(claims)
            return claims, extractor_name
        finally:
            self._record_batch_cost(
                extractor_attempted=attempted, extractor_failed=failed,
                extractor_ms=extract_ms, items_in=len(candidates),
                items_out=items_out, deterministic_fallback=deterministic_fallback)

    def _record(self, candidate: Candidate, claims: Sequence[Mapping[str, Any]],
                extractor_name: str, parent_memory_id: str = "",
                dropped: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        # Claim-value gate: a claim whose value says nothing must not take a
        # slot.  It would manufacture conflicts against a real fact and put a
        # meaningless row in front of retrieval.  See classify_claim_value for
        # why this keys on slot semantics rather than value length.
        if dropped is None:
            dropped = {}
        gated: List[Mapping[str, Any]] = []
        for item in claims:
            if str(item.get("op") or "").strip().upper() == "NOOP":
                dropped["noop"] = dropped.get("noop", 0) + 1
                continue
            reason = classify_claim_value(str(item.get("slot_key", "")),
                                         str(item.get("predicate", "")),
                                         str(item.get("value", "")))
            if reason:
                dropped[reason] = dropped.get(reason, 0) + 1
                continue
            gated.append(item)
        claims = gated
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
        # getattr: tests and adapters pass lightweight candidate objects, so
        # _record must stay duck-typed like the rest of this method.
        source_scope = str(getattr(candidate, "scope", "") or "").strip() or "shared"
        raw_metadata = getattr(candidate, "metadata", {})
        source_metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        # A raw row is untrusted by definition (it is someone else's chat).  A
        # row derived from one stays flagged even when distilled again, so a
        # downstream reader can keep treating the claim as non-authoritative.
        derived_from_raw_chat = bool(getattr(candidate, "kind", "") == "raw_chat"
                                     or source_metadata.get("raw_chat"))
        untrusted_source = bool(derived_from_raw_chat
                                or source_metadata.get("untrusted_source"))
        record = {
            "content": content[:2_000],
            "kind": "semantic",
            "source_agent": "hippomind-distill",
            # Inheritance, not promotion: distillation must not widen a private
            # raw row (scope agent:claude) into shared memory.  Explicit
            # promotion stays a separate, auditable API (WP-D).
            "scope": source_scope,
            "task_id": candidate.task_id,
            "project": candidate.project,
            "session_id": candidate.session_id,
            "evidence": ["memory:" + candidate.memory_id],
            "timestamp": utc_now(),
            "confidence": round(sum(float(item.get("confidence", 0.55)) for item in claims) / max(1, len(claims)), 3),
            "metadata": {"distilled": True, "extractor": extractor_name,
                         "source_memory_ids": [candidate.memory_id],
                         "source_kind": candidate.kind,
                         "source_scope": source_scope,
                         "derived_from_raw_chat": derived_from_raw_chat,
                         "untrusted_source": untrusted_source},
            "claims": [{"subject": item["subject"], "predicate": item["predicate"],
                        "slot_key": item["slot_key"], "value": item["value"],
                        "confidence": item.get("confidence", 0.55)} for item in claims],
        }
        if parent_memory_id:
            # 父节点是「上一批里同 claim 主语的提炼记忆」，关系就是同主语，
            # 不是无差别的 related_to：类型决定联想扩展权重
            # （same_subject 0.9 < related_to 1.0），无差别 related_to 会让
            # 「同主题接续」和「碰巧同批」在图里一样重。
            record["causal_parents"] = [{"memory_id": parent_memory_id,
                                          "relation_type": RELATION_SAME_SUBJECT,
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

        边类型（task-3）：共享 claim 主语 -> ``same_subject``；同一 task_id
        但无共享主语 -> ``same_task``（权重更低，只表达「同一件工作里」这层
        弱关联）。两者都比原来的无差别 ``related_to`` 更可解释，且都只在
        **新写入**时生效——存量 related_to 不会被本方法改写。
        """
        limit = max(1, min(200, int(limit)))
        per_memory = max(1, min(8, int(per_memory)))
        created = 0
        scanned = 0
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT DISTINCT m.memory_id, m.scope FROM memories m "
                "JOIN memory_claims c ON c.memory_id = m.memory_id "
                "WHERE m.source_agent = 'hippomind-distill' AND m.lifecycle = 'active' "
                "ORDER BY m.created_at DESC LIMIT ?", (limit,)).fetchall()
        for row in rows:
            memory_id = str(row["memory_id"])
            mem_scope = str(row["scope"] or "shared")
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
                    "SELECT c.memory_id, MAX(c.confidence) score "
                    "FROM memory_claims c JOIN memories m ON m.memory_id = c.memory_id "
                    "WHERE c.subject IN (" + marks + ") AND c.memory_id != ? "
                    "AND c.state IN ('active','conflict') "
                    "AND m.scope = ? AND m.quality_state IN ('accepted','conflict') "
                    "AND m.lifecycle IN ('active','conflict') "
                    "GROUP BY c.memory_id ORDER BY score DESC LIMIT ?",
                    tuple(subjects) + (memory_id, mem_scope, per_memory)).fetchall()
                # 弱关联通道：同 task_id 且同 scope、但没共享主语的记忆。
                same_task_rows = conn.execute(
                    "SELECT c.memory_id, MAX(c.confidence) score "
                    "FROM memory_claims c JOIN memories m ON m.memory_id=c.memory_id "
                    "JOIN memories me ON me.memory_id=? "
                    "WHERE me.task_id != '' AND m.task_id = me.task_id "
                    "AND m.scope = me.scope "
                    "AND c.memory_id != ? AND c.state IN ('active','conflict') "
                    "AND m.quality_state='accepted' AND m.lifecycle='active' "
                    "AND m.source_agent = 'hippomind-distill' "
                    "GROUP BY c.memory_id ORDER BY score DESC LIMIT ?",
                    (memory_id, memory_id, per_memory)).fetchall()
            parents = [{"memory_id": str(item["memory_id"]),
                        "relation_type": RELATION_SAME_SUBJECT,
                        "confidence": round(float(item["score"] or 0.5), 3)}
                       for item in related]
            seen_parents = {item["memory_id"] for item in parents}
            for item in same_task_rows:
                parent_id = str(item["memory_id"])
                if parent_id in seen_parents:
                    # 已按同主语连过：同主语是更强的证据，不重复记一条弱边。
                    continue
                seen_parents.add(parent_id)
                parents.append({"memory_id": parent_id,
                                "relation_type": RELATION_SAME_TASK,
                                "confidence": round(float(item["score"] or 0.5), 3)})
            if not parents:
                continue
            try:
                result = self.ledger.add_relations(memory_id, parents)
            except (LedgerError, ValueError):
                continue
            created += int(result.get("created", 0) or 0)
        return {"scanned": scanned, "created": created}

    def _conflict_scopes(self) -> List[str]:
        """Scopes that currently hold conflicting claims, shared first.

        Conflicts are scope-local: resolve_claim refuses a winner whose scope
        differs from the claim's, and the ledger only detects a slot collision
        inside one scope.  Iterating every scope keeps private (agent:*) claims
        adjudicable after distillation started inheriting the source scope; the
        old hardcoded scope=shared scan would leave them in conflict forever.
        """
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT DISTINCT scope FROM memory_claims WHERE state='conflict' "
                "ORDER BY scope LIMIT 64").fetchall()
        scopes = ["shared"]
        for row in rows:
            scope = str(row["scope"] or "").strip()
            if scope and scope not in scopes:
                scopes.append(scope)
        return scopes

    def resolve_conflicts(self, *, limit: int = 200,
                          scopes: Optional[Sequence[str]] = None) -> int:
        if hasattr(self.ledger, "repair_claim_states"):
            self.ledger.repair_claim_states()
        limit = max(1, min(2_000, int(limit)))
        resolved = 0
        for scope in (scopes if scopes is not None else self._conflict_scopes()):
            claims = self.ledger.list_claims(scope=str(scope), state="conflict", limit=limit)
            groups: Dict[Tuple[str, str, str], List[Mapping[str, Any]]] = {}
            for item in claims:
                key = (str(item.get("subject", "")), str(item.get("predicate", "")),
                       str(item.get("slot_key", "")))
                groups.setdefault(key, []).append(item)
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
                    # contradictory claims are a genuine disagreement that needs
                    # a human; anything weaker is treated as an evolving fact, where
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
            cand_scope = str(getattr(candidate, "scope", "") or "shared")
            cand_kind = str(getattr(candidate, "kind", "") or "")
            raw_fp = content_fingerprint(candidate.content) if candidate.content.strip() else ""
            if not raw_fp:
                # An empty/unfingerprintable body must never collapse with
                # another one; give it a unique key so it stands alone.
                key = cand_scope + chr(0) + cand_kind + chr(0) + candidate.memory_id
            else:
                # Scope and kind must both participate in the dedupe key so
                # private raw_chat in agent:claude never shares extraction/dedupe
                # with shared or another agent's scope.
                key = cand_scope + chr(0) + cand_kind + chr(0) + raw_fp
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(candidate)
        return [(groups[key][0], groups[key][1:]) for key in order]

    def _active_subject_map(self) -> Dict[str, Dict[str, str]]:
        """Scope -> {lowercase subject: canonical subject} for entity convergence."""
        mapping: Dict[str, Dict[str, str]] = {}
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT scope, subject FROM memory_claims "
                "WHERE state IN ('active','conflict') AND subject != '' "
                "ORDER BY rowid DESC LIMIT 500"
            ).fetchall()
        for row in rows:
            sc = str(row["scope"] or "shared")
            subj = str(row["subject"] or "").strip()
            if not subj:
                continue
            bucket = mapping.setdefault(sc, {})
            bucket.setdefault(subj.lower(), subj)
        return mapping

    @staticmethod
    def _canonicalize_claims(claims: Sequence[Mapping[str, Any]], scope: str,
                             subject_map: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
        bucket = subject_map.setdefault(scope, {})
        out: List[Dict[str, Any]] = []
        for item in claims:
            copied = dict(item)
            subj = str(copied.get("subject", "") or "").strip()
            if subj:
                canon = bucket.get(subj.lower())
                if canon:
                    copied["subject"] = canon
                else:
                    bucket[subj.lower()] = subj
            out.append(copied)
        return out

    def _duplicate_index(self) -> Dict[Tuple[str, str], str]:
        """(scope, fingerprint) -> memory_id for every live distilled memory."""
        cached = getattr(self, "_dup_index_cache", None)
        if cached is not None:
            return cached
        index: Dict[Tuple[str, str], str] = {}
        predicate = fts_indexed_predicate("m")
        with self.ledger._reading() as conn:
            rows = conn.execute(
                "SELECT m.memory_id, m.scope, m.content FROM memories m "
                "WHERE " + predicate + " AND m.source_agent='hippomind-distill'").fetchall()
        for row in rows:
            key = content_fingerprint(str(row["content"] or ""))
            row_scope = str(row["scope"] or "shared")
            if key and (row_scope, key) not in index:
                index[(row_scope, key)] = str(row["memory_id"])
        self._dup_index_cache = index
        return index

    def _remember_written(self, content: str, memory_id: str,
                          scope: str = "shared") -> None:
        key = content_fingerprint(content)
        if not key:
            return
        cached = getattr(self, "_dup_index_cache", None)
        if cached is not None:
            cached.setdefault((str(scope or "shared"), key), memory_id)

    def _reset_duplicate_index(self) -> None:
        self._dup_index_cache = None

    def _existing_duplicate(self, content: str, scope: str = "shared") -> str:
        fingerprint = content_fingerprint(content)
        if not fingerprint:
            return ""
        index = self._duplicate_index()
        return index.get((str(scope or "shared"), fingerprint), "")
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
        self._begin_run_cost()
        # One duplicate snapshot per pass; see _duplicate_index for why this is
        # not keyed on the ledger generation.
        self._reset_duplicate_index()
        limit = default_run_limit() if limit is None else max(1, min(200, int(limit)))
        size = default_batch_size() if batch_size is None else max(1, min(MAX_BATCH, int(batch_size)))
        # Over-fetch so dedupe still yields limit distinct representatives.
        window = limit if not dedupe else min(1_000, limit * 4)
        candidates = self._candidates(limit, include_raw=include_raw, window=window)
        # Content gate: refuse to distil anything that cannot become a useful
        # memory.  This runs *before* dedupe so a polluted row never claims a
        # representative slot, and it counts rejections by reason instead of
        # dropping them silently (see classify_unusable).
        rejected: Dict[str, int] = {}
        kept: List[Candidate] = []
        for candidate in candidates:
            reason = classify_unusable(candidate.content)
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            kept.append(candidate)
        candidates = kept
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
                   # Rows refused by the content gate, by reason.  Visible so an
                   # operator can tell "we filtered noise" from "we lost data".
                   "rejected": dict(rejected),
                   "rejected_total": sum(rejected.values()),
                   # Claim values refused by the value gate, by reason.
                   # Separate from "rejected" (which is about whole source
                   # rows) so an operator can see that the model answered
                   # but said nothing worth storing.
                   "claims_dropped": {}, "claims_dropped_total": 0,
                   "extractor": "none", "extractors": {},
                   # Cost/latency fields (new keys; existing keys keep their
                   # meaning).  Filled from the per-run accumulator below.
                   "extractor_calls": 0, "extractor_failures": 0,
                   "extractor_ms": 0.0, "batch_count": 0,
                   "items_in": 0, "items_out": 0,
                   "deterministic_fallback_count": 0,
                   "conflicts_resolved": 0, "elapsed_ms": 0.0}
        last_by_subject: Dict[Tuple[str, str], str] = {}
        subject_map = self._active_subject_map() if not dry_run else {}
        for start in range(0, len(representatives), size):
            batch = representatives[start:start + size]
            if not batch:
                continue
            summary["batches"] += 1
            claims, extractor_name = self._claims_for(batch)
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
                cand_scope = str(getattr(candidate, "scope", "") or "shared")
                canonical_claims = self._canonicalize_claims(source_claims, cand_scope, subject_map)
                try:
                    subject = str(canonical_claims[0].get("subject", "")).strip().lower()
                    parent = last_by_subject.get((cand_scope, subject), "")
                    dropped: Dict[str, int] = {}
                    record = self._record(candidate, canonical_claims,
                                          extractor_name, parent, dropped)
                    if dropped:
                        bucket = summary["claims_dropped"]
                        for reason, n in dropped.items():
                            bucket[reason] = bucket.get(reason, 0) + n
                        summary["claims_dropped_total"] += sum(dropped.values())
                    if not record.get("claims"):
                        summary["skipped"] += 1 + len(repeats)
                        self._mark(candidate, 0)
                        for repeat in repeats:
                            self._mark_duplicate(repeat, candidate.memory_id)
                        continue
                    existing = self._existing_duplicate(str(record.get("content", "")), scope=cand_scope)
                    if existing:
                        summary["skipped_duplicate"] = summary.get("skipped_duplicate", 0) + 1 + len(repeats)
                        self._mark_duplicate(candidate, existing)
                        for repeat in repeats:
                            self._mark_duplicate(repeat, existing)
                        continue
                    result = self.ledger.ingest(record)
                    self._remember_written(str(record.get("content", "")),
                                           result.memory_id, scope=cand_scope)
                    if subject:
                        last_by_subject[(cand_scope, subject)] = result.memory_id
                    self._mark(candidate, len(canonical_claims))
                    summary["distilled"] += 1
                except (LedgerError, ValueError):
                    summary["errors"] += 1
                    continue
                for repeat in repeats:
                    try:
                        rep_scope = str(getattr(repeat, "scope", "") or "shared")
                        repeat_parent = last_by_subject.get((rep_scope, subject), "") or result.memory_id
                        repeat_record = self._record(repeat, canonical_claims,
                                                     extractor_name, repeat_parent)
                        repeat_dup = self._existing_duplicate(
                            str(repeat_record.get("content", "")), scope=rep_scope)
                        if repeat_dup:
                            summary["skipped_duplicate"] = summary.get("skipped_duplicate", 0) + 1
                            self._mark_duplicate(repeat, repeat_dup)
                            continue
                        written = self.ledger.ingest(repeat_record)
                        self._remember_written(str(repeat_record.get("content", "")),
                                               written.memory_id, scope=rep_scope)
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
        run_cost = self._finish_run_cost(summary["elapsed_ms"])
        for field in COST_FIELDS:
            summary[field] = run_cost.get(field, 0)
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
