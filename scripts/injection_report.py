#!/usr/bin/env python3
"""Injection effectiveness report: did the agent reuse what we injected?"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.injection_quality import report  # noqa: E402
from hippomind.ledger import SQLiteLedger  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Injection reuse report")
    parser.add_argument("--ledger", default=os.path.expanduser(
        "~/Library/Application Support/HippoMind/hippomind.sqlite"))
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    ledger = SQLiteLedger(args.ledger)
    try:
        result = report(ledger, window_minutes=args.window_minutes)
    finally:
        ledger.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("traces=%s with_ids=%s sampled=%s reused=%s reuse_rate=%s mean_overlap=%s" % (
            result["traces"], result["traced_with_ids"], result["sampled"],
            result["reused"], result["reuse_rate"], result["mean_overlap"]))
        for row in result["details"][-8:]:
            print("  %s %s ids=%s replies=%s overlap=%s used=%s" % (
                row["at"], row["agent"], row["memory_ids"], row["replies"],
                row["overlap"], row["used"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
