"""CLI commands for automatic capture and distillation."""

from __future__ import annotations

import sys


def cmd_distill(args) -> int:
    from . import _paths, _print
    from ..distill import ClaudeExtractor, DistillEngine
    from ..ledger import SQLiteLedger

    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    provider = str(getattr(args, "provider", "auto") or "auto")
    if provider == "heuristic":
        extractor = None
    else:
        extractor = ClaudeExtractor()
        if provider == "claude" and not extractor.available:
            print("claude CLI 不可用；请改用 --provider heuristic 或安装 Claude Code", file=sys.stderr)
            return 1
    engine = DistillEngine(ledger, extractor=extractor)
    result = engine.run(limit=int(getattr(args, "limit", 32) or 32),
                        dry_run=bool(getattr(args, "dry_run", False)))
    _print(result)
    return 0


def cmd_sync_transcripts(args) -> int:
    from . import _paths, _print
    from ..ledger import SQLiteLedger
    from ..transcripts import TranscriptScanner

    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    scanner = TranscriptScanner(ledger)
    _print(scanner.sync(max_messages=int(getattr(args, "limit", 200) or 200)))
    return 0
