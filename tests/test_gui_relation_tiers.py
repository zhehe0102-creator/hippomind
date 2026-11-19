"""Behavioural contract for the relation-tier functions (driven under Node).

The static tests in test_gui_retrieval_channels.py pattern-match the SOURCE,
which proves the text exists but not what it returns.  These cases call the
functions: relationTone/relationDash are exported from graph-canvas.js for
exactly this reason.

The four pre-existing branches must keep their meaning -- a screenshot
baseline and the frontend contract both depend on caused_by still being link,
support still being ok, contradict still being danger.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind.ledger.graph import RELATION_WEIGHTS  # noqa: E402

NODE = shutil.which("node")
CANVAS = ROOT / "hippomind" / "web" / "js" / "components" / "graph-canvas.js"

HARNESS = """
const { relationTone, relationDash, RELATION_WEIGHT_HINTS } =
  await import(process.argv[2]);
const colors = { link: "link", ok: "ok", danger: "danger",
                 ink2: "ink2", ink3: "ink3" };
const { readFileSync } = await import("node:fs");
const types = JSON.parse(readFileSync(process.argv[3], "utf8"));
const out = {};
for (const type of types) {
  out[type] = { tone: relationTone(type, colors), dash: relationDash(type) };
}
out.__weights = RELATION_WEIGHT_HINTS;
process.stdout.write(JSON.stringify(out));
"""

TYPES = [
    "caused_by", "causes", "follows",
    "support", "confirm",
    "contradicts", "supersedes",
    "same_subject", "same_task", "related_to",
    "derived_from", "a_type_from_the_future",
]


@unittest.skipUnless(NODE, "node 不在 PATH；跳过关系层级行为检查")
class RelationTierBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="hippo-r4-tiers-")
        root = Path(cls.tmp.name)
        harness = root / "harness.mjs"
        harness.write_text(HARNESS, encoding="utf-8")
        types = root / "types.json"
        types.write_text(json.dumps(TYPES), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(harness), CANVAS.as_uri(), str(types)],
            capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError("node harness failed:\n%s" % result.stderr[-2000:])
        cls.out = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- pre-existing branches must be unchanged -------------------------

    def test_causal_types_stay_link(self):
        for relation in ("caused_by", "causes"):
            with self.subTest(relation=relation):
                self.assertEqual("link", self.out[relation]["tone"])
                self.assertEqual([], self.out[relation]["dash"],
                                 "%s 原来是实线，不能变成虚线" % relation)

    def test_support_types_stay_ok(self):
        for relation in ("support", "confirm"):
            with self.subTest(relation=relation):
                self.assertEqual("ok", self.out[relation]["tone"])
                self.assertEqual([], self.out[relation]["dash"])

    def test_conflict_types_stay_danger(self):
        for relation in ("contradicts", "supersedes"):
            with self.subTest(relation=relation):
                self.assertEqual("danger", self.out[relation]["tone"])
                self.assertEqual([], self.out[relation]["dash"])

    # -- the two tiers this round added ---------------------------------

    def test_same_subject_is_its_own_colour_and_dashed(self):
        row = self.out["same_subject"]
        self.assertEqual("ink2", row["tone"],
                         "same_subject 必须有自己的颜色，不能和 related_to 同色")
        self.assertEqual([7, 3], row["dash"])

    def test_same_task_is_dotted(self):
        row = self.out["same_task"]
        self.assertEqual([2, 3], row["dash"],
                         "same_task 必须与 related_to 在画布上可区分")

    def test_the_two_tiers_are_visually_distinct(self):
        subject = self.out["same_subject"]
        task = self.out["same_task"]
        self.assertNotEqual(subject["tone"] + str(subject["dash"]),
                            task["tone"] + str(task["dash"]),
                            "两个层级必须在颜色或线型上至少差一项")

    # -- legacy and unknown stay untouched ------------------------------

    def test_related_to_and_unknown_stay_solid_and_neutral(self):
        for relation in ("related_to", "derived_from", "a_type_from_the_future"):
            with self.subTest(relation=relation):
                self.assertEqual("ink3", self.out[relation]["tone"])
                self.assertEqual([], self.out[relation]["dash"],
                                 "存量/未知关系必须仍是实线：截图基线依赖它")

    # -- weights --------------------------------------------------------

    def test_exported_weights_match_the_ledger(self):
        weights = {item["type"]: item["weight"] for item in self.out["__weights"]}
        self.assertEqual(RELATION_WEIGHTS["same_subject"], weights["same_subject"])
        self.assertEqual(RELATION_WEIGHTS["same_task"], weights["same_task"])
        self.assertEqual(RELATION_WEIGHTS["related_to"], weights["related_to"])

    def test_exported_weights_cover_every_tier_the_canvas_draws(self):
        weights = {item["type"] for item in self.out["__weights"]}
        self.assertLessEqual({"same_subject", "same_task", "related_to"}, weights,
                             "图例必须覆盖画布会画出的每一类")


if __name__ == "__main__":
    unittest.main()
