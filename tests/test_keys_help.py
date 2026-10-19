"""GUI 键位帮助覆盖契约（迭代 44）：_on_key 处理的每个键必须在帮助行可见。"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class KeysHelpContractTests(unittest.TestCase):
    def test_every_bound_key_appears_in_help_line(self):
        src = (ROOT / "hippomind" / "network_visualizer.py").read_text()
        # 提取 _on_key 中全部键绑定
        body = src.split("def _on_key(self, event) -> None:", 1)[1]
        body = body.split("def ", 2)[0] if "def " in body else body
        bound = set(re.findall(r'key == "([^"]+)"', body))
        self.assertTrue(bound, "应至少解析出一个键绑定")
        # 帮助行文本（keys_help 赋值）
        m = re.search(r'self\.keys_help = \(([^)]+)\)', src)
        self.assertIsNotNone(m, "keys_help 帮助行必须存在")
        help_text = m.group(1)
        for key in sorted(bound):
            label = {" ": "Space"}.get(key, key.upper())
            self.assertIn(label, help_text,
                          f"键 {key!r} 在 _on_key 有绑定但帮助行未展示")

    def test_timeline_and_snapshot_documented_in_help(self):
        src = (ROOT / "hippomind" / "network_visualizer.py").read_text()
        m = re.search(r'self\.keys_help = \(([^)]+)\)', src)
        help_text = m.group(1)
        self.assertIn("时间轴回放", help_text)
        self.assertIn("快照", help_text)


if __name__ == "__main__":
    unittest.main()
