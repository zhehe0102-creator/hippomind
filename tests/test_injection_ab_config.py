"""注入 A/B 的持久化开关。

开关最初只有环境变量一种形态，那是错的：需要它的 hook 由 Agent 运行时
（Claude Code）spawn，不经过登录 shell，所以 .zshrc 里的 export 永远到不了它
——实验会静默保持关闭，而操作者以为已经打开。

这里钉住三件事：
1. 文件开关能被任意进程读到（不依赖环境继承）；
2. 环境变量仍然优先（一次性运行、测试可以覆盖）；
3. 坏配置一律 fail-closed（绝不能因为文件损坏就开始扣留记忆）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hippomind import injection_ab


class PersistentConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hippo-ab-")
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"HIPPOMIND_HOME": self.tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in (injection_ab.ENV_SWITCH, "HIPPOMIND_INJECTION_AB_RATE"):
            os.environ.pop(key, None)

    def test_absent_file_means_off(self):
        self.assertFalse(injection_ab.enabled())
        self.assertEqual(0.2, injection_ab.control_rate())

    def test_file_switch_is_read_without_any_environment(self):
        payload = injection_ab.write_config(enabled=True, rate=0.1)
        self.assertTrue(payload["enabled"])
        self.assertTrue(injection_ab.config_path().is_file())
        # No env var set at all: the file alone must turn the experiment on.
        self.assertTrue(injection_ab.enabled())
        self.assertAlmostEqual(0.1, injection_ab.control_rate())

    def test_written_file_is_private(self):
        injection_ab.write_config(enabled=True)
        mode = injection_ab.config_path().stat().st_mode & 0o777
        self.assertEqual(0o600, mode, "实验开关不应对其他用户可读")

    def test_environment_still_wins(self):
        injection_ab.write_config(enabled=True, rate=0.3)
        os.environ[injection_ab.ENV_SWITCH] = "0"
        self.assertFalse(injection_ab.enabled(),
                         "环境变量必须能覆盖文件（一次性运行/测试用）")
        os.environ[injection_ab.ENV_SWITCH] = "1"
        self.assertTrue(injection_ab.enabled())

    def test_rate_bounds_are_enforced_on_write(self):
        self.assertAlmostEqual(0.5, injection_ab.write_config(enabled=True, rate=9)["rate"])
        self.assertAlmostEqual(0.0, injection_ab.write_config(enabled=True, rate=-1)["rate"])

    def test_corrupt_file_fails_closed(self):
        path = injection_ab.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertFalse(injection_ab.enabled(),
                         "坏配置必须 fail-closed：不能因为解析失败就开始扣留记忆")
        self.assertEqual(0.2, injection_ab.control_rate())

    def test_non_object_root_fails_closed(self):
        path = injection_ab.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        self.assertFalse(injection_ab.enabled())

    def test_oversized_file_is_ignored(self):
        path = injection_ab.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 5000, encoding="utf-8")
        self.assertFalse(injection_ab.enabled())

    def test_explain_reports_the_file_path(self):
        state = injection_ab.explain()
        self.assertEqual(str(injection_ab.config_path()), str(injection_ab.config_path()))
        self.assertFalse(state["enabled"])

    def test_control_script_round_trip(self):
        """scripts/ab_control.py 是操作入口，必须真的改变状态。"""
        import subprocess
        script = ROOT / "scripts" / "ab_control.py"
        env = dict(os.environ, HIPPOMIND_HOME=self.tmp.name)
        env.pop(injection_ab.ENV_SWITCH, None)
        on = subprocess.run([sys.executable, str(script), "on", "0.25"],
                            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(0, on.returncode, on.stderr)
        self.assertTrue(injection_ab.enabled())
        self.assertAlmostEqual(0.25, injection_ab.control_rate())
        off = subprocess.run([sys.executable, str(script), "off"],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(0, off.returncode, off.stderr)
        self.assertFalse(injection_ab.enabled())


if __name__ == "__main__":
    unittest.main()
