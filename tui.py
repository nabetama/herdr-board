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


_WRAP_TOKEN_RE = re.compile(r"[!-~]+| +|.", re.S)


def wrap(s, w):
    """表示幅 w で折り返した行のリスト。ASCII の語はなるべく途中で切らない。s は sanitize 済みの 1 行。"""
    w = max(2, w)  # 全角 1 文字は入る幅にする
    lines, cur, cw = [], "", 0
    for tok in _WRAP_TOKEN_RE.findall(s or ""):
        tw = disp_width(tok)
        if cw + tw <= w:
            cur, cw = cur + tok, cw + tw
            continue
        if tok.startswith(" "):  # 行末に来た空白は捨てて折り返す
            lines.append(cur.rstrip())
            cur, cw = "", 0
            continue
        if cur:
            lines.append(cur.rstrip())
            cur, cw = "", 0
        if tw <= w:
            cur, cw = tok, tw
            continue
        for c in tok:  # 幅より長い語は文字の切れ目で折る
            x = char_width(c)
            if cw + x > w:
                lines.append(cur)
                cur, cw = "", 0
            cur, cw = cur + c, cw + x
    lines.append(cur.rstrip() if lines else cur)
    return lines


def wrap_text(text, w, indent=0):
    """複数行の本文を、1 行ずつ sanitize してから表示幅 w で折り返す。行頭の字下げは残す（タブは 4 桁）。"""
    out = []
    for raw in (text or "").splitlines() or [""]:
        raw = raw.replace("\t", "    ")
        lead = min(len(raw) - len(raw.lstrip(" ")), w // 2)
        body = sanitize(raw)
        if not body:
            out.append(" " * indent)
            continue
        pad = " " * (indent + lead)
        out.extend(pad + l for l in wrap(body, w - indent - lead))
    return out


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


HINT = "Up/Down j/k: select   Enter/click: open in terminal   d: detail   q: quit"
HINT_DETAIL = "j/k: select  Enter: open  d: close  J/K PgUp/PgDn: scroll  q: quit"


def render(data, cols, sel_id, msg=None, detail_open=False):
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
    hint = HINT_DETAIL if detail_open else HINT
    if data.get("error"):
        lines.append(fg(196) + fit(sanitize("herdr: " + data["error"]), cols) + RESET)
    elif data.get("warning") and not msg:
        lines.append(fg(178) + fit(sanitize(data["warning"]), cols) + RESET)
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
    if data.get("recent_done") is not None:
        lines.extend(_recent_done_lines(data["recent_done"], cols, name_w))
    return lines, ids


def _recent_done_lines(done, cols, name_w):
    """--tasks の完了の表。Herdr にいないので行 id を持たせない（選択・移動の対象外）。"""
    out = ["", (fg(COLOR[logic.IDLE]) + BOLD if done else DIM) + f"RECENT DONE ({len(done)})" + RESET]
    left_w = 3 + name_w + 1
    for d in done:
        ended = sanitize(d.get("ended"))
        right = "  ".join(x for x in (sanitize(d.get("repo")) if d.get("repo") != d.get("name") else "", ended) if x)
        title_w = max(0, cols - left_w - disp_width(right) - 2)
        out.append(DIM + " - " + RESET + fit(sanitize(d.get("name")), name_w) + " "
                   + fit(sanitize(d.get("task")), title_w) + "  " + DIM + right + RESET)
        if d.get("result"):
            out.append(" " * left_w + DIM + fit(sanitize(d["result"]), cols - left_w) + RESET)
    return out


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


def detail_lines(detail, cols):
    """詳細（Board.detail() の戻り値）を画面の行（ANSI 付き）にする。全行が表示幅 cols 以下になる。

    出す順: 結果ファイル（--results-dir が無ければ出さない）、--tasks の表の行（--tasks が無ければ出さない）、
    最後の応答。中身はすべて sanitize を通す。"""
    cols = max(20, cols)
    out = []

    def head(label, value=""):
        out.append(BOLD + fg(75) + fit(label + sanitize(value), cols, pad=False) + RESET)

    def note(text):
        out.append(DIM + fit("  " + sanitize(text), cols, pad=False) + RESET)

    def body(text):
        out.extend(wrap_text(text, cols, indent=2))

    if not detail:
        note("(no detail for this agent)")
        return out
    res = detail.get("result")
    if res is not None:
        head("RESULT FILE")
        out.extend(DIM + l + RESET for l in wrap_text(res.get("path") or "", cols, indent=2))
        state = res.get("state")
        if state == "found":
            body(res.get("text") or "")
            if res.get("truncated"):
                note("(cut at 256 KB; open the file to read the rest)")
        elif state == "missing":
            note("(none: the file does not exist)")
        elif state == "refused":
            note("(none: not read; the name has / or .., or the path is outside the directory)")
        else:
            note("(none: could not read: " + (res.get("error") or "") + ")")
        out.append("")
    if detail.get("tasks"):
        task = detail.get("task")
        if not task:
            head("TASK")
            note("(none: no row with this name in --tasks)")
        else:
            active = task.get("table") == "active"
            head("TASK (in progress)" if active else "TASK (done)")
            fields = (("依頼内容", "task"), ("状態", "status"), ("結果・メモ", "note")) if active else \
                (("依頼内容", "task"), ("終了", "ended"), ("結果", "result"))
            for label, key in fields:
                out.append(DIM + "  " + label + RESET)
                if task.get(key):
                    out.extend(wrap_text(task[key], cols, indent=4))
                else:
                    out.append(DIM + "    -" + RESET)
        out.append("")
    head("LAST REPLY")
    if detail.get("last_assistant"):
        body(detail["last_assistant"])
    else:
        note("(none: no conversation log)")
    return out


def detail_bar(name, top, shown, total, cols):
    """詳細の上に固定で出す 1 行（名前と表示中の範囲）。"""
    pos = f"{top + 1}-{top + shown}/{total}" if total else "0/0"
    right = f" {pos}  d: close "
    left = " DETAIL  " + sanitize(name)
    left = fit(left, max(0, cols - len(right)))
    return SEL_BG + BOLD + left + RESET + SEL_BG + DIM + right[:max(0, cols - disp_width(left))] + RESET


# 詳細を開いたとき、一覧と詳細（上の 1 行を含む）にそれぞれこの行数を取れなければ、詳細だけを出す
MIN_PANE = 4


def split_body(rows, detail_open):
    """本文（ヘッダーを除く）の行数を、(一覧, 詳細) に分ける。詳細の行数は上の 1 行を含む。"""
    body_h = max(1, rows - HEADER_LINES)
    if not detail_open:
        return body_h, 0
    list_h = body_h // 2
    if list_h < MIN_PANE or body_h - list_h < MIN_PANE:
        return 0, body_h
    return list_h, body_h - list_h


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
    イベント: ("move", -1|1) / ("open",) / ("quit",) / ("click", 行番号0始まり)
              / ("detail",)（詳細の開閉） / ("scroll", -1|1)（詳細を 1 行） / ("page", -1|1)（詳細を半画面）"""
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
            if rest in ("\x1b", "\x1bO") or re.fullmatch(r"\x1b\[[0-9;]*", rest):
                return events, rest  # 途中で切れたシーケンス
            if rest.startswith(("\x1b[5~", "\x1b[6~")):  # PageUp / PageDown
                events.append(("page", -1 if rest[2] == "5" else 1))
                i += 4
                continue
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
        elif c == "d":
            events.append(("detail",))
        elif c in ("J", "K"):
            events.append(("scroll", 1 if c == "J" else -1))
        i += 1
    return events, ""


class App:
    def __init__(self, board, interval=3.0, out=sys.stdout, web=None):
        self.board = board
        self.interval = interval
        self.out = out
        self.data = {"rows": [], "counts": {}, "now": time.time()}
        self.sel_id = None
        self.missing = 0
        self.top = 0
        self.msg = None
        self.msg_until = 0.0
        self.screen_ids = {}  # 画面の行番号 -> 行 id（一覧の範囲だけ）
        self.resized = True
        self.detail_open = False
        self.detail = None      # 選択中の行の Board.detail()
        self.detail_id = None   # self.detail がどの行 id のものか
        self.dtop = 0           # 詳細の表示開始位置
        self.page = 10          # 半画面の行数（描くたびに詳細の高さから決める）
        self._dlines = (None, None, [])  # (detail, cols, 行) の控え。毎回折り返し直さないように
        # --port を付けたとき: 起動できたら URL をしばらく出し、できなければ警告として 2 行目に出し続ける
        web = web or {}
        self.web_error = web.get("error")
        if web.get("url"):
            self.msg, self.msg_until = f"web: {web['url']}", time.time() + 10

    # 選択中のエージェントが一覧から消えても、この回数の更新までは選択を保つ（一時的な欠けで先頭に飛ばないように）
    KEEP_MISSING = 2

    def refresh(self):
        self.data = self.board.snapshot()
        if self.web_error:  # snapshot はブラウザ版と共有しているので書き換えずに写す
            self.data = dict(self.data, warning="  ".join(w for w in (self.web_error, self.data.get("warning")) if w))
        ids = [r["id"] for r in self.ordered()]
        if ids:
            if self.sel_id in ids:
                self.missing = 0
            else:
                self.missing += 1
                if self.sel_id is None or self.missing > self.KEEP_MISSING:
                    self.sel_id, self.missing = ids[0], 0
        self.load_detail(force=True)

    def load_detail(self, force=False):
        """詳細を開いていれば、選択中の行の詳細を読む。選択が変わったときはスクロール位置を先頭に戻す。
        force でなければ、選択が変わったときだけ読む（ファイルを読むので、キーを押すたびには読まない）。"""
        if not self.detail_open:
            return
        if self.sel_id != self.detail_id:
            self.dtop = 0
        elif not force:
            return
        self.detail = self.board.detail(self.sel_id) if self.sel_id else None
        self.detail_id = self.sel_id

    def toggle_detail(self):
        self.detail_open = not self.detail_open
        if self.detail_open:
            self.load_detail(force=True)

    def scroll_detail(self, n):
        if self.detail_open:
            self.dtop = max(0, self.dtop + n)  # 下の端は描くときにそろえる

    def select(self, row_id):
        self.sel_id = row_id
        self.load_detail()

    def ordered(self):
        order = {k: i for i, (k, _, _) in enumerate(SECTIONS)}
        rows = self.data.get("rows") or []
        return sorted(rows, key=lambda r: order.get(r.get("section"), 9))  # sorted は安定なので節内の順は保つ

    def move(self, d):
        ids = [r["id"] for r in self.ordered()]
        if not ids:
            return
        i = ids.index(self.sel_id) if self.sel_id in ids else 0
        self.select(ids[max(0, min(len(ids) - 1, i + d))])

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
        lines, ids = render(self.data, cols, self.sel_id, msg, self.detail_open)
        body = lines[HEADER_LINES:]
        list_h, detail_h = split_body(rows, self.detail_open)
        sel_lines = [n - HEADER_LINES for n, i in ids.items() if i == self.sel_id]
        self.top = scroll_top(len(body), list_h + HEADER_LINES, sel_lines, self.top)
        shown = lines[:HEADER_LINES] + body[self.top:self.top + list_h]
        shown += [""] * (HEADER_LINES + list_h - len(shown))
        self.screen_ids = {}
        for n, i in ids.items():
            if n < HEADER_LINES:
                continue
            sn = n - self.top
            if HEADER_LINES <= sn < HEADER_LINES + list_h:
                self.screen_ids[sn] = i
        if detail_h:
            shown += self.detail_view(cols, detail_h)
        self.out.write(frame(shown, rows, clear_all=self.resized))
        self.resized = False
        self.out.flush()

    def detail_view(self, cols, height):
        """詳細の欄（上の 1 行 + 本文 height - 1 行）。"""
        d = self.detail
        if self._dlines[0] is not d or self._dlines[1] != cols:
            self._dlines = (d, cols, detail_lines(d, cols))
        dl = self._dlines[2]
        h = max(1, height - 1)
        self.page = max(1, h // 2)
        self.dtop = max(0, min(self.dtop, len(dl) - h))
        part = dl[self.dtop:self.dtop + h]
        name = (d or {}).get("name") or ""
        return [detail_bar(name, self.dtop, len(part), len(dl), cols)] + part


def frame(shown, rows, clear_all=False):
    """画面に書く文字列。各行は「行頭へ移動 -> 行を消す -> 書く」の順にする。
    行末で消去（ESC[K）すると、端末幅ちょうどの行の最終桁まで消えてしまうため。"""
    parts = ["\033[2J"] if clear_all else []
    for n in range(rows):
        parts.append(f"\033[{n + 1};1H\033[2K")
        if n < len(shown):
            parts.append(shown[n] + RESET)
    return "".join(parts)


def run(board, interval=3.0, web=None):
    import termios
    import tty

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("--tui は端末の中で起動してください", file=sys.stderr)
        return 1
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    app = App(board, interval, web=web)

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
                elif ev[0] == "detail":
                    app.toggle_detail()
                elif ev[0] == "scroll":
                    app.scroll_detail(ev[1])
                elif ev[0] == "page":
                    app.scroll_detail(ev[1] * app.page)
                elif ev[0] == "open":
                    app.open()
                    last = 0.0
                elif ev[0] == "click" and ev[1] in app.screen_ids:
                    app.select(app.screen_ids[ev[1]])
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
