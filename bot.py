#!/usr/bin/env python3
# ==============================================================================
# ⚡ BILLY QUIZ FORGE — SQLITE EDITION (GitHub Actions Ready)
#
# env: BOT_TOKEN, ADMIN_IDS, GROUP_ID
# pip: python-telegram-bot>=21.1.1 python-dotenv
# ==============================================================================
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import threading
import time
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
    Poll,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ============================== CONFIG ========================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
EVERYONE_CAN_CREATE = True

def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in re.split(r"[,\s;]+", raw or ""):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            logging.warning("ADMIN_IDS: ignoring %r", chunk)
    return out

ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", ""))

try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0").strip() or 0)
except ValueError:
    GROUP_ID = 0

_missing = [
    name
    for name, val in (
        ("BOT_TOKEN", BOT_TOKEN),
        ("ADMIN_IDS", ADMIN_IDS),
        ("GROUP_ID", GROUP_ID),
    )
    if not val
]
if _missing:
    raise SystemExit(f"Missing environment variables: {', '.join(_missing)}")

ALLOWED = filters.Chat(chat_id=GROUP_ID) | filters.ChatType.PRIVATE

# Telegram hard limits (Bot API)
MIN_TIMER, MAX_TIMER = 5, 600
MAX_QUESTIONS = 100
MAX_OPTIONS = 10
Q_LIMIT, OPT_LIMIT, EXP_LIMIT = 300, 100, 200
COOLDOWN = 2
DEFAULT_NEG = 0.25
PAGE_SIZE = 6

EMOJIS = [
    "🏆", "🎯", "🧠", "🔥", "⚡", "✨", "🚀", "💡", "🌟", "👑", 
    "📈", "💪", "🎖️", "⭐", "🎓", "💎", "🔮", "🌈", "🧩", "🦅", "🦁"
]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("billy")

def esc(v: Any) -> str:
    return html.escape(str(v), quote=False)

def emo(n: int = 2) -> List[str]:
    return random.sample(EMOJIS, min(n, len(EMOJIS)))

def is_admin_id(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_admin(user) -> bool:
    return bool(user and user.id in ADMIN_IDS)

# ============================== SQLITE SETUP ==================================

DB_FILE = "quizforge.db"
_local = threading.local()

def get_db():
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_FILE, timeout=30)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn

def connect_db() -> None:
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS quizzes (
            quiz_id TEXT PRIMARY KEY,
            title TEXT,
            timer INTEGER,
            negative_enabled INTEGER,
            neg_value REAL,
            shuffle INTEGER,
            questions TEXT,
            owner_id INTEGER,
            updated_at REAL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id TEXT,
            title TEXT,
            chat_id INTEGER,
            asked INTEGER,
            players INTEGER,
            duration_s INTEGER,
            scoreboard TEXT,
            finished_at REAL
        )
    ''')
    conn.commit()
    log.info("✅ SQLite ready!")

def close_db() -> None:
    pass

def _format_quiz(r: sqlite3.Row) -> dict:
    return {
        "id": r["quiz_id"],
        "title": r["title"],
        "timer": int(r["timer"]),
        "negative_enabled": bool(r["negative_enabled"]),
        "neg_value": float(r["neg_value"]),
        "shuffle": bool(r["shuffle"]),
        "questions": json.loads(r["questions"]) if r["questions"] else [],
        "owner_id": r["owner_id"],
    }

def save_quiz(q: dict) -> None:
    conn = get_db()
    doc = (
        str(q["id"])[:100],
        str(q.get("title") or "Untitled")[:300],
        max(MIN_TIMER, min(int(q.get("timer", 15)), MAX_TIMER)),
        int(bool(q.get("negative_enabled"))),
        float(q.get("neg_value") or DEFAULT_NEG),
        int(bool(q.get("shuffle", True))),
        json.dumps(q.get("questions") or []),
        q.get("owner_id"),
        time.time()
    )
    conn.execute('''
        INSERT OR REPLACE INTO quizzes 
        (quiz_id, title, timer, negative_enabled, neg_value, shuffle, questions, owner_id, updated_at) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', doc)
    conn.commit()

def load_quiz(qid: str) -> Optional[dict]:
    conn = get_db()
    r = conn.execute("SELECT * FROM quizzes WHERE quiz_id = ?", (qid,)).fetchone()
    return _format_quiz(r) if r else None

def list_quizzes(limit: int = 200) -> List[dict]:
    conn = get_db()
    cursor = conn.execute("SELECT * FROM quizzes ORDER BY updated_at DESC LIMIT ?", (limit,))
    return [_format_quiz(r) for r in cursor]

def delete_quiz(qid: str) -> bool:
    conn = get_db()
    res = conn.execute("DELETE FROM quizzes WHERE quiz_id = ?", (qid,))
    conn.commit()
    return res.rowcount > 0

def delete_all_quizzes() -> int:
    conn = get_db()
    res = conn.execute("DELETE FROM quizzes")
    conn.commit()
    return res.rowcount

def next_auto_id() -> str:
    # Safe collision-free ID for concurrent multi-user creation
    return f"QZ{int(time.time() % 1000000)}{random.randint(10,99)}"

def save_run(quiz: dict, chat_id: int, asked: int, dur: int, board: List[dict]) -> None:
    conn = get_db()
    doc = (
        quiz["id"],
        quiz["title"],
        chat_id,
        asked,
        len(board),
        dur,
        json.dumps(board[:50]),
        time.time()
    )
    conn.execute('''
        INSERT INTO runs (quiz_id, title, chat_id, asked, players, duration_s, scoreboard, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', doc)
    conn.commit()

def recent_runs(n: int = 5) -> List[dict]:
    conn = get_db()
    cursor = conn.execute("SELECT * FROM runs ORDER BY finished_at DESC LIMIT ?", (n,))
    out = []
    for r in cursor:
        d = dict(r)
        d["scoreboard"] = json.loads(d["scoreboard"]) if d["scoreboard"] else []
        out.append(d)
    return out

# ============================ PARSER ==========================================

_LETTERS = {c: i for i, c in enumerate("ABCDEFGHIJ")}

def _clean_opts(raw: List[Any]) -> List[str]:
    out: List[str] = []
    for i, o in enumerate(raw[:MAX_OPTIONS]):
        v = re.sub(r"\s+", " ", str(o)).strip()[:OPT_LIMIT] or f"Option {i+1}"
        base, k = v, 1
        while v in out:
            v = (base + "\u200b" * k)[:OPT_LIMIT]
            k += 1
        out.append(v)
    return out

def _mk(qt: str, opts: List[Any], ans: int, exp: str) -> Optional[dict]:
    o = _clean_opts(opts)
    if not qt or len(o) < 2:
        return None
    if not 0 <= ans < len(o):
        ans = 0
    return {
        "q": re.sub(r"\s+", " ", qt).strip()[:Q_LIMIT],
        "opts": o,
        "ans": ans,
        "exp": (re.sub(r"\s+", " ", exp).strip() or "✅ Correct!")[:EXP_LIMIT],
    }

def _parse_json(text: str) -> List[dict]:
    data = json.loads(text)
    items = data.get("questions", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: List[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        qt = str(it.get("question") or it.get("question_text") or it.get("q") or "")
        opts = it.get("options") or it.get("opts") or []
        opts = [str(o.get("text", "")) if isinstance(o, dict) else str(o) for o in opts]
        raw = it.get("correct_option_id")
        if raw is None:
            raw = it.get("answer", it.get("ans", 0))
        key = str(raw).strip().strip("().").upper()
        ans = _LETTERS.get(key, int(key) if key.isdigit() else 0)
        item = _mk(qt, opts, ans, str(it.get("explanation") or ""))
        if item:
            out.append(item)
        if len(out) >= MAX_QUESTIONS:
            break
    return out

_ANS_RE = re.compile(r"^(?:ANS|ANSWER|CORRECT|KEY|उत्तर)\s*[:\-–]?\s*\(?([A-J]|\d+)\)?", re.I)
_OPT_RE = re.compile(r"^[\(\[]?([A-Ja-j])[\)\].:\-]\s*(.+)$")
_EXP_RE = re.compile(r"^(?:EXP|EXPL\w*|REASON|व्याख्या)\s*[:\-–]?\s*(.*)$", re.I)
_SPLIT_RE = re.compile(r"(?:\n|^)(?=(?:Q\s*)?\d+\s*[).:\-]\s*\S)", re.I)

def _parse_plain(text: str) -> List[dict]:
    out: List[dict] = []
    for block in _SPLIT_RE.split(text.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        qt, opts, ans, exp = "", [], None, []
        for line in lines:
            if m := _ANS_RE.match(line):
                tok = m.group(1).upper()
                ans = _LETTERS.get(tok, int(tok) - 1 if tok.isdigit() else 0)
            elif m := _OPT_RE.match(line):
                opts.append(m.group(2).strip())
            elif m := _EXP_RE.match(line):
                exp.append(m.group(1).strip())
            elif not qt:
                qt = re.sub(r"^(?:Q\s*)?\d+\s*[).:\-]\s*", "", line, flags=re.I)
            else:
                exp.append(line)
        if ans is None:
            continue
        item = _mk(qt, opts, ans, " ".join(exp))
        if item:
            out.append(item)
        if len(out) >= MAX_QUESTIONS:
            break
    return out

def parse_questions(text: str) -> List[dict]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        js = _parse_json(text)
        if js:
            return js
    except Exception:
        pass
    return _parse_plain(text)

def detect_title(text: str) -> str:
    m = re.search(r"^\s*TITLE\s*[:\-]\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip()[:300] if m else "🚀 Quiz Special"


# ============================ ANIMATION KIT ===================================

SPINNER = ["◐", "◓", "◑", "◒"]
FILL, EMPTY = "▰", "▱"

def bar(done: int, total: int, width: int = 12) -> str:
    pct = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    k = round(pct * width)
    return f"{FILL * k}{EMPTY * (width - k)} {int(pct * 100)}%"

def wave(step: int, width: int = 12) -> str:
    pos = step % (2 * width - 2)
    if pos >= width:
        pos = 2 * width - 2 - pos
    return "".join(FILL if i == pos else EMPTY for i in range(width))

async def safe_edit(msg: Message, text: str, kb: Optional[InlineKeyboardMarkup] = None):
    try:
        return await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except RetryAfter as e:
        await asyncio.sleep(float(e.retry_after) + 0.5)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("edit failed: %s", e)
    except TelegramError as e:
        log.debug("edit failed: %s", e)
    return None

async def animate(msg: Message, frames: List[str], delay: float = 0.45):
    for f in frames:
        await safe_edit(msg, f)
        await asyncio.sleep(delay)

async def typing(bot, chat_id: int, action: str = ChatAction.TYPING):
    try:
        await bot.send_chat_action(chat_id, action)
    except TelegramError:
        pass


# ============================ UI / PANEL ======================================

BOX_TOP = "╭───────────────────────────────╮"
BOX_BOT = "╰───────────────────────────────╯"

def card(title: str, rows: List[Tuple[str, str]], footer: str = "") -> str:
    body = "\n".join(f"│ {k:<12}{esc(v)}" for k, v in rows)
    out = f"<pre>{BOX_TOP}\n│ {esc(title)}\n├───────────────────────────────\n{body}\n{BOX_BOT}</pre>"
    if footer:
        out += f"\n{footer}"
    return out

def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)

def public_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("⚡ Forge new quiz", "wiz|new"), btn("📚 Library", "lib|page|0")],
        [btn("📊 Statistics", "sys|stats"), btn("✖️ Close", "nav|close")],
    ])

def admin_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("⚡ Forge new quiz", "wiz|new"), btn("📚 Library", "lib|page|0")],
        [btn("📊 Statistics", "sys|stats"), btn("🛑 Stop live quiz", "sys|stop")],
        [btn("☠️ Wipe everything", "sys|delall"), btn("✖️ Close", "nav|close")],
    ])

def home_kb(uid: int) -> InlineKeyboardMarkup:
    return admin_home_kb() if is_admin_id(uid) else public_home_kb()

def home_text(uid: int) -> str:
    e = emo(2)
    role = "Admin" if is_admin_id(uid) else "Public (User)"
    queue_len = len(QQM.queue)
    queue_status = f"{QSM.describe()} / {queue_len} in queue"
    
    return card(
        f"{e[0]} BILLY QUIZ FORGE {e[1]}",
        [
            ("Access", role),
            ("Database", "SQLite"),
            ("Status", queue_status),
            ("Target", str(GROUP_ID)),
        ],
        "▸ <i>Pick an action below.</i>",
    )

TIMER_PRESETS = (10, 15, 20, 30, 45, 60)

@dataclass
class Wizard:
    step: str = "id"
    qid: str = ""
    title: str = ""
    timer: int = 15
    negative: bool = False
    neg_value: float = DEFAULT_NEG
    shuffle: bool = True
    count: int = 0
    questions: List[dict] = field(default_factory=list)

    ORDER = ("id", "title", "timer", "flags", "upload", "done")

    @property
    def index(self) -> int:
        return self.ORDER.index(self.step)

    def render(self) -> str:
        e = emo(2)
        rows = [
            ("🪪 ID", self.qid or "—"),
            ("🏷 Title", (self.title or "—")[:22]),
            ("⏳ Timer", f"{self.timer}s"),
            ("📉 Negative", f"ON -{self.neg_value:g}" if self.negative else "OFF"),
            ("🌀 Shuffle", "ON" if self.shuffle else "OFF"),
            ("🧩 Questions", self.count or "—"),
        ]
        hint = {
            "id": "Send a quiz ID, or tap <b>Auto</b>.",
            "title": "Send a title, or tap <b>Auto-detect</b>.",
            "timer": "Choose seconds per question.",
            "flags": "Toggle scoring rules, then <b>Continue</b>.",
            "upload": "Send a .txt/.json file or paste the questions.",
            "done": "Saved.",
        }[self.step]
        return card(
            f"{e[0]} QUIZ FORGE — step {self.index + 1}/5 {e[1]}",
            rows,
            f"<code>{bar(self.index, 5)}</code>\n▸ {hint}",
        )

    def kb(self) -> InlineKeyboardMarkup:
        rows: List[List[InlineKeyboardButton]] = []
        if self.step == "id":
            rows.append([btn("🎲 Auto ID", "wiz|autoid")])
        elif self.step == "title":
            rows.append([btn("🪄 Auto-detect title", "wiz|autotitle")])
        elif self.step == "timer":
            rows += [
                [btn(f"{t}s", f"wiz|timer|{t}") for t in TIMER_PRESETS[:3]],
                [btn(f"{t}s", f"wiz|timer|{t}") for t in TIMER_PRESETS[3:]],
            ]
        elif self.step == "flags":
            rows += [
                [
                    btn(f"📉 Negative: {'ON' if self.negative else 'OFF'}", "wiz|tog|neg"),
                    btn(f"🌀 Shuffle: {'ON' if self.shuffle else 'OFF'}", "wiz|tog|shuf"),
                ],
                [btn(f"➖ Penalty {self.neg_value:g}", "wiz|cycleneg")],
                [btn("▶️ Continue", "wiz|goupload")],
            ]
        rows.append([btn("✖️ Cancel", "wiz|cancel")])
        return InlineKeyboardMarkup(rows)

def quiz_card(q: dict, bot_username: str, uid: int) -> Tuple[str, InlineKeyboardMarkup]:
    e = emo(2)
    text = card(
        f"{e[0]} {q['title'][:24]} {e[1]}",
        [
            ("🪪 ID", q["id"]),
            ("🧩 Questions", len(q["questions"])),
            ("⏳ Timer", f"{q['timer']}s"),
            ("📉 Negative", f"-{q['neg_value']:g}" if q["negative_enabled"] else "OFF"),
            ("🌀 Shuffle", "ON" if q["shuffle"] else "OFF"),
        ],
    )
    
    rows = [
        [btn("🚀 Launch in group", f"q|start|{q['id']}"), btn("👁 Preview", f"q|prev|{q['id']}")],
        [
            InlineKeyboardButton("🪐 Add to a group", url=f"https://t.me/{bot_username}?startgroup={q['id']}"),
            InlineKeyboardButton("🪶 Share", switch_inline_query=q["id"]),
        ]
    ]
    
    # Ownership Check for Edit/Delete buttons
    if is_admin_id(uid) or q.get("owner_id") == uid:
        rows.append([btn("🪄 Edit", f"q|edit|{q['id']}"), btn("☄️ Delete", f"q|del|{q['id']}")])
        
    rows.append([btn("⬅️ Library", "lib|page|0")])
    return text, InlineKeyboardMarkup(rows)

def edit_kb(q: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("🏷 Title", f"q|field|title|{q['id']}"), btn("⏳ Timer", f"q|field|timer|{q['id']}")],
        [
            btn(f"📉 Negative {'ON' if q['negative_enabled'] else 'OFF'}", f"q|tog|neg|{q['id']}"),
            btn(f"🌀 Shuffle {'ON' if q['shuffle'] else 'OFF'}", f"q|tog|shuf|{q['id']}"),
        ],
        [btn(f"➖ Penalty {q['neg_value']:g}", f"q|cycleneg|{q['id']}")],
        [btn("⬅️ Back", f"lib|open|{q['id']}")],
    ])


# ============================ LIVE SESSION & QUEUE ============================

class Session:
    def __init__(self, quiz: dict, chat_id: int, host: int):
        self.quiz = quiz
        self.chat_id = chat_id
        self.host = host
        self.questions: List[dict] = list(quiz["questions"])[:MAX_QUESTIONS]
        self.index = 0
        self.paused = False
        self.stop = False
        self.skip = False
        self.players: Dict[int, dict] = {}
        self.trash: List[int] = []
        self.polls: Dict[str, int] = {}
        self.status_msg: Optional[Message] = None
        self.started = time.time()
        self._lock = threading.RLock()

    def score(self, uid: int, name: str, correct: bool) -> None:
        with self._lock:
            p = self.players.setdefault(uid, {"name": name, "correct": 0, "wrong": 0})
            p["name"] = name
            p["correct" if correct else "wrong"] += 1

    def top(self, n: int = 3) -> List[dict]:
        with self._lock:
            return sorted(self.players.values(), key=lambda p: (-p["correct"], p["wrong"]))[:n]

class SessionManager:
    def __init__(self):
        self.current: Optional[Session] = None
        self._lock = threading.RLock()

    def claim(self, quiz: dict, chat_id: int, host: int) -> Optional[Session]:
        with self._lock:
            if self.current and not self.current.stop:
                return None
            self.current = Session(quiz, chat_id, host)
            return self.current

    def get(self) -> Optional[Session]:
        with self._lock:
            return self.current

    def release(self, s: Optional[Session] = None) -> None:
        with self._lock:
            if s is None or self.current is s:
                self.current = None

    def describe(self) -> str:
        s = self.get()
        if not s:
            return "idle"
        return f"{s.quiz['id']} Q{s.index}/{len(s.questions)}"

QSM = SessionManager()

# -------- NEW: Asynchronous FIFO Queue Manager --------
class QuizQueueManager:
    def __init__(self):
        self.queue = []
        self.is_running = False
        self.active_quiz_running = False
        self._lock = None

    @property
    def lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def add_to_queue(self, ctx: ContextTypes.DEFAULT_TYPE, quiz: dict, chat_id: int, host: int, reply_msg: Optional[Message] = None) -> Tuple[bool, int]:
        async with self.lock:
            # Duplicate prevention check
            for item in self.queue:
                if item['host'] == host and item['quiz']['id'] == quiz['id']:
                    return False, -1
            
            self.queue.append({
                'quiz': quiz, 
                'chat_id': chat_id, 
                'host': host, 
                'ctx': ctx, 
                'reply_msg': reply_msg
            })
            
            # Position = waiting items + (1 if a quiz is already running ahead)
            pos = len(self.queue) + (1 if self.active_quiz_running else 0)
            
            if not self.is_running:
                self.is_running = True
                asyncio.create_task(self.worker())
                
            return True, pos

    async def worker(self):
        while True:
            async with self.lock:
                if not self.queue:
                    self.is_running = False
                    self.active_quiz_running = False
                    break
                item = self.queue.pop(0)
                self.active_quiz_running = True
                
            ctx = item['ctx']
            quiz = item['quiz']
            chat_id = item['chat_id']
            host = item['host']
            reply_msg = item['reply_msg']
            
            # Notify requester that their quiz is now starting
            if reply_msg:
                try:
                    await safe_edit(reply_msg, f"🚀 आपकी Quiz <b>{esc(quiz['title'])}</b> अब शुरू हो रही है!", InlineKeyboardMarkup([[btn("⬅️ Library", "lib|page|0")]]))
                except Exception:
                    pass
                    
            try:
                await run_quiz(ctx, quiz, chat_id, host)
            except Exception as e:
                log.error("run_quiz internally failed in queue worker: %s", e)
            
            # Brief buffer to let TG sync and prevent flooding
            await asyncio.sleep(3)
            
            async with self.lock:
                self.active_quiz_running = False

QQM = QuizQueueManager()

# ============================ QUIZ ENGINE =====================================

async def run_quiz(ctx: ContextTypes.DEFAULT_TYPE, quiz: dict, chat_id: int, host: int):
    s = QSM.claim(quiz, chat_id, host)
    if s is None:
        return # Handled safely, queue will move to next gracefully.

    if not s.questions:
        await ctx.bot.send_message(chat_id, "❌ This quiz has no questions.")
        QSM.release(s)
        return

    if quiz.get("shuffle", True):
        random.shuffle(s.questions)

    total = len(s.questions)
    timer = max(MIN_TIMER, min(int(quiz.get("timer", 15)), MAX_TIMER))

    try:
        e = emo(2)
        intro = await ctx.bot.send_message(
            chat_id, "<pre>booting quiz engine…</pre>", parse_mode=ParseMode.HTML
        )
        s.trash.append(intro.message_id)
        await animate(
            intro,
            [f"<pre>[{wave(i)}]  loading {SPINNER[i % 4]}</pre>" for i in range(6)],
            0.4,
        )
        header = card(
            f"{e[0]} {quiz['title'][:24]} {e[1]}",
            [
                ("🧩 Questions", total),
                ("⏳ Per Q", f"{timer}s"),
                ("📉 Negative", f"-{quiz['neg_value']:g}" if quiz["negative_enabled"] else "OFF"),
                ("🌀 Shuffle", "ON" if quiz["shuffle"] else "OFF"),
            ],
        )
        for f in ("3️⃣", "2️⃣", "1️⃣", "🚀 <b>GO!</b>"):
            await safe_edit(intro, f"{header}\n\n{f}")
            await asyncio.sleep(0.9)

        s.status_msg = await ctx.bot.send_message(
            chat_id,
            f"<code>{bar(0, total)}</code>\n🎯 warming up…",
            parse_mode=ParseMode.HTML,
        )
        s.trash.append(s.status_msg.message_id)

        poll_map = ctx.bot_data.setdefault("polls", {})

        while s.index < total and not s.stop:
            while s.paused and not s.stop:
                await asyncio.sleep(1)
            if s.stop:
                break

            q = s.questions[s.index]
            opts = _clean_opts(q.get("opts") or [])
            if len(opts) < 2:
                s.index += 1
                continue

            ans = int(q.get("ans", 0))
            ans = ans if 0 <= ans < len(opts) else 0
            correct_text = opts[ans]
            random.shuffle(opts)
            ans = opts.index(correct_text)

            e = emo(2)
            await typing(ctx.bot, chat_id)
            try:
                msg = await ctx.bot.send_poll(
                    chat_id=chat_id,
                    question=f"{e[0]} Q{s.index + 1}/{total} — {q['q']} {e[1]}"[:Q_LIMIT],
                    options=opts,
                    type=Poll.QUIZ,
                    correct_option_id=ans,
                    explanation=str(q.get("exp") or "✅ Correct!")[:EXP_LIMIT],
                    is_anonymous=False,
                    open_period=timer,
                )
                s.trash.append(msg.message_id)
                s.polls[msg.poll.id] = ans
                poll_map[msg.poll.id] = s
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
                continue
            except TelegramError as exc:
                log.error("send_poll failed on Q%s: %s", s.index + 1, exc)
                s.index += 1
                continue

            waited = 0.0
            while waited < timer + COOLDOWN:
                if s.stop:
                    break
                if s.skip:
                    s.skip = False
                    break
                await asyncio.sleep(0.5)
                waited += 0.5

            s.index += 1

            if s.status_msg:
                lead = s.top(3)
                board = (
                    "\n".join(
                        f"{m} <b>{esc(p['name'])}</b> — {p['correct']}✅ {p['wrong']}❌"
                        for m, p in zip("🥇🥈🥉", lead)
                    )
                    or "<i>no answers yet…</i>"
                )
                await safe_edit(
                    s.status_msg,
                    f"<code>{bar(s.index, total)}</code>\n"
                    f"🎯 <b>Q{min(s.index + 1, total)}/{total}</b> • ⏳ {timer}s\n\n{board}",
                )

        asked = s.index
        if not s.stop and asked:
            await finish(ctx, s, asked)
        elif s.stop:
            await ctx.bot.send_message(chat_id, "🛑 Quiz stopped by admin.")
    finally:
        trash = list(s.trash)
        polls = list(s.polls)
        QSM.release(s)
        asyncio.create_task(_delayed_cleanup(ctx, chat_id, trash, polls))


async def _delayed_cleanup(ctx, chat_id: int, mids: List[int], poll_ids: List[str]):
    await asyncio.sleep(20)
    poll_map = ctx.bot_data.get("polls", {})
    for pid in poll_ids:
        poll_map.pop(pid, None)
    await asyncio.sleep(100)
    for mid in mids:
        try:
            await ctx.bot.delete_message(chat_id, mid)
        except TelegramError:
            pass


async def finish(ctx: ContextTypes.DEFAULT_TYPE, s: Session, asked: int):
    quiz = s.quiz
    if not s.players:
        await ctx.bot.send_message(s.chat_id, "📋 Quiz over — nobody played.")
        return

    neg_on = quiz["negative_enabled"]
    neg_val = quiz["neg_value"]
    board: List[dict] = []
    for uid, p in s.players.items():
        attempted = p["correct"] + p["wrong"]
        penalty = round(p["wrong"] * neg_val, 2) if neg_on else 0.0
        board.append(
            {
                "uid": uid,
                "name": p["name"],
                "correct": p["correct"],
                "wrong": p["wrong"],
                "skipped": max(0, asked - attempted),
                "penalty": penalty,
                "score": round(p["correct"] - penalty, 2),
                "accuracy": round(p["correct"] / attempted * 100) if attempted else 0,
            }
        )
    board.sort(key=lambda r: (-r["score"], r["wrong"], -r["accuracy"]))

    msg = await ctx.bot.send_message(
        s.chat_id, "🏆 <b>Tallying scores…</b>", parse_mode=ParseMode.HTML
    )
    
    head = (
        f"🏆 <b>DETAILED LEADERBOARD</b>\n\n"
        f"📜 Quiz Title: {esc(quiz['title'])}\n"
        f"🧩 Total Questions: {asked}\n"
        f"👥 Participants: {len(board)}\n\n"
    )
    
    lines: List[str] = []
    for i, r in enumerate(board[:30]):
        icon = ["🥇", "🥈", "🥉"][i] if i < 3 else f"#{i+1}"
        neg_text = f"{r['penalty']:g}" if neg_on else "0"
        player_stats = (
            f"{icon} <b>{esc(r['name'])}</b>\n"
            f"✅ Correct : {r['correct']} | ❌ Wrong : {r['wrong']}\n"
            f"⏭️ Skipped : {r['skipped']} | 📊 Acc : {r['accuracy']}%\n"
            f"📉 Neg. Marks : -{neg_text}\n"
            f"🎯 Final Score : {r['score']:g}"
        )
        lines.append(player_stats)
        
    reveal = [head + "\n\n".join(lines[: k + 1]) for k in range(min(3, len(lines)))]
    for frame in reveal:
        await safe_edit(msg, frame)
        await asyncio.sleep(0.8)
    await safe_edit(msg, head + "\n\n".join(lines))

    dur = int(time.time() - s.started)
    stats = (
        "📊 <b>RUN REPORT</b>\n"
        f"🪪 <code>{esc(quiz['id'])}</code>\n"
        f"👥 Players: {len(board)}\n"
        f"⏳ Duration: {dur // 60}m {dur % 60}s\n"
        f"📈 Avg accuracy: {sum(r['accuracy'] for r in board) / len(board):.1f}%\n"
        f"⚖️ Avg score: {sum(r['score'] for r in board) / len(board):.2f}\n"
        f"🔥 Best: {board[0]['score']:g} ({esc(board[0]['name'])})\n"
        f"🥶 Lowest: {board[-1]['score']:g}"
    )
    for admin in ADMIN_IDS:
        try:
            await ctx.bot.send_message(admin, stats, parse_mode=ParseMode.HTML)
        except (TelegramError, Forbidden):
            pass

    try:
        await asyncio.to_thread(save_run, quiz, s.chat_id, asked, dur, board)
    except Exception as exc:
        log.error("save_run failed: %s", exc)


async def poll_answer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = update.poll_answer
    if not a or a.user is None:
        return
    s: Optional[Session] = ctx.bot_data.get("polls", {}).get(a.poll_id)
    if s is None or a.poll_id not in s.polls:
        return
    if not a.option_ids:
        return
    s.score(a.user.id, a.user.full_name, a.option_ids[0] == s.polls[a.poll_id])


# ============================ COMMANDS ========================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    
    if ctx.args:
        qid = ctx.args[0].strip()
        quiz = await asyncio.to_thread(load_quiz, qid)
        if quiz:
            text, kb = quiz_card(quiz, ctx.bot.username, uid)
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            return
            
    await update.message.reply_text(home_text(uid), parse_mode=ParseMode.HTML, reply_markup=home_kb(uid))

async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    await update.message.reply_text(home_text(uid), parse_mode=ParseMode.HTML, reply_markup=home_kb(uid))

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    txt = (
        "<b>Commands</b>\n"
        "/create — quiz forge wizard\n"
        "/viewall — quiz library\n"
        "/quiz &lt;id&gt; — launch in <i>this</i> chat\n"
        "/stats — recent runs\n\n"
        "<b>Admin Commands</b>\n"
        "/panel, /pause, /resume, /skip, /stopquiz, /deletequiz\n\n"
        "<b>Question formats</b>\n"
        "<code>1. Capital of France?\nA) Berlin\nB) Paris\nAns: B\nExp: Since 987 AD.</code>\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def cmd_create(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    wiz = Wizard()
    ctx.user_data.clear()
    ctx.user_data["wiz"] = wiz
    msg = await update.message.reply_text("<pre>opening forge…</pre>", parse_mode=ParseMode.HTML)
    await animate(msg, [f"<pre>[{wave(i)}] igniting {SPINNER[i % 4]}</pre>" for i in range(4)], 0.4)
    await safe_edit(msg, wiz.render(), wiz.kb())

async def cmd_viewall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    text, kb = await library_view(0, ctx.bot.username, uid)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def library_view(page: int, bot_username: str, uid: int):
    quizzes = await asyncio.to_thread(list_quizzes)
    if not quizzes:
        return (
            "📚 <b>Library empty.</b>\nRun /create to forge your first quiz.",
            InlineKeyboardMarkup([[btn("⚡ Forge now", "wiz|new"), btn("🏠 Home", "nav|home")]]),
        )
    pages = max(1, (len(quizzes) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = quizzes[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    
    rows = [
        [btn(f"{q['title'][:20]} • {len(q['questions'])}q", f"lib|open|{q['id']}")]
        for q in chunk
    ]
    nav = []
    if page > 0:
        nav.append(btn("◀️", f"lib|page|{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "nav|noop"))
    if page < pages - 1:
        nav.append(btn("▶️", f"lib|page|{page + 1}"))
    rows.append(nav)
    rows.append([btn("⚡ New", "wiz|new"), btn("🏠 Home", "nav|home")])
    
    text = card(
        "📚 QUIZ LIBRARY",
        [("Total", len(quizzes)), ("Page", f"{page + 1}/{pages}"), ("Live/Queue", f"{QSM.describe()} / {len(QQM.queue)} waiting")],
        "▸ <i>Tap a quiz to manage it.</i>",
    )
    return text, InlineKeyboardMarkup(rows)

async def cmd_quiz_here(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    if not ctx.args:
        return await update.message.reply_text("Usage: <code>/quiz QZ001</code>", parse_mode=ParseMode.HTML)
        
    quiz = await asyncio.to_thread(load_quiz, ctx.args[0].strip())
    if not quiz:
        return await update.message.reply_text("❌ No such quiz.")
        
    msg = await update.message.reply_text(f"🔄 Queueing <code>{esc(quiz['id'])}</code>...", parse_mode=ParseMode.HTML)
    added, pos = await QQM.add_to_queue(ctx, quiz, update.effective_chat.id, update.effective_user.id, msg)
    
    if added:
        if pos > 1:
            before_you = pos - 1
            live_msg = "▶️ अभी एक quiz चल रही है। उसके पूरा होते ही आपकी बारी आएगी।"
            q_msg = f"⏳ आपकी Quiz <b>{esc(quiz['title'])}</b> queue में #{pos} पर है।"
            await safe_edit(msg, f"{q_msg}\n👥 आपसे पहले {before_you} quizzes की बारी है।\n{live_msg}")
    else:
        await safe_edit(msg, "⚠️ आपने पहले ही इस quiz को queue में लगा रखा है।")

async def cmd_stopquiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return await update.message.reply_text("❌ Admins only.")
    s = QSM.get()
    if not s: return await update.message.reply_text("Nothing is running.")
    s.stop = True
    await update.message.reply_text("🛑 Stopping — cleaning up in a moment.")

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return await update.message.reply_text("❌ Admins only.")
    s = QSM.get()
    if not s: return await update.message.reply_text("Nothing is running.")
    s.paused = True
    await update.message.reply_text("⏸ Paused. /resume to continue.")

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return await update.message.reply_text("❌ Admins only.")
    s = QSM.get()
    if not s: return await update.message.reply_text("Nothing is running.")
    s.paused = False
    await update.message.reply_text("▶️ Resumed.")

async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return await update.message.reply_text("❌ Admins only.")
    s = QSM.get()
    if not s: return await update.message.reply_text("Nothing is running.")
    s.skip = True
    await update.message.reply_text("⏭ Skipping ahead.")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    await show_stats(update.message.reply_text)

async def show_stats(sender):
    runs = await asyncio.to_thread(recent_runs, 5)
    quizzes = await asyncio.to_thread(list_quizzes)
    total_q = sum(len(q["questions"]) for q in quizzes)
    text = card(
        "📊 STATISTICS",
        [("Quizzes", len(quizzes)), ("Questions", total_q), ("Queue", f"{len(QQM.queue)} waiting")],
    )
    if runs:
        text += "\n<b>Recent runs</b>\n" + "\n".join(
            f"• <code>{esc(r['quiz_id'])}</code> {esc(r['title'])[:18]} — "
            f"{r['players']}👥 {r['asked']}🧩 {r['duration_s']}s"
            for r in runs
        )
    await sender(text, parse_mode=ParseMode.HTML)

async def cmd_deletequiz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id
    if not ctx.args:
        ctx.user_data["await"] = ("delete", None)
        return await update.message.reply_text("☄️ Send the quiz ID to delete.")
        
    qid = ctx.args[0].strip()
    quiz = await asyncio.to_thread(load_quiz, qid)
    if not quiz:
        return await update.message.reply_text("❌ Not found.")
        
    if not is_admin_id(uid) and quiz.get("owner_id") != uid:
        return await update.message.reply_text("❌ You don't have permission to delete this quiz.")
        
    await do_delete(update.message.reply_text, qid)

async def do_delete(sender, qid: str):
    s = QSM.get()
    if s and s.quiz["id"] == qid:
        s.stop = True
    ok = await asyncio.to_thread(delete_quiz, qid)
    await sender(
        f"{'☄️ Deleted' if ok else '❌ Not found'}: <code>{esc(qid)}</code>",
        parse_mode=ParseMode.HTML,
    )

async def cmd_deleteall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user): return await update.message.reply_text("❌ Admins only.")
    await update.message.reply_text(
        "⚠️ <b>Wipe every quiz permanently?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[btn("🚨 YES, wipe", "sys|delall_ok"), btn("✖️ Cancel", "nav|home")]]
        ),
    )

# ============================ TEXT / FILE INPUT ===============================

async def on_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    uid = update.effective_user.id

    txt = (update.message.text or update.message.caption or "").strip()
    wiz: Optional[Wizard] = ctx.user_data.get("wiz")
    pending = ctx.user_data.get("await")

    if pending:
        kind, qid = pending
        ctx.user_data.pop("await", None)
        
        if kind == "delete":
            quiz = await asyncio.to_thread(load_quiz, txt)
            if not quiz: return await update.message.reply_text("❌ Quiz vanished or not found.")
            if not is_admin_id(uid) and quiz.get("owner_id") != uid:
                return await update.message.reply_text("❌ You don't have permission to delete this quiz.")
            return await do_delete(update.message.reply_text, txt)
            
        quiz = await asyncio.to_thread(load_quiz, qid)
        if not quiz:
            return await update.message.reply_text("❌ Quiz vanished.")
            
        if not is_admin_id(uid) and quiz.get("owner_id") != uid:
            return await update.message.reply_text("❌ You don't have permission to modify this quiz.")
            
        if kind == "title" and txt:
            quiz["title"] = txt[:300]
        elif kind == "timer":
            if not txt.isdigit():
                return await update.message.reply_text("❌ Digits only.")
            quiz["timer"] = max(MIN_TIMER, min(int(txt), MAX_TIMER))
            
        await asyncio.to_thread(save_quiz, quiz)
        text, kb = quiz_card(quiz, ctx.bot.username, uid)
        return await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    if not wiz:
        return

    if wiz.step == "id":
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", txt or ""):
            return await update.message.reply_text("❌ Use 1–60 chars: letters, digits, - or _")
        if await asyncio.to_thread(load_quiz, txt):
            return await update.message.reply_text("⚠️ That ID exists. Pick another.")
        wiz.qid, wiz.step = txt, "title"
    elif wiz.step == "title":
        wiz.title, wiz.step = (txt[:300] or "Untitled"), "timer"
    elif wiz.step == "timer":
        if not txt.isdigit():
            return await update.message.reply_text("❌ Digits only, or tap a preset.")
        wiz.timer, wiz.step = max(MIN_TIMER, min(int(txt), MAX_TIMER)), "flags"
    elif wiz.step == "upload":
        return await ingest(update, ctx, wiz, txt)
    else:
        return

    await update.message.reply_text(wiz.render(), parse_mode=ParseMode.HTML, reply_markup=wiz.kb())

async def ingest(update: Update, ctx: ContextTypes.DEFAULT_TYPE, wiz: Wizard, txt: str):
    msg = await update.message.reply_text("<pre>reading payload…</pre>", parse_mode=ParseMode.HTML)
    raw = txt
    
    if update.message.document:
        doc = update.message.document
        name = (doc.file_name or "").lower()
        if name and not name.endswith((".txt", ".json")):
            return await safe_edit(msg, "❌ Only .txt or .json files.")
        if doc.file_size and doc.file_size > 3 * 1024 * 1024:
            return await safe_edit(msg, "❌ File too big (3 MB max).")
        await safe_edit(msg, f"<pre>[{wave(1)}] downloading…</pre>")
        tg = await doc.get_file()
        raw = bytes(await tg.download_as_bytearray()).decode("utf-8", "ignore")

    if not raw.strip():
        return await safe_edit(msg, "❌ Nothing to parse.")

    for i in range(4):
        await safe_edit(msg, f"<pre>[{wave(i + 2)}] parsing {SPINNER[i % 4]}</pre>")
        await asyncio.sleep(0.4)

    parsed = await asyncio.to_thread(parse_questions, raw)
    if not parsed:
        return await safe_edit(msg, "❌ <b>No valid questions found.</b>\nSee /help for the accepted formats.")

    wiz.questions = parsed[:MAX_QUESTIONS]
    wiz.count = len(wiz.questions)
    if wiz.title.lower() in ("", "auto"):
        wiz.title = detect_title(raw)

    quiz = {
        "id": wiz.qid,
        "title": wiz.title,
        "timer": wiz.timer,
        "negative_enabled": wiz.negative,
        "neg_value": wiz.neg_value,
        "shuffle": wiz.shuffle,
        "questions": wiz.questions,
        "owner_id": update.effective_user.id,
    }

    for k in range(1, wiz.count + 1, max(1, wiz.count // 5)):
        await safe_edit(msg, f"<pre>writing to SQLite\n[{bar(k, wiz.count)}]</pre>")
        await asyncio.sleep(0.35)

    try:
        await asyncio.to_thread(save_quiz, quiz)
    except Exception as exc:
        log.exception("save failed: %s", exc)
        return await safe_edit(msg, "❌ Database error while saving.")

    ctx.user_data.pop("wiz", None)
    quiz = await asyncio.to_thread(load_quiz, wiz.qid) or quiz
    text, kb = quiz_card(quiz, ctx.bot.username, update.effective_user.id)
    await safe_edit(msg, "🎊 <b>Forged!</b>")
    await asyncio.sleep(0.5)
    await safe_edit(msg, "🎊 <b>Quiz saved.</b>\n\n" + text, kb)

# ============================ CALLBACKS =======================================

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    
    # GROUP LOCK ENFORCEMENT
    chat = update.effective_chat
    if chat and chat.id != GROUP_ID and chat.type != "private":
        return await q.answer("Bot is locked to a specific group.", show_alert=True)
    
    parts = (q.data or "").split("|")
    ns = parts[0] if parts else ""

    # ================= GUARD CHECKS =================
    if ns == "sys" and parts[1] in ("stop", "delall", "delall_ok"):
        if not is_admin_id(uid):
            return await q.answer("Admins only.", show_alert=True)
            
    if ns == "q" and parts[1] in ("edit", "field", "tog", "cycleneg", "del", "delok"):
        qid = parts[-1] if parts[1] in ("field", "tog", "cycleneg") else "|".join(parts[2:])
        quiz = await asyncio.to_thread(load_quiz, qid)
        if not quiz:
            return await q.answer("❌ Quiz not found.", show_alert=True)
        if not is_admin_id(uid) and quiz.get("owner_id") != uid:
            return await q.answer("❌ You don't have permission to modify this quiz.", show_alert=True)
    # ================================================

    await q.answer()
    wiz: Optional[Wizard] = ctx.user_data.get("wiz")

    if ns == "nav":
        if parts[1] == "home":
            return await safe_edit(q.message, home_text(uid), home_kb(uid))
        if parts[1] == "close":
            try:
                return await q.message.delete()
            except TelegramError:
                return
        return

    if ns == "wiz":
        act = parts[1]
        if act == "new":
            wiz = Wizard()
            ctx.user_data.clear()
            ctx.user_data["wiz"] = wiz
            return await safe_edit(q.message, wiz.render(), wiz.kb())
        if act == "cancel":
            ctx.user_data.pop("wiz", None)
            return await safe_edit(q.message, "✖️ Forge cancelled.", home_kb(uid))
        if not wiz:
            return await safe_edit(q.message, "⌛ Session expired — /create again.", home_kb(uid))
        if act == "autoid":
            wiz.qid = await asyncio.to_thread(next_auto_id)
            wiz.step = "title"
        elif act == "autotitle":
            wiz.title, wiz.step = "auto", "timer"
        elif act == "timer":
            wiz.timer, wiz.step = int(parts[2]), "flags"
        elif act == "tog":
            if parts[2] == "neg":
                wiz.negative = not wiz.negative
            else:
                wiz.shuffle = not wiz.shuffle
        elif act == "cycleneg":
            steps = [0.25, 0.33, 0.5, 1.0]
            wiz.neg_value = steps[(steps.index(wiz.neg_value) + 1) % len(steps)] if wiz.neg_value in steps else 0.25
            wiz.negative = True
        elif act == "goupload":
            wiz.step = "upload"
        return await safe_edit(q.message, wiz.render(), wiz.kb())

    if ns == "lib":
        if parts[1] == "page":
            text, kb = await library_view(int(parts[2]), ctx.bot.username, uid)
            return await safe_edit(q.message, text, kb)
        if parts[1] == "open":
            quiz = await asyncio.to_thread(load_quiz, "|".join(parts[2:]))
            if not quiz:
                return await safe_edit(q.message, "❌ Quiz not found.", home_kb(uid))
            text, kb = quiz_card(quiz, ctx.bot.username, uid)
            return await safe_edit(q.message, text, kb)
        return

    if ns == "q":
        act = parts[1]
        qid = parts[-1] if act in ("field", "tog", "cycleneg") else "|".join(parts[2:])
        quiz = await asyncio.to_thread(load_quiz, qid)
        if not quiz:
            return await safe_edit(q.message, "❌ Quiz not found.", home_kb(uid))

        if act == "start":
            await safe_edit(q.message, f"🔄 Queueing <code>{esc(qid)}</code>...")
            added, pos = await QQM.add_to_queue(ctx, quiz, GROUP_ID, uid, q.message)
            
            if added:
                if pos > 1:
                    before_you = pos - 1
                    live_msg = "▶️ अभी एक quiz चल रही है। उसके पूरा होते ही आपकी बारी आएगी।"
                    q_msg = f"⏳ आपकी Quiz <b>{esc(quiz['title'])}</b> queue में #{pos} पर है।"
                    await safe_edit(q.message, f"{q_msg}\n👥 आपसे पहले {before_you} quizzes की बारी है।\n{live_msg}", InlineKeyboardMarkup([[btn("⬅️ Back", f"lib|open|{qid}")]]))
            else:
                await safe_edit(q.message, "⚠️ आपने पहले ही इस quiz को queue में लगा रखा है।", InlineKeyboardMarkup([[btn("⬅️ Back", f"lib|open|{qid}")]]))
            return
            
        if act == "prev":
            sample = quiz["questions"][:3]
            body = "\n\n".join(
                f"<b>Q{i+1}.</b> {esc(s['q'])}\n" + "\n".join(f"  {'✅' if j == s['ans'] else '▫️'} {esc(o)}" for j, o in enumerate(s["opts"]))
                for i, s in enumerate(sample)
            )
            return await safe_edit(q.message, f"👁 <b>Preview — {esc(quiz['title'])}</b>\n\n{body}", InlineKeyboardMarkup([[btn("⬅️ Back", f"lib|open|{qid}")]]))
            
        if act == "edit":
            return await safe_edit(q.message, f"🪄 Editing <code>{esc(qid)}</code>", edit_kb(quiz))
            
        if act == "field":
            ctx.user_data["await"] = (parts[2], qid)
            label = "title" if parts[2] == "title" else f"timer ({MIN_TIMER}-{MAX_TIMER}s)"
            return await safe_edit(q.message, f"🪄 Send the new {label}.")
            
        if act == "tog":
            key = "negative_enabled" if parts[2] == "neg" else "shuffle"
            quiz[key] = not quiz[key]
            await asyncio.to_thread(save_quiz, quiz)
            return await safe_edit(q.message, f"🪄 Editing <code>{esc(qid)}</code>", edit_kb(quiz))
            
        if act == "cycleneg":
            steps = [0.25, 0.33, 0.5, 1.0]
            cur = quiz["neg_value"]
            quiz["neg_value"] = steps[(steps.index(cur) + 1) % len(steps)] if cur in steps else 0.25
            quiz["negative_enabled"] = True
            await asyncio.to_thread(save_quiz, quiz)
            return await safe_edit(q.message, f"🪄 Editing <code>{esc(qid)}</code>", edit_kb(quiz))
            
        if act == "del":
            return await safe_edit(
                q.message,
                f"☄️ Delete <b>{esc(quiz['title'])}</b> forever?",
                InlineKeyboardMarkup([[btn("🚨 Delete", f"q|delok|{qid}"), btn("✖️ Keep", f"lib|open|{qid}")]])
            )
            
        if act == "delok":
            s = QSM.get()
            if s and s.quiz["id"] == qid:
                s.stop = True
            await asyncio.to_thread(delete_quiz, qid)
            return await safe_edit(q.message, f"☄️ <code>{esc(qid)}</code> deleted.", home_kb(uid))
        return

    if ns == "sys":
        if parts[1] == "stats":
            return await show_stats(lambda t, **kw: safe_edit(q.message, t, InlineKeyboardMarkup([[btn("🏠 Home", "nav|home")]])))
        if parts[1] == "stop":
            s = QSM.get()
            if not s:
                return await safe_edit(q.message, "Nothing is running.", home_kb(uid))
            s.stop = True
            return await safe_edit(q.message, "🛑 Stop signal sent.", home_kb(uid))
        if parts[1] == "delall":
            return await safe_edit(
                q.message,
                "⚠️ <b>Wipe every quiz permanently?</b>",
                InlineKeyboardMarkup([[btn("🚨 YES", "sys|delall_ok"), btn("✖️ Cancel", "nav|home")]])
            )
        if parts[1] == "delall_ok":
            s = QSM.get()
            if s: s.stop = True
            n = await asyncio.to_thread(delete_all_quizzes)
            return await safe_edit(q.message, f"☠️ Wiped {n} quizzes.", home_kb(uid))

async def on_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip()
    if not query: return
    quiz = await asyncio.to_thread(load_quiz, query)
    if not quiz: return await update.inline_query.answer([], cache_time=5)
    
    e = emo(2)
    text = f"🎲 <b>{e[0]} {esc(quiz['title'])} {e[1]}</b>\n🧩 {len(quiz['questions'])} questions • ⏳ {quiz['timer']}s each"
    
    await update.inline_query.answer([
        InlineQueryResultArticle(
            id=quiz["id"][:64],
            title=f"Share “{quiz['title'][:30]}”",
            description=f"{len(quiz['questions'])} questions",
            input_message_content=InputTextMessageContent(text, parse_mode=ParseMode.HTML),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🪐 Add bot & play", url=f"https://t.me/{ctx.bot.username}?startgroup={quiz['id']}")]]),
        )
    ], cache_time=5)

async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled: %s", ctx.error)

# ============================ MAIN ============================================

def main() -> None:
    connect_db()
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    for name, fn in (
        ("start", cmd_start),
        ("panel", cmd_panel),
        ("help", cmd_help),
        ("create", cmd_create),
        ("viewall", cmd_viewall),
        ("quiz", cmd_quiz_here),
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("skip", cmd_skip),
        ("stopquiz", cmd_stopquiz),
        ("stats", cmd_stats),
        ("deletequiz", cmd_deletequiz),
        ("deleteallquizzes", cmd_deleteall),
    ):
        app.add_handler(CommandHandler(name, fn, filters=ALLOWED))

    app.add_handler(MessageHandler(ALLOWED & (filters.Document.ALL | (filters.TEXT & ~filters.COMMAND)), on_input))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PollAnswerHandler(poll_answer))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_error_handler(on_error)

    log.info("🚀 Billy Quiz Forge polling…")
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    finally:
        close_db()

if __name__ == "__main__":
    main()