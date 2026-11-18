"""作業の一覧表（Markdown）の読み込み（--tasks）。

見出し `## 進行中` / `## 完了`（英語なら `## In progress` / `## Done` など）の下にある表を読む。
列は見出し行の名前で探す。区切り行（|---|）はどこにあっても読み飛ばす。
"""
import os
import re

# 見出し（小文字にして前後の空白を除いたもの）-> セクション
SECTION_HEADINGS = {
    "active": ("進行中", "作業中", "in progress", "in-progress", "active", "doing", "running", "ongoing"),
    "done": ("完了", "済み", "done", "completed", "finished", "closed"),
}

# 列名の別名（小文字・空白除去で比べる）。前にあるものほど優先
COLUMNS = {
    "name": ("名前", "エージェント", "name", "agent"),
    "status": ("状態", "ステータス", "status", "state"),
    "task": ("依頼内容", "依頼", "内容", "task", "request", "description"),
    "note": ("結果・メモ", "メモ", "結果", "note", "notes", "memo", "result"),
    "repo": ("リポジトリ", "repo", "repository"),
    "ended": ("終了", "完了日時", "ended", "end", "finished", "done at", "completed"),
    "result": ("結果", "結果・メモ", "result", "results", "note", "notes"),
}
ACTIVE_COLUMNS = ("name", "status", "task", "note")
DONE_COLUMNS = ("name", "repo", "ended", "task", "result")

# 表の「状態」がこれを含むと NEEDS YOU に入れる
NEEDS_YOU_WORDS = ("質問待ち", "確認待ち", "判断待ち", "回答待ち", "承認待ち",
                   "needs-you", "needs_you", "needs you", "waiting for user", "question")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _split_row(line):
    """| a | b \\| c | -> ["a", "b | c"]"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells = re.split(r"(?<!\\)\|", body)
    return [c.strip().replace("\\|", "|") for c in cells]


def _is_separator(cells):
    return bool(cells) and all(re.fullmatch(r":?-{1,}:?", c) for c in cells if c) and any(cells)


def _section_of(heading):
    h = _norm(heading)
    for key, words in SECTION_HEADINGS.items():
        if any(h == w or h.startswith(w + " ") or h.startswith(w + "（") or h.startswith(w + "(") for w in words):
            return key
    return None


def _column_index(header, wanted):
    """見出し行のセル -> {列の種類: 位置}。別名の優先順に探し、同じ位置を 2 つの種類に使わない。"""
    normed = [_norm(c) for c in header]
    found, used = {}, set()
    for key in wanted:
        for alias in COLUMNS[key]:
            if alias in normed and normed.index(alias) not in used:
                found[key] = normed.index(alias)
                used.add(found[key])
                break
    return found


def parse(text):
    """Markdown を読み、{"active": [...], "done": [...]} を返す。各要素は列の種類 -> 文字列の dict。"""
    out = {"active": [], "done": []}
    section, header = None, None
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            section = _section_of(m.group(2)) if len(m.group(1)) == 2 else (section if len(m.group(1)) > 2 else None)
            header = None
            continue
        if section is None:
            continue
        if not line.startswith("|"):
            header = None  # 表の外に出たら、次の表は見出し行から読み直す
            continue
        cells = _split_row(line)
        if _is_separator(cells):
            continue
        if header is None:
            header = _column_index(cells, ACTIVE_COLUMNS if section == "active" else DONE_COLUMNS)
            if "name" not in header:
                header = {}  # 名前の列がない表は使わない
            continue
        if not header:
            continue
        row = {k: (cells[i] if i < len(cells) else "") for k, i in header.items()}
        if row.get("name"):
            out[section].append(row)
    return out


def needs_you(status):
    s = _norm(status)
    return bool(s) and any(w in s for w in NEEDS_YOU_WORDS)


def _time_key(s):
    """「11-18 10:55」「2026-11-18 10:55」などを、数字の並びで比べられるようにする。"""
    return [int(x) for x in re.findall(r"\d+", s or "")]


def recent_done(done, n=5):
    """完了の表から、終了の新しい順に n 件。終了が同じなら表で下にあるものを新しいとみなす。"""
    indexed = list(enumerate(done))
    indexed.sort(key=lambda p: (_time_key(p[1].get("ended")), p[0]), reverse=True)
    return [r for _, r in indexed[:max(0, n)]]


class TaskFile:
    """--tasks のファイル。mtime が変わったときだけ解析し直す。読めなければ warning に 1 行入れる。"""

    def __init__(self, path):
        self.path = os.path.expanduser(path)
        self._key = None
        self.data = {"active": [], "done": []}
        self.warning = None

    def load(self):
        try:
            st = os.stat(self.path)
            key = (st.st_mtime_ns, st.st_size)
            if key != self._key:
                with open(self.path, encoding="utf-8", errors="replace") as f:
                    self.data = parse(f.read())
                self._key = key
            self.warning = None
        except OSError as ex:
            self.data, self._key = {"active": [], "done": []}, None
            self.warning = f"--tasks を読めません: {ex.strerror or ex}"
        except Exception as ex:  # 想定外の形式でも board は止めない
            self.data, self._key = {"active": [], "done": []}, None
            self.warning = f"--tasks を解析できません: {ex}"
        return self.data
