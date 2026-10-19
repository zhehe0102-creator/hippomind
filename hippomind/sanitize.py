"""Conservative redaction for untrusted chat evidence collected by watchers."""

from __future__ import annotations

import re
from typing import Any, Tuple


MAX_ARCHIVE_MESSAGE_CHARS = 4_000
REDACTED_SECRET = "[REDACTED_SECRET]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_ENVIRONMENT = "[REDACTED_ENVIRONMENT]"
REDACTED_COMMAND_OUTPUT = "[REDACTED_COMMAND_OUTPUT]"
REDACTED_APPROVAL = "[REDACTED_APPROVAL]"

_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_JSON_SECRET = re.compile(
    r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|auth(?:orization)?|secret|password|token|client[_-]?secret)["']?\s*:\s*["'])([^"'\r\n]+)(["'])'''
)
_YAML_SECRET = re.compile(
    r"(?im)(^\s*(?:api[_-]?key|access[_-]?token|auth(?:orization)?|secret|password|token|client[_-]?secret)\s*:\s*)(?!\[REDACTED_SECRET\])([^\r\n]+)"
)
_ENV_SECRET = re.compile(
    r"(?im)(\b(?:[A-Z][A-Z0-9_]*_)?(?:TOKENS?|SECRETS?|PASSWORDS?|PASSWDS?|PASS(?:ES)?|"
    r"API_?KEYS?|PRIVATE_?KEYS?|KEYS?|CREDENTIALS?|AUTH)\s*=\s*|"
    r"\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|secret|passwd|credentials|"
    r"password)\s*=\s*)([^\s\r\n]+)"
)
_BEARER_SECRET = re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)([^\s,;]+)")
_URL_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth(?:orization)?|token|secret|password|"
    r"client[_-]?secret|signature|sig|key)=)([^&#\s\"']+)"
)
_CLI_SECRET = re.compile(
    r"(?i)(--(?:api[-_]?key|access[-_]?token|auth(?:orization)?|token|secret|password)"
    r"(?:=|\s+))([^\s\r\n]+)"
)
_KNOWN_SECRET = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|sk-ant-[A-Za-z0-9_-]{8,}|"
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,})\b"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

# User/assistant transcripts sometimes quote execution context even though
# they are not tool-role rows. Keep the surrounding conversation, but remove
# complete assignment/output/approval lines before they can become searchable
# evidence. Secret-specific patterns above run first so existing callers still
# receive the more precise ``REDACTED_SECRET`` marker.
_ENVIRONMENT_LINE = re.compile(
    r"(?m)^(\s*(?:export\s+)?)([A-Z][A-Z0-9_]{1,63})(\s*=\s*)([^\r\n]*)$"
)
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:TOKENS?|SECRETS?|PASSWORDS?|PASSWDS?|PASS(?:ES)?|API_?KEYS?|PRIVATE_?KEYS?|"
    r"KEYS?|CREDENTIALS?|AUTH)(?:_|$)", re.I
)
_EMBEDDED_CREDENTIAL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]+:[^@/\s]+@")
_COMMAND_OUTPUT_LINE = re.compile(
    r"(?im)^\s*(?:\$\s+|>>>\s+|(?:command|tool)\s+output\s*:\s*|(?:stdout|stderr)\s*:\s*)[^\r\n]*$"
)
_APPROVAL_LINE = re.compile(
    r"(?im)^\s*(?:approval|approved|permission\s+request|审批(?:记录|请求)?|授权请求)\s*[:：].*$"
 )


def sanitize_chat_content(value: Any, *, limit: int = MAX_ARCHIVE_MESSAGE_CHARS) -> Tuple[str, int, bool]:
    """Return redacted bounded text, redaction count, and truncation state."""
    text = str(value or "")
    redactions = 0

    def replace_secret(_match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return REDACTED_SECRET

    def replace_prefixed(match: re.Match[str]) -> str:
        nonlocal redactions
        if match.group(2) == REDACTED_SECRET:
            return match.group(0)
        redactions += 1
        return match.group(1) + REDACTED_SECRET + (match.group(3) if match.lastindex and match.lastindex >= 3 else "")

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal redactions
        if match.group(2) == REDACTED_SECRET:
            return match.group(0)
        redactions += 1
        return match.group(1) + REDACTED_SECRET

    text = _PRIVATE_KEY.sub(replace_secret, text)
    text = _JSON_SECRET.sub(replace_prefixed, text)
    text = _YAML_SECRET.sub(replace_assignment, text)
    text = _ENV_SECRET.sub(replace_assignment, text)
    text = _BEARER_SECRET.sub(replace_assignment, text)
    text = _URL_SECRET.sub(replace_assignment, text)
    text = _CLI_SECRET.sub(replace_assignment, text)
    text = _KNOWN_SECRET.sub(replace_secret, text)

    def replace_embedded(match: re.Match[str]) -> str:
        nonlocal redactions
        if REDACTED_SECRET in match.group(0):
            return match.group(0)
        redactions += 1
        return match.group(1) + REDACTED_SECRET + "@"

    # URL 内嵌凭据（如 https://bot:pwd@host/repo）出现在普通聊天正文时，
    # 不经过环境变量行处理函数；必须在主管线统一抹除。
    text = _EMBEDDED_CREDENTIAL.sub(replace_embedded, text)

    def replace_line(_match: re.Match[str], marker: str) -> str:
        nonlocal redactions
        redactions += 1
        return marker

    def replace_environment(match: re.Match[str]) -> str:
        nonlocal redactions
        prefix, name, separator, value = match.groups()
        if REDACTED_SECRET in value or REDACTED_EMAIL in value:
            return match.group(0)
        # Keep harmless build/runtime context searchable. Only redact an
        # actually sensitive assignment or a URL containing credentials.
        if _SENSITIVE_ENV_NAME.search(name) or _EMBEDDED_CREDENTIAL.search(value):
            redactions += 1
            return prefix + name + separator + REDACTED_SECRET
        return match.group(0)

    text = _ENVIRONMENT_LINE.sub(replace_environment, text)
    text = _COMMAND_OUTPUT_LINE.sub(lambda match: replace_line(match, REDACTED_COMMAND_OUTPUT), text)
    text = _APPROVAL_LINE.sub(lambda match: replace_line(match, REDACTED_APPROVAL), text)

    def replace_email(_match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return REDACTED_EMAIL

    text = _EMAIL.sub(replace_email, text)
    maximum = max(200, int(limit))
    truncated = len(text) > maximum
    if truncated:
        text = text[:maximum - 18].rstrip() + " [TRUNCATED]"
    return text, redactions, truncated
