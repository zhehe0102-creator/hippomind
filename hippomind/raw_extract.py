"""Conservative, non-LLM extraction of structured hints from raw chat evidence.

The output of this module is deliberately incomplete.  It promotes only explicit
task/progress/decision/next-step markers, rejects instruction-like or secret-like
text, and marks every emitted record as untrusted derived evidence.  It is not a
summarizer and must never turn arbitrary historical chat into trusted control
text for an agent.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping


MAX_EXTRACTED_CHARS = 700
_MARKERS = (
    ("task", "goal", re.compile(r"^\s*(?:任务|当前任务|task|goal|目标)\s*(?:[:：\-]|是)\s*(.+)$", re.I)),
    ("decision", "key_decisions", re.compile(r"^\s*(?:决定|决策|decision|方案|结论)\s*(?:[:：\-]|是)\s*(.+)$", re.I)),
    ("progress", "progress", re.compile(r"^\s*(?:进度|progress|完成|已完成|已实现|已修复)\s*(?:[:：\-]\s*|)(.+)$", re.I)),
    ("next_step", "next_steps", re.compile(r"^\s*(?:下一步|后续|next(?:\s+step)?|todo)\s*(?:[:：\-]|是)\s*(.+)$", re.I)),
)
_UNSAFE = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior)|system\s*(?:message|prompt)|"
    r"developer\s*(?:message|instruction)|泄露|reveal\s+(?:a\s+)?(?:secret|token|password)|"
    r"api[ _-]?key|authorization|bearer\s+|\bsk-[A-Za-z0-9_-]+|\bAKIA[0-9A-Z]+|"
    r"rm\s+-rf|sudo\b|curl\s+[^\s]+\s*\||<\s*/?(?:system|instruction|tool)\b|`|\$\()",
    re.I,
)
_BARE_IDENTIFIER = re.compile(
    r"^(?:[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}|[a-z][a-z0-9_-]{0,63})$", re.I
)
_TRAILING_FRAGMENT = re.compile(r"(?:的|和|与|及|以及|并|部分|方面|相关|验证的部分)$")


def _clean_candidate(value: str) -> str:
    value = " ".join(str(value or "").replace("\x00", " ").split()).strip(" -:：")
    if len(value) < 3 or len(value) > MAX_EXTRACTED_CHARS:
        return ""
    if _UNSAFE.search(value):
        return ""
    # Marker extraction is deliberately not a summarizer.  Reject identifiers,
    # lone foreign words and unfinished Chinese fragments rather than letting
    # them become apparently authoritative task state.
    if _BARE_IDENTIFIER.fullmatch(value) or _TRAILING_FRAGMENT.search(value):
        return ""
    if not re.search(r"[\u3400-\u9fff]", value) and len(value.split()) < 2:
        return ""
    return value


def extract_structured_records(raw_record: Mapping[str, Any], raw_memory_id: str, *,
                               derived_task_id: str = "") -> List[Dict[str, Any]]:
    """Return safe, evidence-linked facts from explicit chat markers only.

    Tool/system records are excluded outright.  The original raw evidence must
    be stored first; callers pass its immutable ``raw_memory_id`` so each
    derivative has an auditable source reference and stable idempotence key.
    """
    role = str(raw_record.get("role", "")).lower().strip()
    if role not in {"assistant", "user"}:
        return []
    content = str(raw_record.get("content", ""))
    if not content or len(content) > 200_000:
        return []
    source_agent = str(raw_record.get("source_agent", "unknown"))
    session_id = str(raw_record.get("session_id", ""))
    task_id = str(raw_record.get("task_id", "") or derived_task_id)
    project = str(raw_record.get("project", ""))
    raw_event_id = str(raw_record.get("event_id", raw_memory_id))
    scope = str(raw_record.get("scope", "shared")) or "shared"
    timestamp = raw_record.get("timestamp", raw_record.get("occurred_at", ""))
    output: List[Dict[str, Any]] = []
    seen = set()
    for line in content.splitlines()[:128]:
        for kind, context_field, pattern in _MARKERS:
            match = pattern.match(line)
            if not match:
                continue
            extracted = _clean_candidate(match.group(1))
            key = (kind, extracted.lower())
            if not extracted or key in seen:
                continue
            seen.add(key)
            index = len(output)
            safe_text = "[待核验的聊天提炼] " + extracted
            output.append({
                "kind": kind,
                "content": safe_text,
                "source_agent": source_agent or "unknown",
                "session_id": session_id,
                "task_id": task_id,
                "project": project,
                "role": "derived",
                "event_id": f"derived:{raw_event_id}:{kind}:{index}",
                "timestamp": timestamp,
                "scope": scope,
                "evidence": ["memory:" + raw_memory_id],
                "tags": ["derived-raw-chat", "untrusted"],
                "context": {context_field: [safe_text]},
                "metadata": {
                    "derived_from_raw_chat": True,
                    "untrusted_source": True,
                    "raw_memory_id": raw_memory_id,
                    "raw_event_id": raw_event_id,
                    "extractor": "marker-heuristic-v1",
                },
                "importance": 3,
                "confidence": 0.15,
            })
            break
        if len(output) >= 8:
            break
    return output
