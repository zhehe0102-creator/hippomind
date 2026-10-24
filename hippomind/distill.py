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

from .ledger import LedgerError, SQLiteLedger, utc_now

CURSOR_PREFIX = "distill:"
MAX_BATCH = 8
MAX_CLAIMS_PER_SOURCE = 8
MAX_SOURCE_CHARS = 1_200
DEFAULT_CONFIDENCE = 0.55
DECISION_MARKERS = ("决定", "采用", "结论", "必须", "不能", "不要", "偏好",
                    "选择", "修复", "原因", "部署", "配置", "使用")


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
    """Use the installed Claude Code CLI as a local extraction model."""

    name = "claude"

    def __init__(self, timeout: float = 45.0):
        candidates = [shutil.which("claude"),
                      str(Path.home() / ".local" / "bin" / "claude"),
                      "/usr/local/bin/claude", "/opt/homebrew/bin/claude"]
        self.binary = next((path for path in candidates
                            if path and Path(path).is_file() and os.access(path, os.X_OK)), "")
        self.timeout = timeout
        self._failures = 0
        self._cooldown_until = 0.0
        self.last_error = ""

    @property
    def available(self) -> bool:
        return bool(self.binary) and time.time() >= self._cooldown_until

    def _record_failure(self, exc: Exception) -> None:
        self._failures += 1
        self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        if self._failures >= 2:
            self._cooldown_until = time.time() + 600.0
            self._failures = 0

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
        try:
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
        except (OSError, subprocess.SubprocessError) as exc:
            self._record_failure(exc)
            return None
        if completed.returncode != 0:
            self._record_failure(RuntimeError(completed.stderr[-300:]))
            return None
        text = completed.stdout.strip()
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

    def _candidates(self, limit: int, *, include_raw: bool = True) -> List[Candidate]:
        limit = max(1, min(200, int(limit)))
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

    def run(self, *, limit: int = 32, dry_run: bool = False,
            include_raw: bool = True) -> Dict[str, Any]:
        started = time.time()
        candidates = self._candidates(limit, include_raw=include_raw)
        summary = {"candidates": len(candidates), "batches": 0, "claims": 0,
                   "distilled": 0, "skipped": 0, "errors": 0,
                   "extractor": "none", "conflicts_resolved": 0,
                   "elapsed_ms": 0.0}
        last_by_subject: Dict[str, str] = {}
        for start in range(0, len(candidates), MAX_BATCH):
            batch = candidates[start:start + MAX_BATCH]
            if not batch:
                continue
            summary["batches"] += 1
            claims, extractor_name = self._claims_for(batch)
            summary["extractor"] = extractor_name
            by_source: Dict[int, List[Dict[str, Any]]] = {}
            for claim in claims:
                source = int(claim.get("source", 0) or 0)
                if source < 1 or source > len(batch):
                    source = 1
                by_source.setdefault(source, []).append(claim)
            for index, candidate in enumerate(batch, 1):
                source_claims = by_source.get(index, [])
                if not source_claims:
                    summary["skipped"] += 1
                    if not dry_run:
                        self._mark(candidate, 0)
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
        if not dry_run:
            summary["conflicts_resolved"] = self.resolve_conflicts()
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
                "available": bool(getattr(self.extractor, "available", False))}
