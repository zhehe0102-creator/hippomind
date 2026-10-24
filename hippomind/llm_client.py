"""Direct LLM API client: talk to the model endpoint without the local CLI.

Why this exists
---------------
hippomind/distill.py and scripts/conflict_review.py used to reach a model only
through the 'claude' CLI in print mode.  On this machine that CLI is broken: its
default model (zcode/GLM-5.3-Flash[1m]) is not in the model catalogue of the
installed version, so 'claude -p' hangs or fails outright.  Both callers
therefore tripped their circuit breaker on every pass and fell back to the
deterministic heuristic, which is why production reported extractor=heuristic.

The endpoint behind that CLI is fine.  A plain POST {base}/v1/messages with the
same credentials returns a normal answer in seconds.  This module is that call,
and nothing else: no new dependency (stdlib urllib only), no model list, no
retries that could stall a daemon tick.

Contract
--------
complete() returns str on success and None on *any* failure -- bad config, HTTP
error, timeout, unparseable body, empty answer, open breaker.  It never raises,
because every caller has a working fallback and a raised exception would turn a
degraded pass into a crashed one.

Credentials never leave this module.  describe() reports whether a key is
configured, never the key; LLMConfig.__repr__ masks it; failure messages are
redacted before they reach last_error or a log line.

Configuration
-------------
Highest priority first:

1. environment -- HIPPOMIND_LLM_BASE_URL / HIPPOMIND_LLM_API_KEY /
   HIPPOMIND_LLM_MODEL;
2. the 'env' block of ~/.claude/settings.json -- ANTHROPIC_BASE_URL /
   ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) / ANTHROPIC_MODEL;
3. unavailable.

Reusing the CLI's own settings file is deliberate: it is the configuration the
operator already maintains, and it is the one the measurements in
docs/DISTILL.md were taken against.

Cancellation
------------
complete(..., cancel=token) accepts a threading.Event (or any callable
predicate).  The token is checked before the request; when one is supplied the
blocking read happens on a daemon worker thread so the caller can also abandon a
request that is already in flight instead of waiting out the full timeout.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

__all__ = [
    "Breaker",
    "LLMConfig",
    "available",
    "breaker",
    "complete",
    "describe",
    "load_config",
    "parse_completion",
    "reset",
]

#: Wall-clock ceiling for one request.  Measured latency against this endpoint
#: (docs/DISTILL.md) is 3-6s for a small answer and 25-38s for a real 8-item
#: extraction batch, so 45s leaves headroom without letting a wedged socket
#: occupy a daemon tick for minutes.
DEFAULT_TIMEOUT = 45.0
DEFAULT_MAX_TOKENS = 2_048
#: Consecutive failures before the breaker opens.  Same numbers as
#: distill.ClaudeExtractor and conflict_review.ClaudeDecider: two failures then
#: ten minutes of silence, so a dead endpoint costs at most two timeouts per
#: cooldown window instead of one per batch.
BREAKER_THRESHOLD = 2
COOLDOWN_SECONDS = 600.0
ANTHROPIC_VERSION = "2023-06-01"
MESSAGES_PATH = "/v1/messages"
#: Retries for a *transient* transport failure (a dropped connection, not a bad
#: request).  Measured on this deployment: the same prompt that answers in 10s
#: can also come back as RemoteDisconnected with no response body, at roughly a
#: 1-in-6 rate across runs, and it succeeds on the next attempt.  Without a
#: retry a single dropped socket silently degrades a whole 8-item batch to the
#: heuristic, which is the exact failure mode this module exists to remove.
#: Only connection-level errors are retried -- an HTTP 4xx/5xx or a malformed
#: body is a real answer and retrying it would just waste the budget.
TRANSIENT_RETRIES = 2
#: Base backoff between retries; doubles each attempt.  Kept short because this
#: runs inside a daemon tick with a 45s per-request budget.
RETRY_BACKOFF = 1.0
#: Settings file the local CLI reads.  Overridable so a test (or a second
#: profile) can point at a fixture instead of the operator's real file.
SETTINGS_ENV = "HIPPOMIND_LLM_SETTINGS"
DEFAULT_SETTINGS_PATH = "~/.claude/settings.json"
#: Last-resort model id.  It is the one verified against this deployment on
#: 2026-10-24 (HTTP 200, correct content); explicit configuration always wins,
#: so this only matters when a settings file carries a base URL and key but no
#: model field at all.
DEFAULT_MODEL = "deepseek-v4.1-flash"
MAX_ERROR_CHARS = 200


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LLMConfig:
    """One resolved endpoint + credential + model triple.

    api_key is part of the value because the request needs it, but it must never
    be rendered: repr/str mask it so an accidental print(config), a traceback,
    or a debug log cannot leak it.
    """

    base_url: str
    api_key: str
    model: str
    source: str = ""

    def __repr__(self) -> str:
        return "LLMConfig(base_url=%r, model=%r, api_key=%s, source=%r)" % (
            self.base_url, self.model, "***" if self.api_key else "''", self.source)

    __str__ = __repr__

    @property
    def messages_url(self) -> str:
        return self.base_url.rstrip("/") + MESSAGES_PATH


#: Parsed settings cache keyed by (path, mtime, size).  distill() runs 8 model
#: calls per pass and the daemon repeats that every tick, so re-reading and
#: re-parsing the settings file on every call is pointless work; the key makes
#: an edit to the file invalidate it immediately.
_SETTINGS_CACHE: Dict[str, Any] = {"key": None, "env": {}}


def _settings_path() -> Path:
    raw = os.environ.get(SETTINGS_ENV, "").strip() or DEFAULT_SETTINGS_PATH
    return Path(os.path.expanduser(raw))


def _settings_env() -> Dict[str, str]:
    """Read the 'env' block of the CLI settings file, tolerating anything.

    A missing, unreadable, malformed, or non-object file is simply "no
    configuration": the caller falls through to the next source rather than
    crashing a daemon tick over an unrelated file.
    """
    path = _settings_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _SETTINGS_CACHE["key"] == key:
        return _SETTINGS_CACHE["env"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    block = payload.get("env") if isinstance(payload, Mapping) else None
    if not isinstance(block, Mapping):
        return {}
    env = {str(name): str(value) for name, value in block.items()
           if isinstance(value, (str, int, float))}
    _SETTINGS_CACHE["key"] = key
    _SETTINGS_CACHE["env"] = env
    return env


def _normalise_base(raw: Any) -> str:
    """Turn a configured base into the origin the messages path hangs off.

    Operators write this field in three shapes (https://host, https://host/,
    https://host/v1); all three must produce https://host/v1/messages.  A bare
    host gets https:// so a typo'd scheme cannot silently become a relative
    path.
    """
    base = str(raw or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" not in base:
        base = "https://" + base
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base.rstrip("/")


def load_config() -> Optional[LLMConfig]:
    """Resolve the endpoint, or None when it is not configured.

    Environment wins over the settings file so an operator (or a test) can
    override one field without editing the CLI's configuration.
    """
    settings = _settings_env()

    base = (os.environ.get("HIPPOMIND_LLM_BASE_URL", "").strip()
            or str(settings.get("ANTHROPIC_BASE_URL", "")).strip()
            or str(settings.get("CLAUDE_CODE_API_BASE_URL", "")).strip())
    key = (os.environ.get("HIPPOMIND_LLM_API_KEY", "").strip()
           or str(settings.get("ANTHROPIC_API_KEY", "")).strip()
           or str(settings.get("ANTHROPIC_AUTH_TOKEN", "")).strip())
    model = (os.environ.get("HIPPOMIND_LLM_MODEL", "").strip()
             or str(settings.get("ANTHROPIC_MODEL", "")).strip()
             or str(settings.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")).strip()
             or DEFAULT_MODEL)
    source = "env" if os.environ.get("HIPPOMIND_LLM_BASE_URL", "").strip() else "settings"
    base = _normalise_base(base)
    if not base or not key or not model:
        return None
    return LLMConfig(base, key, model, source)


def available() -> bool:
    """True when a request could be attempted (config only, no network)."""
    return load_config() is not None


def _safe_config() -> Optional[LLMConfig]:
    try:
        return load_config()
    except Exception:  # noqa: BLE001 - config lookup must never raise
        return None


def _redact(text: Any) -> str:
    """Strip the configured key out of a message before it is stored or logged.

    Transport errors are not expected to contain credentials, but last_error is
    copied into logs and JSON payloads, so the guarantee is enforced here rather
    than assumed.
    """
    cleaned = str(text or "")
    for name in ("HIPPOMIND_LLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            cleaned = cleaned.replace(value, "***")
    config = _safe_config()
    if config is not None and config.api_key:
        cleaned = cleaned.replace(config.api_key, "***")
    return cleaned[:MAX_ERROR_CHARS]


def describe() -> Dict[str, Any]:
    """Report the resolved endpoint *without* the credential.

    This is what /stats and the CLI print, so it is the one place a leak would
    be permanent; it deliberately exposes only has_key.
    """
    config = _safe_config()
    report: Dict[str, Any] = {
        "available": config is not None,
        "base_url": config.base_url if config is not None else "",
        "model": config.model if config is not None else "",
        "has_key": bool(config.api_key) if config is not None else False,
        "source": config.source if config is not None else "",
        "breaker": _BREAKER.status(),
    }
    if config is None:
        report["reason"] = ("未配置 LLM 端点：设置 HIPPOMIND_LLM_BASE_URL / "
                            "HIPPOMIND_LLM_API_KEY 或 ~/.claude/settings.json 的 env 段")
    return report


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
class Breaker:
    """Consecutive-failure breaker with a fixed cooldown.

    Same discipline as the CLI extractor it replaces: N consecutive failures
    open the breaker for a fixed window, one success closes it.  clock is
    injectable so tests can advance time without sleeping.
    """

    def __init__(self, threshold: int = BREAKER_THRESHOLD,
                 cooldown: float = COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.threshold = max(1, int(threshold))
        self.cooldown = float(cooldown)
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self.last_error = ""
        self.calls = 0
        self.failures = 0
        self.successes = 0

    @property
    def tripped(self) -> bool:
        """True while the cooldown is holding the client offline."""
        return self._clock() < self._open_until

    def allow(self) -> bool:
        return not self.tripped

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0
        self.successes += 1

    def record_failure(self, reason: Any) -> None:
        self._failures += 1
        self.failures += 1
        self.last_error = _redact(reason)
        if self._failures >= self.threshold:
            self._open_until = self._clock() + self.cooldown
            self._failures = 0

    def status(self) -> Dict[str, Any]:
        return {"tripped": self.tripped, "calls": self.calls,
                "failures": self.failures, "successes": self.successes,
                "last_error": self.last_error}

    def reset(self) -> None:
        self._failures = 0
        self._open_until = 0.0
        self.last_error = ""
        self.calls = 0
        self.failures = 0
        self.successes = 0


_BREAKER = Breaker()


def breaker() -> Breaker:
    """The process-wide breaker complete() consults."""
    return _BREAKER


def reset() -> None:
    """Forget cached configuration and close the breaker (tests, retuning)."""
    _SETTINGS_CACHE["key"] = None
    _SETTINGS_CACHE["env"] = {}
    _BREAKER.reset()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
class _Cancelled(Exception):
    """Raised internally when the caller's cancel token fires mid-flight."""


def _cancelled(token: Any) -> bool:
    """Evaluate a cancel token: an Event, a callable, or a plain truthy."""
    if token is None:
        return False
    checker = getattr(token, "is_set", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - a broken token must not break a call
            return False
    if callable(token):
        try:
            return bool(token())
        except Exception:  # noqa: BLE001
            return False
    return bool(token)


def _post(request: urllib.request.Request, timeout: float) -> str:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _capture(box: Dict[str, Any], request: urllib.request.Request,
             timeout: float) -> None:
    """Worker body: land the outcome in box instead of raising across threads."""
    try:
        box["raw"] = _post(request, timeout)
    except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
        box["error"] = exc


def _dispatch(request: urllib.request.Request, timeout: float, cancel: Any) -> str:
    """Run the blocking request, honouring cancel if one was supplied.

    Without a token this is the plain blocking call -- the common path stays
    exactly as simple as it looks.  With a token the read moves to a daemon
    worker and the caller polls it, so a shutdown does not have to wait out the
    socket timeout.
    """
    if cancel is None:
        return _post(request, timeout)
    box: Dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: _capture(box, request, timeout),
        name="hippomind-llm", daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout + 1.0
    while worker.is_alive():
        if _cancelled(cancel):
            raise _Cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request exceeded %.0fs" % timeout)
        worker.join(min(0.05, remaining))
    if "error" in box:
        raise box["error"]
    return str(box.get("raw", ""))


def parse_completion(raw: Any) -> Optional[str]:
    """Extract the assistant text from a Messages API body.

    The endpoint returns {"content":[{"type":"text","text":"..."}]}; other block
    types (tool use, thinking) are skipped rather than stringified, so a caller
    never receives JSON machinery it did not ask for.  Anything malformed or
    empty is None.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return None
    parts = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        if block.get("type", "text") != "text":
            continue
        piece = block.get("text")
        if isinstance(piece, str):
            parts.append(piece)
    text = "".join(parts).strip()
    return text or None


#: Errors that mean "the connection failed", not "the request was wrong".
#: A retry is safe and cheap for these and pointless for everything else.
_TRANSIENT = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


def complete(prompt: Any, *, max_tokens: int = DEFAULT_MAX_TOKENS,
             timeout: float = DEFAULT_TIMEOUT, model: str = "",
             cancel: Any = None) -> Optional[str]:
    """Ask the endpoint for a completion.  None on any failure, never raises.

    model overrides the configured model for one call (the conflict decider and
    the extractor use the same endpoint but may want different budgets).
    cancel is described in the module docstring.
    """
    text = str(prompt or "")
    if not text.strip():
        return None
    config = _safe_config()
    if config is None:
        return None
    if not _BREAKER.allow():
        return None
    if _cancelled(cancel):
        return None
    try:
        budget = max(1, int(max_tokens))
    except (TypeError, ValueError):
        budget = DEFAULT_MAX_TOKENS
    try:
        limit = max(1.0, float(timeout))
    except (TypeError, ValueError):
        limit = DEFAULT_TIMEOUT
    body = json.dumps({
        "model": str(model or config.model),
        "max_tokens": budget,
        "messages": [{"role": "user", "content": text}],
    }, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "x-api-key": config.api_key,
        "authorization": "Bearer " + config.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    try:
        request = urllib.request.Request(config.messages_url, data=body,
                                         headers=headers, method="POST")
    except (TypeError, ValueError) as exc:
        _BREAKER.record_failure(exc)
        return None
    _BREAKER.calls += 1
    raw = ""
    last: Any = None
    for attempt in range(TRANSIENT_RETRIES + 1):
        try:
            raw = _dispatch(request, limit, cancel)
            last = None
            break
        except _Cancelled:
            # A cancellation is the caller's decision, not an endpoint failure;
            # it must not count towards opening the breaker, and must not retry.
            return None
        except urllib.error.HTTPError as exc:
            # A status code is the server's answer, not a dropped socket:
            # retrying it would just spend the budget twice on the same refusal.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001 - a body-less error fails cleanly
                detail = ""
            _BREAKER.record_failure("HTTP %s: %s" % (exc.code, detail))
            return None
        except Exception as exc:  # noqa: BLE001 - the transport must never escape
            last = exc
            if not isinstance(exc, _TRANSIENT) or attempt >= TRANSIENT_RETRIES:
                break
            # Transient: the connection dropped or timed out.  Wait briefly and
            # re-send the identical request; measured to succeed on retry.
            delay = RETRY_BACKOFF * (2 ** attempt)
            if _cancelled(cancel):
                return None
            if cancel is None:
                time.sleep(delay)
            else:
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if _cancelled(cancel):
                        # The caller withdrew: that is not an endpoint failure,
                        # so it must not count towards opening the breaker.
                        return None
                    time.sleep(min(0.05, delay))
            if _cancelled(cancel):
                return None
    if last is not None:
        _BREAKER.record_failure("%s: %s" % (type(last).__name__, last))
        return None
    answer = parse_completion(raw)
    if answer is None:
        _BREAKER.record_failure("empty or unparseable completion")
        return None
    _BREAKER.record_success()
    return answer
