"""Maintenance commands: checkup, backup/restore, redact/scan of raw
evidence, retention maintenance and the recovery drill.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
from pathlib import Path
from ..ledger import LedgerError, SQLiteLedger, utc_now
from . import _daemon_serves_ledger, _paths, _port
from .common import _print, _reject_restricted_direct


def cmd_checkup(args) -> int:
    """账本深度体检：完整性/FTS 同步/游标健康/声明一致性（默认只读）。"""
    _reject_restricted_direct(args, "checkup")
    ledger = SQLiteLedger(_paths(args).ledger_path)
    repair_note = None
    if getattr(args, "repair", False):
        try:
            report = ledger.fts_sync_report()
            needs_fts = any(report.get(k) for k in ("unindexed", "stale", "orphan", "duplicate_rows"))
            claims_rep = ledger.repair_claim_states() if hasattr(ledger, "repair_claim_states") else {}
            if needs_fts:
                outcome = ledger.repair_fts()
                repair_note = {"triggered": True, **outcome, "claims_repaired": claims_rep}
            else:
                repair_note = {"triggered": False, "reason": "FTS 已同步，无需重建",
                               "claims_repaired": claims_rep}
        except Exception as exc:
            repair_note = {"triggered": False, "error": str(exc)[:200]}
    checks: dict = {}
    failed = False

    # 1) SQLite 结构完整性（--repair 在进入检查前已执行，此处即修复后状态）
    try:
        result = ledger.integrity_check()
        checks["integrity"] = result
        if str(result.get("status", "")).lower() != "ok":
            failed = True
    except Exception as exc:
        checks["integrity"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 2) FTS 同步：投影行 vs FTS 行
    try:
        checks["fts_sync"] = ledger.fts_sync_report()
    except Exception as exc:
        checks["fts_sync"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 3) 游标健康：路径存在性
    try:
        cursors = ledger.list_cursors()
        missing = [c.get("source_path", "") for c in cursors
                   if c.get("source_path") and not Path(c["source_path"]).exists()]
        checks["cursors"] = {"status": "ok" if cursors else "empty",
                             "total": len(cursors), "missing_paths": len(missing)}
    except Exception as exc:
        checks["cursors"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 4) claim 一致性：活跃 claim 指向的记忆必须存在且未遗忘
    try:
        checks["claims"] = ledger.claims_consistency_report()
    except Exception as exc:
        checks["claims"] = {"status": "error", "detail": str(exc)[:200]}
        failed = True

    # 5) 概览计数
    try:
        overview = ledger.health_overview()
        checks["overview"] = overview
    except Exception as exc:
        checks["overview"] = {"status": "error", "detail": str(exc)[:200]}

    verdict = "fail" if failed else "ok"
    payload = {"checkup": verdict, "checks": checks}
    if repair_note is not None:
        payload["repair"] = repair_note
    fmt = getattr(args, "format", "json")
    if fmt == "md":
        sys.stdout.write(_render_checkup_md(payload))
    elif fmt == "junit":
        sys.stdout.write(_render_checkup_junit(payload))
    else:
        _print(payload)
    return 1 if failed else 0


def _render_checkup_md(payload: dict) -> str:
    """把 checkup 报告渲染成 markdown（贴 issue 友好）。"""
    lines = [f"# HippoMind Checkup：**{payload['checkup']}**", ""]
    labels = {"integrity": "SQLite 完整性", "fts_sync": "FTS 同步",
              "cursors": "游标健康", "claims": "Claim 一致性",
              "overview": "概览"}
    for name, detail in payload.get("checks", {}).items():
        title = labels.get(name, name)
        status = str(detail.get("status", "-"))
        lines.append(f"## {title} — `{status}`")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(detail, ensure_ascii=False, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    if "repair" in payload:
        lines.append("## Repair")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(payload["repair"], ensure_ascii=False,
                                indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def _render_checkup_junit(payload: dict) -> str:
    """把 checkup 报告渲染成 JUnit XML（CI 集成）。"""
    from xml.sax.saxutils import escape, quoteattr

    def tc(name: str, detail: dict) -> str:
        # overview 等信息性条目没有 status 字段——视为通过；
        # 真实错误路径总是显式写入 status="error"。
        status = str(detail.get("status", "ok"))
        attrs = quoteattr(name)
        if status in ("ok", "empty"):
            return f'    <testcase name={attrs} status="{escape(status)}"/>'
        if status == "warn":
            msg = escape(str(detail.get("hint") or detail.get("note") or "warning"))
            return (f'    <testcase name={attrs} status="warn">\n'
                    f'      <skipped message={quoteattr(msg)}/>\n'
                    f'    </testcase>')
        msg = escape(str(detail.get("detail", status))[:300])
        return (f'    <testcase name={attrs} status="fail">\n'
                f'      <failure message={quoteattr(msg)}/>\n'
                f'    </testcase>')

    checks = payload.get("checks", {})
    cases = "".join(tc(n, d) for n, d in checks.items())
    failures = sum(1 for d in checks.values()
                   if str(d.get("status", "ok")) not in ("ok", "empty", "warn"))
    verdict = payload.get("checkup", "unknown")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuites name={quoteattr("hippomind-checkup")} '
            f'tests="{len(checks)}" failures="{failures}" '
            f'verdict={quoteattr(verdict)}>\n'
            f'  <testsuite name="checkup" tests="{len(checks)}" '
            f'failures="{failures}">\n'
            f'{cases}\n  </testsuite>\n</testsuites>\n')


def _backup_registry_path(paths) -> Path:
    return paths.state_path.parent / "backups.json"


def _load_backup_registry(paths) -> list:
    path = _backup_registry_path(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_backup_registry(paths, entries: list) -> None:
    path = _backup_registry_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_backup(args) -> int:
    _reject_restricted_direct(args, "backup")
    path = SQLiteLedger(_paths(args).ledger_path).backup_to(args.output)
    # 校验和：跨机迁移或归档前可核对完整性（sha256sum -c 可直接消费）。
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    entries = [e for e in _load_backup_registry(_paths(args))
               if isinstance(e, dict) and e.get("path") != str(path)]
    entries.append({"path": str(path), "bytes": size,
                    "sha256": digest.hexdigest(),
                    "created_at": utc_now()})
    entries.sort(key=lambda e: str(e.get("created_at", "")), reverse=True)
    _save_backup_registry(_paths(args), entries[:200])
    _print({"backup": str(path), "bytes": size,
            "sha256": digest.hexdigest()})
    return 0


def cmd_redact_raw(args) -> int:
    """Apply the explicit, one-time security cleanup to raw chat evidence."""
    _reject_restricted_direct(args, "redact-raw")
    paths = _paths(args)
    if _daemon_serves_ledger(paths, _port(args, paths)):
        raise LedgerError("清理 raw 证据前请先运行 hippomind stop，避免与 daemon 并发写入")
    result = SQLiteLedger(paths.ledger_path).redact_raw_evidence(
        limit=args.max_chars, batch_size=args.batch_size)
    _print({"redacted_raw_evidence": result, "ledger": str(paths.ledger_path)})
    return 0


def cmd_scan_raw(args) -> int:
    """Report aggregate raw-evidence exposure without printing any content."""
    _reject_restricted_direct(args, "scan-raw")
    result = SQLiteLedger(_paths(args).ledger_path).scan_raw_secrets(batch_size=args.batch_size)
    _print({"raw_evidence_scan": result, "ledger": str(_paths(args).ledger_path)})
    return 0


def cmd_maintain(args) -> int:
    """Run bounded retention/archive maintenance while daemon is stopped."""
    if getattr(args, "vacuum", False):
        print("提示：VACUUM 前建议先 hippomind backup 创建备份；大库可能耗时数秒到数分钟。",
              file=sys.stderr)
    _reject_restricted_direct(args, "maintain")
    paths = _paths(args)
    if _daemon_serves_ledger(paths, _port(args, paths)):
        raise LedgerError("维护 raw 证据前请先运行 hippomind stop，避免与 daemon 并发写入")
    ledger = SQLiteLedger(paths.ledger_path)
    dry_run = bool(getattr(args, "dry_run", False))
    if dry_run:
        # A dry run must not write.  Report the exact candidate counts and the
        # file sizes so the operator can decide before anything is touched.
        report = {
            "dry_run": True,
            "memories": ledger.stats()["memories"],
            "purge_candidates": ledger.purge_expired_raw(
                before_days=getattr(args, "raw_ttl_days", 0) or max(args.raw_retention_days, 90),
                dry_run=True,
            )["purge_candidates"],
        }
        try:
            report["duplicate_groups"] = ledger.duplicate_candidates().get("groups", -1)
        except Exception:
            report["duplicate_groups"] = -1
        _print(report)
        return 0
    result = ledger.maintain(
        raw_after_days=args.raw_retention_days,
        raw_capacity=args.raw_capacity,
        raw_archive_dir=args.raw_archive_dir or None,
        raw_archive_max_bytes=args.raw_archive_max_bytes,
        raw_ttl_days=getattr(args, "raw_ttl_days", 0) or None,
        purge_raw=bool(getattr(args, "purge_raw", False)),
    )
    if getattr(args, "dedupe", False):
        try:
            result["dedupe"] = ledger.collapse_duplicate_rows(
                archive_dir=args.raw_archive_dir or None)
        except (LedgerError, sqlite3.Error) as exc:
            result["dedupe"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    # 合并建议：只报告不自动删——合并语义需人工确认（哪条留谱系）。
    try:
        duplicates = ledger.duplicate_candidates()
    except Exception:
        duplicates = {"suggestions": -1}
    if getattr(args, "vacuum", False):
        result["vacuum"] = ledger.vacuum()
    keep = getattr(args, "prune_backups", 0) or 0
    if keep > 0:
        entries = sorted(
            (e for e in _load_backup_registry(paths) if isinstance(e, dict)),
            key=lambda e: str(e.get("created_at", "")), reverse=True)
        kept, removed, skipped = [], [], []
        for index, entry in enumerate(entries):
            f = Path(str(entry.get("path", "")))
            if index < keep:
                kept.append(entry)
                continue
            try:
                if f.exists() and _sha256_of(f) == entry.get("sha256"):
                    f.unlink()
                    removed.append(str(f))
                else:
                    # 文件缺失或内容已变——不动它，仅标记陈旧条目。
                    skipped.append(str(f))
                    kept.append(entry)
            except OSError:
                skipped.append(str(f))
                kept.append(entry)
        _save_backup_registry(paths, kept)
        result["prune_backups"] = {"kept": len(kept), "removed": len(removed),
                                   "skipped": len(skipped)}
    _print({"maintenance": result, "duplicate_candidates": duplicates,
            "ledger": str(paths.ledger_path)})
    return 0


def cmd_recovery_check(args) -> int:
    """Exercise WAL backup/open recovery without replacing the live ledger."""
    _reject_restricted_direct(args, "recovery-check")
    result = SQLiteLedger(_paths(args).ledger_path).recovery_check()
    _print({"recovery_check": result, "ledger": str(_paths(args).ledger_path)})
    return 0


def cmd_restore(args) -> int:
    _reject_restricted_direct(args, "restore")
    paths = _paths(args)
    if _daemon_serves_ledger(paths, _port(args, paths)):
        raise LedgerError("恢复前请先运行 hippomind stop，避免覆盖正在使用的账本")
    source = Path(args.input).expanduser()
    if not source.is_file():
        raise LedgerError(f"备份文件不存在: {source}")
    # 强制校验和核对（衔接 backup 输出的 sha256）：防恢复损坏/调包的备份。
    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if getattr(args, "sha256", "") and args.sha256.lower() != actual:
        raise LedgerError(
            f"校验和不匹配：期望 {args.sha256[:16]}…，实际 {actual[:16]}…"
            "——备份可能损坏或被替换，拒绝恢复")
    SQLiteLedger(paths.ledger_path).restore_from(source)
    _print({"restored": str(source), "sha256": actual})
    return 0
