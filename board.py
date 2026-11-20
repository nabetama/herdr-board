#!/usr/bin/env python3
"""herdr-board: Herdr のエージェントを状態ごとに一覧する web UI。

  python3 board.py [--port 7788] [--no-pr]   # ブラウザ版
  python3 board.py --tui [--no-pr]            # 端末版（サーバーは立てない）
  python3 board.py --tui --results-dir DIR    # 詳細に DIR/<名前>.md を出す
  python3 board.py --tui --port 7788          # 端末版と同じプロセスでブラウザ版も動かす

ブラウザ版は 127.0.0.1 にだけ bind する。ブラウザで http://127.0.0.1:7788/ を開く。
"""
import argparse
import json
import os
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import logic
import tasklist

HERE = Path(__file__).resolve().parent
HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"
TAIL_BYTES = 512 * 1024
HEAD_BYTES = 256 * 1024
HERDR_TIMEOUT = 10
DEFAULT_PORT = 7788
RESULT_MAX_BYTES = 256 * 1024
RESULTS_DIR_ENV = "HERDR_BOARD_RESULTS_DIR"


def run(args, cwd=None, timeout=HERDR_TIMEOUT):
    """引数を配列のまま渡して実行する（シェルは通さない）。"""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)


# ---------------------------------------------------------------- データ取得

def herdr_agents():
    r = run(["herdr", "agent", "list"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "herdr agent list failed")
    return json.loads(r.stdout)["result"]["agents"]


def session_metas():
    """sessionId -> ~/.claude/sessions/<pid>.json の中身。"""
    out = {}
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("sessionId"):
            out[d["sessionId"]] = d
    return out


def transcript_path(cwd, session_id):
    if not session_id:
        return None
    p = PROJECTS_DIR / logic.project_dir_name(cwd) / f"{session_id}.jsonl"
    if p.exists():
        return p
    hits = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


_summary_cache = {}   # path -> ((mtime, size), summary)
_first_prompt_cache = {}  # path -> first prompt


def read_tail_lines(path, nbytes=TAIL_BYTES):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > nbytes and lines:
        lines = lines[1:]  # 途中から読んだ最初の行は捨てる
    return lines


def summary_for(path):
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _summary_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    s = logic.summarize_lines(read_tail_lines(path))
    _summary_cache[path] = (key, s)
    return s


def first_prompt_for(path):
    if path in _first_prompt_cache:
        return _first_prompt_cache[path]
    with open(path, "rb") as f:
        data = f.read(HEAD_BYTES)
    found = None
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and logic.is_human_prompt(e):
            found = logic._text_of((e.get("message") or {}).get("content"))
            break
    _first_prompt_cache[path] = found
    return found


def display_path(p):
    """ホームディレクトリの下なら ~ から始まる形にする（画面に出す用）。"""
    s, home = str(p), str(HOME)
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def read_result(results_dir, name, limit=RESULT_MAX_BYTES):
    """結果ファイル <results_dir>/<name>.md を読む。

    返す dict: path（画面に出す形）、state（"found" / "missing" / "refused" / "unreadable"）、
    text（found のときの本文）、truncated（limit バイトで切ったか）、error（unreadable の理由）。
    名前に / や .. を含むとき、解決したパスが results_dir の外に出るときは読まない（refused）。
    """
    out = {"path": None, "state": "refused", "text": None, "truncated": False, "error": None}
    name = name or ""
    if not name or "/" in name or "\\" in name or ".." in name or "\0" in name:
        return out
    base = Path(results_dir).expanduser()
    p = base / f"{name}.md"
    out["path"] = display_path(p)
    try:
        resolved = p.resolve()
        if base.resolve() not in resolved.parents:
            return out
        if not p.exists():
            out["state"] = "missing"
            return out
        if not stat.S_ISREG(resolved.stat().st_mode):  # FIFO などを開いて止まらないように
            out["state"], out["error"] = "unreadable", "not a regular file"
            return out
        with open(resolved, "rb") as f:
            data = f.read(limit + 1)
    except (OSError, RuntimeError) as ex:  # RuntimeError: Python 3.12 以前でシンボリックリンクが循環しているとき
        out["state"], out["error"] = "unreadable", getattr(ex, "strerror", None) or str(ex)
        return out
    out["state"] = "found"
    out["truncated"] = len(data) > limit
    out["text"] = data[:limit].decode("utf-8", errors="replace")
    return out


# ---------------------------------------------------------------- PR（gh）

class PRCache:
    """(cwd, branch) -> PR 情報。gh は別スレッドで呼び、結果を TTL 秒キャッシュする。"""

    TTL = 120

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._data = {}       # key -> (fetched_at, info or None)
        self._queue = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        if enabled:
            threading.Thread(target=self._worker, daemon=True).start()

    def get(self, cwd, branch, pr_url=None):
        """PR の URL が会話ログにあればそれを、なければブランチで探す。"""
        if not self.enabled or not cwd:
            return None
        if pr_url:
            key = (cwd, pr_url)
        elif branch and branch not in ("main", "master", "HEAD", "develop"):
            key = (cwd, branch)
        else:
            return None
        with self._lock:
            hit = self._data.get(key)
            if not hit or time.time() - hit[0] > self.TTL:
                self._queue.add(key)
                self._wake.set()
            return hit[1] if hit else None

    def _worker(self):
        while True:
            self._wake.wait()
            with self._lock:
                key = self._queue.pop() if self._queue else None
                if not self._queue:
                    self._wake.clear()
            if key:
                info = self._fetch(*key)
                with self._lock:
                    self._data[key] = (time.time(), info)

    @staticmethod
    def _fetch(cwd, ref):
        try:
            r = run(["gh", "pr", "view", ref, "--json",
                     "number,url,state,isDraft,reviewDecision,statusCheckRollup"], cwd=cwd, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        try:
            d = json.loads(r.stdout)
        except ValueError:
            return None
        parts = (d.get("url") or "").split("/")
        repo = parts[4] if len(parts) > 4 else ""
        return {
            "label": f"{repo}#{d.get('number')}",
            "url": d.get("url"),
            "state": (d.get("state") or "").lower(),
            "draft": bool(d.get("isDraft")),
            "review": (d.get("reviewDecision") or "").lower() or None,
            "ci": logic.ci_state(d.get("statusCheckRollup")),
        }


# ---------------------------------------------------------------- 一覧の組み立て

class Board:
    def __init__(self, pr_enabled=True, tasks_path=None, recent_done=5, warn=None, results_dir=None, max_age=1.0):
        self.tracker = logic.SinceTracker()
        self.prs = PRCache(pr_enabled)
        self.lock = threading.Lock()
        self.targets = {}   # 行 id -> herdr focus の対象（名前、無名ならペイン）と tab
        self.details = {}   # 行 id -> 詳細の材料（名前・最後の応答・表の行）。targets と同じく直近の一覧の分だけ
        self.results_dir = results_dir
        self._cache = (0.0, None)
        self.max_age = max_age      # snapshot() がこの秒数より新しい一覧を返し直す（端末版とブラウザ版で共有するとき用）
        self.tasks = tasklist.TaskFile(tasks_path) if tasks_path else None
        self.recent_done_n = recent_done
        self.warn = warn            # 警告が変わったときに呼ぶ（サーバーは stderr に 1 行出す）
        self._last_warning = None

    def snapshot(self):
        """一覧。max_age 秒以内に作ったものがあればそれを返す。作るのは lock を持った 1 スレッドだけ。"""
        with self.lock:
            if time.time() - self._cache[0] < self.max_age and self._cache[1]:
                return self._cache[1]
            data = self._build()
            self._cache = (time.time(), data)
            return data

    def _build(self):
        now = time.time()
        try:
            agents = herdr_agents()
            error = None
        except Exception as ex:  # herdr が動いていないなど
            agents, error = [], str(ex)
        metas = session_metas()
        rows, targets, details = [], {}, {}
        for a in agents:
            rows.append(self._row(a, metas, now, targets, details))
        self.tracker.forget_except({r["id"] for r in rows})
        order = {s: i for i, s in enumerate(logic.SECTION_ORDER)}
        # セクション内は、その状態になったのが新しい順
        warning, recent = self._overlay_tasks(rows, details)
        self.targets = targets
        self.details = details
        rows.sort(key=lambda r: (order[r["section"]], -r["since"]))
        counts = {s: 0 for s in logic.SECTION_ORDER}
        for r in rows:
            counts[r["section"]] += 1
        return {"now": now, "error": error, "warning": warning, "counts": counts, "rows": rows,
                "recent_done": recent}

    def _overlay_tasks(self, rows, details=None):
        """--tasks の表を一覧に重ね、(警告, 直近の完了) を返す。
        details があれば、各行の詳細に表の行（切る前の全文）を入れる。進行中に無ければ完了の表から探す。"""
        if not self.tasks:
            return None, None
        data = self.tasks.load()
        warning = self.tasks.warning
        if warning != self._last_warning and warning and self.warn:
            self.warn(warning)
        self._last_warning = warning
        by_name = {}
        for e in data["active"]:
            by_name.setdefault(e["name"], e)  # 同じ名前が複数あれば上の行
        done_by_name = {}
        for e in tasklist.recent_done(data["done"], len(data["done"])):
            done_by_name.setdefault(e["name"], e)  # 同じ名前が複数あれば終了の新しい方
        for r in rows:
            e = by_name.get(r.get("agent_name")) or by_name.get(r.get("name"))
            if e:
                logic.overlay_task(r, e, tasklist.needs_you(e.get("status")))
            if details is not None and r.get("id") in details:
                if e:
                    details[r["id"]]["task"] = {"table": "active", **e}
                else:
                    d = done_by_name.get(r.get("agent_name")) or done_by_name.get(r.get("name"))
                    details[r["id"]]["task"] = {"table": "done", **d} if d else None
        recent = [{k: logic.short(e.get(k), 300) for k in ("name", "repo", "ended", "task", "result")}
                  for e in tasklist.recent_done(data["done"], self.recent_done_n)]
        return warning, recent

    def _row(self, a, metas, now, targets, details=None):
        status = a.get("agent_status") or "unknown"
        sid = (a.get("agent_session") or {}).get("value")
        meta = metas.get(sid) or {}
        cwd = a.get("cwd") or meta.get("cwd") or ""
        summary, first = {}, None
        tp = transcript_path(cwd, sid)
        if tp:
            try:
                summary = summary_for(tp)
                first = first_prompt_for(tp)
            except OSError:
                pass
        section, reason, ask = logic.classify(status, summary)
        row_id = a.get("name") or a.get("pane_id")
        since = self.tracker.update(row_id, status, summary, now)
        name = a.get("name") or meta.get("name") or Path(cwd).name or "?"
        targets[row_id] = {"target": a.get("name") or a.get("pane_id"),
                           "pane": a.get("pane_id"), "tab": a.get("tab_id")}
        if details is not None:
            details[row_id] = {"name": name, "file_name": a.get("name") or name,
                               "last_assistant": summary.get("last_assistant"), "task": None}
        return {
            "id": row_id,
            "name": name,
            "agent_name": a.get("name"),  # Herdr の名前（--tasks の表の「名前」と突き合わせる）
            "status": status,
            "section": section,
            "reason": reason,
            "title": logic.pick_title(summary, meta, first, cwd) or a.get("terminal_title_stripped") or "",
            "sub": logic.pick_subline(section, summary, ask),
            "kind": a.get("agent") or "",
            "repo": Path(cwd).name,
            "bg": logic.bg_badge(summary.get("bg_tasks")),
            "since": since,
            "elapsed": logic.fmt_elapsed(now - since),
            "pr": self.prs.get(cwd, summary.get("git_branch"), summary.get("pr_url")),
            "focused": bool(a.get("focused")),
        }

    def detail(self, row_id):
        """行 id（直近の一覧にあるものだけ）の詳細。知らない行 id なら None。

        返す dict: id、name、result（結果ファイル。--results-dir も環境変数も無ければ None）、
        tasks（--tasks を付けたか）、task（表の行。table が "active" か "done"。無ければ None）、
        last_assistant（最後の応答の全文。会話ログが無ければ None）。
        """
        info = self.details.get(row_id)
        if info is None:
            return None
        return {
            "id": row_id,
            "name": info["name"],
            "result": read_result(self.results_dir, info["file_name"]) if self.results_dir else None,
            "tasks": self.tasks is not None,
            "task": info["task"],
            "last_assistant": info["last_assistant"],
        }

    def focus(self, row_id):
        """行 id（直近の一覧にあるものだけ）のエージェントへ移る。"""
        t = self.targets.get(row_id)
        if not t:
            return False, "unknown agent"
        r = run(["herdr", "agent", "focus", t["target"]])
        if r.returncode == 0:
            return True, "agent focus"
        tab = t.get("tab")
        if t.get("pane"):
            g = run(["herdr", "pane", "get", t["pane"]])
            if g.returncode == 0:
                try:
                    tab = json.loads(g.stdout)["result"]["pane"]["tab_id"]
                except (ValueError, KeyError, TypeError):
                    pass
        if tab:
            r2 = run(["herdr", "tab", "focus", tab])
            if r2.returncode == 0:
                return True, "tab focus"
            return False, r2.stderr.strip()
        return False, r.stderr.strip()


# ---------------------------------------------------------------- HTTP

ALLOWED_HOSTS = ("127.0.0.1", "localhost")


def make_handler(board, port):
    index = HERE / "static" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _host_ok(self):
            # DNS rebinding 対策: Host が 127.0.0.1 / localhost のときだけ答える
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ALLOWED_HOSTS

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden host"})
            if self.path in ("/", "/index.html"):
                return self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            if self.path == "/api/board":
                return self._send(200, board.snapshot())
            url = urlsplit(self.path)
            if url.path == "/api/detail":
                # 結果ファイルや会話ログの中身を返すので、他サイトのページから読めないよう独自ヘッダーを要求する
                if self.headers.get("X-Herdr-Board") != "1":
                    return self._send(403, {"error": "forbidden"})
                row_id = (parse_qs(url.query).get("id") or [""])[0]
                d = board.detail(row_id)
                return self._send(200, d) if d else self._send(404, {"error": "unknown agent"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden host"})
            # 他サイトからのフォーム送信を受けないよう、JSON と独自ヘッダーを要求する（CORS preflight が必要になる）
            if self.headers.get("X-Herdr-Board") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(403, {"error": "forbidden"})
            if self.path != "/api/focus":
                return self._send(404, {"error": "not found"})
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 4096)
                req = json.loads(self.rfile.read(n) or b"{}")
                row_id = str(req.get("id") or "")
            except (ValueError, AttributeError):
                return self._send(400, {"error": "bad request"})
            ok, how = board.focus(row_id)
            self._send(200 if ok else 404, {"ok": ok, "how": how})

    return Handler


def start_web(board, port):
    """ブラウザ版のサーバーを別スレッド（daemon）で動かす。戻り値: (サーバー, None) か、起動できなければ (None, 理由)。"""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(board, port))
    except OSError as ex:
        return None, ex.strerror or str(ex)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="herdr-board-web", daemon=True).start()
    return srv, None


def stop_web(srv):
    """start_web() で動かしたサーバーを止めて、ポートを閉じる。"""
    if srv:
        srv.shutdown()
        srv.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Herdr のエージェントを状態ごとに一覧する web UI")
    ap.add_argument("--port", type=int, default=None,
                    help=f"ブラウザ版のポート（既定 {DEFAULT_PORT}）。--tui と一緒に付けると、ブラウザ版も同じプロセスで動かす")
    ap.add_argument("--no-pr", action="store_true", help="gh で PR を調べない")
    ap.add_argument("--tui", action="store_true", help="端末に表示する（サーバーは立てない）")
    ap.add_argument("--interval", type=float, default=3.0, help="端末版の更新間隔（秒）")
    ap.add_argument("--tasks", metavar="PATH", help="作業の一覧表（Markdown）を重ねて表示する")
    ap.add_argument("--recent-done", type=int, default=5, metavar="N", help="--tasks の完了の表から出す件数")
    ap.add_argument("--results-dir", metavar="DIR",
                    help=f"詳細に DIR/<名前>.md を出す（無ければ環境変数 {RESULTS_DIR_ENV}）")
    args = ap.parse_args(argv)
    results_dir = args.results_dir or os.environ.get(RESULTS_DIR_ENV) or None
    def warn(msg):
        print(f"herdr-board: {msg}", file=sys.stderr)

    board = Board(pr_enabled=not args.no_pr, tasks_path=args.tasks, recent_done=max(0, args.recent_done),
                  warn=None if args.tui else warn, results_dir=results_dir)
    if args.tui:
        import tui
        interval = max(0.5, args.interval)
        if args.port is None:
            return tui.run(board, interval)
        # 端末版とブラウザ版で同じ Board を使う。一覧を作るのは、どちらかが前回から interval 近く経って求めたときだけ
        board.max_age = max(1.0, interval - 0.5)
        srv, err = start_web(board, args.port)
        web = {"error": f"web: could not open port {args.port} ({err}); terminal only"} if err else \
            {"url": f"http://127.0.0.1:{args.port}/"}
        try:
            return tui.run(board, interval, web=web)
        finally:
            stop_web(srv)  # q / Ctrl-C / SIGHUP / SIGTERM のどれで終えてもポートを閉じる
    port = DEFAULT_PORT if args.port is None else args.port
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(board, port))
    print(f"herdr-board: http://127.0.0.1:{port}/", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
