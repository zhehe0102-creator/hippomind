"""Reliable conversion of DSH ``session.jsonl.zstd`` records.

DSH appends one independent zstd frame per flush, so session files are
multi-frame streams. Decoding goes through the bundled node script (node's
``zlib`` has native zstd; the Python runtime here has no zstd module), which
is the same runtime the DSH ecosystem itself depends on.

Safety rules mirror the archive watcher: only real user / assistant text is
kept, injected memory packs (our own plugin messages) are excluded to prevent
a retrieval feedback loop, every record stays raw untrusted chat with file
and ``dsh-session://`` evidence, and any decode or parse failure skips one
file without ever blocking the sync.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .dsh_install import dsh_home
from .sanitize import MAX_ARCHIVE_MESSAGE_CHARS, sanitize_chat_content


SESSION_FILE_NAME = "session.jsonl.zstd"
DECODE_SCRIPT = Path("integrations") / "dsh-plugin" / "scripts" / "zstd-jsonl.mjs"
MAX_SESSION_FILES = 512
MAX_SESSION_SCAN = 8_192
MAX_TURNS_PER_FILE = 200
MAX_TURN_CONTENT_CHARS = 4_000
MAX_MESSAGE_CHARS = 2_000
DECODE_TIMEOUT_SECONDS = 30.0
UNTRUSTED_MARKER = "<MEMORY_DATA_UNTRUSTED>"


def dsh_sessions_root(home: Optional[Path] = None) -> Path:
    return dsh_home(home) / "sessions"


def decode_script_path(package_root: Path) -> Path:
    return (Path(package_root).expanduser() / DECODE_SCRIPT).resolve()


def node_binary() -> Optional[str]:
    return shutil.which("node")


def find_session_files(root: Path, *, limit: int = MAX_SESSION_FILES) -> List[Path]:
    """Return session files under the DSH sessions tree, newest first."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    found: List[Tuple[float, Path]] = []
    try:
        for candidate in root.glob(f"*/*/{SESSION_FILE_NAME}"):
            try:
                found.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
            if len(found) >= MAX_SESSION_SCAN:
                break
    except OSError:
        return []
    # 先全量收集再排序：glob 顺序近似字母序，边遍历边截断会让最新会话
    # 在文件总数超过 limit 时完全不被同步。
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in found[:max(1, int(limit))]]


def decode_session_file(path: Path, *, package_root: Path) -> str:
    """Decode one multi-frame zstd session file to its JSONL text.

    Raises OSError on transport failure; a corrupted file surfaces as a
    non-zero node exit, which the caller records as a source error.
    """
    executable = node_binary()
    if executable is None:
        raise OSError("node 不可用，无法解码 DSH 会话文件")
    script = decode_script_path(package_root)
    if not script.is_file():
        raise OSError(f"解码脚本缺失: {script}")
    completed = subprocess.run(
        [executable, str(script), str(Path(path).expanduser())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=DECODE_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-200:]
        raise OSError(f"zstd 解码失败 ({completed.returncode}): {detail or path}")
    return completed.stdout.decode("utf-8", errors="replace")


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def _event_text(data: Mapping[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return ""


def _iso_from_epoch_ms(value: Any) -> str:
    try:
        seconds = float(value) / 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def parse_session_events(text: str) -> List[Mapping[str, Any]]:
    """Parse JSONL events defensively; malformed lines are skipped."""
    events: List[Mapping[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) > 1_000_000:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, Mapping):
            events.append(event)
    return events


def session_header(events: List[Mapping[str, Any]]) -> Dict[str, Any]:
    for event in events:
        if event.get("type") == "session":
            data = event if "cwd" in event else (event.get("data") or {})
            return {"id": str(data.get("id", "")), "cwd": str(data.get("cwd", "")),
                    "created_at": _iso_from_epoch_ms(data.get("createdAt"))}
    return {"id": "", "cwd": "", "created_at": ""}


def session_title(events: List[Mapping[str, Any]]) -> str:
    title = ""
    for event in events:
        if event.get("type") == "session/title":
            data = event.get("data") or {}
            value = str(data.get("title", "")).strip()
            if value:
                title = value
    return title[:200]


def is_real_user_message(event: Mapping[str, Any]) -> bool:
    if event.get("type") != "user/message":
        return False
    data = event.get("data")
    if not isinstance(data, Mapping):
        return False
    source = data.get("source")
    if isinstance(source, Mapping) and str(source.get("kind", "")) != "user":
        # Plugin/skill injections (including our own memory packs) must never
        # be re-ingested as user evidence — that would create a feedback loop.
        return False
    return True


def build_turn_records(events: List[Mapping[str, Any]], path: Path) -> List[Dict[str, Any]]:
    """Split a session into per-user-turn raw records.

    A record covers one real user message plus the assistant replies that
    followed, bounded in size, with file and dsh-session:// evidence. Turn
    keys are stable (session id + user event seq), so appended frames only
    add or refresh turns without duplicating earlier ones.
    """
    header = session_header(events)
    session_id = header["id"] or Path(path).parent.name
    cwd = header["cwd"]
    project = _clip(Path(cwd).name if cwd else "", 120)
    title = session_title(events)
    turns: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for position, event in enumerate(events, 1):
        seq = event.get("seq")
        seq = int(seq) if isinstance(seq, (int, float)) else 0
        if is_real_user_message(event):
            text = _event_text(event.get("data") or {})
            if UNTRUSTED_MARKER in text:
                # Belt and braces: never archive injected memory packs.
                # 先关闭上一轮再跳过，否则其后的 assistant 回复会被错误地
                # 追加进前一个真实轮次，污染该轮内容与证据 span。
                if current is not None:
                    turns.append(current)
                    current = None
                continue
            if current is not None:
                turns.append(current)
            current = {
                "user": _clip(text, MAX_MESSAGE_CHARS),
                # seq 缺失时用文件内绝对序号兜底：与小整数混用会撞号，
                # 导致两个不同轮次生成相同 event_id 而被当作重复丢弃。
                "user_seq": seq or position,
                "assistant": [],
                "start_seq": seq or position,
                "end_seq": seq or position,
                "position_end": position,
                "timestamp": _iso_from_epoch_ms(event.get("time")),
            }
        elif event.get("type") == "assistant/message" and current is not None:
            text = _event_text(event.get("data") or {})
            if text:
                current["assistant"].append(_clip(text, MAX_MESSAGE_CHARS))
            # Some event kinds carry no seq; a position counter keeps the
            # evidence span meaningful either way.
            current["end_seq"] = max(current["end_seq"], seq) if seq else current["end_seq"]
            current["position_end"] = position
    if current is not None:
        turns.append(current)
    if len(turns) > MAX_TURNS_PER_FILE:
        turns = turns[-MAX_TURNS_PER_FILE:]
    records: List[Dict[str, Any]] = []
    for turn in turns:
        assistant_text = "\n".join(turn["assistant"])[:MAX_TURN_CONTENT_CHARS]
        content = "\n".join(part for part in (
            f"用户：{turn['user']}" if turn["user"] else "",
            f"助手：{assistant_text}" if assistant_text else "",
        ) if part)
        if not content:
            continue
        content, redactions, truncated = sanitize_chat_content(
            content, limit=min(MAX_TURN_CONTENT_CHARS, MAX_ARCHIVE_MESSAGE_CHARS))
        span_end = max(turn["end_seq"], turn["position_end"])
        records.append({
            "kind": "raw_chat",
            "raw_chat": True,
            "content": content,
            "source_agent": "deepseek",
            "session_id": _clip(session_id, 200),
            "event_id": f"dsh:{session_id}:turn:{turn['user_seq']}",
            "timestamp": turn["timestamp"],
            "task_id": "",
            "project": project,
            "role": "user",
            "scope": "shared",
            "evidence": [
                "file:" + _clip(str(Path(path).resolve()), 480),
                f"dsh-session://{session_id}#seq={turn['start_seq']}-{span_end}",
            ],
            "metadata": {
                "archive": str(path), "cwd": cwd[:480], "title": title,
                "assistant_turns": len(turn["assistant"]),
                "dsh_session": True,
                "redactions": redactions,
                "truncated": truncated,
            },
        })
    return records
