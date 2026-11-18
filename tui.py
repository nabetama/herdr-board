"""herdr-board の端末版（python3 board.py --tui）。

ブラウザ版と同じ Board.snapshot() / Board.focus() を使い、表示だけを端末向けにする。
素の ANSI エスケープで描く（代替画面・SGR マウス）。表示する文字は ASCII か、
表示幅が確定している文字（East Asian Wide / Fullwidth と Narrow）だけにする。
"""
import os
import re
import select
import signal
import sys
import time
import unicodedata

import logic

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
REV = "\033[7m"
SEL_BG = "\033[48;5;237m"


def fg(n):
    return f"\033[38;5;{n}m"


# ブラウザ版の色に合わせる（オレンジ・緑・青・グレー）
COLOR = {logic.NEEDS_YOU: 208, logic.DONE: 71, logic.WORKING: 75, logic.IDLE: 245}
SECTIONS = [
    (logic.NEEDS_YOU, "NEEDS YOU", "needs you"),
    (logic.DONE, "DONE / UNSEEN", "done/unseen"),
    (logic.WORKING, "WORKING", "working"),
    (logic.IDLE, "IDLE", "idle"),
]
HEADER_LINES = 2

# 幅が揺れる文字（East Asian Ambiguous）のうち、よく出るものの置き換え先
AMBIGUOUS_MAP = {
    "…": "...", "—": "-", "–": "-", "―": "-", "‐": "-", "·": "-", "•": "*", "●": "*", "○": "o",
    "→": "->", "←": "<-", "↑": "^", "↓": "v", "×": "x", "※": "*", "°": "o",
    "“": '"', "”": '"', "‘": "'", "’": "'", "★": "*", "☆": "*", "■": "#", "□": "#",
    "▶": ">", "▼": "v", "◆": "*", "◇": "*", "✓": "v",
}
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


def sanitize(s):
    """表示用に整える: 制御文字・エスケープを除き、幅が揺れる文字を ASCII に置き換え、空白を 1 つにまとめる。"""
    s = _ANSI_RE.sub("", s or "")
    out = []
    for c in s:
        if c in "\t\n\r":
            out.append(" ")
            continue
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn") or c in ("\ufe0e", "\ufe0f"):
            continue
        if unicodedata.east_asian_width(c) == "A":
            out.append(AMBIGUOUS_MAP.get(c, "?"))
            continue
        out.append(c)
    return " ".join("".join(out).split())


def char_width(c):
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def disp_width(s):
    return sum(char_width(c) for c in s)


def fit(s, w, pad=True):
    """表示幅 w に収める。はみ出すときは末尾を ~ にする。pad なら足りない分を空白で埋める。"""
    if w <= 0:
        return ""
    if disp_width(s) <= w:
        return s + (" " * (w - disp_width(s)) if pad else "")
    out, cur = [], 0
    for c in s:
        cw = char_width(c)
        if cur + cw > w - 1:
            break
        out.append(c)
        cur += cw
    s2 = "".join(out) + "~"
    cur += 1
    return s2 + (" " * (w - cur) if pad else "")


def visible(s):
    """ANSI エスケープを除いた文字列（テスト・幅計算用）。"""
    return _ANSI_RE.sub("", s)


def pr_text(pr):
    if not pr:
        return ""
    bits = [pr.get("label") or ""]
    if pr.get("state") and pr["state"] != "open":
        bits.append(pr["state"])
    if pr.get("draft"):
        bits.append("draft")
    ci = {"pass": "CI ok", "fail": "CI NG", "pending": "CI .."}.get(pr.get("ci"))
    if ci:
        bits.append(ci)
    if pr.get("review") == "approved":
        bits.append("approved")
    elif pr.get("review") == "changes_requested":
        bits.append("changes-req")
    return sanitize(" ".join(bits))


def render(data, cols, sel_id, msg=None):
    """描く行（ANSI 付き）と、行番号 -> 行 id の対応を返す。行番号は 0 始まりで、ヘッダーを含む。"""
    cols = max(40, cols)
    lines, ids = [], {}
    counts = data.get("counts") or {}

    # ヘッダー 1 行目: タイトルと件数
    head = [BOLD + "herdr / board" + RESET]
    head_w = len("herdr / board")
    for key, _, label in SECTIONS:
        n = counts.get(key, 0)
        t = f"{n} {label}"
        head.append((fg(COLOR[key]) + BOLD if n else DIM) + t + RESET)
        head_w += 2 + len(t)
    clock = time.strftime("%H:%M:%S", time.localtime(data.get("now") or time.time()))
    gap = max(1, cols - head_w - len(clock))
    if head_w + 1 + len(clock) > cols:
        lines.append(fit(visible("  ".join(head)), cols))
    else:
        lines.append("  ".join(head) + " " * gap + DIM + clock + RESET)
    # 2 行目: 操作ヒントか、直前の操作の結果
    hint = "Up/Down j/k: select   Enter/click: open in terminal   q: quit"
    if data.get("error"):
        lines.append(fg(196) + fit(sanitize("herdr: " + data["error"]), cols) + RESET)
    elif msg:
        lines.append(fg(229) + fit(sanitize(msg), cols) + RESET)
    else:
        lines.append(DIM + fit(hint, cols) + RESET)

    rows = data.get("rows") or []
    name_w = min(30, max(12, cols // 5))
    for key, title, _ in SECTIONS:
        sect = [r for r in rows if r.get("section") == key]
        lines.append("")
        color = fg(COLOR[key])
        lines.append((color + BOLD if sect else DIM) + f"{title} ({len(sect)})" + RESET)
        if not sect and key == logic.NEEDS_YOU:
            lines.append(DIM + "   (nothing is waiting for you)" + RESET)
        for r in sect:
            sel = r.get("id") == sel_id
            l1, l2 = _row_lines(r, cols, name_w, color, sel)
            ids[len(lines)] = r.get("id")
            lines.append(l1)
            ids[len(lines)] = r.get("id")
            lines.append(l2)
    return lines, ids


def _row_lines(r, cols, name_w, color, sel):
    bg = SEL_BG if sel else ""
    mark = ">" if sel else " "
    elapsed = (r.get("elapsed") or "").rjust(4)
    # 右側: bg バッジ・PR・種別。幅に余裕がなければ落とす
    right = [elapsed]
    if r.get("bg"):
        right.insert(0, sanitize(r["bg"]))
    if cols >= 90 and r.get("kind"):
        right.insert(len(right) - 1, sanitize(r["kind"]))
    if cols >= 100 and r.get("pr"):
        right.insert(0, pr_text(r["pr"]))
    right_s = "  ".join(right)
    # 左: マーク(1) 点(1) 空白(1) 名前 空白(1)
    left_w = 3 + name_w + 1
    title_w = cols - left_w - disp_width(right_s) - 2
    if title_w < 10:  # 狭いときは PR から落とす
        right_s = "  ".join(x for x in right if x != pr_text(r.get("pr")))
        title_w = cols - left_w - disp_width(right_s) - 2
    name = fit(sanitize(r.get("name")), name_w)
    title = fit(sanitize(r.get("title")), max(0, title_w))
    l1 = (bg + BOLD + mark + RESET + bg + color + "*" + RESET + bg + " " + BOLD + name + RESET + bg + " "
          + title + "  " + fg(75) + right_s + RESET)
    # 2 行目: NEEDS YOU は理由と求めていること、ほかは補足
    indent = " " * left_w
    sub = sanitize(r.get("sub"))
    if r.get("reason"):
        reason = sanitize(r["reason"])
        body = fit(sub, max(0, cols - left_w - disp_width(reason) - 3))
        l2 = (bg + indent + fg(16) + "\033[48;5;208m" + " " + reason + " " + RESET + bg + " "
              + fg(COLOR[logic.NEEDS_YOU]) + body + RESET)
    else:
        l2 = bg + indent + DIM + fit(sub, cols - left_w) + RESET
    if sel:  # 選択行は背景を行末まで伸ばす
        l1 = l1 + bg + " " * max(0, cols - disp_width(visible(l1))) + RESET
        l2 = l2 + bg + " " * max(0, cols - disp_width(visible(l2))) + RESET
    return l1, l2


def scroll_top(n_lines, rows, sel_lines, top):
    """本文（ヘッダーを除く）の表示開始位置。選択行が見えるように動かす。"""
    body_h = max(1, rows - HEADER_LINES)
    if sel_lines:
        first, last = min(sel_lines), max(sel_lines)
        if first < top:
            top = first
        elif last >= top + body_h:
            top = last - body_h + 1
    return max(0, min(top, max(0, n_lines - body_h)))


_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


def parse_input(buf):
    """キー入力を解釈する。戻り値: (イベントのリスト, 次に回す未完の入力)。
    イベント: ("move", -1|1) / ("open",) / ("quit",) / ("click", 行番号0始まり)"""
    events = []
    i = 0
    while i < len(buf):
        c = buf[i]
        if c == "\x1b":
            rest = buf[i:]
            if rest.startswith("\x1b[<"):
                m = _MOUSE_RE.match(rest)
                if not m:
                    if re.fullmatch(r"\x1b\[<[\d;]*", rest):
                        return events, rest  # 途中で切れたマウスイベント
                    i += 3
                    continue
                b, _x, y, kind = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
                if kind == "M" and b == 0:
                    events.append(("click", y - 1))
                elif kind == "M" and b in (64, 65):  # ホイール
                    events.append(("move", -1 if b == 64 else 1))
                i += m.end()
                continue
            if rest in ("\x1b", "\x1b[", "\x1bO"):
                return events, rest
            seq = rest[:3]
            if seq in ("\x1b[A", "\x1bOA"):
                events.append(("move", -1))
                i += 3
            elif seq in ("\x1b[B", "\x1bOB"):
                events.append(("move", 1))
                i += 3
            else:
                m = re.match(r"\x1b(\[[0-9;]*[@-~]|O.|.)?", rest)
                i += m.end() if m and m.end() else 1
            continue
        if c in ("k",):
            events.append(("move", -1))
        elif c in ("j",):
            events.append(("move", 1))
        elif c in ("\r", "\n"):
            events.append(("open",))
        elif c in ("q", "Q"):
            events.append(("quit",))
        i += 1
    return events, ""


class App:
    def __init__(self, board, interval=3.0, out=sys.stdout):
        self.board = board
        self.interval = interval
        self.out = out
        self.data = {"rows": [], "counts": {}, "now": time.time()}
        self.sel_id = None
        self.missing = 0
        self.top = 0
        self.msg = None
        self.msg_until = 0.0
        self.screen_ids = {}  # 画面の行番号 -> 行 id
        self.resized = True

    # 選択中のエージェントが一覧から消えても、この回数の更新までは選択を保つ（一時的な欠けで先頭に飛ばないように）
    KEEP_MISSING = 2

    def refresh(self):
        self.data = self.board.snapshot()
        ids = [r["id"] for r in self.ordered()]
        if not ids:
            return
        if self.sel_id in ids:
            self.missing = 0
            return
        self.missing += 1
        if self.sel_id is None or self.missing > self.KEEP_MISSING:
            self.sel_id, self.missing = ids[0], 0

    def ordered(self):
        order = {k: i for i, (k, _, _) in enumerate(SECTIONS)}
        rows = self.data.get("rows") or []
        return sorted(rows, key=lambda r: order.get(r.get("section"), 9))  # sorted は安定なので節内の順は保つ

    def move(self, d):
        ids = [r["id"] for r in self.ordered()]
        if not ids:
            return
        i = ids.index(self.sel_id) if self.sel_id in ids else 0
        self.sel_id = ids[max(0, min(len(ids) - 1, i + d))]

    def open(self):
        if not self.sel_id:
            return
        name = next((r["name"] for r in self.data.get("rows") or [] if r["id"] == self.sel_id), "")
        ok, how = self.board.focus(self.sel_id)
        self.flash(f"-> {name}" if ok else f"could not open {name}: {how}")

    def flash(self, msg):
        self.msg, self.msg_until = msg, time.time() + 3

    def draw(self):
        cols, rows = os.get_terminal_size(self.out.fileno()) if self.out.isatty() else (100, 40)
        msg = self.msg if time.time() < self.msg_until else None
        lines, ids = render(self.data, cols, self.sel_id, msg)
        body = lines[HEADER_LINES:]
        sel_lines = [n - HEADER_LINES for n, i in ids.items() if i == self.sel_id]
        self.top = scroll_top(len(body), rows, sel_lines, self.top)
        shown = lines[:HEADER_LINES] + body[self.top:self.top + rows - HEADER_LINES]
        self.screen_ids = {}
        for n, i in ids.items():
            if n < HEADER_LINES:
                continue
            sn = n - self.top
            if HEADER_LINES <= sn < rows:
                self.screen_ids[sn] = i
        self.out.write(frame(shown, rows, clear_all=self.resized))
        self.resized = False
        self.out.flush()


def frame(shown, rows, clear_all=False):
    """画面に書く文字列。各行は「行頭へ移動 -> 行を消す -> 書く」の順にする。
    行末で消去（ESC[K）すると、端末幅ちょうどの行の最終桁まで消えてしまうため。"""
    parts = ["\033[2J"] if clear_all else []
    for n in range(rows):
        parts.append(f"\033[{n + 1};1H\033[2K")
        if n < len(shown):
            parts.append(shown[n] + RESET)
    return "".join(parts)


def run(board, interval=3.0):
    import termios
    import tty

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("--tui は端末の中で起動してください", file=sys.stderr)
        return 1
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    app = App(board, interval)

    def on_winch(*_):
        app.resized = True

    def on_term(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGWINCH, on_winch)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)
    out = sys.stdout
    try:
        tty.setcbreak(fd)
        # 代替画面・カーソル非表示・マウス（SGR 形式）
        out.write("\033[?1049h\033[?25l\033[?1000h\033[?1006h")
        out.flush()
        pending, last = "", 0.0
        while True:
            if time.monotonic() - last >= interval:
                app.refresh()
                last = time.monotonic()
                app.draw()
            elif app.resized:
                app.draw()
            try:
                ready, _, _ = select.select([fd], [], [], max(0.05, min(0.5, interval - (time.monotonic() - last))))
            except InterruptedError:  # SIGWINCH
                continue
            if not ready:
                if app.msg and time.time() >= app.msg_until:
                    app.msg = None
                    app.draw()
                continue
            data = os.read(fd, 1024).decode("utf-8", errors="ignore")
            events, pending = parse_input(pending + data)
            for ev in events:
                if ev[0] == "quit":
                    return 0
                if ev[0] == "move":
                    app.move(ev[1])
                elif ev[0] == "open":
                    app.open()
                    last = 0.0
                elif ev[0] == "click" and ev[1] in app.screen_ids:
                    app.sel_id = app.screen_ids[ev[1]]
                    app.open()
                    last = 0.0
            app.draw()
    except KeyboardInterrupt:
        return 0
    finally:
        # 端末の状態を必ず戻す
        out.write("\033[?1006l\033[?1000l\033[?25h" + RESET + "\033[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
