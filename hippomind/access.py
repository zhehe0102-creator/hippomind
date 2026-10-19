"""Cooperative agent scope and operation isolation for daemon and MCP calls.

The OS account remains the ultimate local trust boundary: a process with direct
filesystem access can still open SQLite.  This module prevents a configured
agent credential from impersonating another agent or reading/writing scopes it
was not granted through the supported HTTP/MCP surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .ledger import AgentCredential, LedgerError


RAW_KINDS = frozenset({"raw_chat", "tool_output", "system_message"})


class AccessDenied(LedgerError):
    """Raised when a scoped credential attempts an unsupported operation."""


@dataclass(frozen=True)
class AccessPolicy:
    agent_id: str
    scopes: Tuple[str, ...]
    permissions: Tuple[str, ...]
    admin: bool = False

    @classmethod
    def administrator(cls) -> "AccessPolicy":
        return cls("local-admin", ("*",), ("read", "ingest", "feedback", "stats", "sync", "shutdown"), True)

    @classmethod
    def read_only_shared(cls) -> "AccessPolicy":
        return cls("unconfigured-mcp", ("shared",), ("read", "stats"), False)

    @classmethod
    def from_credential(cls, credential: AgentCredential) -> "AccessPolicy":
        return cls(credential.agent_id, credential.scopes, credential.permissions, False)

    def require(self, permission: str) -> None:
        if self.admin or permission in self.permissions:
            return
        raise AccessDenied(f"agent {self.agent_id} 没有 {permission} 权限")

    def require_scope(self, scope: object) -> str:
        candidate = str(scope or "shared").strip()[:128] or "shared"
        if self.admin or "*" in self.scopes or candidate in self.scopes:
            return candidate
        raise AccessDenied(f"agent {self.agent_id} 无权访问 scope: {candidate}")

    def enforce_options(self, scope: object, *, raw_chat: bool = False) -> str:
        self.require("read")
        if raw_chat and not self.admin:
            # Raw chat is a local audit surface. A scoped Agent may read the
            # structured shared projection, but cannot turn its bearer token
            # into a transcript export switch.
            raise AccessDenied("受限 agent 不能读取 raw chat 证据")
        return self.require_scope(scope)

    def enforce_ingest(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        self.require("ingest")
        data = dict(record)
        requested_scope = data.get("scope", "agent:" + self.agent_id if not self.admin else "shared")
        data["scope"] = self.require_scope(requested_scope)
        if not self.admin and data["scope"] == "shared":
            self.require("shared_ingest")
        if self.admin:
            return data
        requested_agent = str(data.get("source_agent", data.get("agent", ""))).strip()
        if requested_agent and requested_agent != self.agent_id:
            raise AccessDenied(f"agent {self.agent_id} 不能伪造 source_agent={requested_agent}")
        kind = str(data.get("kind", data.get("event_type", "fact"))).lower().strip()
        if bool(data.get("raw_chat")) or kind in RAW_KINDS:
            raise AccessDenied("受限 agent 不能写入 raw chat；仅 watcher/admin 可保存原始证据")
        data["source_agent"] = self.agent_id
        data.pop("agent", None)
        return data

    def enforce_feedback_scope(self, scope: object) -> None:
        self.require("feedback")
        resolved = self.require_scope(scope)
        if not self.admin and resolved == "shared":
            self.require("shared_feedback")

    def enforce_feedback_source(self, source_agent: object) -> str:
        """Do not let a scoped credential attribute feedback to another agent."""
        requested = str(source_agent or "").strip()[:128]
        if self.admin:
            return requested
        if requested and requested != self.agent_id:
            raise AccessDenied(f"agent {self.agent_id} 不能伪造 feedback source_agent={requested}")
        return self.agent_id

    def can_read_stats(self) -> None:
        self.require("stats")


def policy_for_token(token: str, *, master_token: str, credential_lookup) -> Optional[AccessPolicy]:
    """Resolve a daemon master token or a stored agent credential in one place."""
    # This helper is also used by alternate transports; retain constant-time
    # master-token comparison rather than creating a weaker side channel.
    import hmac

    if token and master_token and hmac.compare_digest(token, master_token):
        return AccessPolicy.administrator()
    credential = credential_lookup(token) if token else None
    return AccessPolicy.from_credential(credential) if credential is not None else None
