"""Deterministic A/B assignment for injection.

The experiment is only worth running if the assignment is reproducible and if
the control arm is recorded with the same fidelity as the treatment arm.  These
tests pin both properties, plus the one that matters most in daily use: the
default path must behave exactly as it did before A/B existed.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_ab


class InjectionAbTests(unittest.TestCase):
    def setUp(self):
        # Clear both knobs so a developer's shell cannot change the outcome.
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in (injection_ab.ENV_SWITCH, "HIPPOMIND_INJECTION_AB_RATE"):
            os.environ.pop(key, None)

    def test_default_is_treatment_and_costs_nothing(self):
        """Off means inject.  The pre-existing behaviour must be untouched."""
        decision = injection_ab.assign(agent="claude", query="任何问题")
        self.assertEqual(injection_ab.TREATMENT, decision["arm"])
        self.assertTrue(decision["inject"])
        self.assertFalse(decision["enabled"])
        self.assertEqual("ab_disabled", decision["reason"])
        # No bucket is computed when the decision cannot vary - otherwise a
        # reader would start interpreting a number that means nothing.
        self.assertIsNone(decision["bucket"])

    def test_assignment_is_deterministic_across_calls_and_processes(self):
        os.environ[injection_ab.ENV_SWITCH] = "1"
        first = injection_ab.assign(agent="claude", query="检查 WAL 并发", now=1_760_000_000)
        second = injection_ab.assign(agent="claude", query="检查 WAL 并发", now=1_760_000_100)
        self.assertEqual(first["arm"], second["arm"])
        self.assertEqual(first["bucket"], second["bucket"])
        # Same inputs must hash identically regardless of when we ask.
        self.assertEqual(injection_ab._bucket("claude", "检查 WAL 并发", "2026-10-25"),
                         injection_ab._bucket("claude", "检查 WAL 并发", "2026-10-25"))

    def test_arm_changes_with_agent_and_query(self):
        os.environ[injection_ab.ENV_SWITCH] = "1"
        buckets = {injection_ab._bucket(agent, query, "2026-10-25")
                   for agent in ("claude", "codex")
                   for query in ("a", "b", "c", "d", "e", "f")}
        self.assertGreater(len(buckets), 1, "不同 (agent, query) 必须能落到不同分桶")

    def test_rate_bounds_and_split_share(self):
        os.environ[injection_ab.ENV_SWITCH] = "1"
        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "9"  # 越界必须被夹紧
        self.assertEqual(injection_ab.MAX_CONTROL_RATE, injection_ab.control_rate())
        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "-3"
        self.assertEqual(0.0, injection_ab.control_rate())
        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "nan"
        self.assertEqual(injection_ab.DEFAULT_CONTROL_RATE, injection_ab.control_rate())
        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "garbage"
        self.assertEqual(injection_ab.DEFAULT_CONTROL_RATE, injection_ab.control_rate())

        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "0.2"
        now = 1_760_000_000
        arms = [injection_ab.assign(agent="claude", query="q%d" % index, now=now)["arm"]
                for index in range(400)]
        share = arms.count(injection_ab.CONTROL) / len(arms)
        # 400 samples at p=0.2: the binomial 99% band is roughly 0.15..0.26.
        # A wider bound would not catch a broken split; a tighter one would
        # flake on an unlucky hash.
        self.assertGreater(share, 0.13, "对照组占比过低：%r" % share)
        self.assertLess(share, 0.28, "对照组占比过高：%r" % share)

    def test_rate_zero_is_treatment_without_hashing(self):
        os.environ[injection_ab.ENV_SWITCH] = "1"
        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "0"
        decision = injection_ab.assign(agent="claude", query="x")
        self.assertTrue(decision["inject"])
        self.assertEqual("rate_zero", decision["reason"])

    def test_switch_accepts_only_explicit_on_values(self):
        for value in ("0", "off", "false", "", "no", "maybe"):
            os.environ[injection_ab.ENV_SWITCH] = value
            self.assertFalse(injection_ab.enabled(), value)
        for value in ("1", "on", "true", "yes", "TRUE"):
            os.environ[injection_ab.ENV_SWITCH] = value
            self.assertTrue(injection_ab.enabled(), value)

    def test_explain_reports_the_sample_arithmetic(self):
        os.environ[injection_ab.ENV_SWITCH] = "1"
        os.environ["HIPPOMIND_INJECTION_AB_RATE"] = "0.1"
        info = injection_ab.explain(now=1_760_000_000)
        self.assertTrue(info["enabled"])
        self.assertAlmostEqual(10.0, info["expected_control_per_100_prompts"])
        # 30 samples at 10% needs ~300 prompts; printing this is what stops a
        # report from saying "insufficient evidence" without explaining why.
        self.assertEqual(300, info["prompts_needed_for_min_samples"])


if __name__ == "__main__":
    unittest.main()
