"""Portable ledger data: JSONL/SQL export and idempotent import."""

from __future__ import annotations

import json
import tempfile
import getpass
import hashlib
import os
import sys
from pathlib import Path
from ..ledger import LedgerError, SQLiteLedger, utc_now
from . import _paths
from .common import _print

def cmd_export(args) -> int:
    """导出记忆为 JSONL（ingest 兼容）或可回放 SQL（审计友好）。"""
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    fmt = getattr(args, "format", "jsonl") or "jsonl"
    if fmt == "sql":
        payload = ledger.export_sql(scope=args.scope,
                                    include_raw_chat=args.include_raw_chat,
                                    limit=args.limit,
                                    since=getattr(args, "since", ""))
        rows = [{"count": payload.count("INSERT OR IGNORE INTO memories")}]
    else:
        rows = ledger.export_rows(scope=args.scope,
                                  include_raw_chat=args.include_raw_chat,
                                  limit=args.limit,
                                  since=getattr(args, "since", ""))
        payload = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True)
                            for row in rows)
    encrypted = bool(getattr(args, "encrypt", False))
    if encrypted:
        from ..crypto_stream import encrypt_bytes
        passphrase = getattr(args, "passphrase", "") or getpass.getpass(
            "导出口令: ")
        if not passphrase:
            raise LedgerError("加密导出需要非空口令")
        blob = encrypt_bytes(payload.encode("utf-8"), passphrase)
    if str(args.output) in ("-", ""):
        if encrypted:
            raise LedgerError("加密导出必须指定输出文件（不能写 stdout）")
        sys.stdout.write(payload + ("\n" if payload else ""))
    else:
        out_path = Path(args.output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + out_path.name + ".",
                                         suffix=".tmp", dir=str(out_path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                data = blob if encrypted else payload.encode("utf-8")
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, out_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        exported_count = (rows[0]["count"] if fmt == "sql" else len(rows))
        result = {"exported": exported_count, "out": str(out_path),
                  "format": fmt}
        if encrypted:
            result["encrypted"] = True
            result["note"] = "混淆级保护；高敏感迁移请配合 age/gpg"
        _print(result)
    return 0


def cmd_import(args) -> int:
    """从 JSONL 导入记忆；content_hash 幂等，重复导入自动去重。

    每条导入记忆的 metadata 会带上 import_batch（源文件内容指纹）与
    import_source（文件路径/stdin），供审计与按批回滚定位。
    """
    paths = _paths(args)
    ledger = SQLiteLedger(paths.ledger_path)
    def _stamp(record: dict, batch: str, origin: str) -> dict:
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        metadata.setdefault("import_batch", batch)
        metadata.setdefault("import_source", origin)
        record["metadata"] = metadata
        return record

    if args.input in ("-", ""):
        source = sys.stdin
        raw_for_fingerprint = "<stdin>"
        batch_id = "imp-stdin-" + utc_now().replace(":", "")[:19]
    else:
        source_path = Path(args.input).expanduser()
        raw_bytes = source_path.read_bytes()
        from ..crypto_stream import decrypt_bytes, is_encrypted
        if is_encrypted(raw_bytes):
            passphrase = getattr(args, "passphrase", "") or getpass.getpass(
                "导入口令: ")
            try:
                plain = decrypt_bytes(raw_bytes, passphrase)
            except ValueError as exc:
                raise LedgerError(str(exc))
            text = plain.decode("utf-8")
            batch_id = "imp-" + hashlib.sha256(raw_bytes).hexdigest()[:12]
            raw_for_fingerprint = str(source_path)
            inserted = duplicate = rejected = 0
            batch: list = []
            for line_number, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    rejected += 1
                    continue
                if isinstance(record, dict):
                    batch.append(_stamp(record, batch_id, raw_for_fingerprint))
            if batch:
                res = ledger.ingest_many(batch)
                inserted += sum(1 for x in res if x.inserted)
                duplicate += sum(1 for x in res if not x.inserted)
            _print({"imported": inserted, "duplicate": duplicate,
                    "rejected": rejected, "batch": batch_id,
                    "decrypted": True})
            return 0
        source = source_path.open("r", encoding="utf-8")
        batch_id = "imp-" + hashlib.sha256(raw_bytes).hexdigest()[:12]
        raw_for_fingerprint = str(source_path)
    inserted = duplicate = rejected = 0
    batch: list = []

    try:
        for line_number, line in enumerate(source, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                # 整行解析失败：若是单对象 JSON（旧版 Hub 导出格式），整文件转交
                # import_json 兼容路径处理。
                if line.lstrip().startswith("{") and line_number == 1:
                    source.close()
                    result = ledger.import_json(args.input,
                                                source_agent=args.source_agent)
                    result["batch"] = batch_id + "-legacy"
                    _print(result)
                    return 0
                rejected += 1
                if rejected <= 3:
                    print(f"[跳过] 第 {line_number} 行不是合法 JSON: {exc}")
                continue
            if not isinstance(record, dict):
                rejected += 1
                continue
            # 旧版 Hub 导出（单对象 JSON，含 hippomind_memory_hub_export 键）：
            # 整文件转交 import_json 兼容路径。
            if "hippomind_memory_hub_export" in record:
                source.close()
                result = ledger.import_json(args.input,
                                            source_agent=args.source_agent)
                result["batch"] = batch_id + "-legacy"
                _print(result)
                return 0
            if args.scope:
                record["scope"] = args.scope
            batch.append(_stamp(record, batch_id, raw_for_fingerprint))
            if len(batch) >= 500:
                result = ledger.ingest_many(batch)
                inserted += sum(1 for r in result if r.inserted)
                duplicate += sum(1 for r in result if not r.inserted)
                batch.clear()
        if batch:
            result = ledger.ingest_many(batch)
            inserted += sum(1 for r in result if r.inserted)
            duplicate += sum(1 for r in result if not r.inserted)
    finally:
        if source is not sys.stdin:
            source.close()
    _print({"imported": inserted, "duplicate": duplicate, "rejected": rejected,
            "batch": batch_id})
    return 0
