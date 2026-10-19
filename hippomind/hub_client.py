"""Small read-mostly localhost client used by the on-demand GUI."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from .daemon import RuntimePaths, load_runtime_state, read_local_token


class HubStatusClient:
    def __init__(self, ledger_path: str = "", runtime_dir: str = "", port: Optional[int] = None):
        self.paths = RuntimePaths.for_ledger(ledger_path or None, runtime_dir or None)
        state = load_runtime_state(self.paths)
        self.port = int(port or state.get("port") or 8765)

    def _request(self, method: str, endpoint: str, body: Optional[Dict[str, Any]] = None,
                 auth: bool = True) -> Optional[Dict[str, Any]]:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{endpoint}", data=payload, method=method)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        if auth:
            token = read_local_token(self.paths)
            if token:
                request.add_header("X-HippoMind-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=0.18) as response:
                data = json.loads(response.read(256_000).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, UnicodeDecodeError, ValueError):
            return None

    def stats(self) -> Optional[Dict[str, Any]]:
        import time as _time
        started = _time.perf_counter()
        health = self._request("GET", "/health", auth=False)
        if not health:
            return None
        stats = self._request("GET", "/stats")
        rtt_ms = (_time.perf_counter() - started) * 1000.0
        return {"health": health, "stats": stats or {}, "rtt_ms": round(rtt_ms, 1)}

    def conflicts(self, scope: str = "shared", limit: int = 20) -> Optional[Dict[str, Any]]:
        """冲突/取代 claim 只读视图（GUI 裁决面板数据源）。"""
        return self._request("GET", f"/conflicts?scope={scope}&limit={limit}")

    def context_pack(self, query: str) -> Optional[Dict[str, Any]]:
        return self._request("POST", "/context-pack", {
            "query": query, "options": {"max_items": 4, "max_chars": 900,
                                             "raw_chat": False, "require_evidence": True},
        })
