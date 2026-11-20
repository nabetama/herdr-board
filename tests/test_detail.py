"""選んだエージェントの詳細（結果ファイル・表の行・最後の応答）のテスト。データはすべて架空のもの。"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import board  # noqa: E402
import logic  # noqa: E402
import tui  # noqa: E402

TASKS = """## 進行中

| 名前 | 状態 | 依頼内容 | 結果・メモ |
|---|---|---|---|
| alpha | 確認待ち | 利用手順の書き直し""" + "。とても長い依頼" * 40 + """ | 図を入れるか聞いている |

## 完了

| 名前 | リポジトリ | 終了 | 依頼内容 | 結果 |
|---|---|---|---|---|
| docs-writer | sample-app | 01-01 09:00 | 古い依頼 | 古い結果 |
| docs-writer | sample-app | 01-02 18:05 | lint の警告を消す | 12 件修正 |
"""


def check_lines(test, lines, cols):
    for l in lines:
        v = tui.visible(l)
        test.assertLessEqual(tui.disp_width(v), cols, v)
        for c in v:
            test.assertNotEqual(unicodedata.east_asian_width(c), "A", f"{c!r} in {v!r}")
            test.assertFalse(unicodedata.category(c).startswith("C"), f"{c!r} in {v!r}")


class WrapTest(unittest.TestCase):
    def test_wrap_width_and_words(self):
        for w in (2, 3, 10, 37, 80):
            text = "a sentence with words " * 5 + "日本語の文章" * 8 + " " + "x" * 100
            lines = tui.wrap(tui.sanitize(text), w)
            self.assertGreater(len(lines), 1)
            for l in lines:
                self.assertLessEqual(tui.disp_width(l), w, l)
            # 文字は落とさない（折り返しで捨てるのは空白だけ）
            self.assertEqual("".join(lines).replace(" ", ""), tui.sanitize(text).replace(" ", ""))
        self.assertEqual(tui.wrap("hello world foo", 11), ["hello world", "foo"])
        self.assertEqual(tui.wrap("", 10), [""])

    def test_wrap_text_keeps_lines_and_indent_and_sanitizes(self):
        text = "見出し\n\n  - 字下げした項目\n\tタブ\x1b[31m赤\x1b[0m…①‮"
        lines = tui.wrap_text(text, 40, indent=2)
        self.assertEqual(lines[0], "  見出し")
        self.assertEqual(lines[1].strip(), "")
        self.assertEqual(lines[2], "    - 字下げした項目")
        self.assertEqual(lines[3], "      タブ赤...?")
        check_lines(self, lines, 40)


def sample_detail(**kw):
    d = {"id": "alpha", "name": "alpha",
         "result": {"path": "~/work/results/alpha.md", "state": "found", "truncated": False, "error": None,
                    "text": "# 結果\n\n- 変更: 3 ファイル\n" + "長い行の本文。" * 60 + "\n\x1b]0;evil\x07①→“引用”"},
         "tasks": True,
         "task": {"table": "active", "name": "alpha", "status": "確認待ち", "task": "依頼" * 200, "note": ""},
         "last_assistant": "Done.\n\n" + "word " * 100 + "\n```\n  code\n```"}
    d.update(kw)
    return d


class DetailLinesTest(unittest.TestCase):
    def text(self, d, cols=80):
        return [tui.visible(l) for l in tui.detail_lines(d, cols)]

    def test_widths_and_sections(self):
        for cols in (40, 80, 120):
            lines = tui.detail_lines(sample_detail(), cols)
            check_lines(self, lines, cols)
        t = self.text(sample_detail())
        heads = [l for l in t if l and not l.startswith(" ")]
        self.assertEqual([h.split()[0] for h in heads], ["RESULT", "TASK", "LAST"])
        self.assertEqual(t[:2], ["RESULT FILE", "  ~/work/results/alpha.md"])
        self.assertIn("TASK (in progress)", t)
        # 表の依頼内容は切らずに出す（200 文字で切った ~ が付かない）
        self.assertEqual("".join(l.strip() for l in t if "依頼依頼" in l), "依頼" * 200)
        self.assertIn("    code", t)  # 字下げを残す

    def test_no_result_section_without_results_dir(self):
        t = self.text(sample_detail(result=None))
        self.assertFalse(any(l.startswith("RESULT") for l in t))

    def test_result_states(self):
        for state, words in (("missing", "does not exist"), ("refused", "outside the directory"),
                             ("unreadable", "could not read: denied")):
            res = {"path": "~/work/results/alpha.md", "state": state, "text": None, "truncated": False,
                   "error": "denied"}
            t = self.text(sample_detail(result=res))
            i = t.index("RESULT FILE")
            self.assertEqual(t[i + 1], "  ~/work/results/alpha.md")
            self.assertIn("none", t[i + 2])
            self.assertIn(words, t[i + 2])
        res = dict(sample_detail()["result"], truncated=True)
        self.assertTrue(any("cut at 256 KB" in l for l in self.text(sample_detail(result=res))))

    def test_task_done_none_and_without_tasks(self):
        done = {"table": "done", "name": "docs-writer", "ended": "01-02 18:05", "task": "lint", "result": "12 件修正"}
        t = self.text(sample_detail(task=done))
        self.assertIn("TASK (done)", t)
        self.assertEqual([l.strip() for l in t if l.strip() in ("依頼内容", "終了", "結果")], ["依頼内容", "終了", "結果"])
        self.assertIn("    12 件修正", t)
        t = self.text(sample_detail(task=None))
        self.assertTrue(any("no row with this name" in l for l in t))
        t = self.text(sample_detail(tasks=False, task=None))
        self.assertFalse(any(l.startswith("TASK") for l in t))

    def test_no_last_reply_and_no_detail(self):
        t = self.text(sample_detail(last_assistant=None))
        self.assertIn("(none: no conversation log)", t[t.index("LAST REPLY") + 1])
        self.assertTrue(self.text(None)[0].strip().startswith("(no detail"))


class ReadResultTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.base = Path(self.dir.name) / "results"
        self.base.mkdir()

    def test_found_missing_and_truncated(self):
        (self.base / "alpha.md").write_bytes("# 結果\n済み\n".encode("utf-8") + b"\xff")
        r = board.read_result(str(self.base), "alpha")
        self.assertEqual((r["state"], r["truncated"]), ("found", False))
        self.assertTrue(r["text"].startswith("# 結果\n済み"))
        self.assertTrue(r["path"].endswith("alpha.md"))
        r = board.read_result(str(self.base), "beta")
        self.assertEqual((r["state"], r["text"]), ("missing", None))
        (self.base / "big.md").write_bytes(b"x" * 100)
        r = board.read_result(str(self.base), "big", limit=10)
        self.assertEqual((r["state"], r["truncated"], r["text"]), ("found", True, "x" * 10))
        r = board.read_result(str(self.base), "big", limit=100)
        self.assertFalse(r["truncated"])

    def test_names_with_slash_or_dotdot_are_not_read(self):
        (Path(self.dir.name) / "secret.md").write_text("secret", encoding="utf-8")
        (self.base / "sub").mkdir()
        (self.base / "sub" / "x.md").write_text("x", encoding="utf-8")
        for name in ("../secret", "sub/x", "..", "a..b", "", None, "/etc/passwd"):
            r = board.read_result(str(self.base), name)
            self.assertEqual(r["state"], "refused", name)
            self.assertIsNone(r["text"])

    def test_symlink_out_of_dir_is_not_read(self):
        outside = Path(self.dir.name) / "secret.md"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, self.base / "alpha.md")
        r = board.read_result(str(self.base), "alpha")
        self.assertEqual((r["state"], r["text"]), ("refused", None))

    def test_symlink_loop(self):
        os.symlink(self.base / "alpha.md", self.base / "alpha.md")
        r = board.read_result(str(self.base), "alpha")
        self.assertIn(r["state"], ("unreadable", "missing", "refused"))
        self.assertIsNone(r["text"])

    def test_not_a_regular_file(self):
        (self.base / "alpha.md").mkdir()
        r = board.read_result(str(self.base), "alpha")
        self.assertEqual(r["state"], "unreadable")


class BoardDetailTest(unittest.TestCase):
    def make_board(self, results_dir=None, tasks=TASKS):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and None or os.rmdir(d))
        tasks_path = None
        if tasks is not None:
            tasks_path = os.path.join(d, "tasks.md")
            Path(tasks_path).write_text(tasks, encoding="utf-8")
        return board.Board(pr_enabled=False, tasks_path=tasks_path, results_dir=results_dir), d

    def build(self, b, agents, summaries=None):
        """herdr と会話ログを差し替えて _build を呼ぶ。"""
        summaries = summaries or {}
        orig = (board.herdr_agents, board.session_metas, board.transcript_path, board.summary_for,
                board.first_prompt_for)
        board.herdr_agents = lambda: agents
        board.session_metas = lambda: {}
        board.transcript_path = lambda cwd, sid: sid if sid in summaries else None
        board.summary_for = lambda p: summaries[p]
        board.first_prompt_for = lambda p: None
        try:
            return b._build()
        finally:
            (board.herdr_agents, board.session_metas, board.transcript_path, board.summary_for,
             board.first_prompt_for) = orig

    AGENTS = [
        {"name": "alpha", "pane_id": "x:1", "agent_status": "idle", "cwd": "/tmp/sample-app",
         "agent_session": {"value": "s-alpha"}},
        {"name": "docs-writer", "pane_id": "x:2", "agent_status": "done", "cwd": "/tmp/sample-app"},
        {"name": "unlisted", "pane_id": "x:3", "agent_status": "idle", "cwd": "/tmp/sample-app"},
    ]
    SUMMARY = {"s-alpha": {"last_assistant": "最後の応答の全文\n" + "長い" * 300}}

    def test_detail_parts(self):
        b, d = self.make_board(results_dir=None)
        data = self.build(b, self.AGENTS, self.SUMMARY)
        a = b.detail("alpha")
        self.assertIsNone(a["result"])  # 未指定なら欄を出さない
        self.assertTrue(a["tasks"])
        self.assertEqual(a["task"]["table"], "active")
        self.assertTrue(a["task"]["task"].endswith("とても長い依頼"))  # 切らずに出す
        alpha_row = next(r for r in data["rows"] if r["id"] == "alpha")
        self.assertLess(len(alpha_row["title"]), len(a["task"]["task"]))
        self.assertEqual(a["last_assistant"], self.SUMMARY["s-alpha"]["last_assistant"])
        w = b.detail("docs-writer")
        self.assertEqual((w["task"]["table"], w["task"]["result"]), ("done", "12 件修正"))  # 新しい方
        self.assertIsNone(w["last_assistant"])
        u = b.detail("unlisted")
        self.assertIsNone(u["task"])
        # 一覧の各行には全文を載せない
        self.assertNotIn("last_assistant", alpha_row)
        self.assertNotIn("最後の応答の全文", json.dumps(data, ensure_ascii=False))

    def test_only_rows_in_latest_list(self):
        b, _ = self.make_board()
        self.assertIsNone(b.detail("alpha"))
        self.build(b, self.AGENTS)
        self.assertIsNotNone(b.detail("alpha"))
        self.assertIsNone(b.detail("x:1"))
        self.build(b, self.AGENTS[1:])
        self.assertIsNone(b.detail("alpha"))

    def test_result_file_and_without_tasks(self):
        with tempfile.TemporaryDirectory() as rd:
            Path(rd, "alpha.md").write_text("結果の本文", encoding="utf-8")
            b, _ = self.make_board(results_dir=rd, tasks=None)
            self.build(b, self.AGENTS)
            a = b.detail("alpha")
            self.assertEqual((a["result"]["state"], a["result"]["text"]), ("found", "結果の本文"))
            self.assertFalse(a["tasks"])
            self.assertEqual(b.detail("unlisted")["result"]["state"], "missing")

    def test_main_reads_option_and_env(self):
        seen = []

        class FakeBoard:
            def __init__(self, **kw):
                seen.append(kw.get("results_dir"))

        orig_board, orig_run = board.Board, None
        import tui as tui_mod
        orig_run = tui_mod.run
        board.Board = FakeBoard
        tui_mod.run = lambda b, i: 0
        old_env = os.environ.pop(board.RESULTS_DIR_ENV, None)
        try:
            board.main(["--tui"])
            os.environ[board.RESULTS_DIR_ENV] = "/tmp/from-env"
            board.main(["--tui"])
            board.main(["--tui", "--results-dir", "/tmp/from-option"])
        finally:
            board.Board, tui_mod.run = orig_board, orig_run
            os.environ.pop(board.RESULTS_DIR_ENV, None)
            if old_env is not None:
                os.environ[board.RESULTS_DIR_ENV] = old_env
        self.assertEqual(seen, [None, "/tmp/from-env", "/tmp/from-option"])


class HttpDetailTest(unittest.TestCase):
    def test_detail_api(self):
        class FakeBoard:
            def detail(self, row_id):
                return {"id": row_id, "name": row_id} if row_id == "alpha" else None

        srv = ThreadingHTTPServer(("127.0.0.1", 0), board.make_handler(FakeBoard(), 0))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]

        def get(path, headers):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, json.loads(e.read())

        ok = {"X-Herdr-Board": "1"}
        self.assertEqual(get("/api/detail?id=alpha", ok), (200, {"id": "alpha", "name": "alpha"}))
        self.assertEqual(get("/api/detail?id=other", ok)[0], 404)
        self.assertEqual(get("/api/detail?id=alpha", {})[0], 403)
        self.assertEqual(get("/api/detail?id=alpha", {"X-Herdr-Board": "1", "Host": "evil.example"})[0], 403)


class DetailInputTest(unittest.TestCase):
    def test_new_keys(self):
        ev, rest = tui.parse_input("dJK\x1b[6~\x1b[5~")
        self.assertEqual(ev, [("detail",), ("scroll", 1), ("scroll", -1), ("page", 1), ("page", -1)])
        self.assertEqual(rest, "")

    def test_partial_page_key_is_kept(self):
        self.assertEqual(tui.parse_input("d\x1b[6"), ([("detail",)], "\x1b[6"))
        self.assertEqual(tui.parse_input("\x1b[6" + "~"), ([("page", 1)], ""))

    def test_hint_fits_80(self):
        for open_ in (False, True):
            lines, _ = tui.render({"rows": [], "counts": {}, "now": 0}, 80, None, detail_open=open_)
            hint = tui.visible(lines[1])
            self.assertLessEqual(len(hint.rstrip()), 80)
            self.assertNotIn("~", hint)
            self.assertIn("d: ", hint)
        self.assertIn("J/K", tui.visible(tui.render({"rows": [], "counts": {}, "now": 0}, 80, None,
                                                    detail_open=True)[0][1]))


def many_rows(n=12):
    rows = [{"id": f"agent-{i}", "name": f"agent-{i}", "section": logic.IDLE, "title": "t", "sub": "s",
             "reason": None, "bg": None, "pr": None, "kind": "claude", "elapsed": "1m", "status": "idle"}
            for i in range(n)]
    return {"now": 0, "error": None, "counts": {logic.IDLE: n}, "rows": rows}


class DetailAppTest(unittest.TestCase):
    class FakeBoard:
        def __init__(self, data):
            self.data, self.calls = data, []

        def snapshot(self):
            return self.data

        def focus(self, row_id):
            return True, "agent focus"

        def detail(self, row_id):
            self.calls.append(row_id)
            return sample_detail(id=row_id, name=row_id)

    def make_app(self, rows=40):
        b = self.FakeBoard(many_rows())
        app = tui.App(b, out=io.StringIO())
        app.refresh()
        return app, b

    def test_screen_ids_only_cover_the_list(self):
        app, _ = self.make_app()
        app.draw()  # StringIO は端末でないので 100 桁 x 40 行
        closed = dict(app.screen_ids)
        self.assertTrue(any(n >= 21 for n in closed))
        app.toggle_detail()
        app.draw()
        list_h, detail_h = tui.split_body(40, True)
        self.assertEqual((list_h, detail_h), (19, 19))
        self.assertTrue(app.screen_ids)
        self.assertTrue(all(tui.HEADER_LINES <= n < tui.HEADER_LINES + list_h for n in app.screen_ids))
        # 選択行は一覧の範囲に見えている
        app.move(10)
        app.draw()
        self.assertIn(app.sel_id, app.screen_ids.values())

    def test_draw_widths(self):
        app, _ = self.make_app()
        app.toggle_detail()
        app.draw()
        out = app.out.getvalue()
        lines, _ = tui.render(app.data, 100, app.sel_id, None, True)
        dl = app.detail_view(100, 19)
        self.assertEqual(len(dl), 19)
        self.assertTrue(tui.visible(dl[0]).startswith(" DETAIL  agent-0"))
        check_lines(self, dl, 100)
        self.assertIn("RESULT FILE", out)

    def test_scroll_resets_on_selection_change_and_kept_on_refresh(self):
        app, b = self.make_app()
        app.toggle_detail()
        app.draw()
        app.scroll_detail(5)
        app.scroll_detail(app.page)
        app.draw()
        self.assertEqual(app.dtop, 5 + app.page)
        app.refresh()
        app.draw()
        self.assertEqual(app.dtop, 5 + app.page)  # 同じエージェントの更新では保つ
        app.move(1)
        self.assertEqual(app.dtop, 0)
        self.assertEqual(b.calls[-1], "agent-1")
        # 下の端より先へは進まない
        app.scroll_detail(10 ** 6)
        app.draw()
        total = len(tui.detail_lines(app.detail, 100))
        self.assertEqual(app.dtop, total - 18)
        app.scroll_detail(-10 ** 6)
        self.assertEqual(app.dtop, 0)
        # 閉じているときのスクロールは何もしない
        app.toggle_detail()
        app.scroll_detail(5)
        self.assertEqual(app.dtop, 0)

    def test_detail_is_read_only_when_needed(self):
        app, b = self.make_app()
        app.move(1)
        self.assertEqual(b.calls, [])  # 閉じているときは読まない
        app.toggle_detail()
        self.assertEqual(b.calls, ["agent-1"])
        app.draw()
        app.draw()
        self.assertEqual(b.calls, ["agent-1"])  # 描き直しでは読まない
        app.toggle_detail()
        app.move(1)
        self.assertEqual(b.calls, ["agent-1"])

    def test_small_terminal_shows_only_detail(self):
        self.assertEqual(tui.split_body(9, True), (0, 7))
        self.assertEqual(tui.split_body(9, False), (7, 0))
        self.assertEqual(tui.split_body(10, True), (4, 4))


if __name__ == "__main__":
    unittest.main()
