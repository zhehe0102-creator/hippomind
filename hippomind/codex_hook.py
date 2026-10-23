"""Native Codex UserPromptSubmit Context Pack hook.

Codex sends the current request as JSON on standard input before it calls the
model. This adapter returns only a bounded, untrusted Context Pack through
the documented hook output shape. It never stores the submitted prompt,
never asks for raw chat, and fails closed on malformed input or an unavailable
local daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional

from .auto_context import (AUTO_MAX_CHARS, AUTO_MAX_ITEMS, extract_memory_request,
                           render_untrusted_context)
from .cli import fetch_auto_context


HOOK_EVENT_NAME = "UserPromptSubmit"
MAX_HOOK_INPUT_BYTES = 64 * 1024
MAX_HOOK_PROMPT_CHARS = 16 * 1024


def parse_hook_input(raw: bytes) -> Optional[Dict[str, Any]]:
    """Return a bounded, schema-shaped hook payload or None.

    Unknown Codex fields are ignored so a client upgrade does not prevent an
    ordinary request. Only the event name, prompt, and working directory are
    consumed.
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


def build_hook_response(payload: Mapping[str, Any], *, agent_config: str,
                        trigger_mode: str = "") -> Optional[Dict[str, Any]]:
    """Build the documented additional-context response, or no output.

    The user request may exceed the retrieval limit. The original request
    still proceeds normally; only the lookup query is bounded. Operational
    failures are intentionally silent because a memory helper must never
    block or modify a user request when its local service is unavailable.
    """
    if payload.get("hook_event_name") != HOOK_EVENT_NAME:
        return None
    prompt = payload.get("prompt")
    cwd = payload.get("cwd")
    if not isinstance(prompt, str) or not isinstance(cwd, str):
        return None
    request = prompt.strip()
    if not request:
        return None
    # v2 default: every ordinary prompt is a candidate for automatic memory
    # (no 记忆： marker).  HIPPOMIND_AUTO_INJECT=anywhere restores the explicit
    # marker gate; tests and cautious operators keep the old behaviour.
    mode = str(trigger_mode or os.environ.get("HIPPOMIND_AUTO_INJECT", "anywhere")).strip().lower()
    if mode not in {"always", "anywhere", "prefix", "off"}:
        mode = "anywhere"
    if mode != "always" and not extract_memory_request(request, mode=mode)["triggered"]:
        return None
    args = SimpleNamespace(
        ledger=None, runtime_dir=None, port=None, agent_config=str(agent_config),
        offline=False, no_start=False, archive=None, watch_interval=30.0,
        no_watch=False, agent="codex", scope="shared", project="", task="",
        max_items=AUTO_MAX_ITEMS, max_chars=AUTO_MAX_CHARS, debug=False, cwd=cwd,
        trigger_mode=mode,
    )
    try:
        pack = fetch_auto_context(args, request[:MAX_HOOK_PROMPT_CHARS])
        context = render_untrusted_context(pack, max_chars=AUTO_MAX_CHARS)
    except Exception:
        return None
    if not context:
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
    return build_hook_response(payload, agent_config=agent_config, trigger_mode=trigger_mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HippoMind Codex UserPromptSubmit hook")
    parser.add_argument("--agent-config", required=True, help="Codex scoped 0600 credential file")
    parser.add_argument("--managed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--trigger-mode", choices=("always", "anywhere", "prefix", "off"),
                        default="", help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    response = run_from_stdin(agent_config=args.agent_config,
                              trigger_mode=args.trigger_mode)
    if response is not None:
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
