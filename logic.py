"""herdr-board の判定ロジック（I/O を持たない純粋関数）。

- 会話ログ（jsonl）の行から、最後の発言・未完了のツール呼び出し・バックグラウンドタスクを取り出す
- エージェントをセクション（needs_you / done / working / idle）に振り分ける
- 行に出すタイトル・補足・経過時間を決める
"""
import json
import re
from datetime import datetime
from pathlib import Path

NEEDS_YOU = "needs_you"
DONE = "done"
WORKING = "working"
IDLE = "idle"
SECTION_ORDER = [NEEDS_YOU, DONE, WORKING, IDLE]

# ツールの種類 -> バックグラウンドバッジの表示名
BG_KIND = {"Bash": "shell", "Monitor": "monitor", "Agent": "agent", "Task": "agent", "Workflow": "workflow"}

# ユーザーへの質問として扱う言い回し（最後の数行に含まれていれば「質問で停止」）
QUESTION_PATTERNS = [
    r"[?？]\s*[)）」』\]]*\s*$",
    r"ますか", r"ませんか", r"でしょうか", r"ですか[。．]?\s*$",
    r"(いい|よい|良い|よろしい)(です)?か",
    r"どちら", r"どれ(に|を|が)", r"どう(します|しましょう|すれば)",
    r"教えてください", r"選んでください", r"指示(を|して)ください", r"お知らせください",
    r"決めてください", r"判断(を|して)ください", r"返信(を|して)ください", r"回答(を|して)ください",
    r"承認(を|して)ください", r"許可(を|して)ください",
    r"\b(should i|shall i|do you want|would you like|let me know|which (one|option))\b",
]
_QUESTION_RE = re.compile("|".join(QUESTION_PATTERNS), re.IGNORECASE)


def short(s, n=160):
    """改行・連続空白を 1 つの空白にまとめ、n 文字を超えたら末尾を … にする。"""
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text":
            out.append(c.get("text", ""))
    return "\n".join(out)


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_human_prompt(e):
    """jsonl の 1 行が、ユーザーが自分で打った発言かどうか。"""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary"):
        return False
    origin = e.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    content = (e.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
        return False
    text = _text_of(content).strip()
    return bool(text) and not text.startswith("<")


def _tool_desc(name, inp):
    inp = inp or {}
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        if qs and isinstance(qs[0], dict):
            return qs[0].get("question") or ""
    d = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("url") or inp.get("prompt") or ""
    return short(str(d), 100)


PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_TASK_STATUS_RE = re.compile(r"<status>([^<]+)</status>")


def summarize_lines(lines):
    """会話ログの行（文字列）のリストを要約する。末尾だけを渡す前提で、途中から始まっても壊れない。"""
    s = {
        "last_ts": None,
        "last_user": None, "last_user_ts": None,
        "last_assistant": None, "last_assistant_ts": None,
        "last_tool": None,
        "pending_tools": [],   # 結果が返っていない tool_use（許可待ち・質問 UI の手がかり）
        "bg_tasks": {},        # 動いているバックグラウンドタスク id -> 種類
        "git_branch": None,
        "custom_title": None,
        "pr_url": None,       # 会話ログに最後に出た PR の URL
    }
    pending = {}  # tool_use_id -> {"name", "desc"}
    tool_names = {}
    for line in lines:
        try:
            e = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(e, dict):
            continue
        if "/pull/" in line:
            urls = PR_URL_RE.findall(line)
            if urls:
                s["pr_url"] = urls[-1]
        t = e.get("type")
        ts = _parse_ts(e.get("timestamp"))
        if ts:
            s["last_ts"] = ts
        if e.get("gitBranch"):
            s["git_branch"] = e["gitBranch"]
        if t in ("custom-title", "ai-title") or e.get("customTitle"):
            title = e.get("customTitle") or e.get("aiTitle") or e.get("title")
            if title and (t != "ai-title" or not s["custom_title"]):
                s["custom_title"] = title
            continue
        content = (e.get("message") or {}).get("content")
        if t == "assistant":
            for c in content or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    s["last_assistant"] = c["text"]
                    s["last_assistant_ts"] = ts
                elif c.get("type") == "tool_use":
                    desc = _tool_desc(c.get("name"), c.get("input"))
                    s["last_tool"] = f"{c.get('name')}: {desc}" if desc else c.get("name")
                    pending[c.get("id")] = {"name": c.get("name"), "desc": desc}
                    tool_names[c.get("id")] = c.get("name")
        elif t == "user":
            if is_human_prompt(e):
                s["last_user"] = _text_of(content)
                s["last_user_ts"] = ts
                continue
            # タスク完了の通知（文字列か text ブロック）
            text = _text_of(content)
            if "<task-notification>" in text:
                for block in text.split("<task-notification>")[1:]:
                    m_id, m_st = _TASK_ID_RE.search(block), _TASK_STATUS_RE.search(block)
                    if m_id and (not m_st or m_st.group(1).strip() != "running"):
                        s["bg_tasks"].pop(m_id.group(1).strip(), None)
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tid = c.get("tool_use_id")
                    pending.pop(tid, None)
                    r = e.get("toolUseResult")
                    if isinstance(r, dict):
                        bg = r.get("backgroundTaskId") or (r.get("taskId") if r.get("status") in ("async_launched", "running") else None)
                        if bg:
                            s["bg_tasks"][bg] = BG_KIND.get(tool_names.get(tid), "task")
    s["pending_tools"] = list(pending.values())
    return s


def find_question(text):
    """アシスタントの最後の発言から、ユーザーへの質問・確認依頼の行を返す。なければ None。"""
    if not text:
        return None
    body = re.sub(r"```.*?```", "", text, flags=re.S)
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    # 最後の 4 行（箇条書きの選択肢が続く場合を考えて少し広めに見る）
    for l in reversed(lines[-4:]):
        plain = re.sub(r"[*_`#>]+", "", l).strip()
        if _QUESTION_RE.search(plain):
            return plain
    return None


def classify(herdr_status, summary):
    """エージェントを振り分ける。

    戻り値: (section, reason, ask)
      section: NEEDS_YOU / DONE / WORKING / IDLE
      reason:  NEEDS_YOU の理由（"blocked" / "質問 UI" / "許可待ち?" / "質問で停止" / "状態不明"）。他は None
      ask:     何を求めているかの 1 行（"you: ..."）。他は None
    """
    summary = summary or {}
    status = herdr_status or "unknown"
    pending = summary.get("pending_tools") or []
    ask_q = next((p for p in pending if p.get("name") == "AskUserQuestion"), None)
    other = next((p for p in reversed(pending) if p.get("name") != "AskUserQuestion"), None)
    question = None
    la, lu = summary.get("last_assistant"), summary.get("last_user_ts")
    la_ts = summary.get("last_assistant_ts")
    if la and (lu is None or la_ts is None or la_ts >= lu):
        question = find_question(la)

    if status == "working":
        return WORKING, None, None
    if status == "blocked":
        if ask_q:
            return NEEDS_YOU, "blocked", "you: answer — " + short(ask_q["desc"], 120)
        if other:
            return NEEDS_YOU, "blocked", "you: approve " + _tool_label(other)
        if question:
            return NEEDS_YOU, "blocked", "you: " + short(question, 120)
        return NEEDS_YOU, "blocked", "you: 許可ダイアログか質問に答える"
    # idle / done / unknown
    if ask_q:
        return NEEDS_YOU, "質問 UI", "you: answer — " + short(ask_q["desc"], 120)
    if other:
        return NEEDS_YOU, "許可待ち?", "you: approve " + _tool_label(other)
    if question:
        return NEEDS_YOU, "質問で停止", "you: " + short(question, 120)
    if status == "unknown":
        return NEEDS_YOU, "状態不明", "you: 画面を確認する（Herdr が状態を判定できない）"
    if status == "done":
        return DONE, None, None
    return IDLE, None, None


def _tool_label(p):
    return f"{p.get('name')}: {p.get('desc')}" if p.get("desc") else str(p.get("name"))


def first_line_title(text, n=90):
    """依頼文などから、タイトルにする 1 行を取り出す（見出し記号や「依頼:」を外す）。"""
    for l in (text or "").splitlines():
        l = re.sub(r"^[#>\-*\s]+", "", l).strip()
        l = re.sub(r"^(依頼|お願い|task)\s*[:：]\s*", "", l, flags=re.I)
        if l:
            return short(l, n)
    return None


def pick_title(summary, session_meta, first_prompt, cwd):
    """行のタイトル。優先順:
    1. 会話ログの custom-title（/rename など）
    2. Claude Code のセッション名（自動で付いた名前 nameSource=derived は除く）
    3. 最初の依頼の 1 行目
    4. 最後のユーザー発言
    5. cwd のディレクトリ名
    """
    summary = summary or {}
    session_meta = session_meta or {}
    if summary.get("custom_title"):
        return short(summary["custom_title"], 90)
    if session_meta.get("name") and session_meta.get("nameSource") not in (None, "derived"):
        return short(session_meta["name"], 90)
    for cand in (first_prompt, summary.get("last_user")):
        t = first_line_title(cand)
        if t:
            return t
    return Path(cwd).name if cwd else ""


def pick_subline(section, summary, ask):
    """2 行目の補足。NEEDS YOU は求めていること、WORKING は直近のツール、他は最後の発言。"""
    summary = summary or {}
    if section == NEEDS_YOU:
        return ask
    if section == WORKING and summary.get("last_tool"):
        return short(summary["last_tool"], 140)
    if summary.get("last_assistant"):
        return short(summary["last_assistant"], 140)
    return short(summary.get("last_user"), 140) or None


def bg_badge(bg_tasks):
    """{"id": "shell", ...} -> "bg: 2 shell, 1 monitor"。なければ None。"""
    if not bg_tasks:
        return None
    counts = {}
    for kind in bg_tasks.values():
        counts[kind] = counts.get(kind, 0) + 1
    return "bg: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))


def fmt_elapsed(seconds):
    """経過秒数を 45s / 12m / 5h / 3d にする。"""
    if seconds is None:
        return ""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def initial_since(status, summary, now):
    """サーバーが状態の変化を見ていないとき（起動直後）に、その状態になった時刻を推定する。
    working はユーザーの最後の発言（ターンの開始）、それ以外は会話ログの最後の記録。"""
    summary = summary or {}
    if status == "working":
        return summary.get("last_user_ts") or summary.get("last_ts") or now
    return summary.get("last_ts") or now


class SinceTracker:
    """エージェントごとに「今の状態になった時刻」を覚える。状態が変わったらその時刻にする。"""

    def __init__(self):
        self._seen = {}  # key -> (status, since)

    def update(self, key, status, summary, now):
        prev = self._seen.get(key)
        if prev and prev[0] == status:
            return prev[1]
        since = now if prev else initial_since(status, summary, now)
        self._seen[key] = (status, since)
        return since

    def forget_except(self, keys):
        for k in list(self._seen):
            if k not in keys:
                del self._seen[k]


def ci_state(rollup):
    """gh の statusCheckRollup から pass / fail / pending / None を出す。"""
    if not rollup:
        return None
    fail_words = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    pending = False
    for c in rollup:
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        if concl in fail_words:
            return "fail"
        if (status and status != "COMPLETED") or concl in ("PENDING", "EXPECTED", ""):
            pending = True
    return "pending" if pending else "pass"


def project_dir_name(cwd):
    """Claude Code が会話ログを置くディレクトリ名（cwd の / と . を - にしたもの）。"""
    return re.sub(r"[/.]", "-", cwd or "")
