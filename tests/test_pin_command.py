"""``hippomind pin`` end-to-end: the CLI wiring that ``cmd_pin`` reads.

The regression this file exists for
-----------------------------------
``build_parser`` registered the positional as ``"memory-id"``, and argparse
derives a positional's ``dest`` from its own name -- so the namespace carried
``memory-id`` while ``cmd_pin`` read ``args.memory_id``.  Every single
``hippomind pin <id>`` invocation therefore died with::

    AttributeError: 'Namespace' object has no attribute 'memory_id'

with a bare traceback (``main`` only formats LedgerError/OSError/ValueError).
The ledger API itself was fine -- ``tests/test_pin_memory.py`` exercised
``SQLiteLedger.pin_memory`` directly, which is exactly why the broken CLI layer
went unnoticed.

The fix keeps the *spelling in the help text* by passing ``metavar``, and uses
the identifier only as the dest.  Both halves are pinned here: the binding, and
the fact that the rendered usage text still says ``memory-id`` and never leaks
the underscore form.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hippomind.cli import build_parser, main
from hippomind.ledger import SQLiteLedger


def _record(text: str, marker: str) -> dict:
    return {"kind": "fact", "scope": "shared", "source_agent": "t",
            "content": text, "evidence": [f"t:{marker}"]}


class PinNamespaceTests(unittest.TestCase):
    """The dest must be the identifier cmd_pin actually reads."""

    def test_pin_positional_binds_memory_id(self):
        args = build_parser().parse_args(["pin", "mem-abc", "--ledger", "/tmp/x.sqlite"])
        self.assertEqual(args.memory_id, "mem-abc")
        self.assertNotIn("memory-id", vars(args))

    def test_unpin_flag_still_present(self):
        args = build_parser().parse_args(["pin", "mem-abc", "--unpin"])
        self.assertTrue(args.unpin)
        self.assertEqual(args.memory_id, "mem-abc")

    def test_help_text_still_reads_the_dashed_name(self):
        """Repairing the dest must not have rewritten operator-facing text."""
        choices = build_parser()._subparsers._group_actions[0].choices
        positional = [a for a in choices["pin"]._actions if a.dest == "memory_id"]
        self.assertEqual(len(positional), 1)
        self.assertEqual(positional[0].metavar, "memory-id")
        lines = choices["pin"].format_help().splitlines()
        # the dashed spelling is what users see, in usage and in the listing
        self.assertIn("memory-id", [line.strip() for line in lines])
        self.assertTrue(any(line.strip().endswith("memory-id") for line in lines))
        self.assertNotIn("memory_id", choices["pin"].format_help())

    def test_root_listing_still_describes_pin(self):
        """The sub-command keeps its name and its help text side by side."""
        listing = [" ".join(line.split()) for line in build_parser().format_help().splitlines()
                   if "置顶/取消置顶记忆" in line]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0], "pin 置顶/取消置顶记忆（--unpin 取消）")


class PinCommandTests(unittest.TestCase):
    """Real CLI runs against a throwaway ledger -- never a live one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-pin-cmd-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "pin.sqlite"
        self.runtime = self.root / "rt"
        self.led = SQLiteLedger(self.ledger)
        self.target = self.led.ingest(_record("生产数据库备份策略是每日全量", "bak")).memory_id
        self.other = self.led.ingest(_record("常规会议周报记忆", "n1")).memory_id

    def _pin(self, *extra: str) -> tuple[int, dict]:
        out = io.StringIO()
        argv = ["pin", *extra, "--ledger", str(self.ledger),
                "--runtime-dir", str(self.runtime), "--port", "1"]
        with redirect_stdout(out):
            code = main(argv)
        return code, json.loads(out.getvalue())

    def test_pin_marks_the_memory_and_reports_it(self):
        code, payload = self._pin(self.target)
        self.assertEqual(code, 0)
        self.assertEqual(payload["memory_id"], self.target)
        self.assertTrue(payload["pinned"])
        pinned = self.led.list_pinned()
        self.assertEqual([row["memory_id"] for row in pinned], [self.target])
        self.assertTrue(payload["event_id"])

    def test_unpin_clears_it_again(self):
        self._pin(self.target)
        code, payload = self._pin(self.target, "--unpin")
        self.assertEqual(code, 0)
        self.assertFalse(payload["pinned"])
        self.assertEqual(self.led.list_pinned(), [])

    def test_second_pin_is_idempotent_for_the_operator(self):
        self._pin(self.target)
        _code, payload = self._pin(self.target)
        self.assertTrue(payload["pinned"])
        self.assertEqual(len(self.led.list_pinned()), 1)

    def test_unknown_memory_id_is_a_clean_error_exit_two(self):
        """No traceback: cmd_pin's LedgerError must go through the error path."""
        out, err = io.StringIO(), io.StringIO()
        argv = ["pin", "mem-does-not-exist", "--ledger", str(self.ledger),
                "--runtime-dir", str(self.runtime), "--port", "1"]
        from contextlib import redirect_stderr
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        self.assertEqual(code, 2)
        self.assertIn("记忆不存在", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        # nothing got pinned on the way
        self.assertEqual(self.led.list_pinned(), [])

    def test_restricted_agent_cannot_pin_directly(self):
        config = self.root / "agent.json"
        config.write_text(json.dumps({"token": "x" * 32}), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["pin", self.target, "--ledger", str(self.ledger),
                         "--runtime-dir", str(self.runtime), "--port", "1",
                         "--agent-config", str(config)])
        self.assertEqual(code, 2)
        self.assertIn("受限 Agent", err.getvalue())
        self.assertEqual(self.led.list_pinned(), [])

    def test_pin_does_not_touch_the_other_rows(self):
        self._pin(self.target)
        with self.led._reading() as conn:
            rows = conn.execute("SELECT memory_id, pinned FROM memories").fetchall()
        self.assertEqual({r["memory_id"]: r["pinned"] for r in rows},
                         {self.target: 1, self.other: 0})


if __name__ == "__main__":
    unittest.main()
