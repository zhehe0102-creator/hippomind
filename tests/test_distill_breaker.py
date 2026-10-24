"""Extractor circuit breaker: a failing API must not burn every cycle."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.distill import ClaudeExtractor, Candidate


class CircuitBreakerTests(unittest.TestCase):
    def test_two_failures_open_the_breaker(self):
        extractor = ClaudeExtractor(timeout=1)
        extractor.binary = "/bin/false"
        candidate = Candidate("m1", "task", "内容", "codex", "", "", "", "", {})
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            self.assertIsNone(extractor.extract([candidate]))
            self.assertTrue(extractor.available)
            self.assertIsNone(extractor.extract([candidate]))
        self.assertFalse(extractor.available)
        self.assertIn("OSError", extractor.last_error)

    def test_success_resets_the_breaker(self):
        extractor = ClaudeExtractor(timeout=1)
        extractor.binary = "/bin/echo"
        candidate = Candidate("m1", "task", "内容", "codex", "", "", "", "", {})
        payload = '{"claims":[{"subject":"S","predicate":"P","slot_key":"k","value":"v","confidence":0.9,"source":1}]}'
        completed = mock.Mock(returncode=0, stdout=payload, stderr="")
        with mock.patch("subprocess.run", return_value=completed):
            claims = extractor.extract([candidate])
        self.assertEqual(1, len(claims))
        self.assertTrue(extractor.available)


if __name__ == "__main__":
    unittest.main()
