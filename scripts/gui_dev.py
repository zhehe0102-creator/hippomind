#!/usr/bin/env python3
"""One-command Atlas GUI QA cycle: seed -> CDP screenshots -> report -> clean.

Wraps scripts/gui_seed.py (isolated fixture daemon) and scripts/gui_shot.mjs
(10 views x paper/night screenshots + REPORT.md).  Nothing outside the temp
fixture root and output/gui is written.

Usage:
    python3 scripts/gui_dev.py                      # seed, capture all, clean up
    python3 scripts/gui_dev.py --keep-daemon        # keep the fixture running
    python3 scripts/gui_dev.py --theme paper        # one theme only
    python3 scripts/gui_dev.py --views pulse,facts  # subset of views
    python3 scripts/gui_dev.py --seed-only          # only prepare the daemon
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "scripts" / "gui_seed.py"
SHOT = ROOT / "scripts" / "gui_shot.mjs"


def _parse_payload(stdout: str) -> dict:
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("seed did not print a JSON payload:\n" + stdout[-2000:])
    return json.loads(stdout[start:end + 1])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--theme", default="both", choices=["paper", "night", "both"],
                        help="theme(s) to capture (default: both)")
    parser.add_argument("--views", default="",
                        help="comma separated view ids (default: all 10)")
    parser.add_argument("--out", default=str(ROOT / "output" / "gui"),
                        help="screenshot/report directory")
    parser.add_argument("--memories", type=int, default=720,
                        help="seed memory count (default 720)")
    parser.add_argument("--anchor", default="",
                        help="optional ISO-8601 anchor for the 90-day distribution")
    parser.add_argument("--port", type=int, default=0,
                        help="fixture daemon port (default: free loopback port)")
    parser.add_argument("--wait", type=int, default=2000,
                        help="settle wait per view in ms (default 2000)")
    parser.add_argument("--chrome", default="", help="Chrome binary override")
    parser.add_argument("--keep-daemon", action="store_true",
                        help="leave the fixture daemon running after capture")
    parser.add_argument("--seed-only", action="store_true",
                        help="only seed and start the fixture daemon")
    parser.add_argument("--quiet", action="store_true", help="suppress per-view progress logs")
    args = parser.parse_args(argv)

    node = shutil.which("node")
    if not node:
        sys.stderr.write("[gui_dev] node is required for the screenshot harness\n")
        return 2

    seed_command = [sys.executable, str(SEED), "--daemon",
                    "--memories", str(int(args.memories))]
    if args.port:
        seed_command.extend(["--port", str(int(args.port))])
    if args.anchor:
        seed_command.extend(["--anchor", args.anchor])
    print("[gui_dev] seeding isolated fixture ...", flush=True)
    seed_result = subprocess.run(seed_command, capture_output=True, text=True, timeout=300,
                                 cwd=str(ROOT))
    if seed_result.returncode != 0:
        sys.stderr.write(seed_result.stdout)
        sys.stderr.write(seed_result.stderr)
        return seed_result.returncode
    payload = _parse_payload(seed_result.stdout)
    daemon_url = str(payload["daemon_url"])
    token_path = str(payload["token_path"])
    report_path = Path(args.out) / "REPORT.md"
    print("[gui_dev] fixture daemon %s (pid=%s, memories=%s)"
          % (daemon_url, payload.get("pid"),
             (payload.get("counts") or {}).get("memories")), flush=True)

    if args.seed_only:
        print(json.dumps({"daemon_url": daemon_url, "token_path": token_path,
                          "port": payload.get("port"), "pid": payload.get("pid")},
                         ensure_ascii=False, indent=2))
        return 0

    shot_command = [node, str(SHOT),
                    "--daemon-url", daemon_url,
                    "--token-file", token_path,
                    "--out", str(Path(args.out)),
                    "--theme", args.theme,
                    "--wait", str(int(args.wait))]
    if args.views:
        shot_command.extend(["--views", args.views])
    if args.chrome:
        shot_command.extend(["--chrome", args.chrome])
    if args.quiet:
        shot_command.append("--quiet")
    code = subprocess.call(shot_command, cwd=str(ROOT))

    if args.keep_daemon:
        print("[gui_dev] keeping fixture daemon at %s" % daemon_url)
        print("[gui_dev] token file: %s" % token_path)
        print("[gui_dev] report: %s" % report_path)
        print(json.dumps({"daemon_url": daemon_url, "token_path": token_path,
                          "report": str(report_path), "exit_code": code},
                         ensure_ascii=False, indent=2))
        return code

    cleanup = subprocess.run([sys.executable, str(SEED), "--clean"], capture_output=True,
                             text=True, timeout=60, cwd=str(ROOT))
    message = (cleanup.stdout or cleanup.stderr).strip()
    if message:
        print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

