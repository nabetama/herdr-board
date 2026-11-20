"""端末版とブラウザ版を同じプロセスで動かす（--tui --port）ときのテスト。"""
import io
import json
import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import board  # noqa: E402
import tui  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_is_free(port):
    """待ち受けが残っておらず、サーバーを起動し直せる（HTTPServer と同じく SO_REUSEADDR で bind できる）。
    接続のあとは TIME_WAIT が残るので、SO_REUSEADDR なしの bind では見ない。"""
    with socket.socket() as c:
        c.settimeout(1)
        if c.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            s.listen()
            return True
        except OSError:
            return False


def get_json(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


class CountingBoard(board.Board):
    """_build を数える（herdr は呼ばない）。"""

    def __init__(self, **kw):
        super().__init__(pr_enabled=False, **kw)
        self.builds = 0

    def _build(self):
        self.builds += 1
        time.sleep(0.05)  # 作っている間にほかのスレッドが来るように
        return {"now": time.time(), "error": None, "warning": None, "counts": {}, "rows": [], "recent_done": None,
                "n": self.builds}


class SharedSnapshotTest(unittest.TestCase):
    def test_threads_share_one_build(self):
        b = CountingBoard(max_age=5.0)
        results = []
        threads = [threading.Thread(target=lambda: results.append(b.snapshot()["n"])) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(b.builds, 1)
        self.assertEqual(results, [1] * 8)

    def test_rebuilds_after_max_age(self):
        b = CountingBoard(max_age=0.1)
        b.snapshot()
        b.snapshot()
        self.assertEqual(b.builds, 1)
        time.sleep(0.15)
        b.snapshot()
        self.assertEqual(b.builds, 2)

    def test_default_max_age_is_unchanged(self):
        self.assertEqual(board.Board(pr_enabled=False).max_age, 1.0)


class StartWebTest(unittest.TestCase):
    def test_serves_and_stops(self):
        b = CountingBoard(max_age=5.0)
        port = free_port()
        srv, err = board.start_web(b, port)
        self.assertIsNone(err)
        try:
            self.assertEqual(get_json(port, "/api/board")["n"], 1)
            b.snapshot()
            self.assertEqual(b.builds, 1)  # 端末版の呼び出しもブラウザ版と同じ一覧を使う
        finally:
            board.stop_web(srv)
        self.assertTrue(port_is_free(port))

    def test_port_in_use(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            srv, err = board.start_web(CountingBoard(), s.getsockname()[1])
        self.assertIsNone(srv)
        self.assertTrue(err)
        board.stop_web(None)  # None でも何もしない


class MainTest(unittest.TestCase):
    def run_main(self, argv, fake_run):
        orig = tui.run
        tui.run = fake_run
        try:
            return board.main(argv)
        finally:
            tui.run = orig

    def test_tui_with_port_runs_web_and_stops_it(self):
        port = free_port()
        seen = {}

        def fake_run(b, interval, web=None):
            seen["web"], seen["max_age"] = web, b.max_age
            seen["served"] = not port_is_free(port)
            return 0

        self.assertEqual(self.run_main(["--tui", "--no-pr", "--port", str(port)], fake_run), 0)
        self.assertEqual(seen["web"], {"url": f"http://127.0.0.1:{port}/"})
        self.assertTrue(seen["served"])
        self.assertEqual(seen["max_age"], 2.5)
        self.assertTrue(port_is_free(port))

    def test_port_is_closed_however_the_tui_ends(self):
        for exc in (KeyboardInterrupt, SystemExit, RuntimeError):
            port = free_port()

            def fake_run(b, interval, web=None, exc=exc):
                raise exc()

            with self.assertRaises(exc):
                self.run_main(["--tui", "--no-pr", "--port", str(port)], fake_run)
            self.assertTrue(port_is_free(port), exc)

    def test_tui_keeps_running_when_port_is_in_use(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            seen = {}

            def fake_run(b, interval, web=None):
                seen["web"] = web
                return 0

            self.assertEqual(self.run_main(["--tui", "--no-pr", "--port", str(port)], fake_run), 0)
        self.assertIn(f"could not open port {port}", seen["web"]["error"])

    def test_tui_without_port_is_terminal_only(self):
        seen = []
        orig = board.start_web
        board.start_web = lambda *a: seen.append("web") or (None, "x")
        try:
            self.run_main(["--tui", "--no-pr"], lambda b, interval: seen.append(("tui", b.max_age)) or 0)
        finally:
            board.start_web = orig
        self.assertEqual(seen, [("tui", 1.0)])

    def test_web_only_uses_default_port(self):
        seen = []

        class FakeServer:
            def __init__(self, addr, handler):
                seen.append(addr)

            def serve_forever(self):
                raise KeyboardInterrupt

        orig = board.ThreadingHTTPServer
        board.ThreadingHTTPServer = FakeServer
        orig_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            board.main(["--no-pr"])
            board.main(["--no-pr", "--port", "8000"])
        finally:
            board.ThreadingHTTPServer = orig
            sys.stderr = orig_stderr
        self.assertEqual(seen, [("127.0.0.1", 7788), ("127.0.0.1", 8000)])


class AppWebNoticeTest(unittest.TestCase):
    class FakeBoard:
        def __init__(self):
            self.data = {"now": 0, "error": None, "warning": "--tasks を読めません: x", "counts": {}, "rows": []}

        def snapshot(self):
            return self.data

    def test_error_is_shown_without_touching_shared_snapshot(self):
        b = self.FakeBoard()
        app = tui.App(b, out=io.StringIO(), web={"error": "web: could not open port 7790 (in use); terminal only"})
        app.refresh()
        lines, _ = tui.render(app.data, 100, None)
        self.assertIn("web: could not open port 7790", tui.visible(lines[1]))
        self.assertIn("--tasks を読めません", tui.visible(lines[1]))
        self.assertEqual(b.data["warning"], "--tasks を読めません: x")  # 共有の一覧は書き換えない
        app.refresh()
        self.assertEqual(app.data["warning"].count("web:"), 1)

    def test_url_is_flashed(self):
        app = tui.App(self.FakeBoard(), out=io.StringIO(), web={"url": "http://127.0.0.1:7790/"})
        self.assertEqual(app.msg, "web: http://127.0.0.1:7790/")
        self.assertGreater(app.msg_until, time.time())
        self.assertIsNone(tui.App(self.FakeBoard(), out=io.StringIO()).msg)


if __name__ == "__main__":
    unittest.main()
