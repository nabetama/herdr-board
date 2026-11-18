#!/usr/bin/env python3
"""herdr-board: Herdr のエージェントを状態ごとに一覧する web UI。

  python3 board.py [--port 7788] [--no-pr]   # ブラウザ版
  python3 board.py --tui [--no-pr]            # 端末版（サーバーは立てない）

ブラウザ版は 127.0.0.1 にだけ bind する。ブラウザで http://127.0.0.1:7788/ を開く。
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import logic

HERE = Path(__file__).resolve().parent
HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"
TAIL_BYTES = 512 * 1024
HEAD_BYTES = 256 * 1024
HERDR_TIMEOUT = 10


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
    def __init__(self, pr_enabled=True):
        self.tracker = logic.SinceTracker()
        self.prs = PRCache(pr_enabled)
        self.lock = threading.Lock()
        self.targets = {}   # 行 id -> herdr focus の対象（名前、無名ならペイン）と tab
        self._cache = (0.0, None)

    def snapshot(self):
        with self.lock:
            if time.time() - self._cache[0] < 1.0 and self._cache[1]:
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
        rows, targets = [], {}
        for a in agents:
            rows.append(self._row(a, metas, now, targets))
        self.tracker.forget_except({r["id"] for r in rows})
        self.targets = targets
        order = {s: i for i, s in enumerate(logic.SECTION_ORDER)}
        # セクション内は、その状態になったのが新しい順
        rows.sort(key=lambda r: (order[r["section"]], -r["since"]))
        counts = {s: 0 for s in logic.SECTION_ORDER}
        for r in rows:
            counts[r["section"]] += 1
        return {"now": now, "error": error, "counts": counts, "rows": rows}

    def _row(self, a, metas, now, targets):
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
        return {
            "id": row_id,
            "name": name,
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Herdr のエージェントを状態ごとに一覧する web UI")
    ap.add_argument("--port", type=int, default=7788)
    ap.add_argument("--no-pr", action="store_true", help="gh で PR を調べない")
    ap.add_argument("--tui", action="store_true", help="端末に表示する（サーバーは立てない）")
    ap.add_argument("--interval", type=float, default=3.0, help="端末版の更新間隔（秒）")
    args = ap.parse_args(argv)
    board = Board(pr_enabled=not args.no_pr)
    if args.tui:
        import tui
        return tui.run(board, max(0.5, args.interval))
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(board, args.port))
    print(f"herdr-board: http://127.0.0.1:{args.port}/", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
