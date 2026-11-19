"""Read-side commands: Context Pack retrieval, the injection switch, sync
and watch.

The auto-context preflight stays in :mod:`hippomind.cli` itself, because both
the CLI and :mod:`hippomind.codex_hook` reach it through that module.
"""

from __future__ import annotations

from typing import Any, Dict
from ..context_pack import ContextPackBuilder, ContextPackOptions
from ..embeddings import HashingNGramEmbedding
from ..injection import InjectionState
from ..ledger import LedgerError, SQLiteLedger
from ..watcher import ArchiveWatcher, default_dsh_sessions_root
from . import (_agent_token, _daemon_reachable, _paths, _port, _request)
from .common import _print, _reject_restricted_direct


def _pack_body(args) -> Dict[str, Any]:
    return {
        "query": args.query,
        "options": {
            "max_items": args.max_items, "max_chars": args.max_chars, "max_tokens": args.max_tokens,
            "raw_chat": args.raw_chat,
            "require_evidence": not args.allow_no_evidence, "scope": args.scope,
            "project": args.project, "task_id": args.task, "source_agents": args.source_agent,
            "from_time": args.from_time, "to_time": args.to_time,
            "as_of": getattr(args, "as_of", "") or "",
            # WP-M：会话感知 follow-up 锚定；旧 CLI 不传时是空串（行为不变）。
            "session_id": getattr(args, "session_id", "") or "",
            "debug": args.debug,
        },
    }


def cmd_context(args) -> int:
    paths = _paths(args)
    body = _pack_body(args)
    if args.offline:
        _reject_restricted_direct(args, "context --offline")
        options = ContextPackOptions(**body["options"])
        builder = ContextPackBuilder(
            SQLiteLedger(paths.ledger_path),
            embedder=(HashingNGramEmbedding()
                      if getattr(args, "enable_ngram_vector", False) else None))
        result = builder.build(args.query, options)
    else:
        result = _request(paths, _port(args, paths), "POST", "/context-pack", body,
                          token=_agent_token(args, paths))
    if args.text_only:
        print(result.get("text", ""))
    else:
        _print(result)
    return 0


def cmd_inject(args) -> int:
    """Change only automatic Context Pack injection."""
    _reject_restricted_direct(args, "inject")
    paths = _paths(args)
    _print(InjectionState(paths.state_path.parent).update(args.action, args.agent))
    return 0


def cmd_sync(args) -> int:
    paths = _paths(args)
    try:
        result = _request(paths, _port(args, paths), "POST", "/sync", {},
                          token=_agent_token(args, paths))
    except LedgerError:
        # sync_once 同样直写主账本，因此只有「daemon 不可达」才允许降级；
        # daemon 在跑时它的拒绝（权限不足、只读模式）必须原样抛给调用方。
        if _daemon_reachable(paths, _port(args, paths)):
            raise
        _reject_restricted_direct(args, "sync")
        result = ArchiveWatcher(SQLiteLedger(paths.ledger_path), args.archive,
                                poll_interval=args.watch_interval,
                                dsh_sessions_root=default_dsh_sessions_root()).sync_once()
    _print(result)
    return 0


def cmd_watch(args) -> int:
    _reject_restricted_direct(args, "watch")
    watcher = ArchiveWatcher(SQLiteLedger(_paths(args).ledger_path), args.archive,
                             poll_interval=args.watch_interval,
                             dsh_sessions_root=default_dsh_sessions_root())
    if args.once:
        _print(watcher.sync_once())
        return 0
    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
    return 0
