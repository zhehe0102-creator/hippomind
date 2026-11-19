"""Deterministic A/B assignment for memory injection.

Why this module exists
----------------------
Injection has been *observable* since round 7 (injection_trace records what was
offered), but never *measurable*: the hook only writes a trace when it injects
something, so there is no control arm.  Every "did it help?" number the system
could produce was therefore observational - an agent may have reused a term it
already knew.

The only way to upgrade observation to causation is to sometimes **not** inject,
and to record that decision with the same fidelity as an injection.  This module
owns that decision.

Design constraints
------------------
1. **Deterministic, not random-at-runtime.**  The assignment must be
   reproducible from (agent, query, bucket date) alone, so a run can be replayed
   and a disagreement can be audited.  hashlib (not random) keeps it stable
   across processes and Python versions.
2. **Stable within a conversation.**  The bucket is the query text, so the same
   prompt asked twice in the same day always lands in the same arm.  A user must
   never see memory appear, disappear, and reappear for the same question - that
   reads as a bug and destroys trust faster than a missing pack.
3. **Off by default, and off means "inject".**  The control arm exists for
   measurement, not for normal use.  HIPPOMIND_INJECTION_AB=0 (the default)
   short-circuits to treatment before hashing anything, so the default path costs
   one string comparison and behaves exactly as before this module existed.
4. **Honest about sample size.**  A 5% control arm on a personal assistant
   produces a handful of samples a week.  explain() reports the effective rate so
   an operator can see that before drawing conclusions.

The trace contract
------------------
Both arms write exactly one injection_trace line per prompt.  The control arm
writes injected=false with items=0 and an empty memory_ids list.
hippomind.injection_quality can then use the control rows as a *matched*
comparison instead of post-hoc guessing which traces had no counterpart.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

#: Environment switch.  Anything other than an explicit on-value disables A/B
#: entirely, so a typo can never silently start withholding memory.
ENV_SWITCH = "HIPPOMIND_INJECTION_AB"

#: Share of prompts that go to the control arm (no injection).  Kept small on
#: purpose: this is a personal assistant, and a withheld pack is a real (if
#: small) cost paid by the user in exchange for evidence.
DEFAULT_CONTROL_RATE = 0.20

#: Control share bounds.  A rate of 1.0 would disable injection outright and a
#: rate of 0.0 would make the module a no-op with extra bookkeeping; both are
#: configuration errors, not experiments.
MIN_CONTROL_RATE = 0.0
MAX_CONTROL_RATE = 0.5

TREATMENT = "treatment"
CONTROL = "control"


def _env_rate() -> float:
    raw = str(os.environ.get("HIPPOMIND_INJECTION_AB_RATE", "") or "").strip()
    if not raw:
        stored = _file_config().get("rate")
        try:
            raw = "" if stored is None else str(float(stored))
        except (TypeError, ValueError):
            raw = ""
    if not raw:
        return DEFAULT_CONTROL_RATE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CONTROL_RATE
    if value != value:  # NaN
        return DEFAULT_CONTROL_RATE
    return max(MIN_CONTROL_RATE, min(MAX_CONTROL_RATE, value))


def config_path() -> Path:
    """Where the persistent experiment switch lives.

    The switch started as an environment variable only, which turned out to be
    the wrong shape for the thing it controls.  The hook that needs it is
    spawned by Claude Code, not by a login shell, so an export in .zshrc never
    reaches it - the experiment would silently stay off no matter what the
    operator did.  A small JSON file next to the ledger is read by every process
    regardless of how it was launched, and editing it is a deliberate act with a
    visible diff (unlike an inherited env var nobody can audit).

    Precedence: environment variable > this file > built-in default.  The env
    var stays first so a one-off run (tests, a manual hook invocation) can
    override without touching shared state.
    """
    home = Path(os.environ.get("HIPPOMIND_HOME") or
                (Path.home() / "Library" / "Application Support" / "HippoMind"))
    return home / "injection-ab.json"


def _file_config() -> Dict[str, Any]:
    """Read the persistent switch; any problem means "off".

    Fail-closed is the only acceptable direction here: a corrupt file must not
    silently start withholding memory from the user.
    """
    path = config_path()
    try:
        if not path.is_file() or path.stat().st_size > 4096:
            return {}
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def enabled() -> bool:
    """Whether A/B assignment is active at all.

    Only an explicit truthy value turns it on.  0/off/false/empty all mean
    "inject everything", which is the pre-existing behaviour.
    """
    raw = str(os.environ.get(ENV_SWITCH, "") or "").strip().lower()
    if raw:
        return raw in {"1", "on", "true", "yes"}
    return bool(_file_config().get("enabled"))


def write_config(*, enabled: bool, rate: Optional[float] = None) -> Dict[str, Any]:
    """Persist the experiment switch atomically (0600, next to the ledger).

    Returns the payload that was written, including the effective values, so a
    caller can print exactly what is now in force instead of assuming.
    """
    payload: Dict[str, Any] = {
        "enabled": bool(enabled),
        "rate": (DEFAULT_CONTROL_RATE if rate is None
                 else max(MIN_CONTROL_RATE, min(MAX_CONTROL_RATE, float(rate)))),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "delete this file or set enabled=false to inject everything again",
    }
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + chr(10),
                          encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return payload


def control_rate() -> float:
    """Effective control-arm share, clamped to a sane range."""
    return _env_rate()


def _bucket(agent: str, query: str, day: str) -> float:
    """Map (agent, query, day) to a stable value in [0, 1).

    The day is part of the key so the experiment does not freeze: a prompt asked
    every day eventually lands in both arms, which is what makes a within-prompt
    comparison possible at all.  Within one day it is stable.
    """
    payload = "%s\\x1f%s\\x1f%s" % (str(agent or "")[:64],
                                  str(query or "")[:16_384], str(day or ""))
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    # First 8 bytes are plenty; float division keeps it uniform enough for a
    # coarse 0..0.5 split.
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def day_key(now: float | None = None) -> str:
    """UTC day bucket (YYYY-MM-DD).  Local time is deliberately not used."""
    stamp = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%d", time.gmtime(stamp))


def assign(*, agent: str, query: str, now: float | None = None) -> Dict[str, Any]:
    """Decide the arm for one prompt and explain the decision.

    Returns a dict with arm (treatment/control), inject (the boolean the caller
    should act on), enabled, rate, day and bucket.  When A/B is disabled the
    bucket is None - hashing a query for a decision that was never going to vary
    would only invite someone to read meaning into it.
    """
    if not enabled():
        return {"arm": TREATMENT, "inject": True, "enabled": False,
                "rate": 0.0, "day": "", "bucket": None,
                "reason": "ab_disabled"}
    rate = control_rate()
    if rate <= 0.0:
        return {"arm": TREATMENT, "inject": True, "enabled": True,
                "rate": 0.0, "day": "", "bucket": None,
                "reason": "rate_zero"}
    day = day_key(now)
    value = _bucket(agent, query, day)
    arm = CONTROL if value < rate else TREATMENT
    return {"arm": arm, "inject": arm == TREATMENT, "enabled": True,
            "rate": rate, "day": day, "bucket": round(value, 6),
            "reason": "ab_assign"}


def explain(now: float | None = None) -> Dict[str, Any]:
    """Describe the current configuration without touching a query.

    expected_control_per_100_prompts is deliberately an expectation: at the
    default rate a user who sends 20 prompts a day gets 4 control traces, which
    is nowhere near the 30-sample floor injection_quality.MIN_SAMPLES requires.
    Printing that arithmetic is the point - the alternative is a report that says
    "insufficient evidence" for months without saying why.
    """
    rate = control_rate()
    return {"enabled": enabled(), "rate": rate,
            "day": day_key(now),
            "expected_control_per_100_prompts": round(rate * 100.0, 1),
            "min_samples_for_conclusion": 30,
            "prompts_needed_for_min_samples": (
                int(round(30 / rate)) if rate > 0 else None)}
