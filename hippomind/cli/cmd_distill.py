"""CLI commands for automatic capture and distillation."""

from __future__ import annotations

import sys
from typing import Any


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
        # --provider claude now means "a model, by whichever transport works":
        # the direct API when it is configured, the CLI otherwise.  The old gate
        # failed the command whenever the CLI was missing even though the
        # endpoint was reachable, which is exactly the state this machine was in.
        extractor = ClaudeExtractor()
        if provider == "claude" and not extractor.available:
            print("模型后端不可用：请配置 HIPPOMIND_LLM_BASE_URL / HIPPOMIND_LLM_API_KEY"
                  "（或 ~/.claude/settings.json），或改用 --provider heuristic",
                  file=sys.stderr)
            return 1
    engine = DistillEngine(ledger, extractor=extractor)
    result = engine.run(limit=int(getattr(args, "limit", 32) or 32),
                        dry_run=bool(getattr(args, "dry_run", False)),
                        include_raw=not bool(getattr(args, "no_raw", False)))
    _print(result)
    return 0


def cmd_digest(args) -> int:
    from . import _paths, _print
    from ..ledger import SQLiteLedger
    from ..session_digest import SessionDigester

    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    provider = str(getattr(args, "provider", "auto") or "auto")
    extractor: Any = None
    if provider != "heuristic":
        from ..distill import ClaudeExtractor
        extractor = ClaudeExtractor()
    digester = SessionDigester(ledger, extractor=extractor, provider=provider)
    limit = int(getattr(args, "limit", 4) or 4)
    min_messages = int(getattr(args, "min_messages", 4) or 4)
    dry_run = bool(getattr(args, "dry_run", False))
    if not getattr(args, "drain", False):
        _print(digester.run(limit=limit, dry_run=dry_run, min_messages=min_messages))
        return 0
    # Drain mode: keep taking bounded batches until the backlog is empty or the
    # caller-supplied batch budget is spent.  Heuristic extraction is fast enough
    # to clear hundreds of sessions; the model path stays bounded per batch.
    totals = {"sessions": 0, "digested": 0, "skipped": 0, "errors": 0,
              "claims": 0, "messages": 0, "batches": 0, "elapsed_ms": 0.0}
    max_batches = max(1, int(getattr(args, "max_batches", 40) or 40))
    for _ in range(max_batches):
        result = digester.run(limit=limit, dry_run=dry_run, min_messages=min_messages)
        totals["batches"] += 1
        for key in ("sessions", "digested", "skipped", "errors", "claims", "messages"):
            totals[key] += int(result.get(key, 0) or 0)
        totals["elapsed_ms"] = round(totals["elapsed_ms"] + float(result.get("elapsed_ms", 0) or 0), 1)
        totals["extractor"] = result.get("extractor", "none")
        if int(result.get("sessions", 0) or 0) == 0 or dry_run:
            break
    _print(totals)
    return 0


def cmd_semantic_build(args) -> int:
    from . import _paths, _print
    from ..ledger import SQLiteLedger
    from ..semantic_space import build, save

    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    model = build(ledger, dim=int(getattr(args, "dim", 128) or 128))
    if not model.get("vectors"):
        _print({"built": False, "reason": model.get("reason", "empty corpus")})
        return 1
    target = save(model)
    _print({"built": True, "path": str(target), "docs": model["docs"],
            "terms": len(model["terms"]), "dim": model["dim"]})
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
