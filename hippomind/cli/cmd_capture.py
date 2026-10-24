"""Capture self-check: what each local Agent store would be captured from.

`hippomind capture-status` answers one question a user can act on: "I installed
the Agent -- is HippoMind going to read its history?"  It is probe-only: it
opens nothing for write and ingests nothing, so it is safe to run while the
daemon is live.

The Cursor row reports exactly one of three states, because Cursor's schema is
undocumented and versioned: `detected` (a known store was recognised),
`not_installed` (no Cursor data directory at all) and `unrecognized` (Cursor data
exists but no known schema matched -- NOT the same as "zero messages").
"""

from __future__ import annotations

from typing import Any, Dict, List

from .. import cursor_capture, gemini_capture, pi_capture
from ..transcripts import TranscriptScanner
from .common import _print, _reject_restricted_direct


def _pi_report() -> Dict[str, Any]:
    roots = pi_capture.default_pi_session_roots()
    files = 0
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        try:
            files += sum(1 for path in root.rglob("*.jsonl")
                         if path.is_file() and not path.is_symlink())
        except OSError:
            continue
    return {"agent": "pi",
            "state": "detected" if files else "not_installed",
            "roots": [str(root) for root in roots], "files": files}


def _gemini_report() -> Dict[str, Any]:
    databases = gemini_capture.default_gemini_summary_databases()
    present = [str(path) for path in databases if path.is_file()]
    return {"agent": "gemini",
            "state": "detected" if present else "not_installed",
            "databases": [str(path) for path in databases], "present": present}


def _transcripts_report() -> Dict[str, Any]:
    scanner = TranscriptScanner.__new__(TranscriptScanner)
    scanner.ledger = None  # only the root discovery is used here
    roots = TranscriptScanner.default_roots()
    found = {"claude": 0, "codex": 0}
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        agent = "claude" if "claude" in str(root).lower() else "codex"
        try:
            found[agent] += sum(1 for path in root.rglob("*.jsonl")
                                if path.is_file() and not path.is_symlink())
        except OSError:
            continue
    return {"agent": "claude+codex",
            "state": "detected" if any(found.values()) else "not_installed",
            "roots": [str(root) for root in roots], "files": found}


def capture_status() -> List[Dict[str, Any]]:
    """Probe every capture source without writing or ingesting anything."""
    return [_transcripts_report(), _gemini_report(), _pi_report(),
            cursor_capture.probe()]


def cmd_capture_status(args) -> int:
    _reject_restricted_direct(args, "capture-status")
    reports = capture_status()
    if getattr(args, "format", "text") == "json":
        _print({"sources": reports})
        return 0
    for report in reports:
        state = report.get("state", "?")
        agent = report.get("agent", "?")
        print("%-14s %s" % (agent, state))
        for key in ("roots", "databases", "present", "files", "total_sources",
                    "recognized", "data_roots", "store_roots"):
            if key not in report:
                continue
            value = report[key]
            if isinstance(value, list):
                for item in value:
                    print("               %s: %s" % (key, item))
            else:
                print("               %s: %s" % (key, value))
        for note in report.get("notes", []) or []:
            print("               note: %s" % note)
        for item in report.get("present_but_unrecognized", []) or []:
            print("               unrecognized: %s (%s) -- %s"
                  % (item.get("kind"), item.get("reason"), item.get("path")))
        for source in report.get("sources", []) or []:
            if source.get("kind") == "store-db":
                print("               %s: %s (%s store.db, 未解析 protobuf DAG)"
                      % (source["kind"], source.get("reason"),
                         source.get("store_dbs_found")))
    if any(report.get("state") == cursor_capture.STATE_UNRECOGNIZED for report in reports):
        print("\n注意：检测到 Cursor 数据但 schema 未识别——这不等于「没有消息」，"
              "见 docs/AGENT_CAPTURE_COVERAGE.md")
    return 0
