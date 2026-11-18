import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logic  # noqa: E402
from logic import DONE, IDLE, NEEDS_YOU, WORKING  # noqa: E402


def j(**kw):
    return json.dumps(kw, ensure_ascii=False)


def human(text, ts="2026-11-18T07:00:00Z"):
    return j(type="user", timestamp=ts, origin={"kind": "human"}, message={"role": "user", "content": text})


def assistant_text(text, ts="2026-11-18T07:01:00Z"):
    return j(type="assistant", timestamp=ts, message={"role": "assistant", "content": [{"type": "text", "text": text}]})


def tool_use(tid, name, inp, ts="2026-11-18T07:02:00Z"):
    return j(type="assistant", timestamp=ts,
             message={"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]})


def tool_result(tid, ts="2026-11-18T07:03:00Z", result=None, text="ok"):
    e = {"type": "user", "timestamp": ts,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]}}
    if result is not None:
        e["toolUseResult"] = result
    return json.dumps(e, ensure_ascii=False)


def notification(task_id, status="completed", ts="2026-11-18T07:10:00Z"):
    text = (f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>{status}</status>\n"
            "<summary>Background command done</summary>\n</task-notification>")
    return j(type="user", timestamp=ts, origin={"kind": "task-notification"}, message={"role": "user", "content": text})


class SummarizeTest(unittest.TestCase):
    def test_last_messages_and_pending(self):
        s = logic.summarize_lines([
            human("最初の依頼"),
            assistant_text("やります"),
            tool_use("t1", "Bash", {"command": "ls", "description": "一覧"}),
            tool_result("t1"),
            tool_use("t2", "Edit", {"file_path": "/a.py"}),
        ])
        self.assertEqual(s["last_user"], "最初の依頼")
        self.assertEqual(s["last_assistant"], "やります")
        self.assertEqual(s["pending_tools"], [{"name": "Edit", "desc": "/a.py"}])
        self.assertEqual(s["last_tool"], "Edit: /a.py")

    def test_skips_broken_first_line_and_non_human_user(self):
        s = logic.summarize_lines([
            '{"type":"user","mess',  # 途中から読んだ行
            human("本当の発言"),
            notification("x1"),
            j(type="user", message={"content": "<command-name>/clear</command-name>"}),
        ])
        self.assertEqual(s["last_user"], "本当の発言")

    def test_background_tasks_start_and_finish(self):
        s = logic.summarize_lines([
            tool_use("t1", "Bash", {"command": "sleep 100", "run_in_background": True}),
            tool_result("t1", result={"backgroundTaskId": "b1"}),
            tool_use("t2", "Bash", {"command": "sleep 5", "run_in_background": True}),
            tool_result("t2", result={"backgroundTaskId": "b2"}),
            notification("b2"),
        ])
        self.assertEqual(s["bg_tasks"], {"b1": "shell"})
        self.assertEqual(s["pending_tools"], [])

    def test_pr_url_and_branch(self):
        s = logic.summarize_lines([
            j(type="user", gitBranch="fix/x", origin={"kind": "human"},
              message={"content": "https://github.com/org/repo/pull/12 を見て"}),
        ])
        self.assertEqual(s["pr_url"], "https://github.com/org/repo/pull/12")
        self.assertEqual(s["git_branch"], "fix/x")

    def test_pr_url_sources(self):
        url = "https://github.com/org/repo/pull/{}".format
        s = logic.summarize_lines([
            tool_use("t1", "Bash", {"command": "gh pr create --fill"}),
            tool_result("t1", text=url(1) + "\n"),
        ])
        self.assertEqual(s["pr_url"], url(1))
        s = logic.summarize_lines([assistant_text(f"PR を作りました: {url(2)}")])
        self.assertEqual(s["pr_url"], url(2))
        # ファイルを読んだ結果などに含まれる URL は拾わない
        s = logic.summarize_lines([
            assistant_text(f"PR を作りました: {url(3)}"),
            tool_use("t2", "Bash", {"command": "cat notes.md"}),
            tool_result("t2", text=f"| 他の仕事 | {url(4)} |"),
            tool_use("t3", "Read", {"file_path": "/x.md"}),
            tool_result("t3", text=url(5)),
        ])
        self.assertEqual(s["pr_url"], url(3))

    def test_custom_title(self):
        s = logic.summarize_lines([j(type="custom-title", customTitle="調査タスク")])
        self.assertEqual(s["custom_title"], "調査タスク")


class FindQuestionTest(unittest.TestCase):
    def test_questions(self):
        for text in [
            "調べました。\nこの方針で進めてよいですか？",
            "A と B があります。どちらにしますか",
            "結果です。\n\n**PR を作ってもよろしいでしょうか。**",
            "Done.\nShould I push the branch?",
            "次の 2 案があります。\n1. 案 A\n2. 案 B\nどれにするか教えてください。\n- A\n- B",
        ]:
            self.assertIsNotNone(logic.find_question(text), text)

    def test_not_questions(self):
        for text in [
            "完了しました。結果は /tmp/x.md に書きました。",
            "まだ実行中です。終わったら通知が来るので、その時点で次の作業を進めます。",
            "```\nwhat?\n```\nテストは全て通りました。",
            "",
            None,
        ]:
            self.assertIsNone(logic.find_question(text), text)

    def test_only_last_lines_are_checked(self):
        text = "これでよいですか？\n" + "\n".join(f"作業 {i} 完了" for i in range(5))
        self.assertIsNone(logic.find_question(text))


class ClassifyTest(unittest.TestCase):
    def summ(self, **kw):
        base = {"pending_tools": [], "last_assistant": None, "last_assistant_ts": None, "last_user_ts": None}
        base.update(kw)
        return base

    def test_working(self):
        self.assertEqual(logic.classify("working", self.summ(last_assistant="どうしますか？"))[0], WORKING)

    def test_blocked_with_pending_tool(self):
        sec, reason, ask = logic.classify("blocked", self.summ(pending_tools=[{"name": "Bash", "desc": "rm -rf build"}]))
        self.assertEqual((sec, reason), (NEEDS_YOU, "blocked"))
        self.assertEqual(ask, "you: approve Bash: rm -rf build")

    def test_blocked_with_ask_user_question(self):
        sec, reason, ask = logic.classify("blocked", self.summ(
            pending_tools=[{"name": "AskUserQuestion", "desc": "どの DB を使いますか？"}]))
        self.assertEqual((sec, reason), (NEEDS_YOU, "blocked"))
        self.assertIn("どの DB を使いますか？", ask)

    def test_blocked_without_clue(self):
        sec, reason, ask = logic.classify("blocked", self.summ())
        self.assertEqual((sec, reason), (NEEDS_YOU, "blocked"))
        self.assertTrue(ask.startswith("you: "))

    def test_idle_with_question(self):
        sec, reason, ask = logic.classify("idle", self.summ(last_assistant="この方針で進めてよいですか？"))
        self.assertEqual((sec, reason, ask), (NEEDS_YOU, "質問で停止", "you: この方針で進めてよいですか？"))

    def test_done_with_question_goes_to_needs_you(self):
        self.assertEqual(logic.classify("done", self.summ(last_assistant="push しますか？"))[0], NEEDS_YOU)

    def test_question_already_answered_by_user(self):
        s = self.summ(last_assistant="進めてよいですか？", last_assistant_ts=100.0, last_user_ts=200.0)
        self.assertEqual(logic.classify("idle", s)[0], IDLE)

    def test_idle_with_pending_tool_is_needs_you(self):
        sec, reason, _ = logic.classify("idle", self.summ(pending_tools=[{"name": "Write", "desc": "/x"}]))
        self.assertEqual((sec, reason), (NEEDS_YOU, "許可待ち?"))

    def test_unknown_is_needs_you(self):
        self.assertEqual(logic.classify("unknown", self.summ())[:2], (NEEDS_YOU, "状態不明"))
        self.assertEqual(logic.classify(None, None)[:2], (NEEDS_YOU, "状態不明"))

    def test_done_and_idle(self):
        self.assertEqual(logic.classify("done", self.summ(last_assistant="完了しました。"))[0], DONE)
        self.assertEqual(logic.classify("idle", self.summ(last_assistant="完了しました。"))[0], IDLE)


class TitleTest(unittest.TestCase):
    def test_custom_title_first(self):
        t = logic.pick_title({"custom_title": "名前付き"}, {"name": "x", "nameSource": "user"}, "依頼", "/a/b")
        self.assertEqual(t, "名前付き")

    def test_session_name_unless_derived(self):
        self.assertEqual(logic.pick_title({}, {"name": "手で付けた名前", "nameSource": "user"}, "依頼", "/a"), "手で付けた名前")
        self.assertEqual(logic.pick_title({}, {"name": "repo-01", "nameSource": "derived"}, "依頼文", "/a"), "依頼文")

    def test_first_prompt_heading(self):
        prompt = "\n# 依頼: Herdr のエージェントを一覧する web UI を作る\n\n## 背景\n..."
        self.assertEqual(logic.pick_title({}, {}, prompt, "/a"), "Herdr のエージェントを一覧する web UI を作る")

    def test_fallbacks(self):
        self.assertEqual(logic.pick_title({"last_user": "最後の発言"}, {}, None, "/a/b"), "最後の発言")
        self.assertEqual(logic.pick_title({}, {}, None, "/a/repo"), "repo")

    def test_long_japanese_title_is_cut(self):
        t = logic.pick_title({}, {}, "あ" * 200, "/a")
        self.assertEqual(len(t), 90)
        self.assertTrue(t.endswith("…"))


class SubLineAndBadgeTest(unittest.TestCase):
    def test_subline(self):
        s = {"last_tool": "Bash: テスト", "last_assistant": "説明"}
        self.assertEqual(logic.pick_subline(NEEDS_YOU, s, "you: approve X"), "you: approve X")
        self.assertEqual(logic.pick_subline(WORKING, s, None), "Bash: テスト")
        self.assertEqual(logic.pick_subline(IDLE, s, None), "説明")
        self.assertIsNone(logic.pick_subline(IDLE, {}, None))

    def test_bg_badge(self):
        self.assertIsNone(logic.bg_badge({}))
        self.assertEqual(logic.bg_badge({"a": "shell"}), "bg: 1 shell")
        self.assertEqual(logic.bg_badge({"a": "shell", "b": "shell", "c": "monitor"}), "bg: 1 monitor, 2 shell")


class ElapsedTest(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(logic.fmt_elapsed(5), "5s")
        self.assertEqual(logic.fmt_elapsed(12 * 60 + 30), "12m")
        self.assertEqual(logic.fmt_elapsed(5 * 3600 + 59 * 60), "5h")
        self.assertEqual(logic.fmt_elapsed(3 * 86400), "3d")
        self.assertEqual(logic.fmt_elapsed(-3), "0s")
        self.assertEqual(logic.fmt_elapsed(None), "")

    def test_tracker_initial_estimate_then_transitions(self):
        tr = logic.SinceTracker()
        summ = {"last_ts": 900.0, "last_user_ts": 500.0}
        self.assertEqual(tr.update("a", "working", summ, 1000.0), 500.0)   # 起動直後: ターン開始時刻
        self.assertEqual(tr.update("a", "working", summ, 1010.0), 500.0)   # 状態が同じなら変わらない
        self.assertEqual(tr.update("a", "idle", summ, 1020.0), 1020.0)     # 変わったらその時刻
        self.assertEqual(tr.update("b", "idle", summ, 1020.0), 900.0)      # 起動直後: 最後の記録
        self.assertEqual(tr.update("c", "idle", {}, 1020.0), 1020.0)
        tr.forget_except({"a"})
        self.assertEqual(tr.update("b", "done", summ, 1030.0), 900.0)


class MiscTest(unittest.TestCase):
    def test_ci_state(self):
        self.assertIsNone(logic.ci_state([]))
        self.assertEqual(logic.ci_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}]), "pass")
        self.assertEqual(logic.ci_state([{"status": "IN_PROGRESS", "conclusion": ""}]), "pending")
        self.assertEqual(logic.ci_state([{"status": "COMPLETED", "conclusion": "FAILURE"},
                                         {"status": "IN_PROGRESS"}]), "fail")

    def test_project_dir_name(self):
        # 実行しているユーザーのホームを使う（/ と . が - になる）
        home = str(Path.home())
        expected = home.replace("/", "-").replace(".", "-") + "-src-example-com-x"
        self.assertEqual(logic.project_dir_name(home + "/src/example.com/x"), expected)


if __name__ == "__main__":
    unittest.main()
