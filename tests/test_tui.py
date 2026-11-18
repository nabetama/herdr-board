import sys
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logic  # noqa: E402
import tui  # noqa: E402


def row(id_, section, name="agent-a", title="タイトル", sub=None, reason=None, bg=None, pr=None, elapsed="5m"):
    return {"id": id_, "name": name, "section": section, "title": title, "sub": sub, "reason": reason,
            "bg": bg, "pr": pr, "kind": "claude", "elapsed": elapsed, "status": "idle"}


def sample():
    rows = [
        row("x:p1", logic.NEEDS_YOU, name="needs", title="依頼のタイトル", reason="blocked",
            sub="you: approve Bash: テストを実行"),
        row("done-1", logic.DONE, name="finished"),
        row("work-1", logic.WORKING, name="busy-agent-with-a-very-long-name-that-overflows", bg="bg: 1 shell",
            title="とても長い日本語のタイトル" * 10,
            pr={"label": "repo#12", "ci": "fail", "draft": True, "review": "approved", "state": "open"}),
        row("idle-1", logic.IDLE, name="rest"),
    ]
    counts = {logic.NEEDS_YOU: 1, logic.DONE: 1, logic.WORKING: 1, logic.IDLE: 1}
    return {"now": 1794989000.0, "error": None, "counts": counts, "rows": rows}


class WidthTest(unittest.TestCase):
    def test_disp_width(self):
        self.assertEqual(tui.disp_width("abc"), 3)
        self.assertEqual(tui.disp_width("日本語"), 6)
        self.assertEqual(tui.disp_width("ｱ"), 1)    # 半角カナ
        self.assertEqual(tui.disp_width("Ａ"), 2)   # 全角英字
        self.assertEqual(tui.disp_width("\u304b\u3099"), 2)  # か + 結合用濁点（幅 0）

    def test_fit_pads_and_cuts(self):
        self.assertEqual(tui.fit("ab", 5), "ab   ")
        self.assertEqual(tui.fit("abcdef", 4), "abc~")
        # 全角の途中で切れるときは 1 桁空けてそろえる
        cut = tui.fit("日本語テキスト", 6)
        self.assertEqual(tui.disp_width(cut), 6)
        self.assertTrue(cut.startswith("日本"))
        self.assertIn("~", cut)
        self.assertEqual(tui.disp_width(tui.fit("日本語テキスト", 7)), 7)
        self.assertEqual(tui.fit("abc", 0), "")
        self.assertEqual(tui.fit("abcdef", 4, pad=False), "abc~")

    def test_sanitize(self):
        self.assertEqual(tui.sanitize("a\x1b[31mred\x1b[0m\tb\nc"), "ared b c")
        self.assertEqual(tui.sanitize("待ち…→完了 ● ①"), "待ち...->完了 * ?")
        self.assertEqual(tui.sanitize("ok\u200b\ufe0f!"), "ok!")
        self.assertEqual(tui.sanitize(None), "")
        for c in tui.sanitize("“引用” — ※注意 · ★"):
            self.assertNotEqual(unicodedata.east_asian_width(c), "A", c)


class RenderTest(unittest.TestCase):
    def check_lines(self, lines, cols):
        for l in lines:
            v = tui.visible(l)
            self.assertLessEqual(tui.disp_width(v), cols, v)
            for c in v:
                self.assertNotEqual(unicodedata.east_asian_width(c), "A", f"{c!r} in {v!r}")
                self.assertFalse(unicodedata.category(c).startswith("C"), f"{c!r} in {v!r}")

    def test_sections_and_widths(self):
        for cols in (80, 100, 120):
            lines, ids = tui.render(sample(), cols, "work-1")
            self.check_lines(lines, cols)
            text = "\n".join(tui.visible(l) for l in lines)
            pos = [text.index(h) for h in ("NEEDS YOU (1)", "DONE / UNSEEN (1)", "WORKING (1)", "IDLE (1)")]
            self.assertEqual(pos, sorted(pos))
            self.assertIn("1 needs you", text)
            self.assertIn("bg: 1 shell", text)
            self.assertNotIn("x:p1", text)  # ペイン ID は出さない

    def test_needs_you_line_has_reason_and_ask(self):
        lines, ids = tui.render(sample(), 100, None)
        i = next(n for n, v in ids.items() if v == "x:p1")
        second = tui.visible(lines[i + 1])
        self.assertIn("blocked", second)
        self.assertIn("you: approve Bash: テストを実行", second)

    def test_pr_only_when_wide(self):
        wide = "\n".join(tui.visible(l) for l in tui.render(sample(), 120, None)[0])
        narrow = "\n".join(tui.visible(l) for l in tui.render(sample(), 80, None)[0])
        self.assertIn("repo#12 draft CI NG approved", wide)
        self.assertNotIn("repo#12", narrow)

    def test_ids_map_both_lines_of_each_row(self):
        lines, ids = tui.render(sample(), 100, "done-1")
        rows_lines = sorted(n for n, v in ids.items() if v == "done-1")
        self.assertEqual(len(rows_lines), 2)
        self.assertEqual(rows_lines[1], rows_lines[0] + 1)
        self.assertTrue(tui.visible(lines[rows_lines[0]]).startswith(">"))  # 選択行の印

    def test_hostile_text_is_neutralized(self):
        data = sample()
        data["rows"][1]["title"] = "evil\x1b]0;title\x07\x1b[2J" + "①②" + "\u202e"
        lines, _ = tui.render(data, 80, None)
        self.check_lines(lines, 80)

    def test_empty_and_error(self):
        lines, ids = tui.render({"rows": [], "counts": {}, "now": 0, "error": "connection refused"}, 80, None)
        self.assertEqual(ids, {})
        self.assertIn("herdr: connection refused", tui.visible(lines[1]))
        self.check_lines(lines, 80)

    def test_message_replaces_hint(self):
        lines, _ = tui.render(sample(), 80, None, msg="-> needs")
        self.assertIn("-> needs", tui.visible(lines[1]))


class FrameTest(unittest.TestCase):
    def test_lines_are_cleared_before_writing_not_after(self):
        out = tui.frame(["a" * 10, "b"], 3)
        self.assertEqual(out, "\033[1;1H\033[2K" + "a" * 10 + tui.RESET
                         + "\033[2;1H\033[2K" + "b" + tui.RESET + "\033[3;1H\033[2K")
        self.assertNotIn("\033[K", out)
        self.assertTrue(tui.frame([], 1, clear_all=True).startswith("\033[2J"))


class ScrollTest(unittest.TestCase):
    def test_scroll_keeps_selection_visible(self):
        # 本文 50 行、画面 12 行（本文は 10 行）
        self.assertEqual(tui.scroll_top(50, 12, [3, 4], 0), 0)
        self.assertEqual(tui.scroll_top(50, 12, [20, 21], 0), 12)
        self.assertEqual(tui.scroll_top(50, 12, [5, 6], 12), 5)
        self.assertEqual(tui.scroll_top(5, 12, [], 30), 0)


class InputTest(unittest.TestCase):
    def test_keys(self):
        ev, rest = tui.parse_input("jk\x1b[A\x1b[B\x1bOA\rq")
        self.assertEqual(ev, [("move", 1), ("move", -1), ("move", -1), ("move", 1), ("move", -1), ("open",), ("quit",)])
        self.assertEqual(rest, "")

    def test_mouse(self):
        ev, rest = tui.parse_input("\x1b[<0;10;5M\x1b[<0;10;5m\x1b[<64;1;1M\x1b[<2;3;3M")
        self.assertEqual(ev, [("click", 4), ("move", -1)])
        self.assertEqual(rest, "")

    def test_partial_sequences_are_kept(self):
        self.assertEqual(tui.parse_input("j\x1b[<0;1"), ([("move", 1)], "\x1b[<0;1"))
        self.assertEqual(tui.parse_input("\x1b"), ([], "\x1b"))
        self.assertEqual(tui.parse_input("\x1b[<0;1;3M"), ([("click", 2)], ""))

    def test_unknown_sequences_are_skipped(self):
        self.assertEqual(tui.parse_input("\x1b[C\x1b[1;5Dj"), ([("move", 1)], ""))


class AppTest(unittest.TestCase):
    class FakeBoard:
        def __init__(self, data):
            self.data, self.focused = data, []

        def snapshot(self):
            return self.data

        def focus(self, row_id):
            self.focused.append(row_id)
            return True, "agent focus"

    def test_selection_follows_agent_and_open_uses_board_focus(self):
        data = sample()
        board = self.FakeBoard(data)
        app = tui.App(board)
        app.refresh()
        self.assertEqual(app.sel_id, "x:p1")
        app.move(1)
        app.move(1)
        self.assertEqual(app.sel_id, "work-1")
        # 並びが変わっても同じエージェントを選んだまま
        data["rows"] = [data["rows"][2], data["rows"][0], data["rows"][3]]
        app.refresh()
        self.assertEqual(app.sel_id, "work-1")
        app.move(-10)
        self.assertEqual(app.sel_id, "x:p1")
        app.open()
        self.assertEqual(board.focused, ["x:p1"])
        # 一覧が空（herdr の失敗など）でも選択は変えない
        board.data = {"rows": [], "counts": {}, "now": 0, "error": "x"}
        app.refresh()
        self.assertEqual(app.sel_id, "x:p1")
        # 選んでいた行が消えても 2 回の更新までは保ち、3 回目で先頭に移る
        board.data = data
        data["rows"] = [data["rows"][2]]
        app.refresh()
        app.refresh()
        self.assertEqual(app.sel_id, "x:p1")
        app.refresh()
        self.assertEqual(app.sel_id, "idle-1")


if __name__ == "__main__":
    unittest.main()
