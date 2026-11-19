"""Native Claude Code UserPromptSubmit Context Pack hook.

Claude is the one agent in this account that produces assistant replies every
day, yet it was the one agent that was never injected into: it had rules and an
MCP server (both pull-only) but no push path.  That is the entire reason the
injection-quality report has always said "usable samples: 0" - the injection
happens in codex, whose capture stopped on 10-16, so the two evidence chains
never overlapped in time.

This module is the push path for Claude.  It is deliberately a near-copy of
hippomind.codex_hook rather than a shared abstraction: the two clients differ in
their hook payload field names and in what they are allowed to do with a
failure, and a premature abstraction here would be the thing that makes both
harder to reason about.  The shared parts (context rendering, the A/B decision,
the trace) live in their own modules.

Contract (Claude Code hooks, 2026-10):
  stdin  {"session_id":..., "transcript_path":..., "cwd":...,
          "hook_event_name":"UserPromptSubmit", "prompt":...}
  stdout {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                 "additionalContext": "..."}}
Anything else, including a crash, must be silent: a memory helper may never
block or corrupt a user's prompt.

The experiment (injection_ab) is a *matched* control: both arms retrieve, and
both record the memory ids that were offered.  Only delivery differs.  A control
arm that skipped retrieval would know nothing about what it skipped, and the two
arms could then only be compared on unrelated prompts - which is exactly the
observational comparison this whole change exists to replace.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional

from . import injection_ab
from .auto_context import (AUTO_MAX_CHARS, AUTO_MAX_ITEMS, extract_memory_request,
                           render_untrusted_context)
from .cli import fetch_auto_context


HOOK_EVENT_NAME = "UserPromptSubmit"
MAX_HOOK_INPUT_BYTES = 64 * 1024
MAX_HOOK_PROMPT_CHARS = 16 * 1024
AGENT = "claude"


def managed_command(*, package_root, agent_config, python) -> str:
    """The single canonical invocation form for this hook.

    Both the installer and the repairer call this, because the one thing that
    must never happen is the two of them disagreeing: an installer that writes
    form A and a repairer that writes form B makes every repair run look like a
    real change, which destroys the idempotence the audit depends on.

    PYTHONPATH is set explicitly rather than inherited.  A hook is spawned by
    the agent runtime, not by a login shell, so it cannot rely on the user's
    environment - and in production the package lives in the built-in-disk
    runtime copy, not in the checkout on the external volume (macOS TCC refuses
    background reads there, silently).
    """
    root = shlex.quote(str(Path(package_root).expanduser().resolve()))
    interpreter = shlex.quote(str(Path(python).expanduser()))
    config = shlex.quote(str(Path(agent_config).expanduser().resolve()))
    return ("cd %s && PYTHONPATH=%s %s -m hippomind.claude_hook --managed "
            "--trigger-mode always --agent-config %s" % (root, root, interpreter, config))


def parse_hook_input(raw: bytes) -> Optional[Dict[str, Any]]:
    """Return a bounded, schema-shaped hook payload or None.

    Unknown Claude fields (transcript_path, session_id, ...) are ignored so a
    client upgrade cannot break an ordinary request.  Only the event name, the
    prompt and the working directory are consumed - the transcript path is
    deliberately never read: this hook injects, it does not capture.
    """
    if not raw or len(raw) > MAX_HOOK_INPUT_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("hook_event_name") != HOOK_EVENT_NAME:
        return None
    if not isinstance(value.get("prompt"), str):
        return None
    if not isinstance(value.get("cwd"), str):
        return None
    return value


def _memory_ids(pack: Mapping[str, Any]) -> list:
    ids = []
    for item in pack.get("items", []) or []:
        for evidence in (item.get("evidence") or [])[:2]:
            if isinstance(evidence, dict) and evidence.get("memory_id"):
                ids.append(str(evidence["memory_id"]))
    return ids[:8]


def _trace(*, query: str, context: str, item_count: int, injected: bool,
           memory_ids: Optional[list] = None, source: str = "claude-hook",
           arm: str = "", reason: str = "") -> None:
    """Write exactly one trace line, for both arms.

    The control arm is the whole point of this module: a trace that only exists
    when something was injected can never be compared against anything.  Every
    failure here is swallowed - tracing must not be able to break a prompt.
    """
    if os.environ.get("HIPPOMIND_INJECTION_TRACE", "1") == "0":
        return
    try:
        from .injection_trace import record
        record(agent=AGENT, query=query, context=context,
               item_count=item_count, injected=injected,
               source=source, memory_ids=list(memory_ids or []),
               arm=arm, arm_reason=reason)
    except Exception:
        return


def build_hook_response(payload: Mapping[str, Any], *, agent_config: str,
                        trigger_mode: str = "") -> Optional[Dict[str, Any]]:
    """Build the documented additional-context response, or no output."""
    if payload.get("hook_event_name") != HOOK_EVENT_NAME:
        return None
    prompt = payload.get("prompt")
    cwd = payload.get("cwd")
    if not isinstance(prompt, str) or not isinstance(cwd, str):
        return None
    request = prompt.strip()
    if not request:
        return None
    mode = str(trigger_mode or os.environ.get("HIPPOMIND_AUTO_INJECT", "anywhere")).strip().lower()
    if mode not in {"always", "anywhere", "prefix", "off"}:
        mode = "anywhere"
    if mode != "always" and not extract_memory_request(request, mode=mode)["triggered"]:
        return None

    # A/B runs *after* the trigger gate but *before* retrieval, so a prompt that
    # would not have been injected anyway cannot consume a control slot (that
    # would bias the control arm toward low-relevance prompts).
    decision = injection_ab.assign(agent=AGENT, query=request)

    # WP-M：Claude Code hook payload 同样携带 session_id（camelCase 变体也
    # 兼容），用于「继续/同上」的会话锚定。
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")[:256]
    args = SimpleNamespace(
        ledger=None, runtime_dir=None, port=None, agent_config=str(agent_config),
        offline=False, no_start=False, archive=None, watch_interval=30.0,
        no_watch=False, agent=AGENT, scope="shared", project="", task="",
        max_items=AUTO_MAX_ITEMS, max_chars=AUTO_MAX_CHARS, debug=False, cwd=cwd,
        trigger_mode=mode, session_id=session_id,
        ab_handled=True, ab_arm=str(decision.get("arm") or "") if decision.get("enabled") else "",
    )
    try:
        pack = fetch_auto_context(args, request[:MAX_HOOK_PROMPT_CHARS],
                                  session_id=session_id)
        context = render_untrusted_context(pack, max_chars=AUTO_MAX_CHARS)
    except Exception:
        return None
    # **The control arm still retrieves.**  This is the whole design decision:
    # a control that skips retrieval knows nothing about what it skipped, so the
    # two arms cannot be compared on the same candidate set.  Retrieval is a
    # local SQLite query (tens of milliseconds) against a model call that costs
    # seconds; paying it twice to obtain a *matched* pair is the entire point.
    # What differs between arms is only delivery.
    offered = _memory_ids(pack)
    item_count = len(pack.get("items", []) or [])
    if not context:
        # A triggered prompt with no pack is evidence that retrieval found
        # nothing - exactly what the refusal work needs to see.  Recording it
        # keeps the treatment arm from being silently filtered to successful
        # lookups, which would inflate every reuse rate.
        _trace(query=request, context="", item_count=0,
               injected=decision["inject"], arm=decision["arm"],
               reason="empty_pack")
        return None
    _trace(query=request, context=context if decision["inject"] else "",
           item_count=item_count if decision["inject"] else 0,
           injected=decision["inject"], memory_ids=offered,
           arm=decision["arm"], reason=decision["reason"])
    if not decision["inject"]:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT_NAME,
            "additionalContext": context,
        }
    }


def run_from_stdin(*, agent_config: str, trigger_mode: str = "") -> Optional[Dict[str, Any]]:
    raw = sys.stdin.buffer.read(MAX_HOOK_INPUT_BYTES + 1)
    payload = parse_hook_input(raw)
    if payload is None:
        return None
    return build_hook_response(payload, agent_config=agent_config,
                               trigger_mode=trigger_mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HippoMind Claude Code UserPromptSubmit hook")
    parser.add_argument("--agent-config", required=True, help="Claude scoped 0600 credential file")
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--trigger-mode", choices=("always", "anywhere", "prefix", "off"),
                        default="", help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    response = run_from_stdin(agent_config=args.agent_config,
                              trigger_mode=args.trigger_mode)
    if response is not None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False,
                                    separators=(",", ":")) + chr(10))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
