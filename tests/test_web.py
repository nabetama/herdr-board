"""ブラウザ版のキーの判定（static/index.html の keyAction）のテスト。node があるときだけ動かす。"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")


def key_action_source():
    start = HTML.index("const KEY_BY_CODE")
    end = HTML.index("\n}\n", HTML.index("function keyAction")) + 3
    return HTML[start:end]


@unittest.skipUnless(shutil.which("node"), "node が無い")
class KeyActionTest(unittest.TestCase):
    def actions(self, cases):
        """cases: [(イベントの属性, 詳細を開いているか)] -> keyAction の戻り値のリスト"""
        script = key_action_source() + "\nconst cases = " + json.dumps(cases) + ";\n" + \
            "console.log(JSON.stringify(cases.map(([ev, open]) => keyAction(ev, open))));\n"
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_keys(self):
        cases = [
            ({"key": "j", "code": "KeyJ"}, False), ({"key": "k", "code": "KeyK"}, False),
            ({"key": "ArrowDown"}, False), ({"key": "ArrowUp"}, False), ({"key": "Enter"}, False),
            ({"key": "d", "code": "KeyD"}, False), ({"key": "d", "code": "KeyD"}, True),
            ({"key": "J", "shiftKey": True}, True), ({"key": "K", "shiftKey": True}, True),
            ({"key": "PageDown"}, True), ({"key": "PageUp"}, True),
        ]
        self.assertEqual(self.actions(cases), ["down", "up", "down", "up", "open", "detail", "detail",
                                               "line-down", "line-up", "page-down", "page-up"])

    def test_detail_keys_only_when_open(self):
        cases = [({"key": "J", "shiftKey": True}, False), ({"key": "PageDown"}, False)]
        self.assertEqual(self.actions(cases), [None, None])  # 閉じているときはブラウザのスクロールのまま

    def test_modifiers_and_other_keys_are_ignored(self):
        cases = [({"key": "j", "metaKey": True}, False), ({"key": "d", "ctrlKey": True}, False),
                 ({"key": "k", "altKey": True}, False), ({"key": "x"}, False), ({"key": "D"}, False)]
        self.assertEqual(self.actions(cases), [None] * 5)

    def test_code_is_used_only_when_key_is_unknown(self):
        cases = [({"key": "Process", "code": "KeyJ"}, False), ({"key": "Unidentified", "code": "KeyD"}, False),
                 ({"key": "Process", "code": "KeyJ", "shiftKey": True}, True),
                 # 配列が違うキーボードでは、ev.key を優先する（KeyJ の位置に別の文字があっても j としない）
                 ({"key": "h", "code": "KeyJ"}, False)]
        self.assertEqual(self.actions(cases), ["down", "detail", "line-down", None])


class ListenerTest(unittest.TestCase):
    def test_keydown_on_window_capture(self):
        self.assertRegex(HTML, r'window\.addEventListener\("keydown", ev => \{')
        m = re.search(r'window\.addEventListener\("keydown", ev => \{.*?\n\}, true\);', HTML, re.S)
        self.assertIsNotNone(m, "capture フェーズで登録する")
        self.assertNotIn('document.addEventListener("keydown"', HTML)

    def test_detail_button(self):
        self.assertIn('id="detail-btn"', HTML)


if __name__ == "__main__":
    unittest.main()
