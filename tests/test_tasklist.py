"""--tasks（作業の一覧表）の解析と重ね方のテスト。データはすべて架空のもの。"""
import os
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import board  # noqa: E402
import logic  # noqa: E402
import tasklist  # noqa: E402
import tui  # noqa: E402

SAMPLE_JA = """# 振った仕事

説明の文章。

## 進行中

| 名前 | ペイン | リポジトリ | 開始 | 状態 | 依頼内容 | 結果・メモ |
|---|---|---|---|---|---|---|
| alpha | w1:p1 | sample-repo | 01-02 10:00 | 確認待ち | 設定ファイルの整理 | 方針 A か B かを聞いている |
| beta | w1:p2 | sample-repo | 01-02 11:00 | working | テストの追加 | |
| gamma | w1:p3 | other-repo | 01-02 12:00 | 質問待ち（パスワードの扱い） | 認証まわりの調査 | パイプ \\| を含むメモ |

## 完了

| 名前 | リポジトリ | 開始 | 終了 | 依頼内容 | 結果 |
| old-1 | sample-repo | 01-01 09:00 | 01-01 09:30 | 古い仕事 | 済み |
| old-2 | other-repo | 01-01 10:00 | 01-01 18:05 | 新しい仕事 | 済み 2 |
|---|---|---|---|---|---|
| old-3 | sample-repo | 01-01 11:00 | 01-01 12:5x | 途中の仕事 | 済み 3 |

## メモ

| 名前 | 状態 |
|---|---|
| ignored | 確認待ち |
"""

SAMPLE_EN = """## In progress

| Agent | Task | Status | Note |
|---|---|---|---|
| delta | Write docs | needs-you | asks about tone |

## Done

| name | repo | ended | task | result |
|---|---|---|---|---|
| eps | r | 2026-01-03 10:00 | t1 | ok |
| zeta | r | 2026-01-03 09:00 | t2 | ok |
"""


class ParseTest(unittest.TestCase):
    def test_japanese_sections_and_columns_by_name(self):
        d = tasklist.parse(SAMPLE_JA)
        self.assertEqual([r["name"] for r in d["active"]], ["alpha", "beta", "gamma"])
        self.assertEqual(d["active"][0], {"name": "alpha", "status": "確認待ち", "task": "設定ファイルの整理",
                                          "note": "方針 A か B かを聞いている"})
        self.assertNotIn("pane", d["active"][0])  # ペインの列は使わない
        self.assertEqual(d["active"][2]["note"], "パイプ | を含むメモ")
        # 完了の表は見出し行の直後に区切り行がなく、途中に区切り行がある
        self.assertEqual([r["name"] for r in d["done"]], ["old-1", "old-2", "old-3"])
        self.assertEqual(d["done"][1], {"name": "old-2", "repo": "other-repo", "ended": "01-01 18:05",
                                        "task": "新しい仕事", "result": "済み 2"})

    def test_other_sections_are_ignored(self):
        d = tasklist.parse(SAMPLE_JA)
        self.assertNotIn("ignored", [r["name"] for r in d["active"] + d["done"]])

    def test_english(self):
        d = tasklist.parse(SAMPLE_EN)
        self.assertEqual(d["active"], [{"name": "delta", "status": "needs-you", "task": "Write docs",
                                        "note": "asks about tone"}])
        self.assertEqual([r["name"] for r in d["done"]], ["eps", "zeta"])

    def test_table_without_name_column_and_garbage(self):
        d = tasklist.parse("## 進行中\n| 状態 | 依頼内容 |\n|---|---|\n| 確認待ち | x |\n")
        self.assertEqual(d["active"], [])
        self.assertEqual(tasklist.parse(""), {"active": [], "done": []})
        self.assertEqual(tasklist.parse(None), {"active": [], "done": []})
        d = tasklist.parse("## 進行中\n| 名前 |\n| only-name |\n| a | extra | cells |\n|\n")
        self.assertEqual([r["name"] for r in d["active"]], ["only-name", "a"])

    def test_needs_you_words(self):
        for s in ("質問待ち", "確認待ち", "判断待ち（どちらの案か）", "needs-you", "Needs You", "question"):
            self.assertTrue(tasklist.needs_you(s), s)
        for s in ("working", "done", "", None, "レビュー中"):
            self.assertFalse(tasklist.needs_you(s), s)

    def test_recent_done_order_and_count(self):
        d = tasklist.parse(SAMPLE_JA)
        self.assertEqual([r["name"] for r in tasklist.recent_done(d["done"], 5)], ["old-2", "old-3", "old-1"])
        self.assertEqual([r["name"] for r in tasklist.recent_done(d["done"], 1)], ["old-2"])
        self.assertEqual(tasklist.recent_done(d["done"], 0), [])
        # 同じ終了時刻なら表で下にあるものを新しいとみなす
        same = [{"name": "a", "ended": "01-01 10:00"}, {"name": "b", "ended": "01-01 10:00"}]
        self.assertEqual([r["name"] for r in tasklist.recent_done(same)], ["b", "a"])


class TaskFileTest(unittest.TestCase):
    def test_missing_file_gives_warning(self):
        tf = tasklist.TaskFile("/nonexistent/dir/tasks.md")
        self.assertEqual(tf.load(), {"active": [], "done": []})
        self.assertTrue(tf.warning.startswith("--tasks を読めません"))

    def test_reload_only_when_changed(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "tasks.md")
            Path(p).write_text(SAMPLE_JA, encoding="utf-8")
            tf = tasklist.TaskFile(p)
            first = tf.load()
            self.assertIsNone(tf.warning)
            self.assertIs(tf.load(), first)  # 変わっていなければ解析し直さない
            Path(p).write_text(SAMPLE_EN, encoding="utf-8")
            st = os.stat(p)
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
            self.assertEqual([r["name"] for r in tf.load()["active"]], ["delta"])
            os.remove(p)
            tf.load()
            self.assertIsNotNone(tf.warning)


def agent_row(name, section, reason=None, sub=None, agent_name=None):
    return {"id": name, "name": name, "agent_name": agent_name or name, "section": section, "reason": reason,
            "title": "会話ログのタイトル", "sub": sub, "since": time.time(), "kind": "claude", "elapsed": "1m",
            "bg": None, "pr": None, "status": "idle"}


class OverlayTest(unittest.TestCase):
    def test_title_and_note(self):
        r = logic.overlay_task(agent_row("beta", logic.WORKING, sub="Bash: ls"),
                               {"name": "beta", "status": "working", "task": "テストの追加", "note": "半分済み"}, False)
        self.assertEqual((r["section"], r["title"], r["sub"]), (logic.WORKING, "テストの追加", "半分済み"))
        self.assertIsNone(r["reason"])

    def test_empty_note_keeps_sub(self):
        r = logic.overlay_task(agent_row("beta", logic.IDLE, sub="最後の発言"),
                               {"name": "beta", "task": "t", "note": ""}, False)
        self.assertEqual(r["sub"], "最後の発言")

    def test_table_status_moves_to_needs_you_regardless_of_herdr(self):
        for section in (logic.WORKING, logic.IDLE, logic.DONE):
            r = logic.overlay_task(agent_row("alpha", section), {"name": "alpha", "status": "確認待ち",
                                                                 "task": "整理", "note": "方針を聞いている"}, True)
            self.assertEqual((r["section"], r["reason"], r["sub"]), (logic.NEEDS_YOU, "表: 確認待ち", "方針を聞いている"))

    def test_ask_from_transcript_wins(self):
        r = logic.overlay_task(agent_row("alpha", logic.NEEDS_YOU, reason="blocked", sub="you: approve Bash: x"),
                               {"name": "alpha", "status": "確認待ち", "task": "整理", "note": "メモ"}, True)
        self.assertEqual((r["reason"], r["sub"], r["title"]), ("blocked", "you: approve Bash: x", "整理"))

    def test_no_entry(self):
        r = agent_row("x", logic.IDLE)
        self.assertEqual(logic.overlay_task(dict(r), None, False), r)


class BoardOverlayTest(unittest.TestCase):
    def make_board(self, text, n=5):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tasks.md")
        Path(p).write_text(text, encoding="utf-8")
        self.addCleanup(lambda: (os.path.exists(p) and os.remove(p), os.rmdir(d)))
        warnings = []
        b = board.Board(pr_enabled=False, tasks_path=p, recent_done=n, warn=warnings.append)
        return b, warnings

    def test_overlay_and_recent_done(self):
        b, warnings = self.make_board(SAMPLE_JA, n=2)
        rows = [agent_row("alpha", logic.WORKING), agent_row("beta", logic.IDLE),
                agent_row("unlisted", logic.IDLE)]
        warning, recent = b._overlay_tasks(rows)
        self.assertIsNone(warning)
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["section"], logic.NEEDS_YOU)
        self.assertEqual(rows[1]["title"], "テストの追加")
        self.assertEqual(rows[2]["title"], "会話ログのタイトル")
        self.assertEqual([r["name"] for r in recent], ["old-2", "old-3"])
        self.assertEqual(set(recent[0]), {"name", "repo", "ended", "task", "result"})

    def test_matches_session_name_when_herdr_has_no_name(self):
        b, _ = self.make_board(SAMPLE_JA)
        rows = [agent_row("gamma", logic.IDLE, agent_name=None)]
        rows[0]["agent_name"] = None
        b._overlay_tasks(rows)
        self.assertEqual(rows[0]["reason"], "表: 質問待ち（パスワードの扱い）")

    def test_without_tasks_option(self):
        b = board.Board(pr_enabled=False)
        self.assertEqual(b._overlay_tasks([agent_row("alpha", logic.IDLE)]), (None, None))

    def test_warning_is_reported_once(self):
        warnings = []
        b = board.Board(pr_enabled=False, tasks_path="/nonexistent/tasks.md", warn=warnings.append)
        b._overlay_tasks([])
        b._overlay_tasks([])
        self.assertEqual(len(warnings), 1)


class TuiRecentDoneTest(unittest.TestCase):
    def data(self):
        recent = [{"name": "old-2", "repo": "other-repo", "ended": "01-01 18:05", "task": "新しい仕事" * 20,
                   "result": "済み…→" * 30},
                  {"name": "old-1", "repo": "old-1", "ended": "01-01 09:30", "task": "古い仕事", "result": ""}]
        rows = [agent_row("alpha", logic.NEEDS_YOU, reason="表: 確認待ち", sub="方針を聞いている")]
        return {"now": 0, "error": None, "warning": None, "counts": {logic.NEEDS_YOU: 1}, "rows": rows,
                "recent_done": recent}

    def test_recent_done_rendered_but_not_selectable(self):
        for cols in (80, 100, 120):
            lines, ids = tui.render(self.data(), cols, "alpha")
            text = [tui.visible(l) for l in lines]
            start = next(i for i, l in enumerate(text) if l.startswith("RECENT DONE (2)"))
            self.assertTrue(all(n < start for n in ids))  # 完了の行には行 id がない
            self.assertTrue(any("old-2" in l for l in text[start:]))
            for l in text:
                self.assertLessEqual(tui.disp_width(l), cols, l)
                for c in l:
                    self.assertNotEqual(unicodedata.east_asian_width(c), "A", l)

    def test_app_never_selects_recent_done(self):
        class FakeBoard:
            def __init__(s, data):
                s.data = data

            def snapshot(s):
                return s.data

            def focus(s, row_id):
                raise AssertionError("should not be called")

        app = tui.App(FakeBoard(self.data()))
        app.refresh()
        app.move(5)
        self.assertEqual(app.sel_id, "alpha")

    def test_table_reason_and_warning_shown(self):
        d = self.data()
        d["warning"] = "--tasks を読めません: No such file or directory"
        lines, _ = tui.render(d, 100, None)
        text = "\n".join(tui.visible(l) for l in lines)
        self.assertIn("表: 確認待ち", text)
        self.assertIn("--tasks を読めません", tui.visible(lines[1]))

    def test_no_recent_done_section_without_option(self):
        d = self.data()
        d["recent_done"] = None
        text = "\n".join(tui.visible(l) for l in tui.render(d, 100, None)[0])
        self.assertNotIn("RECENT DONE", text)


if __name__ == "__main__":
    unittest.main()
