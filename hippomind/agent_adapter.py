"""Generic lifecycle adapter shared by Codex, Pi and other local agents.

It intentionally has no model SDK dependency. An agent calls ``begin_task``
to obtain a compact untrusted Context Pack, writes explicit checkpoints as it
works, and records external feedback only after a real result is known.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

from .context_pack import DEFAULT_MAX_CHARS, ContextPackBuilder, ContextPackOptions, as_untrusted_prompt
from .ledger import IngestResult, LedgerError, SQLiteLedger


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

    def record(self, kind: str, content: str, *, evidence: Iterable[str],
               context: Optional[Mapping[str, Any]] = None, tags: Iterable[str] = (),
               role: str = "assistant", metadata: Optional[Mapping[str, Any]] = None,
               importance: int = 5, confidence: float = 0.5) -> IngestResult:
        data = dict(metadata or {})
        data["lifecycle_adapter"] = self.identity.source_agent
        payload = {
            "kind": kind, "content": content, "source_agent": self.identity.source_agent,
            "session_id": self.identity.session_id, "task_id": self.identity.task_id,
            "project": self.identity.project, "scope": self.identity.scope, "role": role,
            "evidence": list(evidence), "context": dict(context or {}), "tags": list(tags),
            "metadata": data, "importance": importance, "confidence": confidence,
        }
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
        return result

    def checkpoint(self, progress: str, *, evidence: Iterable[str], next_steps: Iterable[str] = ()) -> IngestResult:
        return self.record("progress", progress, evidence=evidence,
                           context={"progress": [progress], "next_steps": list(next_steps)}, importance=6)

    def decision(self, decision: str, *, evidence: Iterable[str]) -> IngestResult:
        return self.record("decision", decision, evidence=evidence,
                           context={"key_decisions": [decision]}, importance=7)

    def blocker(self, blocker: str, *, evidence: Iterable[str], next_steps: Iterable[str] = ()) -> IngestResult:
        return self.record("blocker", blocker, evidence=evidence,
                           context={"blockers": [blocker], "next_steps": list(next_steps)}, importance=8)

    def complete(self, result: str, *, evidence: Iterable[str], next_steps: Iterable[str] = ()) -> IngestResult:
        return self.record("result", result, evidence=evidence,
                           context={"progress": [result], "next_steps": list(next_steps)}, importance=7)

    def feedback(self, memory_id: str, signal: float, note: str = "") -> bool:
        return self.ledger.feedback(memory_id, signal, note, self.identity.source_agent)
