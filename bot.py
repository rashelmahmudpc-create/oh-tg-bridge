#!/usr/bin/env python3
"""
bot.py — OpenHands Cloud ↔ Telegram bridge
------------------------------------------
টেলিগ্রাম বট থেকে মেসেজ লিখলে সেটা OpenHands Cloud-এর এজেন্টের কাছে যাবে,
আর এজেন্টের লাইভ আউটপুট (কথা, টুল কল, টার্মিনাল আউটপুট, এরর) টেলিগ্রামে চলে আসবে।

কোনো থার্ড-পার্টি লাইব্রেরি লাগবে না — শুধু Python 3.9+।

চালানোর নিয়ম:
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export OPENHANDS_API_KEY="oh-..."
    python3 bot.py

অথবা একই ফোল্ডারে config.env ফাইল রেখে: python3 bot.py
"""

from __future__ import annotations

import base64

try:  # ছবি ছোট করার জন্য — থাকলে base64 অনেক ছোট হয়, এজেন্টের খরচও কমে
    import io as _io
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover
    _io = None
    _PILImage = None
import html as _html_mod
import json
import mimetypes
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from openhands_client import (
    DEFAULT_BASE_URL,
    EXECUTION_DONE,
    OpenHandsClient,
    OpenHandsError,
    conversation_url,
)
from events import (
    TG_LIMIT,
    build_html_file,
    build_live_card,
    extract_text,
    first_line,
    has_code,
    md_to_plain,
    md_to_telegram,
    plain_step,
    render_event,
    split_message,
)

# ====================================================================== #
#  কনফিগারেশন
# ====================================================================== #


_ENV_KEYS_EXPLICIT: set = set(os.environ)   # env/config.env-তে স্পষ্ট দেওয়া কীগুলো


def _load_config_env(path: str = "config.env") -> None:
    """config.env ফাইল থেকে KEY=VALUE লোড (env ভেরিয়েবল আগে অগ্রাধিকার পাবে)।"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
            if key:
                _ENV_KEYS_EXPLICIT.add(key)


_load_config_env()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name) or default))
    except ValueError:
        return default


class Config:
    def __init__(self) -> None:
        self.telegram_token = _env("TELEGRAM_BOT_TOKEN")
        # টেস্টিংয়ের জন্য TELEGRAM_API_BASE দিয়ে অন্য (mock) সার্ভারে পাঠানো যায়
        self.telegram_api_base = _env("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self.oh_api_key = _env("OPENHANDS_API_KEY")
        self.oh_base_url = _env("OPENHANDS_BASE_URL", DEFAULT_BASE_URL)
        self.db_path = _env("DB_PATH", "state.db")

        raw_allowed = _env("ALLOWED_TELEGRAM_IDS")
        self.allowed_ids = set()
        for chunk in raw_allowed.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    self.allowed_ids.add(int(chunk))
                except ValueError:
                    pass

        self.poll_interval = _env_float("POLL_INTERVAL", 1.0)          # ইভেন্ট পোলিং (সেকেন্ড) — দ্রুত লাইভ
        self.idle_poll_interval = _env_float("IDLE_POLL_INTERVAL", 0.8)  # এজেন্ট আইডল থাকলে
        self.start_timeout = _env_float("START_TIMEOUT", 420.0)        # স্যান্ডবক্স রেডি হওয়ার অপেক্ষা
        self.done_grace = _env_float("DONE_GRACE", 2.5)                # শেষ হওয়ার পর আরও কতক্ষণ ইভেন্ট টানা হবে
        self.default_repo = _env("DEFAULT_REPO")                       # যেমন: owner/repo
        self.default_branch = _env("DEFAULT_BRANCH")
        self.default_verbosity = _env("VERBOSITY", "quiet").lower()    # quiet | normal | verbose (ডিফল্ট: শুধু আউটপুট)
        self.use_live_card = _env_bool("LIVE_CARD", False)   # ডিফল্ট বন্ধ: প্রসেসিং দেখানো হবে না
        self.live_card_interval = _env_float("LIVE_CARD_INTERVAL", 2.0)
        self.llm_model = _env("LLM_MODEL")
        self.agent_profile_id = _env("AGENT_PROFILE_ID")
        self.tg_min_interval = _env_float("TG_MIN_INTERVAL", 0.5)      # এক চ্যাটে দুই মেসেজের মধ্যকার ন্যূনতম গ্যাপ
        self.big_text_chars = _env_int("BIG_TEXT_CHARS", 800)  # ৮০০ অক্ষরের উপরে -> প্রিমিয়াম ডক ফাইল
        self.photo_max_px = _env_int("PHOTO_MAX_PX", 1024)      # ছবির বড় বাহু এত পিক্সেলে নামানো হয় (খরচ কমে)
        self.photo_quality = _env_int("PHOTO_QUALITY", 72)      # JPEG কোয়ালিটি
        self.photo_max_b64 = _env_int("PHOTO_MAX_B64", 900_000)  # এর বেশি base64 হলে পাঠানো হবে না
        self.photo_llm = _env("PHOTO_LLM", "openhands/gemini-3-flash")  # ছবির জন্য ভিশন মডেল
        self.brain_max_chars = _env_int("BRAIN_MAX_CHARS", 12000)  # ব্রেইনের সর্বোচ্চ সাইজ (টোকেন খরচের হিসাব)
        self.system_suffix = _env("SYSTEM_SUFFIX")              # এজেন্টের ডিফল্ট নির্দেশনা (পার্সোনা)
        self.obs_truncate = _env_int("OBS_TRUNCATE", 900)
        self.max_startup_events = _env_int("MAX_STARTUP_EVENTS", 60)
        self.restart_keep_window = _env_float("RESTART_KEEP_WINDOW", 30.0)
        self.watch_extend_min = _env_float("WATCH_EXTEND_MIN", 120.0)  # কিছু ডেলিভার না হলে চুপচাপ ততক্ষণ দেখতে থাকি  # রিস্টার্টের পর কত সেকেন্ডের নতুন আউটপুট ধরা হবে
        self.expect_run_window = _env_float("EXPECT_RUN_WINDOW", 150.0)  # নতুন মেসেজের পর এত সেকেন্ড পর্যন্ত "এজেন্ট চালুর" অপেক্ষা
        self.prewarm = _env_bool("PREWARM", True)   # আইডল sandbox আগেভাগে জাগিয়ে রাখা (সাধারণ মেসেজে সেকেন্ডে উত্তর)

        if self.default_verbosity not in ("quiet", "normal", "verbose"):
            self.default_verbosity = "normal"


CFG = Config()

# ====================================================================== #
#  🧠 BRAIN — স্থায়ী নলেজ বেস (BRAIN.md ফাইল থেকে)
#  প্রতিটি কনভারসেশনের প্রথম মেসেজে এটা ঢুকে যায়, তাই নতুন/পুরনো
#  কোনো সেশনেই এজেন্ট ইউজারের কথা ভোলে না।
# ====================================================================== #
BRAIN_TEXT = ""
BRAIN_HASH = ""


def load_brain() -> None:
    global BRAIN_TEXT, BRAIN_HASH
    try:
        with open("BRAIN.md", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except Exception:
        raw = ""
    cap = CFG.brain_max_chars
    if cap <= 0:
        raw = ""
    elif len(raw) > cap:
        raw = raw[:cap] + "\n…(ব্রেইন অনেক বড় — পুরো ফাইল /brain কমান্ডে দেখুন)"
    BRAIN_TEXT = raw
    import hashlib
    BRAIN_HASH = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10] if raw else ""



# ☁️ ক্লাউড-ব্যাকআপ: state.db+BRAIN.md প্রাইভেট ভল্ট থেকে আগে টানে,
# তারপর ব্রেইন লোড — নতুন রিপো/হোস্টেও মেমোরি অটুট
if _env_bool("STATE_SYNC", False):
    import state_sync
    state_sync.configure(_env("GH_PAT"),
                         _env("STATE_GIT_REPO", "sheikhrashel47-stack/agent-hq-vault"),
                         CFG.db_path)
    state_sync.start()

load_brain()


def brain_prefix(text: str) -> str:
    if not BRAIN_TEXT:
        return text
    return ("[PERMANENT KNOWLEDGE BASE — এটা তোমার স্থায়ী মেমোরি। পুরো সেশনে এটা মেনে চলো; "
            "নিজের পুরনো ধারণার সাথে সংঘাত হলে এটাই আগে গণ্য হবে। ইউজারকে ব্রেইনের ব্যাখ্যা দিতে হবে না।]\n"
            + BRAIN_TEXT +
            "\n[END KNOWLEDGE BASE]\n\n👤 ইউজারের আসল মেসেজ:\n" + text)

STOP = threading.Event()
LOG_LOCK = threading.Lock()


def html_escape(t) -> str:
    return _html_mod.escape(t or "", quote=False)


def log(*args) -> None:
    with LOG_LOCK:
        print(f"[{time.strftime('%H:%M:%S')}]", *args, flush=True)


# ====================================================================== #
#  Telegram API (শুধু stdlib)
# ====================================================================== #


class TelegramAPIError(RuntimeError):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        super().__init__(f"Telegram HTTP {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org"):
        self.token = token
        self.api_base = (api_base or "https://api.telegram.org").rstrip("/")
        self.base = f"{self.api_base}/bot{token}"
        self._lock = threading.Lock()
        self._last_sent: dict = {}   # chat_id -> timestamp

    # ------------------------------------------------------------------ #
    def call(self, method: str, payload: dict | None = None, timeout: float = 75.0,
             files: dict | None = None) -> dict:
        payload = payload or {}
        url = f"{self.base}/{method}"

        data = None
        headers = {}
        if files:
            boundary = "----ohbridge%d" % int(time.time() * 1000)
            body = b""
            for key, val in payload.items():
                if val is None:
                    continue
                if not isinstance(val, str):
                    val = json.dumps(val, ensure_ascii=False)
                body += f"--{boundary}\r\n".encode()
                body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                body += val.encode("utf-8") + b"\r\n"
            for key, (filename, content, ctype) in files.items():
                body += f"--{boundary}\r\n".encode()
                body += (
                    f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                ).encode()
                body += f"Content-Type: {ctype}\r\n\r\n".encode()
                body += content + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            data = body
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        else:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(4):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                out = json.loads(raw or "{}")
                if out.get("ok"):
                    return out.get("result") or {}
                raise TelegramAPIError(0, str(out.get("description")))
            except urllib.error.HTTPError as e:
                detail = ""
                retry_after = 0
                try:
                    detail = e.read().decode("utf-8", errors="replace")
                    parsed = json.loads(detail)
                    retry_after = int((parsed.get("parameters") or {}).get("retry_after", 0))
                    detail = parsed.get("description") or detail
                except Exception:
                    pass
                if e.code == 429 or e.code >= 500:
                    wait = retry_after or min(2 ** attempt, 10)
                    log(f"telegram {method} HTTP {e.code}, retry in {wait}s: {detail[:160]}")
                    if STOP.wait(wait):
                        return {}
                    continue
                # চ্যাট ব্লকড / বটকে স্টার্ট করা হয়নি
                raise TelegramAPIError(e.code, str(detail)[:400], retry_after)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                wait = min(2 ** attempt, 8)
                log(f"telegram {method} network error: {e}; retry in {wait}s")
                if STOP.wait(wait):
                    return {}
        return {}

    # ------------------------------------------------------------------ #
    def _throttle(self, chat_id) -> None:
        with self._lock:
            last = self._last_sent.get(chat_id, 0.0)
            wait = CFG.tg_min_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_sent[chat_id] = time.time()

    def send(self, chat_id, text: str, **kw) -> dict:
        if not text:
            return {}
        for chunk in split_message(text, TG_LIMIT):
            self._throttle(chat_id)
            payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                       "disable_web_page_preview": True}
            payload.update({k: v for k, v in kw.items() if v is not None})
            try:
                return self.call("sendMessage", payload, timeout=40)
            except TelegramAPIError as e:
                # HTML পার্স সমস্যা হলে প্লেইন টেক্সটে ফলব্যাক
                if "can't parse" in e.description.lower() or "parse" in e.description.lower():
                    payload.pop("parse_mode", None)
                    payload["text"] = chunk.replace("<b>", "").replace("</b>", "") \
                        .replace("<i>", "").replace("</i>", "").replace("<code>", "") \
                        .replace("</code>", "").replace("<pre>", "").replace("</pre>", "")
                    try:
                        return self.call("sendMessage", payload, timeout=40)
                    except TelegramAPIError as e2:
                        log("send fallback failed:", e2.description[:200])
                        return {}
                log(f"send failed ({chat_id}):", e.description[:200])
                return {}
        return {}

    def edit(self, chat_id, message_id: int, text: str) -> bool:
        if not message_id:
            return False
        self._throttle(chat_id)
        try:
            self.call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text[:TG_LIMIT],
                "parse_mode": "HTML", "disable_web_page_preview": True,
            }, timeout=40)
            return True
        except TelegramAPIError as e:
            if "message is not modified" in e.description.lower():
                return True
            if "can't parse" in e.description.lower():
                try:
                    self.call("editMessageText", {
                        "chat_id": chat_id, "message_id": message_id, "text": text[:TG_LIMIT],
                    }, timeout=40)
                    return True
                except TelegramAPIError:
                    return False
            log(f"edit failed ({chat_id}):", e.description[:200])
            return False

    def typing(self, chat_id) -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=15)
        except TelegramAPIError:
            pass

    def delete(self, chat_id, message_id: int) -> None:
        if not message_id:
            return
        try:
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=20)
        except TelegramAPIError:
            pass

    def send_document(self, chat_id, filename: str, content: bytes, caption: str = "") -> dict:
        self._throttle(chat_id)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            return self.call("sendDocument", {
                "chat_id": chat_id, "caption": caption[:1000],
            }, files={"document": (filename, content, ctype)}, timeout=90)
        except TelegramAPIError as e:
            log("send_document failed:", e.description[:200])
            return {}

    def get_file_url(self, file_id: str) -> str:
        res = self.call("getFile", {"file_id": file_id}, timeout=30)
        path = (res or {}).get("file_path") or ""
        url = f"https://api.telegram.org/file/bot{self.token}/{path}" if path else ""
        return url

    def download(self, file_id: str, max_bytes: int = 4 * 1024 * 1024) -> tuple:
        url = self.get_file_url(file_id)
        if not url:
            return ("", b"")
        if url.startswith("https://api.telegram.org/file/"):
            url = self.api_base + url[len("https://api.telegram.org"):]
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read(max_bytes)
        name = url.rsplit("/", 1)[-1]
        return (name, data)


TG = Telegram(CFG.telegram_token, CFG.telegram_api_base) if CFG.telegram_token else None


# ====================================================================== #
#  স্টেট (SQLite)
# ====================================================================== #

DB_LOCK = threading.Lock()
_conn: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with DB_LOCK:
            if _conn is None:
                _conn = sqlite3.connect(CFG.db_path, check_same_thread=False)
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("""
                    CREATE TABLE IF NOT EXISTS kv (
                        chat_id INTEGER PRIMARY KEY,
                        conversation_id TEXT,
                        title TEXT,
                        repo TEXT,
                        branch TEXT,
                        verbosity TEXT,
                        live_card INTEGER,
                        updated_at REAL
                    )
                """)
                _conn.execute("CREATE TABLE IF NOT EXISTS seen (chat_id INTEGER, ev_id TEXT, ts REAL)")
                _conn.execute("CREATE TABLE IF NOT EXISTS misc (key TEXT PRIMARY KEY, val TEXT)")
                _conn.execute("CREATE INDEX IF NOT EXISTS idx_seen ON seen(chat_id, ev_id)")
                _conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER, due REAL, kind TEXT, payload TEXT, done INTEGER DEFAULT 0)""")
                _conn.commit()
    return _conn


DEFAULT_STATE = {
    "conversation_id": None,
    "title": None,
    "repo": CFG.default_repo or None,
    "branch": CFG.default_branch or None,
    "verbosity": CFG.default_verbosity,
    "live_card": 1 if CFG.use_live_card else 0,
}


def get_state(chat_id: int) -> dict:
    row = db().execute("SELECT conversation_id,title,repo,branch,verbosity,live_card FROM kv WHERE chat_id=?",
                       (chat_id,)).fetchone()
    st = dict(DEFAULT_STATE)
    if row:
        st.update({
            "conversation_id": row[0], "title": row[1], "repo": row[2],
            "branch": row[3], "verbosity": row[4] or CFG.default_verbosity,
            "live_card": CFG.use_live_card if row[5] is None else int(row[5]),
        })
    eng = misc_get(f"eng:{chat_id}")
    if eng:
        st["engine_profile"] = eng
    return st


def save_state(chat_id: int, st: dict) -> None:
    with DB_LOCK:
        db().execute(
            """INSERT INTO kv (chat_id,conversation_id,title,repo,branch,verbosity,live_card,updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 conversation_id=excluded.conversation_id, title=excluded.title,
                 repo=excluded.repo, branch=excluded.branch, verbosity=excluded.verbosity,
                 live_card=excluded.live_card, updated_at=excluded.updated_at""",
            (chat_id, st.get("conversation_id"), st.get("title"), st.get("repo"), st.get("branch"),
             st.get("verbosity"), int(st.get("live_card", 1)), time.time()),
        )
        db().commit()
    persist_state_soon()


_PERSIST_LOCK = threading.Lock()
_last_persist = 0.0


def persist_state_now() -> None:
    """state.db গিট রিপোতে পুশ — রান ক্যানসেল/রিস্টার্ট হলেও সেশন হারাবে না।
    এটা না থাকলে নতুন রনে বট পুরনো কনভারসেশন ভুলে গিয়ে নতুন সেশন শুরু করত।"""
    global _last_persist
    pat = _env("GH_PAT")
    if not pat:
        return
    import subprocess
    with _PERSIST_LOCK:
        try:
            try:
                db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db().commit()
            except Exception:
                pass
            subprocess.run(["git", "add", "state.db"], capture_output=True, timeout=60)
            if subprocess.run(["git", "diff", "--cached", "--quiet"],
                              capture_output=True).returncode == 0:
                _last_persist = time.time()
                return
            subprocess.run(["git", "-c", "user.name=oh-bot", "-c", "user.email=bot@local",
                            "commit", "-q", "-m", "chore: save bot state [skip ci]"],
                           capture_output=True, timeout=60)
            owner = _env("PAGES_OWNER", "sheikhrashel47-stack")
            subprocess.run(["git", "remote", "set-url", "origin",
                            f"https://x-access-token:{pat}@github.com/{owner}/oh-telegram-bot.git"],
                           capture_output=True)
            p = subprocess.run(["git", "push", "-q", "origin", "HEAD:main"],
                               capture_output=True, timeout=120)
            if p.returncode != 0:
                subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"],
                               capture_output=True, timeout=120)
                subprocess.run(["git", "push", "-q", "origin", "HEAD:main"],
                               capture_output=True, timeout=120)
            subprocess.run(["git", "remote", "set-url", "origin",
                            f"https://github.com/{owner}/oh-telegram-bot.git"],
                           capture_output=True)
            _last_persist = time.time()
            log("state persisted to git")
        except Exception as e:
            log("persist state failed:", str(e)[:150])


def persist_state_soon(delay: float = 15.0) -> None:
    global _last_persist
    if not _env("GH_PAT"):
        return
    if time.time() - _last_persist < 60:
        return
    _last_persist = time.time()   # ডিবাউন্স: পরপর অনেক কল হলে একবারই পুশ

    def _run():
        time.sleep(delay)
        persist_state_now()
    threading.Thread(target=_run, daemon=True).start()


def mark_seen(chat_id: int, ev_ids: list) -> None:
    if not ev_ids:
        return
    now = time.time()
    with DB_LOCK:
        db().executemany("INSERT OR IGNORE INTO seen (chat_id,ev_id,ts) VALUES (?,?,?)",
                         [(chat_id, str(e), now) for e in ev_ids])
        db().execute("DELETE FROM seen WHERE ts < ?", (now - 7 * 24 * 3600,))
        db().commit()
    if time.time() - _last_persist > 240:
        persist_state_soon(30)


def seen_ids(chat_id: int, limit: int = 4000) -> set:
    rows = db().execute(
        "SELECT ev_id FROM seen WHERE chat_id=? ORDER BY ts DESC LIMIT ?", (chat_id, limit)
    ).fetchall()
    return {r[0] for r in rows}


def misc_get(key: str) -> str | None:
    r = db().execute("SELECT val FROM misc WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def misc_set(key: str, val: str) -> None:
    with DB_LOCK:
        db().execute("INSERT OR REPLACE INTO misc (key,val) VALUES (?,?)", (key, val))
        db().commit()


# ====================================================================== #
#  OpenHands ক্লায়েন্ট
# ====================================================================== #

OH: OpenHandsClient | None = None
if CFG.oh_api_key:
    OH = OpenHandsClient(CFG.oh_api_key, base_url=CFG.oh_base_url)


def oh() -> OpenHandsClient:
    if OH is None:
        raise OpenHandsError("OPENHANDS_API_KEY সেট করা নেই — config.env বা environment variable এ দিন।")
    return OH


# ====================================================================== #
#  লাইভ ইভেন্ট ওয়াচার
# ====================================================================== #

WATCHERS: dict = {}      # chat_id -> Watcher
WATCHER_LOCK = threading.Lock()
DELIVERED_SIGS: dict = {}  # chat_id -> set(md5) ডেলিভারি-দেওয়া এজেন্ট টেক্সট (ডুপ আটকাতে)

_FILE_URL_RE = __import__("re").compile(
    r"https?://[^\s)\"'`]+?\.(?:pdf|zip|docx?|xlsx?|pptx?|csv|txt|png|jpe?g)(?:\?[^\s)\"'`]*)?",
    __import__("re").I)


class Watcher(threading.Thread):
    """
    একটি কনভারসেশনের ইভেন্ট পোলিং করে টেলিগ্রামে ফরোয়ার্ড করে।
    প্রতিটি চ্যাটের জন্য একটা থ্রেড।
    """

    def __init__(self, chat_id: int, conversation_id: str, state: dict, fresh: bool = False):
        super().__init__(daemon=True, name=f"watch-{chat_id}")
        self.chat_id = chat_id
        self.cid = conversation_id
        self.state = dict(state)
        self.seen: set = set()
        self.stop_evt = threading.Event()
        # fresh=True  -> এইমাত্র তৈরি হওয়া কনভারসেশন, সব ইভেন্ট দেখানো হবে
        # fresh=False -> বট রিস্টার্ট/পুরনো কনভারসেশন, আগের ইভেন্ট স্কিপ করা হবে
        self.fresh = fresh
        self.seed_events = _env_bool("SEND_HISTORY_ON_START", False)
        # নতুন মেসেজ পাঠানোর পর শুরু হওয়া ওয়াচার: এজেন্ট সত্যিই চালু হয়েছে
        # কিনা না দেখা পর্যন্ত "শেষ" বলা হবে না (পুরনো finished স্ট্যাটাসের ফাঁদ)
        self.start_ts = time.time()
        self.saw_running = False
        self._prewarm_done = False
        self.delivered_runs = 0
        self.last_err = ""
        # ডেলিভারি-গেট: এই রানে এজেন্ট আসল কাজ (টুল কল) করেছে কিনা
        self.run_actions = 0
        self.nudges = 0
        self._stuck_nudged = False
        self._last_new_ev = time.time()
        self.run_start_ts = 0.0
        self.last_agent_kind = ""
        self.run_delivered = 0
        self.docs_sent = 0
        self.capture_mode = ""
        self.capture_deadline = 0.0
        self.swallowed: set = set()
        self.brain_poll_ts = 0.0
        self.brain_polled = False
        self.finished_once = False
        self.prev_execution = None
        self.last_agent_text = ""
        # লাইভ কার্ড
        self.card_msg_id = 0
        self.card_steps: list = []
        self.card_agent_text = ""
        self.card_dirty = False
        self.last_card_update = 0.0
        self.last_status_text = ""
        self.last_typing = 0.0
        self.error_count = 0

    # ------------------------- helpers ------------------------------- #
    def send(self, text: str, **kw) -> None:
        if not text:
            return
        try:
            TG.send(self.chat_id, text, **kw)
        except Exception as e:
            log("watcher send error:", e)

    def close_card(self, final_status: str = "") -> None:
        if self.card_msg_id:
            text = build_live_card(self.card_steps, self.card_agent_text,
                                   final_status or self.last_status_text)
            label = "✅ <b>Done</b>" if "finished" in (final_status or "") else "⏹ <b>Stopped</b>"
            text = text.replace("⚡ <b>Working…</b>", label, 1)
            TG.edit(self.chat_id, self.card_msg_id, text)
            self.card_msg_id = 0
            self.card_steps = []
            self.card_agent_text = ""

    # ------------------------- main loop ----------------------------- #
    def run(self) -> None:
        log(f"[chat {self.chat_id}] watcher start for {self.cid}")
        self.seen = seen_ids(self.chat_id)
        _wm = misc_get(f"evwm:{self.cid}")
        if _wm:
            self.wm = float(_wm)          # রিস্টার্ট: সেভ করা ওয়াটারমার্কের পরের ইভেন্টই শুধু
        elif self.fresh:
            self.wm = self.start_ts - 120 # নতুন মেসেজ: এর আগের ইতিহাস স্প্যাম নয়
        else:
            self.wm = time.time() - 90    # পুরনো সেশন ওয়াচ: সম্প্রতিরটা বাদে সব স্কিপ
        first_loop = True
        terminal_since: float | None = None

        while not self.stop_evt.is_set() and not STOP.is_set():
            try:
                conv = oh().get_conversation(self.cid)
            except OpenHandsError as e:
                self.error_count += 1
                if self.error_count in (3, 10):
                    self.send(f"⚠️ OpenHands API সমস্যা: {str(e)[:300]}")
                if self.error_count > 25:
                    self.send("🛑 ওয়াচার বন্ধ করা হলো (API বারবার ব্যর্থ)। /watch দিয়ে আবার চালু করুন।")
                    break
                self.stop_evt.wait(5)
                continue

            self.error_count = 0
            conv = conv or {}
            sandbox = conv.get("sandbox_status") or "?"
            execution = (conv.get("execution_status") or "?")
            self.last_status_text = f"{sandbox}/{execution}"
            if execution == "running":
                self.saw_running = True
                self._prewarm_done = False
            # প্রি-ওয়ার্ম: কাজ শেষে sandbox ঘুমালে আগেভাগে জাগিয়ে রাখি,
            # যাতে পরের সাধারণ মেসেজে রিজিউম-দেরি (১৫-৩০ সে) না লাগে
            elif (CFG.prewarm and sandbox == "PAUSED" and execution in EXECUTION_DONE
                    and not self._prewarm_done):
                self._prewarm_done = True
                sid = conv.get("sandbox_id")
                if sid:
                    threading.Thread(target=self._prewarm, args=(sid,), daemon=True).start()
            if self.prev_execution in EXECUTION_DONE and execution == "running":
                # নতুন রান শুরু -> কাজ/নাজ গণনা রিসেট
                self.run_actions = 0
                self.nudges = 0
                self.run_start_ts = time.time()
                self._stuck_nudged = False
                self._last_new_ev = time.time()
                self.last_agent_kind = ""
                self.run_delivered = 0
            self.prev_execution = execution

            # ---- ইভেন্ট টানা ----
            try:
                events = list(oh().iter_new_events(self.cid, limit=100))
            except OpenHandsError as e:
                log("events fetch error:", str(e)[:200])
                events = []
                self.stop_evt.wait(3)
                continue

            new = []
            for ev in events:
                eid = str(ev.get("id") or "")
                if not eid:
                    continue
                if eid in self.seen:
                    continue
                new.append(ev)
                self.seen.add(eid)

            if self.wm:
                def _ts_ok(ev):
                    et = _parse_iso(ev.get("timestamp"))
                    return et is None or et > self.wm
                new = [ev for ev in new if _ts_ok(ev)]
            for ev in new:
                if (ev.get("kind") or "").endswith("ErrorEvent"):
                    self.last_err = str(ev.get("detail") or ev.get("code") or "")[:220]
            if new:
                self._last_new_ev = time.time()
                mark_seen(self.chat_id, [str(e.get("id")) for e in new])
            for ev in events:
                et = _parse_iso(ev.get("timestamp"))
                if et and et > self.wm:
                    self.wm = et
            misc_set(f"evwm:{self.cid}", repr(self.wm))

            first_loop = False

            self._render(new, execution, sandbox)
            self._update_live_card(execution)

            # ---- হ্যাং-ডিটেক্ট: running কিন্তু ৩ মিনিট ধরে কোনো নতুন ইভেন্ট নেই ----
            if (execution == "running" and self.saw_running and not self._stuck_nudged
                    and time.time() - getattr(self, "_last_new_ev", 0) > 90):
                self._stuck_nudged = True
                log("run silent >90s -> resume sandbox + nudge agent")
                try:
                    conv0 = oh().get_conversation(self.cid) or {}
                    sid0 = conv0.get("sandbox_id")
                    if sid0 and (conv0.get("sandbox_status") or "") == "PAUSED":
                        oh().resume_sandbox(sid0)
                except Exception as e:
                    log("stuck resume failed:", str(e)[:120])
                try:
                    oh().send_message(self.cid, _NUDGE_TEXT, run=True)
                except OpenHandsError as e:
                    log("stuck nudge failed:", str(e)[:150])

            # ---- টাইপিং ইন্ডিকেটর ----
            if execution == "running" and time.time() - self.last_typing > 4.5:
                self.last_typing = time.time()
                try:
                    TG.typing(self.chat_id)
                except Exception:
                    pass

            # ---- শেষ হওয়া ডিটেক্ট ----
            if execution in EXECUTION_DONE or sandbox in ("PAUSED", "MISSING", "ERROR"):
                # নতুন মেসেজ পাঠানো হয়েছে কিন্তু এজেন্ট এখনো চালুই হয়নি
                # (পুরনো finished স্ট্যাটাস) -> আগেভাগে "শেষ" বলা যাবে না
                if (self.fresh and not self.saw_running
                        and time.time() - self.start_ts < CFG.expect_run_window):
                    self.stop_evt.wait(CFG.poll_interval)
                    continue
                if terminal_since is None:
                    terminal_since = time.time()
                elif time.time() - terminal_since >= CFG.done_grace:
                    if self.capture_mode:
                        if time.time() < self.capture_deadline:
                            terminal_since = None
                            self.stop_evt.wait(3.0)
                            continue
                        self.capture_mode = ""
                    if not self._finish(conv, execution, sandbox):
                        # এজেন্টকে কাজ করিয়ে আনা হচ্ছে -> শেষ বলা হয়নি
                        terminal_since = None
                        if execution in EXECUTION_DONE:
                            self.stop_evt.wait(3.0)   # নীরব এক্সটেনশনে ধীর পোলিং
                        continue
                    if self._maybe_brain_poll():
                        terminal_since = None
                        self.stop_evt.wait(2.0)
                        continue
                    break
                time.sleep(1.0)
                continue

            terminal_since = None
            wait = CFG.poll_interval if execution == "running" else CFG.idle_poll_interval
            self.stop_evt.wait(wait)

        with WATCHER_LOCK:
            if WATCHERS.get(self.chat_id) is self:
                WATCHERS.pop(self.chat_id, None)
        log(f"[chat {self.chat_id}] watcher stopped")

    # ------------------------- রেন্ডার ------------------------------- #
    def deliver_agent_text(self, text: str) -> None:
        """
        এজেন্টের আসল উত্তর পাঠানোর একমাত্র জায়গা:
          - ছোট হলে সরাসরি মেসেজ (markdown -> Telegram HTML, raw ** দেখাবে না)
          - কোড থাকলে সুন্দর একটা .html ফাইল
          - খুব লম্বা হলে প্রিমিয়াম স্টাইল .html ডক (বোল্ড হেডিং, কার্ড, বড় ফন্ট —
            plain .txt আর নয়)
        একই উত্তর দুবার এলে দ্বিতীয়বার পাঠানো হয় না (ডুপ্লিকেট বন্ধ)।
        """
        text = (text or "").strip()
        if not text:
            return
        import hashlib
        sig = hashlib.md5(text.encode("utf-8")).hexdigest()
        if sig == getattr(self, "_last_delivered_sig", ""):
            log("duplicate agent message skipped")
            return
        self._last_delivered_sig = sig

        # ক্রস-ওয়াচার ডুপ: একই টেক্সট আগে ডেলিভারি হয়ে থাকলে আবার নয়
        sigs = DELIVERED_SIGS.setdefault(self.chat_id, set())
        if sig in sigs:
            log("duplicate delivery (across watchers) skipped -> re-answer nudge")
            if not getattr(self, "_dup_nudged", False):
                self._dup_nudged = True
                threading.Thread(target=self._ask_reanswer, daemon=True).start()
            return
        sigs.add(sig)
        self.delivered_runs = getattr(self, "delivered_runs", 0) + 1
        if len(sigs) > 60:
            DELIVERED_SIGS[self.chat_id] = {sig}

        # লিংক-গার্ড: এজেন্টের দেওয়া work-লিংক বাইরে থেকে মরা হলে
        # নিজে থেকেই এজেন্টকে সারানোর নির্দেশ পাঠাই (যাতে ১ বারেই খোলে)
        urls = _WORK_URL_RE.findall(text)
        if urls and not getattr(self, "_repair_sent", False):
            if not any(link_alive(u) for u in urls[:2]):
                self._repair_sent = True
                threading.Thread(target=self._ask_link_repair,
                                 args=(urls[0],), daemon=True).start()

        # এজেন্ট ফাইলের সরাসরি লিংক দিলে -> ফাইলটা নামিয়ে Telegram ডকুমেন্ট করি
        doc_bytes = None
        doc_fname = None
        fu = _FILE_URL_RE.search(text)
        if fu:
            try:
                req = urllib.request.Request(fu.group(0), headers={"User-Agent": "oh-tg-bot"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    doc_bytes = r.read(25_000_000)
                doc_fname = fu.group(0).rsplit("/", 1)[-1].split("?")[0] or "file.bin"
            except Exception as e:
                log("file-link fetch failed:", str(e)[:120])
                doc_bytes = None

        clean = md_to_plain(text)
        title = (self.state.get("title") or "এজেন্টের উত্তর")[:60]

        if doc_bytes:
            try:
                TG.send_document(self.chat_id, doc_fname, doc_bytes, caption=f"📎 {doc_fname}")
                self.docs_sent += 1
            except Exception as e:
                log("doc send failed:", str(e)[:120])

        if has_code(text) or len(clean) > CFG.big_text_chars:
            self.out_n = getattr(self, "out_n", 0) + 1
            data = build_html_file(text, title)
            name = f"output-{self.out_n}.html"
            TG.send_document(self.chat_id, name, data, caption=f"🤖 {first_line(clean)}")
            self.docs_sent += 1
            return
        self.send(md_to_telegram(text))

    def _ask_link_repair(self, url: str) -> None:
        """মরা লিংক পাঠানো হয়েছে -> এজেন্টকে পোর্ট ১২০০০-এ সার্ভার সারাতে বলি।"""
        msg = ("The link you just shared is DEAD from outside (404/timeout): " + url + "\n"
               "Fix it now, exactly these steps: cd into the website directory; make sure "
               "index.html is at that root; start a background server on port 12000 bound to "
               "0.0.0.0 (nohup python3 -m http.server 12000 --bind 0.0.0.0 >/dev/null 2>&1 &); "
               "then verify the PUBLIC work-1 URL with curl -s -o /dev/null -w '%{http_code}' "
               "until it prints 200. Only then reply with the working link in ONE line.")
        try:
            oh().send_message(self.cid, msg, run=True)
            log("link-repair instruction sent")
        except Exception as e:
            log("link repair send failed:", str(e)[:150])

    def _prewarm(self, sid: str) -> None:
        try:
            oh().resume_sandbox(sid)
            log("sandbox pre-warmed (resumed while idle)")
        except Exception as e:
            log("prewarm failed:", str(e)[:120])

    def _ask_reanswer(self) -> None:
        """এজেন্ট আগের মেসেজই হুবহু আবার পাঠালে -> নতুন প্রশ্নের উত্তর দিতে বলি।"""
        msg = ("STOP: your previous message was delivered to the user AGAIN by mistake — it is "
               "NOT an answer to the user's newest message. Read the user's LAST message in this "
               "conversation and answer THAT specifically with fresh, relevant content. "
               "Never repeat an earlier message of yours.")
        try:
            oh().send_message(self.cid, msg, run=True)
            log("re-answer nudge sent")
        except Exception as e:
            log("reanswer nudge failed:", str(e)[:120])

    def _render(self, new_events: list, execution: str, sandbox: str) -> None:
        if not new_events:
            return
        verbosity = self.state.get("verbosity") or "quiet"
        for ev in new_events:
            kind = ev.get("kind") or ""
            source = (ev.get("source") or "").lower()
            if kind == "ActionEvent":
                self.run_actions += 1
                self.last_agent_kind = "ActionEvent"

            # এজেন্টের উত্তর = একমাত্র আসল আউটপুট
            if kind == "MessageEvent" and source == "agent":
                txt = extract_text(ev.get("llm_message")) or extract_text(ev.get("extended_content"))
                if self.capture_mode and txt.strip():
                    mode, self.capture_mode = self.capture_mode, ""
                    self.swallowed.add(ev.get("id") or "")
                    threading.Thread(target=self._apply_capture, args=(mode, txt.strip()),
                                     daemon=True).start()
                    continue   # মেমোরি-উত্তর: ইউজারকে দেখানো হবে না
                if txt.strip():
                    self.last_agent_text = txt.strip()
                    self.last_agent_kind = "MessageEvent"
                    self.run_delivered += 1
                self.deliver_agent_text(txt)
                if self.state.get("live_card"):
                    self.card_agent_text = txt.strip()
                    self.card_dirty = True
                continue

            # প্রসেসিং/টুল ধাপ: ডিফল্ট(quiet)-এ দেখানো হয় না
            if verbosity == "quiet":
                continue

            step = plain_step(ev)
            if step and self.state.get("live_card"):
                self.card_steps.append(step)
                self.card_steps = self.card_steps[-40:]
                self.card_dirty = True
            if self.state.get("live_card") and kind in ("ActionEvent", "ObservationEvent") \
                    and verbosity != "verbose":
                continue
            for msg in render_event(ev, verbosity):
                self.send(msg)

    def _update_live_card(self, execution: str) -> None:
        if not self.state.get("live_card"):
            return
        if not self.card_dirty:
            return
        now = time.time()
        if now - self.last_card_update < CFG.live_card_interval:
            return
        self.last_card_update = now
        self.card_dirty = False
        text = build_live_card(self.card_steps, self.card_agent_text, self.last_status_text)
        if not self.card_msg_id:
            res = TG.send(self.chat_id, text)
            self.card_msg_id = int((res or {}).get("message_id") or 0)
        else:
            if not TG.edit(self.chat_id, self.card_msg_id, text):
                self.card_msg_id = 0

    def _finish(self, conv: dict, execution: str, sandbox: str) -> bool:
        """True = সত্যিই শেষ (ফিনিশ লাইন পাঠানো হয়েছে);
        False = এজেন্ট শুধু 'করছি:' বলে থেমে ছিল, কাজ করিয়ে আনছি -> শেষ বলা হয়নি।"""
        self.close_card(f"{sandbox}/{execution}")
        self.card_steps = []
        self.card_agent_text = ""

        # 💀 ডেড-রান সেল্ফ-হিল: টুল-কল চলতে চলতে স্যান্ডবক্স ঘুমিয়ে রান মরে
        # গেলে (উত্তর আসেনি) -> নিজে জাগিয়ে চালিয়ে যেতে বলি; শেষ বলি না
        if (self.nudges < 2 and self.run_actions > 0 and self.run_delivered == 0
                and self.last_agent_kind == "ActionEvent"
                and execution not in ("error", "stuck")):
            self.nudges += 1
            log("dead-run detected -> resume + continue-nudge")
            sid = conv.get("sandbox_id")
            if sid:
                try:
                    oh().resume_sandbox(sid)
                except Exception as e:
                    log("dead-run resume failed:", str(e)[:120])
            try:
                oh().send_message(self.cid, _CONTINUE_TEXT, run=True)
            except OpenHandsError as e:
                log("continue-nudge failed:", str(e)[:150])
            self.saw_running = False
            self.run_actions = 0
            self._stuck_nudged = False
            self._last_new_ev = time.time()
            return False

        if (self.nudges < 2 and self.run_actions == 0
                and _is_intent_only(self.last_agent_text)
                and execution not in ("error", "stuck")):
            self.nudges += 1
            log(f"intent-only finish detected -> nudge {self.nudges}")
            try:
                oh().send_message(self.cid, _NUDGE_TEXT, run=True)
            except Exception as e:
                log("nudge failed:", str(e)[:120])
                self._send_finish_line(conv, execution, sandbox)
                return True
            self.saw_running = False
            self.run_actions = 0
            return False

        # 🕯 নীরব-এক্সটেনশন: এই সাইকেলে এখনো কিছুই ডেলিভার হয়নি অথচ execution
        # DONE দেখাচ্ছে (স্যান্ডবক্সের ঝাপসা স্ট্যাটাস) -> আগেভাগে "শেষ" নয়;
        # WATCH_EXTEND_MIN মিনিট চুপচাপ দেখতে থাকি, আউটপুট এলে সাথে সাথে পাঠাব
        if (self.run_delivered == 0 and self.docs_sent == 0
                and execution not in ("error", "stuck")
                and time.time() - self.start_ts < CFG.watch_extend_min * 60):
            log("nothing delivered yet -> silent extension (no false finish)")
            return False

        # 🛡 ডেলিভারি সেফটি-নেট: স্ট্রিমিং যেকোনো কারণে মিস করলেও শেষ-লাইনের
        # আগে এই রানের সব এজেন্ট-উত্তর পৌঁছে দিই — উত্তর কখনো হাওয়া যাবে না
        try:
            from datetime import datetime as _dt
            cutoff = _dt.utcfromtimestamp(
                getattr(self, "run_start_ts", 0) or self.start_ts)
            for ev in oh().iter_new_events(self.cid, limit=100):
                if (ev.get("kind") or "") != "MessageEvent":
                    continue
                if (ev.get("source") or "") != "agent":
                    continue
                et = _parse_iso(ev.get("timestamp"))
                if not et or et < cutoff:
                    continue
                if (ev.get("id") or "") in self.swallowed:
                    continue
                if self.brain_poll_ts and et >= self.brain_poll_ts - 1:
                    continue   # মেমোরি-উত্তর: ইউজারকে দেখানো নিষেধ
                txt = extract_text(ev.get("llm_message")) \
                    or extract_text(ev.get("extended_content"))
                if txt and txt.strip():
                    self.deliver_agent_text(txt)
        except Exception as e:
            log("finish safety-net error:", str(e)[:150])

        self._send_finish_line(conv, execution, sandbox)
        return True

    def _maybe_brain_poll(self) -> bool:
        """রান শেষে এজেন্টের কাছে নতুন মেমোরি চাও (উত্তর ইউজারকে দেখানো হয় না)।"""
        if self.capture_mode:
            return time.time() < self.capture_deadline
        if self.brain_polled or self.run_actions < 1:
            return False
        self.brain_polled = True
        self.brain_poll_ts = time.time()
        self.capture_mode = "brain"
        self.capture_deadline = time.time() + 150
        try:
            oh().send_message(self.cid, _BRAIN_POLL_TEXT, run=True)
            log("brain poll sent (auto-brain)")
        except Exception as e:
            log("brain poll failed:", str(e)[:120])
            self.capture_mode = ""
            return False
        return True

    def _apply_capture(self, mode: str, txt: str) -> None:
        """এজেন্টের মেমোরি/নিয়ম-উত্তর BRAIN.md-তে লেখো (ডুপ্লিকেট বাদ)।"""
        try:
            low = txt.strip().lower()
            if low in ("none", "none.", "নাই", "নেই", "কিছু না"):
                return
            lines = [l.strip(" -•\t") for l in txt.splitlines() if l.strip(" -•\t")]
            if mode == "rule":
                lines = [lines[0][:220]] if lines else []
                header = "## ভুল-থেকে-শেখা নিয়ম"
            else:
                lines = [l[:220] for l in lines][:4]
                header = "## অটো-মেমোরি"
            if not lines:
                return
            path = "BRAIN.md"
            try:
                cur = io.open(path, encoding="utf-8").read()
            except Exception:
                cur = ""
            norm = {re.sub(r"\s+", " ", l).strip().lower() for l in cur.splitlines()}
            add = [l for l in lines if re.sub(r"\s+", " ", l).strip().lower() not in norm]
            if not add:
                log("brain capture: all duplicates, skipped")
                return
            if header not in cur:
                cur = cur.rstrip() + "\n\n" + header + "\n"
            import datetime as _dt
            date = _dt.date.today().isoformat()
            block = "\n".join(f"- {l} ({date})" for l in add)
            idx = cur.index(header) + len(header)
            nl = cur.find("\n", idx)
            cur = cur[:nl + 1] + block + "\n" + cur[nl + 1:]
            io.open(path, "w", encoding="utf-8").write(cur)
            load_brain()
            log(f"brain updated ({mode}): +{len(add)} line")
        except Exception:
            log("capture apply crash:", traceback.format_exc()[:300])

    def _send_finish_line(self, conv: dict, execution: str, sandbox: str) -> None:
        if self.finished_once:
            return
        self.finished_once = True
        # ইঞ্জিন/এজেন্ট এরর + কিছু ডেলিভার হয়নি -> শেষ লাইন নয়, ব্যাকআপে স্বয়ংক্রিয় সুইচ
        if (execution == "error" or self.last_err) and self.delivered_runs == 0 and self.run_actions == 0:
            chain = ["gemini-free", "groq-free", "openrouter-free"]
            st = self.state
            cur = st.get("engine_profile") or CFG.agent_profile_id or ""
            nxt = chain[chain.index(cur) + 1] if cur in chain and chain.index(cur) + 1 < len(chain) else None
            task = (st.get("last_task") or "").strip()
            if nxt and task and st.get("fb_task") != task:
                st["engine_profile"] = nxt
                st["fb_task"] = task
                save_state(self.chat_id, st)
                self.send(f"🛑 ইঞ্জিন এরর: {(self.last_err or execution)[:140]}\n"
                          f"🔁 ব্যাকআপ ইঞ্জিন ({nxt})-এ সুইচ করে আপনার শেষ 요청টা আবার চালাচ্ছি…")
                threading.Thread(target=_do_start_conversation,
                                 args=(self.chat_id, st, task, st.get("repo"), st.get("branch")),
                                 daemon=True).start()
                return
            self.send(f"🛑 এজেন্ট এরর: {(self.last_err or execution)[:200]}\n"
                      "আবার লিখুন, বা /engine দিয়ে ইঞ্জিন বদলান।")
            return

        metrics = conv.get("metrics") or {}
        cost = metrics.get("accumulated_cost") or 0
        tokens = metrics.get("accumulated_token_usage") or {}
        combo = tokens.get("combined_metrics") or tokens
        total = int(combo.get("total_tokens") or
                    ((combo.get("input_tokens") or 0) + (combo.get("output_tokens") or 0)))
        # 💬 চ্যাট-সাইকেল (কোনো ডকুমেন্ট/খরচ নেই) -> আলাদা "শেষ" লাইন স্প্যাম নয়
        if (self.docs_sent == 0 and float(cost or 0) <= 0.0
                and execution not in ("error", "stuck") and not self.last_err):
            if time.time() - self.start_ts >= CFG.watch_extend_min * 60 - 1:
                self.send("⚠️ এই রানে কোনো আউটপুট ধরা যায়নি — আবার লিখুন, সাথে সাথে ধরব।")
            return
        icon = {"finished": "✅", "error": "🛑", "stuck": "⚠️"}.get(execution, "⏹")
        line = f"{icon} শেষ | 💰 ${float(cost):.4f}"
        if total:
            line += f" | 🔢 {total:,} tokens"
        extra = []
        if execution in ("error", "stuck") or sandbox in ("ERROR", "MISSING"):
            extra.append(f"🌐 {conversation_url(self.cid, CFG.oh_base_url)}")
        if sandbox == "PAUSED":
            extra.append("/resume দিয়ে চালু করুন")
        if extra:
            line += "\n" + " | ".join(extra)
        self.send(line)

    def stop(self) -> None:
        self.stop_evt.set()


def _parse_iso(ts):
    """ISO timestamp -> epoch সেকেন্ড (ব্যর্থ হলে None)।"""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        t = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def verbosity_of(w) -> str:
    return (w.state.get("verbosity") or CFG.default_verbosity or "quiet")


_INTENT_RE = __import__("re").compile(
    r"(করি:|করছি:|করব:|বানাচ্ছি:|বানাব:|দেখি:|শুরু করি:|চালাই:|নিচ্ছি:|যাচ্ছি:|দিচ্ছি:|"
    r"I will|I'll|Let me|Now I|Next I)\s*$", __import__("re").I)


def _is_intent_only(text: str) -> bool:
    """শেষ মেসেজটা কি শুধু 'এখন করছি:' ধরনের ইচ্ছা-ঘোষণা (কাজ নয়)?"""
    t = (text or "").strip()
    if not t or len(t) < 12:
        return False
    return t.endswith(":") or bool(_INTENT_RE.search(t))


_BRAIN_POLL_TEXT = ("INTERNAL (user-কে দেখাবে না): এইমাত্র শেষ হওয়া কাজ থেকে নতুন কোনো স্থায়ী তথ্য/"
                    "সিদ্ধান্ত/ইউজার-পছন্দ শিখে থাকলে প্রতিটি এক লাইনে বুলেট (- ) হিসেবে সর্বোচ্চ ৪টা লেখো। "
                    "কিছু না থাকলে শুধু NONE লেখো। এই উত্তর শুধু মেমোরি-সিস্টেমের জন্য।")
_RULE_TEXT = ("INTERNAL (user-কে দেখাবে না): ইউজারের মন্তব্য: \"{user}\" — অর্থাৎ তোমার আগের আউটপুটে ভুল ছিল। "
              "এই ভুলটি ভবিষ্যতে ঠেকাবার স্থায়ী নিয়ম এক লাইনে (সর্বোচ্চ ২২০ অক্ষর) লেখো। শুধু সেই এক লাইন উত্তর দাও।")
_LIVE_SEARCH_INSTR = ("[LIVE-SEARCH MODE — বাধ্যতামূলক] এই প্রশ্নে টাটকা তথ্য লাগবে। উত্তরের আগে shell দিয়ে লাইভ ডেটা নাও: "
                      "curl -s 'https://html.duckduckgo.com/html/?q=<query>' ; যেকোনো ফলাফলের পুরো পাতা পড়তে "
                      "curl -s 'https://r.jina.ai/<url>' ; বিকল্প: curl -s 'https://en.wikipedia.org/w/api.php?action=query&list=search&srquery=<query>&format=json' । "
                      "২+ সোর্স মिलाও; উত্তরে তারিখ ও সোর্স-URL দাও; সময়-সংবেদনশীল তথ্য কখনো স্মৃতি থেকে নয়।")
SEARCH_RE = __import__("re").compile(
    r"(সার্চ|খবর|news|latest|সর্বশেষ|আজকের|আবহাওয়া|weather|stock|শেয়ারের দাম|টাটকা)", __import__("re").I)

_NUDGE_TEXT = ("🛑 থামো: তোমার শেষ মেসেজটা শুধু ইচ্ছের কথা ছিল, আসল কাজ নয়। এই টার্নেই এখন "
               "টুল কল করে কাজটা শেষ করো (ফাইল তৈরি / কমান্ড চালানো), ফলাফল যাচাই করো, "
               "তারপর এক মেসেজে চূড়ান্ত ডেলিভারি দাও (ফাইল / লিংক / পুরো উত্তর)। "
               "'এখন করছি:' বলে টার্ন শেষ করা যাবে না।")

_CONTINUE_TEXT = ("Your previous run was INTERRUPTED mid-work (the sandbox fell asleep). Continue NOW from exactly where you stopped and finish with a final user-facing message. Never repeat an earlier message of yours.")


def start_watcher(chat_id: int, conversation_id: str, state: dict, replace: bool = True,
                  fresh: bool = False) -> Watcher:
    with WATCHER_LOCK:
        old = WATCHERS.get(chat_id)
        if old is not None:
            if old.cid == conversation_id and old.is_alive() and not replace:
                return old
            old.stop()
        w = Watcher(chat_id, conversation_id, state, fresh=fresh)
        WATCHERS[chat_id] = w
        w.start()
        return w


def stop_watcher(chat_id: int) -> bool:
    with WATCHER_LOCK:
        w = WATCHERS.pop(chat_id, None)
    if w:
        w.stop()
        return True
    return False


# ====================================================================== #
#  অথেনটিকেশন
# ====================================================================== #


def allowed(user) -> bool:
    if not CFG.allowed_ids:
        return True  # অ্যালাউলিস্ট সেট করা নেই → সবার জন্য খোলা
    return bool(user) and int(user.get("id") or 0) in CFG.allowed_ids


# ====================================================================== #
#  ইনলাইন কিবোর্ড helper
# ====================================================================== #


def kb(rows: list) -> str:
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def btn(text: str, cb: str) -> dict:
    return {"text": text[:60], "callback_data": cb[:64]}


def url_btn(text: str, url: str) -> dict:
    return {"text": text[:60], "url": url}


# ====================================================================== #
#  কমান্ড হ্যান্ডলার
# ====================================================================== #

HELP_TEXT = """🤖 <b>OpenHands Telegram Bridge</b>

সোজা মেসেজ লিখলেই সেটা OpenHands এজেন্টের কাছে চলে যাবে, আর এজেন্টের লাইভ আউটপুট এখানে চলে আসবে।

<b>কনভারসেশন</b>
/new [owner/repo] — নতুন কনভারসেশন শুরু
/repo owner/repo — ডিফল্ট রিপো সেট (repo ছাড়াও চলবে)
/convs — সাম্প্রতিক কনভারসেশন লিস্ট
/use &lt;id&gt; — নির্দিষ্ট কনভারসেশনে ফিরে যাওয়া
/status — এখন কী চলছে
/link — ওয়েব UI লিংক (ওখানেও একই কনভারসেশন দেখা যাবে)
/watch — লাইভ স্ট্রিম আবার চালু করা
/stop — স্যান্ডবক্স পজ করে খরচ বন্ধ করা
/resume — পজ করা স্যান্ডবক্স চালু করা

<b>আউটপুট কন্ট্রোল</b>
/quiet — শুধু এজেন্টের কথা + এরর
/normal — সাথে টুল কলের সামারি (ডিফল্ট)
/verbose — পুরো টুল আর্গুমেন্ট ও আউটপুট
/card on|off — লাইভ প্রোগ্রেস কার্ড

<b>রেজাল্ট</b>
/get &lt;path&gt; — স্যান্ডবক্সের যেকোনো ফাইল নামিয়ে আনা (txt/html/pdf/zip/ছবি…)
      যেমন <code>/get index.html</code> বা <code>/get /workspace/project/app.pdf</code>
/changes [path] — কোন কোন ফাইল বদলেছে
/diff &lt;path&gt; — ফাইলের ডিফ
/cost — খরচ ও টোকেন

<b>অন্যান্য</b>
/id — আপনার Telegram ID (অ্যালাউলিস্টের জন্য)
/help — এই মেসেজ"""


def cmd_help(chat_id: int, _args: str, _st: dict) -> None:
    TG.send(chat_id, HELP_TEXT)


def cmd_id(chat_id: int, _args: str, _st: dict, user=None) -> None:
    uid = int((user or {}).get("id") or 0)
    TG.send(chat_id, f"👤 আপনার Telegram ID: <code>{uid}</code>\n(অ্যালাউলিস্টে বসালে অন্য কেউ বট ব্যবহার করতে পারবে না)")


def cmd_repo(chat_id: int, args: str, st: dict) -> None:
    parts = args.split()
    if not parts:
        cur = st.get("repo")
        TG.send(chat_id, f"📦 বর্তমান ডিফল্ট রিপো: <code>{cur or 'সেট করা নেই'}</code>\n"
                         "ব্যবহার: <code>/repo owner/repo [branch]</code>\n"
                         "রিপো ছাড়াই কাজ করাতে চাইলে <code>/repo none</code>")
        return
    if parts[0].lower() in ("none", "clear", "off"):
        st["repo"] = None
        st["branch"] = None
        save_state(chat_id, st)
        TG.send(chat_id, "👌 রিপো সরানো হলো — এখন এজেন্ট খালি স্যান্ডবক্সে কাজ করবে।")
        return
    st["repo"] = parts[0].strip()
    if len(parts) > 1:
        st["branch"] = parts[1].strip()
    save_state(chat_id, st)
    TG.send(chat_id, f"👌 ডিফল্ট রিপো: <code>{st['repo']}</code>"
                     + (f"  ব্রাঞ্চ: <code>{st['branch']}</code>" if st.get("branch") else "")
                     + "\nএবার <code>/new</code> লিখুন।")


def cmd_repos(chat_id: int, args: str, _st: dict) -> None:
    try:
        items = oh().search_repositories(args, limit=25)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ রিপো লিস্ট আনা যায়নি: {str(e)[:300]}")
        return
    if not items:
        TG.send(chat_id, "কোনো কানেক্টেড রিপো পাওয়া যায়নি। app.all-hands.dev → Settings → GitHub/GitLab integration চেক করুন।")
        return
    rows = []
    for it in items[:20]:
        if isinstance(it, dict):
            full = it.get("full_name") or it.get("name") or it.get("id") or ""
        else:
            full = str(it)
        if full:
            rows.append([btn(full, f"repo:{full}")])
    TG.send(chat_id, "📚 আপনার কানেক্টেড রিপো (ট্যাপ করলে ডিফল্ট রিপো সেট হবে):",
            reply_markup=kb(rows))


def cmd_new(chat_id: int, args: str, st: dict, pending_text: str = "") -> None:
    """
    /new                       -> রিপো জিজ্ঞেস না করে পরের মেসেজটাকে টাস্ক ধরবে
    /new owner/repo            -> রিপো সেট, পরের মেসেজ = টাস্ক
    /new owner/repo <task...>  -> সরাসরি শুরু
    /new <task...>             -> ডিফল্ট রিপো দিয়ে শুরু
    """
    args = (args or "").strip()
    repo = st.get("repo")
    branch = st.get("branch")
    task_text = pending_text.strip()

    looks_like_repo = (
        args
        and "/" in args.split("\n", 1)[0].split(" ", 1)[0]
        and " " not in args.split("\n", 1)[0].split(" ", 1)[0]
    )
    first_tok = args.split("\n", 1)[0].split(" ", 1)[0] if args else ""

    if first_tok.lower() in ("none", "-"):
        repo, branch = None, None
        task_text = task_text or args[len(first_tok):].strip()
    elif looks_like_repo:
        repo = first_tok
        remainder = args[len(first_tok):].strip()
        # রিপোর পরে branch না টাস্ক — branch হলে একটুকু, স্পেস ছাড়া
        nxt = remainder.split("\n", 1)[0].split(" ", 1)[0]
        if nxt and " " not in nxt and "/" not in nxt and len(nxt) < 40 \
                and remainder.startswith(nxt) and len(remainder) == len(nxt):
            branch = nxt
            remainder = ""
        task_text = task_text or remainder.strip()

    if not args:
        pass
    elif not task_text and not looks_like_repo and first_tok.lower() not in ("none", "-"):
        # পুরো args-ই টাস্ক (ডিফল্ট রিপো ব্যবহার হবে)
        task_text = args

    if not task_text:
        # পরের মেসেজটাকে টাস্ক ধরা হবে
        PENDING_NEW[chat_id] = {"repo": repo, "branch": branch, "ts": time.time()}
        TG.send(chat_id, "🆕 নতুন কনভারসেশন।\n"
                         + (f"📦 রিপো: <code>{repo}</code>\n" if repo else "📦 রিপো: নেই (খালি স্যান্ডবক্স)\n")
                         + "এখন এজেন্টকে কী করাতে চান সেটা লিখুন 👇")
        return

    _do_start_conversation(chat_id, st, task_text, repo, branch)


PENDING_NEW: dict = {}


def _do_start_conversation(chat_id: int, st: dict, text: str, repo, branch,
                           model: str | None = None) -> None:
    progress_id = 0
    use_model = model or CFG.llm_model or None
    # শুধু যাচাই-করা ফ্রি প্রোফাইল ID-ই চলবে (ভুল/অচেনা ভ্যালু -> hosted-এ পড়ে গেলে
    # ভিশন হারায় + ক্রেডিট পোড়ে) — স্টেট > misc > env ক্রমে
    valid = ("gemini-free", "groq-free", "openrouter-free")
    cand = [st.get("engine_profile"), misc_get(f"eng:{chat_id}"), CFG.agent_profile_id]
    use_profile = next((c for c in cand if c in valid), None)
    if model:
        use_profile = None      # এক্সপ্লিসিট মডেল -> প্রোফাইল বন্ধ
    elif use_profile:
        use_model = None        # ফ্রি প্রোফাইল ইঞ্জিন -> hosted মডেল চাপাবো না (403 গার্ড)
    log(f"launch: profile={use_profile} model={use_model} chat={chat_id}")

    def work():
        nonlocal progress_id
        try:
            TG.typing(chat_id)
            progress = TG.send(chat_id, "⏳ শুরু হচ্ছে…") or {}
            progress_id = int(progress.get("message_id") or 0)

            # 🧠 ব্রেইন: নতুন কনভারসেশন মানেই নলেজ বেস সহ জন্ম
            payload = text
            if BRAIN_HASH and (st.get("brain_hash") or "") != BRAIN_HASH:
                payload = brain_prefix(text)

            task = oh().start_conversation(
                payload,
                repository=repo,
                branch=branch,
                llm_model=use_model,
                agent_profile_id=use_profile,
                system_message_suffix=CFG.system_suffix or None,
            )
            task_id = task.get("id")
            cid = task.get("app_conversation_id")

            def on_status(status, detail=None):
                # স্ট্যাটাস ইউজারকে দেখানো হয় না — শুধু লগে (নয়েজ ফ্রি অভিজ্ঞতা)
                log(f"[chat {chat_id}] start status: {status}"
                    + (f" ({detail})" if detail and status == "ERROR" else ""))

            if not (cid and (task.get("status") or "").upper() == "READY"):
                task = oh().wait_until_ready(task, timeout=CFG.start_timeout, on_status=on_status)
                cid = task.get("app_conversation_id") or cid

            if not cid:
                TG.send(chat_id, "⚠️ কনভারসেশন ID পাওয়া যায়নি। আবার চেষ্টা করুন।")
                return

            st["conversation_id"] = cid
            st["repo"] = repo
            st["branch"] = branch
            st["model"] = use_model or ""
            st["brain_hash"] = BRAIN_HASH
            save_state(chat_id, st)

            conv = oh().get_conversation(cid) or {}
            title = conv.get("title") or (text[:48] + ("…" if len(text) > 48 else ""))
            st["title"] = title
            save_state(chat_id, st)

            # প্রসেসিং মেসেজটা সরিয়ে দেওয়া হলো — এখন শুধু আসল আউটপুট আসবে
            TG.delete(chat_id, progress_id)

            start_watcher(chat_id, cid, st, fresh=True)

        except OpenHandsError as e:
            log("start conversation error:", str(e)[:400])
            hint = ""
            low = str(e).lower()
            if "401" in low or "403" in low:
                hint = "\n👉 API key ঠিক আছে কিনা দেখুন (app.all-hands.dev → Settings → API Keys)।"
            elif "repository" in low or "not found" in low:
                hint = "\n👉 রিপোর নাম/access চেক করুন। <code>/repos</code> দিয়ে লিস্ট দেখুন।"
            if progress_id:
                TG.delete(chat_id, progress_id)
            TG.send(chat_id, f"⚠️ শুরু করা যায়নি:\n<code>{str(e)[:500]}</code>{hint}")
        except Exception as e:
            log("start conversation crash:", traceback.format_exc()[:600])
            TG.send(chat_id, f"⚠️ অপ্রত্যাশিত সমস্যা: {str(e)[:300]}")

    threading.Thread(target=work, daemon=True).start()


def cmd_convs(chat_id: int, _args: str, _st: dict) -> None:
    try:
        items = oh().search_conversations(limit=20)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ লিস্ট আনা যায়নি: {str(e)[:300]}")
        return
    if not items:
        TG.send(chat_id, "কোনো কনভারসেশন নেই। <code>/new</code> দিয়ে শুরু করুন।")
        return
    rows, lines = [], []
    for c in items[:15]:
        cid = str(c.get("id") or "")
        title = (c.get("title") or "untitled")[:40]
        st = c.get("sandbox_status") or "?"
        ex = c.get("execution_status") or "-"
        lines.append(f"• <code>{cid[:8]}</code> {title} — {st}/{ex}")
        rows.append([btn(f"{title[:35]} ({ex})", f"use:{cid}")])
    TG.send(chat_id, "🗂 <b>সাম্প্রতিক কনভারসেশন</b>\n" + "\n".join(lines),
            reply_markup=kb(rows))


def cmd_use(chat_id: int, args: str, st: dict) -> None:
    target = args.strip()
    if not target:
        TG.send(chat_id, "ব্যবহার: <code>/use &lt;conversation_id&gt;</code> অথবা /convs থেকে বেছে নিন।")
        return
    # ছোট আইডি (prefix) হলে পুরো আইডি খোঁজা
    if len(target) < 32:
        try:
            items = oh().search_conversations(limit=50)
        except OpenHandsError:
            items = []
        match = [c for c in items if str(c.get("id") or "").startswith(target)]
        if len(match) == 1:
            target = str(match[0].get("id"))
        elif len(match) > 1:
            TG.send(chat_id, f"⚠️ '{target}' দিয়ে {len(match)}টা মিলেছে — পুরো আইডি দিন।")
            return
    try:
        conv = oh().get_conversation(target)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ পাওয়া যায়নি: {str(e)[:300]}")
        return
    if not conv:
        TG.send(chat_id, "⚠️ এই কনভারসেশন পাওয়া যায়নি।")
        return
    st["conversation_id"] = target
    st["repo"] = conv.get("selected_repository") or st.get("repo")
    st["branch"] = conv.get("selected_branch") or st.get("branch")
    st["title"] = conv.get("title")
    save_state(chat_id, st)
    TG.send(chat_id, f"🔀 এখন এই কনভারসেশনে আছি:\n<code>{target}</code>\n"
                     f"📦 {conv.get('selected_repository') or '—'} | "
                     f"{conv.get('sandbox_status')}/{conv.get('execution_status')}")
    start_watcher(chat_id, target, st)


def cmd_status(chat_id: int, _args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "এখনো কোনো কনভারসেশন নেই। <code>/new</code> লিখুন।")
        return
    try:
        conv = oh().get_conversation(cid) or {}
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ স্ট্যাটাস আনা যায়নি: {str(e)[:300]}")
        return
    m = conv.get("metrics") or {}
    tok = (m.get("accumulated_token_usage") or {}).get("combined_metrics") or {}
    with WATCHER_LOCK:
        watching = bool(WATCHERS.get(chat_id))
    lines = [
        "📊 <b>স্ট্যাটাস</b>",
        f"🔑 <code>{cid}</code>",
        f"📝 {conv.get('title') or '—'}",
        f"📦 repo: <code>{conv.get('selected_repository') or '—'}</code> "
        f"branch: <code>{conv.get('selected_branch') or '—'}</code>",
        f"🧠 model: {conv.get('llm_model') or m.get('model_name') or '—'}",
        f"📦 sandbox: {conv.get('sandbox_status') or '?'}   ⚙️ execution: {conv.get('execution_status') or '?'}",
        f"💰 cost: ${float(m.get('accumulated_cost') or 0):.4f}"
        + (f"   🔢 tokens: {int(tok.get('total_tokens') or 0):,}" if tok.get("total_tokens") else ""),
        f"📡 live watcher: {'চালু' if watching else 'বন্ধ (/watch দিয়ে চালু করুন)'}",
    ]
    TG.send(chat_id, "\n".join(lines),
            reply_markup=kb([[url_btn("🌐 Open in web", conv.get("conversation_url")
                                      or conversation_url(cid, CFG.oh_base_url))]]))


def cmd_link(chat_id: int, _args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "কনভারসেশন নেই। <code>/new</code> লিখুন।")
        return
    url = conversation_url(cid, CFG.oh_base_url)
    TG.send(chat_id, f"🌐 {url}\n(একই কনভারসেশন — ওয়েবেও দেখতে ও চালাতে পারবেন)",
            reply_markup=kb([[url_btn("🌐 Open in web", url)]]))


def cmd_watch(chat_id: int, _args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "কনভারসেশন নেই। <code>/new</code> লিখুন।")
        return
    start_watcher(chat_id, cid, st)
    TG.send(chat_id, "📡 লাইভ স্ট্রিম চালু হলো।")


def cmd_stop(chat_id: int, _args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "কনভারসেশন নেই।")
        return
    try:
        conv = oh().get_conversation(cid) or {}
        sid = conv.get("sandbox_id")
        if not sid:
            TG.send(chat_id, "⚠️ sandbox_id পাওয়া যায়নি।")
            return
        oh().pause_sandbox(sid)
        TG.send(chat_id, "⏸ স্যান্ডবক্স পজ করা হলো — এখন আর খরচ হবে না।\n/resume দিয়ে আবার চালু করুন।")
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ থামানো যায়নি: {str(e)[:300]}")


def cmd_resume(chat_id: int, _args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "কনভারসেশন নেই।")
        return
    try:
        conv = oh().get_conversation(cid) or {}
        sid = conv.get("sandbox_id")
        if not sid:
            TG.send(chat_id, "⚠️ sandbox_id পাওয়া যায়নি।")
            return
        oh().resume_sandbox(sid)
        TG.send(chat_id, "▶️ স্যান্ডবক্স রিজিউম হচ্ছে… কয়েক সেকেন্ড পর মেসেজ পাঠান।")
        start_watcher(chat_id, cid, st)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ রিজিউম করা যায়নি: {str(e)[:300]}")


def _set_verbosity(chat_id: int, st: dict, level: str) -> None:
    st["verbosity"] = level
    save_state(chat_id, st)
    with WATCHER_LOCK:
        w = WATCHERS.get(chat_id)
        if w:
            w.state["verbosity"] = level
    desc = {"quiet": "শুধু এজেন্টের কথা + এরর",
            "normal": "এজেন্টের কথা + টুল কলের সামারি",
            "verbose": "সবকিছু (টুল আর্গুমেন্ট, পুরো আউটপুট, স্টেট আপডেট)"}[level]
    TG.send(chat_id, f"🔊 ভার্ভোসিটি: <b>{level}</b> — {desc}")


def cmd_quiet(chat_id, _a, st): _set_verbosity(chat_id, st, "quiet")
def cmd_normal(chat_id, _a, st): _set_verbosity(chat_id, st, "normal")
def cmd_verbose(chat_id, _a, st): _set_verbosity(chat_id, st, "verbose")


def cmd_card(chat_id: int, args: str, st: dict) -> None:
    arg = args.strip().lower()
    if arg in ("on", "1", "true"):
        st["live_card"] = 1
    elif arg in ("off", "0", "false"):
        st["live_card"] = 0
    else:
        st["live_card"] = 0 if st.get("live_card") else 1
    save_state(chat_id, st)
    with WATCHER_LOCK:
        w = WATCHERS.get(chat_id)
        if w:
            w.state["live_card"] = st["live_card"]
            if not st["live_card"]:
                w.card_msg_id = 0
    TG.send(chat_id, f"⚡ লাইভ প্রোগ্রেস কার্ড: {'চালু' if st['live_card'] else 'বন্ধ'}")


_ENGINE_PROFILES = {
    "gemini": "gemini-free",
    "groq": "groq-free",
    "openrouter": "openrouter-free",
    "default": "",
}


def cmd_engine(chat_id: int, args: str, st: dict) -> None:
    """ইঞ্জিন দেখাও/বদলাও — জেমিনি প্রধান, groq/openrouter ব্যাকআপ (সব ফ্রি)।"""
    a = (args or "").strip().lower()
    if not a:
        cur = st.get("engine_profile") or CFG.agent_profile_id or "default(hosted)"
        TG.send(chat_id, "🔧 <b>ইঞ্জিন</b>\n"
                         f"• এখন: <b>{cur}</b>\n"
                         "• অপশন: <code>/engine gemini</code> (প্রধান, ফ্রি) | "
                         "<code>/engine groq</code> | <code>/engine openrouter</code> | "
                         "<code>/engine default</code> (ক্রেডিট)\n"
                         "• বদলালে পরের <b>নতুন</b> কনভারসেশন থেকে লাগবে (/new)")
        return
    if a not in _ENGINE_PROFILES:
        TG.send(chat_id, "⚠️ অচেনা ইঞ্জিন। অপশন: gemini / groq / openrouter / default")
        return
    prof = _ENGINE_PROFILES[a]
    if prof:
        st["engine_profile"] = prof
    else:
        st.pop("engine_profile", None)
    misc_set(f"eng:{chat_id}", prof or "")
    persist_state_soon(5)
    TG.send(chat_id, f"🔧 ইঞ্জিন সেট: <b>{prof or 'default (hosted ক্রেডিট)'}</b> — "
                     "পরের /new থেকে চালু হবে।")


def cmd_cost(chat_id: int, _args: str, st: dict) -> None:
    """পুরো অ্যাকাউন্টের মোট খরচ + বড় খরুচে সেশনগুলো (ব্যালেন্স UI-তে)।"""
    try:
        items = oh().search_conversations(limit=50)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ খরচ আনা যায়নি: {str(e)[:200]}")
        return
    total = 0.0
    cur = st.get("conversation_id") or ""
    cur_cost = None
    rows = []
    for c in items:
        m = c.get("metrics") or {}
        cost = float(m.get("accumulated_cost") or 0.0)
        total += cost
        if cur and str(c.get("id") or "") == cur:
            cur_cost = cost
        if cost > 0.01:
            rows.append((cost, (c.get("title") or "untitled")[:34]))
    rows.sort(reverse=True)
    lines = ["💰 <b>খরচের রিপোর্ট</b>",
             f"• সবদিন মিলিয়ে মোট খরচ: <b>${total:.4f}</b>"]
    if cur_cost is not None:
        lines.append(f"• বর্তমান সেশন: <b>${cur_cost:.4f}</b>")
    for cost, title in rows[:3]:
        lines.append(f"   – ${cost:.2f} → {title}")
    lines.append("• বাকি ব্যালেন্স: app.all-hands.dev → Settings → Billing "
                 "(API-তে দেয় না, তাই এখানে দেখাতে পারি না)")
    lines.append("• সিস্টেম: প্রি-পেইড ক্রেডিট — দৈনিক রিফিল নয়; "
                 "ফুরালে টপ-আপ লাগবে")
    TG.send(chat_id, "\n".join(lines))


def cmd_file(chat_id: int, args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "কনভারসেশন নেই।")
        return
    path = args.strip() or "/workspace/project/PLAN.md"
    if not path.startswith("/"):
        path = f"/workspace/project/{path}"
    try:
        content = oh().read_file(cid, path)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ ফাইল পড়া যায়নি: {str(e)[:300]}")
        return
    if not content:
        TG.send(chat_id, f"⚠️ <code>{path}</code> খালি বা নেই।")
        return
    name = path.rsplit("/", 1)[-1] or "file.txt"
    raw = content.encode("utf-8", errors="replace")
    if len(raw) <= 3500:
        TG.send(chat_id, f"📄 <code>{path}</code>\n<pre>{content.replace('</pre>', '')}</pre>")
    else:
        TG.send_document(chat_id, name, raw, caption=path)


def cmd_changes(chat_id: int, args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "কনভারসেশন নেই।")
        return
    path = args.strip()
    if not path:
        repo = (st.get("repo") or "").split("/")[-1]
        path = f"/workspace/{repo}" if repo else "/workspace/project"
    try:
        out = oh().git_changes(cid, path)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ changes আনা যায়নি: {str(e)[:300]}\n"
                         f"সঠিক path দিন, যেমন <code>/changes /workspace/project</code>")
        return
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2)
    TG.send(chat_id, f"🔀 <b>Git changes</b> (<code>{path}</code>)\n" +
            (f"<pre>{text[:3500].replace('</pre>', '')}</pre>" if text.strip() else "<i>কোনো পরিবর্তন নেই</i>"))


def cmd_diff(chat_id: int, args: str, st: dict) -> None:
    cid = st.get("conversation_id")
    path = args.strip()
    if not cid or not path:
        TG.send(chat_id, "ব্যবহার: <code>/diff /workspace/project/src/app.py</code>")
        return
    try:
        out = oh().git_diff(cid, path)
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ diff আনা যায়নি: {str(e)[:300]}")
        return
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2)
    if not text.strip():
        TG.send(chat_id, "কোনো ডিফ নেই।")
        return
    if len(text) > 3500:
        TG.send_document(chat_id, path.rsplit("/", 1)[-1] + ".diff", text.encode("utf-8"), caption=path)
    else:
        TG.send(chat_id, f"🔍 <b>diff</b> <code>{path}</code>\n<pre>{text.replace('</pre>', '')}</pre>")


def cmd_github(chat_id: int, args: str, _st: dict) -> None:
    """
    /github <PAT> [username] — GitHub টোকেন OpenHands Cloud অ্যাকাউন্টে সেভ করে।
    নোট: প্রাইভেট রিপো ক্লোনের জন্য বেশিরভাগ ক্ষেত্রে GitHub App ইনস্টল করাই ভালো।
    """
    parts = args.split()
    if not parts:
        TG.send(chat_id, "ব্যবহার: <code>/github ghp_xxxx your-github-username</code>\n"
                         "⚠️ টোকেন চ্যাটে পাঠাবেন না — নিরাপদ উপায়:\n"
                         "app.all-hands.dev → Settings → GitHub → Install GitHub App")
        return
    token = parts[0]
    user_id = parts[1] if len(parts) > 1 else ""
    try:
        res = oh().store_git_provider_token("github", token, user_id)
        TG.send(chat_id, f"✅ {res.get('message') or 'টোকেন সেভ হয়েছে'}\n"
                         "🔐 নিরাপত্তার জন্য এখন এই টোকেনটা GitHub থেকে রিভোক করে নতুনটা দিন।")
    except OpenHandsError as e:
        TG.send(chat_id, f"⚠️ টোকেন সেভ হয়নি: {str(e)[:400]}")


BINARY_EXT = {".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".docx",
              ".xlsx", ".pptx", ".mp3", ".mp4", ".wav", ".ttf", ".woff", ".woff2",
              ".exe", ".bin", ".tar", ".gz", ".7z", ".rar", ".ico", ".svg"}


def _is_binary(path):
    import os as _os
    return _os.path.splitext(path)[1].lower() in BINARY_EXT


def cmd_get(chat_id, args, st):
    """
    /get <path> — স্যান্ডবক্সের ফাইল টেলিগ্রামে নামিয়ে আনা (যেকোনো ফরম্যাট)।
    টেক্সট হলে সরাসরি, বাইনারি (pdf/zip/ছবি…) হলে এজেন্ট দিয়ে base64 করে আনা হয়।
    """
    cid = st.get("conversation_id")
    path = (args or "").strip()
    if not cid:
        TG.send(chat_id, "কোনো কনভারসেশন নেই।")
        return
    if not path:
        TG.send(chat_id, "ব্যবহার: <code>/get /workspace/project/index.html</code>\n"
                         "শুধু নাম দিলে <code>/workspace/project/</code>-এর ভেতরে খোঁজা হবে।")
        return
    if not path.startswith("/"):
        path = "/workspace/project/" + path

    def work():
        try:
            name = path.rsplit("/", 1)[-1] or "file.bin"
            if not _is_binary(path):
                content = oh().read_file(cid, path)
                if not content.strip():
                    TG.send(chat_id, f"⚠️ <code>{path}</code> খালি বা পাওয়া যায়নি।")
                    return
                TG.send_document(chat_id, name, content.encode("utf-8", "replace"), caption=path)
                return

            TG.send(chat_id, f"📦 <code>{name}</code> আনা হচ্ছে…")
            instr = ("Run EXACTLY this single command, then reply only DONE: "
                     f"base64 -w0 '{path}' > /tmp/oh_get.b64 && wc -c < /tmp/oh_get.b64")
            oh().send_message(cid, instr, run=True)
            b64 = ""
            for _ in range(45):
                time.sleep(2)
                b64 = (oh().read_file(cid, "/tmp/oh_get.b64") or "").strip()
                if b64 and len(b64) > 8:
                    break
            if not b64:
                TG.send(chat_id, "⚠️ ফাইলটা base64 করা যায়নি — পাথ ঠিক আছে কিনা দেখুন।")
                return
            if len(b64) > 26_000_000:
                TG.send(chat_id, "⚠️ ফাইলটা টেলিগ্রামের সীমার চেয়ে বড়।")
                return
            TG.send_document(chat_id, name, base64.b64decode(b64), caption=path)
        except OpenHandsError as e:
            TG.send(chat_id, f"⚠️ ফাইল আনা যায়নি: {str(e)[:300]}")
        except Exception:
            log("cmd_get crash:", traceback.format_exc()[:400])
            TG.send(chat_id, "⚠️ ফাইল আনতে সমস্যা হলো।")

    threading.Thread(target=work, daemon=True).start()


def cmd_remember(chat_id: int, args: str, st: dict) -> None:
    """/remember <কথা> -> BRAIN.md-এ স্থায়ীভাবে সেভ, পরের সব সেশনে মাথায় থাকবে।"""
    line = args.strip()
    if not line:
        TG.send(chat_id, "ব্যবহার: <code>/remember আমি চা-এর বদলে কফি পছন্দ করি</code>")
        return
    try:
        with open("BRAIN.md", "a", encoding="utf-8") as fh:
            fh.write(f"- {line}\n")
        load_brain()
        threading.Thread(target=_push_brain, daemon=True).start()
        TG.send(chat_id, f"🧠 স্থায়ীভাবে মনে রাখলাম — এখন থেকে প্রতিটি সেশনে থাকবে:\n• {line}")
    except Exception as e:
        TG.send(chat_id, f"⚠️ সেভ করা যায়নি: {str(e)[:200]}")


def cmd_brain(chat_id: int, args: str, st: dict) -> None:
    """বর্তমান ব্রেইনটা ফাইল আকারে দেখিয়ে দেই।"""
    if not BRAIN_TEXT:
        TG.send(chat_id, "🧠 ব্রেইন এখনো খালি।")
        return
    TG.send_document(chat_id, "BRAIN.md", BRAIN_TEXT.encode("utf-8"),
                     caption=f"🧠 বর্তমান নলেজ বেস ({len(BRAIN_TEXT)} অক্ষর)")


def _push_brain() -> None:
    pat = _env("GH_PAT")
    if not pat:
        return
    import subprocess
    owner = _env("PAGES_OWNER", "sheikhrashel47-stack")
    with _PERSIST_LOCK:
        try:
            subprocess.run(["git", "add", "BRAIN.md"], capture_output=True, timeout=60)
            if subprocess.run(["git", "diff", "--cached", "--quiet"],
                              capture_output=True).returncode == 0:
                return
            subprocess.run(["git", "-c", "user.name=oh-bot", "-c", "user.email=bot@local",
                            "commit", "-q", "-m", "brain: remember update"],
                           capture_output=True, timeout=60)
            subprocess.run(["git", "remote", "set-url", "origin",
                            f"https://x-access-token:{pat}@github.com/{owner}/oh-telegram-bot.git"],
                           capture_output=True)
            p = subprocess.run(["git", "push", "-q", "origin", "HEAD:main"],
                               capture_output=True, timeout=120)
            if p.returncode != 0:
                subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"],
                               capture_output=True, timeout=120)
                subprocess.run(["git", "push", "-q", "origin", "HEAD:main"],
                               capture_output=True, timeout=120)
            subprocess.run(["git", "remote", "set-url", "origin",
                            f"https://github.com/{owner}/oh-telegram-bot.git"],
                           capture_output=True)
            log("brain pushed to git")
        except Exception as e:
            log("brain push failed:", str(e)[:150])


def cmd_publish(chat_id: int, args: str, st: dict) -> None:
    """লাইভ সাইটটা স্থায়ী GitHub Pages লিংকে প্রকাশ করে — যেটা ১ বারেই খোলে,
    স্যান্ডবক্স ঘুমালেও মরে না।"""
    cid = st.get("conversation_id")
    if not cid:
        TG.send(chat_id, "আগে কনভারসেশন দরকার — মেসেজ বা /new পাঠান।")
        return
    path = (args or "/workspace/project").strip()
    TG.send(chat_id, "📤 স্থায়ী লিংকে প্রকাশ করছি… (৩০-৯০ সেকেন্ড)")

    def work():
        import shutil, subprocess, tempfile
        tmp = tempfile.mkdtemp(prefix="pub_")
        try:
            conv = oh().get_conversation(cid) or {}
            sid = conv.get("sandbox_id")
            if not sid:
                TG.send(chat_id, "⚠️ sandbox_id পাওয়া যায়নি।")
                return
            if (conv.get("sandbox_status") or "") == "PAUSED":
                oh().resume_sandbox(sid)
                time.sleep(8)
            url = f"https://work-1-{sid}.prod-runtime.all-hands.dev/"
            if not link_alive(url):
                oh().send_message(
                    cid,
                    f"Serve the static site at {path} on port 12000 bound to 0.0.0.0 in the "
                    "background (index.html at the served root). No reply needed.",
                    run=True)
                for _ in range(24):
                    time.sleep(5)
                    if link_alive(url):
                        break
            if not link_alive(url):
                TG.send(chat_id, "⚠️ সাইট সার্ভার চালু হলো না — এজেন্টকে আগে সাইট চালাতে বলুন, তারপর /publish।")
                return
            mirror = os.path.join(tmp, "site")
            os.makedirs(mirror)
            subprocess.run(["wget", "-q", "-r", "-np", "-nH", "--cut-dirs=0",
                            "-R", "robots.txt", "-e", "robots=off", "-P", mirror, url],
                           timeout=300, capture_output=True)
            files = [f for f in os.listdir(mirror) if f != "robots.txt"]
            if not files:
                TG.send(chat_id, "⚠️ সাইট থেকে কিছু ডাউনলোড করা যায়নি।")
                return
            pat = _env("GH_PAT")
            owner = _env("PAGES_OWNER", "sheikhrashel47-stack")
            repo = _env("PAGES_REPO", "rashel-site")
            if not pat:
                TG.send(chat_id, "⚠️ GH_PAT সেট করা নেই (workflow secret)।")
                return
            clone = os.path.join(tmp, "repo")
            subprocess.run(["git", "clone", "-q",
                            f"https://x-access-token:{pat}@github.com/{owner}/{repo}.git", clone],
                           check=True, capture_output=True, timeout=120)
            for n in os.listdir(clone):
                if n != ".git":
                    p = os.path.join(clone, n)
                    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
            shutil.copytree(mirror, clone, dirs_exist_ok=True)
            subprocess.run(["git", "-C", clone, "add", "-A"], check=True, capture_output=True)
            r = subprocess.run(["git", "-C", clone, "-c", "user.name=oh-publish",
                                "-c", "user.email=pub@bot.local", "commit", "-q",
                                "-m", "publish from telegram"], capture_output=True)
            if r.returncode != 0:
                TG.send(chat_id, "👍 কোনো নতুন চেঞ্জ নেই — আগের লিংকই চালু আছে।")
                return
            subprocess.run(["git", "-C", clone, "push", "-q", "origin", "HEAD:main"],
                           check=True, capture_output=True, timeout=180)
            TG.send(chat_id, f"🌐 <b>স্থায়ী লিংক (সবসময় ১ বারেই খোলে):</b>\n"
                             f"https://{owner}.github.io/{repo}/\n"
                             f"(১-২ মিনিটে আপডেট হবে; স্যান্ডবক্স ঘুমালেও মরবে না)")
        except Exception as e:
            TG.send(chat_id, f"⚠️ publish ব্যর্থ: {str(e)[:250]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    threading.Thread(target=work, daemon=True).start()


COMMANDS = {
    "help": cmd_help, "start": cmd_help,
    "github": cmd_github,
    "get": cmd_get, "file": cmd_get, "download": cmd_get,
    "repo": cmd_repo, "repos": cmd_repos,
    "convs": cmd_convs, "list": cmd_convs,
    "use": cmd_use, "status": cmd_status, "link": cmd_link, "watch": cmd_watch,
    "stop": cmd_stop, "pause": cmd_stop, "resume": cmd_resume,
    "quiet": cmd_quiet, "normal": cmd_normal, "verbose": cmd_verbose,
    "card": cmd_card, "cost": cmd_cost, "engine": cmd_engine,
    "changes": cmd_changes, "diff": cmd_diff,
    "publish": cmd_publish,
    "remember": cmd_remember, "brain": cmd_brain,
}


# ====================================================================== #
#  ইউজারের মেসেজ -> এজেন্ট
# ====================================================================== #


def shrink_photo(data: bytes, max_px: int, quality: int) -> tuple[bytes, str]:
    """ছবিকে max_px-এ নামিয়ে JPEG করে দেয় -> base64 ছোট হয় -> এজেন্টের খরচ কমে।

    Pillow না থাকলে (বা ছবি পড়া না গেলে) মূল বাইটই ফিরিয়ে দেয়।
    """
    if _PILImage is None:
        return data, ("png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg")
    try:
        im = _PILImage.open(_io.BytesIO(data))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_px:
            r = max_px / float(max(w, h))
            im = im.resize((max(1, int(w * r)), max(1, int(h * r))), _PILImage.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        return (out, "jpg") if len(out) < len(data) else (data, "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg")
    except Exception as e:  # ছবি পড়া যায়নি -> যেমন আছে তেমনি
        log("shrink_photo failed:", e)
        return data, ("png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg")


def compose_photo_instruction(text: str, b64: str, ext: str) -> str:
    path = f"/workspace/inbox/photo.{ext}"
    user_q = text.strip() or "(ছবিটা দেখে সংক্ষেপে বলো এতে কী আছে)"
    return (
        "The user attached a PHOTO. OpenHands Cloud drops image content over this API, "
        "so the image arrives as base64 text at the end of this message.\n\n"
        "STEP 1 - save it (ONE terminal call, heredoc; do not split it):\n"
        "mkdir -p /workspace/inbox && cat > /workspace/inbox/photo.b64 <<'B64EOF'\n"
        "<<<BASE64>>>\n"
        "B64EOF\n"
        f"base64 -d /workspace/inbox/photo.b64 > {path} && rm -f /workspace/inbox/photo.b64\n\n"
        f"STEP 2 - SEE it with exactly one call: file_editor view {path}\n"
        "(file_editor view renders the image to you - this works. Do NOT try anything else.)\n\n"
        "HARD RULES:\n"
        "- Do NOT install packages, do NOT use PIL/opencv/python for image analysis.\n"
        "- Do NOT use any browser_* tool, do NOT start http.server, do NOT take screenshots.\n"
        "- Do NOT verify hashes/signatures/dimensions, do NOT print or echo the base64.\n"
        "- Maximum TWO tool calls total (step 1 + step 2), then answer.\n\n"
        f"STEP 3 - answer this request about the image, in the user's language, concisely "
        f"(direct answer first, max 6 sentences, no narration of your steps):\n{user_q}"
    ).replace("<<<BASE64>>>", b64)



_WORK_URL_RE = _re_compile = __import__("re").compile(
    r"https?://work-\d+-[\w.\-]+\.prod-runtime\.all-hands\.dev[^\s)\"'`]*")


def link_alive(url: str, timeout: int = 10) -> bool:
    """লিংকটা বাইরে থেকে সত্যিই খোলে কিনা (স্যান্ডবক্স ঘুমালে/সার্ভার মরলে False)।"""
    try:
        r = urllib.request.Request(url, headers={"User-Agent": "oh-tg-bot"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)) < 400
    except Exception:
        return False


_TEXT_EXTS = (".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css",
              ".yml", ".yaml", ".xml", ".log", ".sh", ".ini", ".toml", ".env")


def _looks_text(name: str, data: bytes) -> bool:
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext in _TEXT_EXTS:
        return True
    if b"\x00" in data[:2000]:
        return False
    try:
        data.decode("utf-8")
        return len(data) < 300_000
    except Exception:
        return False


def compose_doc_instruction(text: str, name: str, data: bytes):
    """যেকোনো ফাইল -> এজেন্টের কাছে পৌঁছানোর নির্দেশনা।
    টেক্সট হলে সরাসরি কনটেন্ট, বাইনারি হলে base64 (ছবির মতো)।"""
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", name or "file.bin")[:80] or "file.bin"
    user_q = text.strip() or "(ফাইলটা দেখে সংক্ষেপে বলো এতে কী আছে)"
    if _looks_text(safe, data):
        content = data.decode("utf-8", "replace")[:500_000]
        return (f"The user attached a TEXT FILE named {safe}. Its FULL content is between "
                "the markers below. If you need it as a real file, save it to "
                f"/workspace/inbox/{safe} with file_editor create.\n"
                "Then answer the user's request about it, concisely, in the user's language.\n\n"
                f"---FILEBEGIN {safe}---\n{content}\n---FILEEND---\n\n"
                f"User's message: {user_q}")
    b64 = base64.b64encode(data).decode()
    if len(b64) > CFG.photo_max_b64:
        return None
    return (f"The user attached a BINARY FILE named {safe} ({len(data)} bytes) as base64.\n"
            "STEP 1 - save it (ONE terminal call):\n"
            f"mkdir -p /workspace/inbox && cat > /workspace/inbox/{safe}.b64 <<'B64EOF'\n"
            f"{b64}\nB64EOF\n"
            f"base64 -d /workspace/inbox/{safe}.b64 > /workspace/inbox/{safe} && "
            f"rm -f /workspace/inbox/{safe}.b64\n"
            "STEP 2 - inspect with the right tools (file, unzip -l, pdftotext etc.), max 3 calls.\n"
            f"Then answer concisely in the user's language: {user_q}")


def _verify_run(chat_id: int, cid: str, payload: str, imgs: list | None = None) -> None:
    """send_message চুপচাপ গেলে (run শুরু না হলে) আবার পাঠাই — কিন্তু ওয়াচার আটকে নয়।"""
    t_send = time.time()
    for attempt in range(3):
        for _ in range(6):
            time.sleep(1.5)
            try:
                c2 = oh().get_conversation(cid) or {}
            except OpenHandsError:
                continue
            if (c2.get("execution_status") or "") == "running":
                return
        # এজেন্ট সত্যিই কিছু করেছে কি না (ইভেন্ট থাকলে resend নয়)
        try:
            evs = oh().search_events(cid, limit=5)
            items = evs.get("items") or []
            if items:
                lt = _parse_iso(items[-1].get("timestamp"))
                if lt and lt >= t_send - 2:
                    return
        except OpenHandsError:
            pass
        log(f"agent did not start after send (attempt {attempt + 1}) -> resending")
        try:
            oh().send_message(cid, payload, run=True, image_urls=imgs)
        except OpenHandsError as e:
            log("resend failed:", str(e)[:150])
    TG.send(chat_id, "⚠️ এজেন্ট চালু হতে দেরি করছে — একটু পরে আবার লিখুন অথবা /status দেখুন।")


# ===================== নলেজ-বেস (RAG) + লাইভ-সার্চ ইনজেকশন ===================== #
KB_DIR = "kb"
_FTS = None
_KB_READY = False


def _kb_lazy() -> None:
    global _KB_READY
    if not _KB_READY:
        _KB_READY = True
        threading.Thread(target=kb_reindex, daemon=True).start()


def _kb_extract(name: str, data: bytes) -> str:
    ext = name.lower().rsplit(".", 1)[-1]
    try:
        if ext in ("txt", "md", "csv", "log"):
            return data.decode("utf-8", "replace")
        if ext == "html":
            t = data.decode("utf-8", "replace")
            t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
            return re.sub(r"<[^>]+>", " ", t)
        if ext == "pdf":
            try:
                import pypdf, io as _io
                r = pypdf.PdfReader(_io.BytesIO(data))
                return "\n".join((pg.extract_text() or "") for pg in r.pages[:120])
            except Exception:
                return ""
        if ext == "docx":
            try:
                import docx, io as _io
                d = docx.Document(_io.BytesIO(data))
                return "\n".join(p.text for p in d.paragraphs)
            except Exception:
                return ""
    except Exception:
        return ""
    return ""


def kb_reindex() -> None:
    global _FTS
    import sqlite3, os
    try:
        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.execute("create virtual table kb using fts5(fname, body)")
        n = 0
        if os.path.isdir(KB_DIR):
            for fn in os.listdir(KB_DIR):
                try:
                    body = _kb_extract(fn, open(os.path.join(KB_DIR, fn), "rb").read())
                except Exception:
                    body = ""
                if body.strip():
                    con.execute("insert into kb(fname, body) values(?, ?)", (fn, body[:200000]))
                    n += 1
        _FTS = con
        log(f"[kb] index ready: {n} file")
    except Exception:
        log("[kb] reindex crash:", traceback.format_exc()[:300])
        _FTS = None


def kb_search(q: str, k: int = 3) -> list:
    if _FTS is None or not (q or "").strip():
        return []
    toks = re.findall(r"[\w\u0980-\u09FF]{3,}", q)[:6]
    if not toks:
        return []
    match = " OR ".join(f'"{t}"*' for t in toks)
    try:
        return _FTS.execute(
            "select fname, snippet(kb, 1, '', '', ' … ', 14) from kb where kb match ? "
            "order by rank limit ?", (match, k)).fetchall()
    except Exception:
        return []


def kb_add(chat_id: int, name: str, data: bytes) -> None:
    import os, base64 as _b64
    try:
        os.makedirs(KB_DIR, exist_ok=True)
        safe = re.sub(r"[^\w.\-]", "_", name)[:80]
        with open(os.path.join(KB_DIR, safe), "wb") as f:
            f.write(data)
        try:
            import state_sync
            body = _b64.b64encode(data).decode()
            sha = None
            try:
                sha = (state_sync._req("GET", f"/repos/{state_sync._REPO}/contents/kb/{safe}") or {}).get("sha")
            except Exception:
                sha = None
            bd = {"message": f"kb: add {safe} [skip ci]", "content": body}
            if sha:
                bd["sha"] = sha
            state_sync._req("PUT", f"/repos/{state_sync._REPO}/contents/kb/{safe}", bd)
        except Exception as e:
            log("[kb] vault push failed:", str(e)[:120])
        kb_reindex()
        TG.send(chat_id, f"📚 <b>{safe}</b> নলেজ-বেসে ঢুকে গেছে ✅ — এখন প্রশ্ন করলে উৎসসহ উত্তর দেব।")
    except Exception:
        log("[kb] add crash:", traceback.format_exc()[:300])
        TG.send(chat_id, "⚠️ নলেজ-বেসে ফাইলটা রাখা যায়নি।")


def enrich_outgoing(chat_id: int, text: str) -> str:
    t = text or ""
    pre: list[str] = []
    hits = kb_search(t, 3)
    if hits:
        src = "\n".join(f"[উৎস: {f}] …{sn}…" for f, sn in hits)
        pre.append("[USER-KNOWLEDGE-BASE — ইউজারের সংরক্ষিত ফাইল থেকে প্রাসঙ্গিক অংশ। "
                   "উত্তরে উৎসের ফাইলনাম উল্লেখ করো:\n" + src + "]")
    if SEARCH_RE.search(t):
        pre.append(_LIVE_SEARCH_INSTR)
    return "\n\n".join(pre) + "\n\n" + t if pre else t


def send_to_agent(chat_id: int, text: str, photo_b64: str | None = None,
                  photo_ext: str = "png", doc_name: str | None = None,
                  doc_data: bytes | None = None) -> None:
    st = get_state(chat_id)
    cid = st.get("conversation_id")
    if text and photo_b64 is None and doc_data is None:
        st["last_task"] = text[:2000]
        save_state(chat_id, st)

    if doc_data is not None:
        composed = compose_doc_instruction(text, doc_name or "file.bin", doc_data)
        if composed is None:
            TG.send(chat_id, "⚠️ ফাইলটা এজেন্টের কাছে পাঠানোর মতো নয় (খুব বড়) — ৭০০KB এর কম করে পাঠান।")
            return
        text = composed

    if photo_b64:
        # ভিশন-ক্ষমতা আসে ইঞ্জিন/প্রোফাইল থেকে (BYOK কনভারসেশনে llm_model ফিল্ড ভুল দেখায়)
        prof = (st.get("engine_profile") or CFG.agent_profile_id or "").lower()
        cur = (st.get("model") or "").lower() or prof
        vision_ok = any(v in cur for v in ("gemini", "gpt-4", "gpt-5", "claude", "-vl", "vision"))
        if cid and not vision_ok:
            TG.send(chat_id, "📸 এই কনভারসেশনটা টেক্সট-মডেলে চলছে বলে ছবি দেখতে পাবে না। "
                             "সেশন না ভাঙতে: এখনকার কাজ শেষ হলে <code>/new</code> দিয়ে একবার "
                             "ভিশন মডেলে যান — পরের সব কনভারসেশন নিজে থেকেই ভিশন মডেলে হবে।")
            photo_b64 = None   # ক্যাপশন-টেক্সট swallowed হবে না -> সাধারণ মেসেজ হিসেবে যাবে

    if not cid:
        # কোনো কনভারসেশন নেই -> এই টেক্সট দিয়েই নতুন কনভারসেশন শুরু
        # (ডিফল্ট মডেলই এখন ভিশন-সক্ষম, তাই ছবি/ফাইল সব এক সেশনেই চলবে)
        _do_start_conversation(chat_id, st, text, st.get("repo"), st.get("branch"))
        return

    def work():
        try:
            TG.typing(chat_id)
            conv = oh().get_conversation(cid) or {}
            sandbox = conv.get("sandbox_status")

            if sandbox == "PAUSED":
                sid = conv.get("sandbox_id")
                if sid:
                    try:
                        oh().resume_sandbox(sid)
                        log("sandbox was paused -> resumed")
                        time.sleep(1.5)
                    except OpenHandsError as e:
                        log("resume error:", str(e)[:200])

            # 🧠 ব্রেইন শুধু সেশন জন্মের সময় একবারই — ফলো-আপে আর নয়
            # (প্রতি মেসেজে বিশাল প্রম্পট = ধীর গতি + টোকেন পোড়া; মালিকের নির্দেশ)
            payload = text
            # 👁 ছবি থাকলে: আগে নেটিভ ইমেজ-কনটেন্ট (মডেল সরাসরি দেখে),
            # না চললে পুরনো base64-heredoc ফলব্যাক
            imgs = [f"data:image/{photo_ext};base64,{photo_b64}"] if photo_b64 else None
            send_err = None
            try:
                oh().send_message(cid, payload, run=True, image_urls=imgs)
            except OpenHandsError as e:
                send_err = e
                if imgs and not any(k in str(e) for k in
                                    ("409", "404", "425", "not ready", "STARTING", "MISSING")):
                    payload = compose_photo_instruction(text, photo_b64, photo_ext)
                    imgs = None
                    send_err = None
                    try:
                        oh().send_message(cid, payload, run=True)
                    except OpenHandsError as e2:
                        send_err = e2
            if send_err is not None:
                msg = str(send_err)
                # স্যান্ডবক্স রেডি না / কনভারসেশন শুরু হচ্ছে → pending message কিউ
                if any(k in msg for k in ("409", "404", "425", "not ready", "STARTING", "MISSING")):
                    try:
                        res = oh().queue_pending_message(cid, payload)
                        log("message queued:", res.get("position"))
                        start_watcher(chat_id, cid, st, replace=False, fresh=True)
                        return
                    except OpenHandsError as e2:
                        TG.send(chat_id, f"⚠️ পাঠানো যায়নি:\n<code>{str(e2)[:400]}</code>")
                        return
                low = msg.lower()
                hint = ""
                if "401" in low or "403" in low:
                    hint = "\n👉 API key চেক করুন।"
                elif "stuck" in low or "error" in low:
                    hint = "\n👉 /status দিয়ে দেখুন, প্রয়োজনে /new দিয়ে নতুন কনভারসেশন শুরু করুন।"
                TG.send(chat_id, f"⚠️ মেসেজ পাঠানো যায়নি:\n<code>{msg[:400]}</code>{hint}")
                return

            # ⚡ আগে ওয়াচার (আউটপুট স্ট্রিম শুরু), তারপর ব্যাকগ্রাউন্ডে verify-retry
            with WATCHER_LOCK:
                w = WATCHERS.get(chat_id)
                alive = bool(w and w.cid == cid and w.is_alive())
            if not alive:
                start_watcher(chat_id, cid, st, replace=False, fresh=True)
            threading.Thread(target=_verify_run, args=(chat_id, cid, payload, imgs),
                             daemon=True).start()

        except Exception as e:
            log("send_to_agent crash:", traceback.format_exc()[:600])
            TG.send(chat_id, f"⚠️ সমস্যা: {str(e)[:300]}")

    threading.Thread(target=work, daemon=True).start()


# ====================================================================== #
#  আপডেট ডিসপ্যাচ
# ====================================================================== #


_QUICK_QA = (
    (r"(আমি কে|আমার নাম (কী|কে)|who am i|আমারে চেনো|চিনো আমায়)",
     "আপনি **Rashel Zayan** — Admission candidate, AI দিয়ে web/app development করছেন।\n"
     "আপনার অ্যাপ: **Admission Hub / LaStHand** (GitHub + Cloudflare)।\n"
     "পছন্দ: সহজ গুছানো বাংলা, মোবাইল-ফ্রেন্ডলি, নয়েজ-ফ্রি উত্তর। 🧠 (ব্রেইন থেকে তাৎক্ষণিক)",
     False),
    (r"(তুমি কে|তোমার নাম (কী|কে)|your name|who are you)",
     "আমি আপনার OpenHands এজেন্ট — Telegram bridge সহ। আমার ব্রেইনে আপনার পরিচিতি, "
     "পছন্দ আর Admission Hub প্রজেক্টের পুরো ইতিহাস আছে। বলুন, কী করি? ⚡",
     False),
    (r"(হ্যালো|হাই|সালাম|আসসালামু|hello|hi|hey)",
     "হাই ভাই! ⚡ একদম প্রস্তুত — কী করতে বলবেন? (সাইট, PDF, কোড, MCQ — সব হবে)",
     True),
    (r"(কেমন আছো|kemon acho|how are you)",
     "একদম ফুরফুরে! 😄⚡ আপনি কেমন আছেন? কী করব আজ?",
     True),
    (r"(ধন্যবাদ|thanks|thank you|শুকরিয়া)",
     "স্বাগতম ভাই! 🙏 আর কিছু লাগলে বলবেন — আমি আছি।",
     True),
)


def brain_quick_answer(text: str):
    """সহজ চ্যাট/পরিচিতি প্রশ্নের তাৎক্ষণিক উত্তর (এজেন্ট-লুপ ছাড়া, <১ সেকেন্ড)।
    full=True প্যাটার্ন শুধু খাঁটি ছোট মেসেজে ধরে — কাজের মেসেজ এজেন্টেই যাবে।"""
    import re as _r
    t = (text or "").strip()
    if not t or len(t) > 90:
        return None
    for pat, ans, full in _QUICK_QA:
        if full:
            if _r.fullmatch(pat + r"[!।?.\s~]*", t, _r.I):
                return ans
        elif _r.search(pat, t, _r.I):
            return ans
    return None


# ====================================================================== #
#  ⏰ শিডিউলার — রিমাইন্ডার/সময়-নির্ভর কাজ (বটের নিজের ঘড়ি; এজেন্টের ঘড়ি নেই)
# ====================================================================== #
def add_job(chat_id: int, due: float, kind: str, payload: str) -> int:
    with DB_LOCK:
        cur = db().execute("INSERT INTO jobs (chat_id,due,kind,payload) VALUES (?,?,?,?)",
                           (chat_id, due, kind, payload))
        db().commit()
        return int(cur.lastrowid)


_NEXT_WARM = [0.0]


def _keep_warm() -> None:
    """খালি স্যান্ডবক্স ঘুমিয়ে পড়লে আগেভাগে জাগিয়ে রাখি — যেন ইউজারের মেসেজে
    ১৫-০ সে রিজিউম-দেরি না লাগে। প্রতি ২ মিনিটে একবার।"""
    if time.time() < _NEXT_WARM[0]:
        return
    _NEXT_WARM[0] = time.time() + 120
    try:
        rows = db().execute("SELECT chat_id, conversation_id FROM kv "
                            "WHERE conversation_id IS NOT NULL").fetchall()
    except Exception:
        return
    for _chat_id, cid in rows:
        try:
            conv = oh().get_conversation(cid) or {}
            if conv.get("sandbox_status") == "PAUSED" and conv.get("sandbox_id"):
                oh().resume_sandbox(conv["sandbox_id"])
                log(f"keep-warm: resumed {cid[:8]}")
        except Exception as e:
            log("keep-warm error:", str(e)[:120])


def _scheduler_loop() -> None:
    while not STOP.is_set():
        try:
            _keep_warm()
        except Exception as e:
            log("keep-warm crash:", str(e)[:120])
        rows = []
        try:
            now = time.time()
            with DB_LOCK:
                rows = db().execute(
                    "SELECT id,chat_id,kind,payload FROM jobs WHERE done=0 AND due<=?",
                    (now,)).fetchall()
                if rows:
                    db().executemany("UPDATE jobs SET done=1 WHERE id=?",
                                     [(r[0],) for r in rows])
                    db().commit()
            for _jid, chat_id, kind, payload in rows:
                try:
                    if kind == "msg":
                        TG.send(chat_id, payload)
                    else:
                        send_to_agent(chat_id, payload)
                except Exception as e:
                    log("job exec failed:", str(e)[:150])
            if rows:
                persist_state_soon(10)
        except Exception as e:
            log("scheduler error:", str(e)[:150])
        STOP.wait(10)


def _bn_digits(s: str) -> str:
    """বাংলা সংখ্যা (০-৯) -> ইংরেজি (0-9); বাকি টেক্সট অপরিবর্তিত।"""
    if not s:
        return s
    return s.translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))


def cmd_remind(chat_id: int, args: str, st: dict) -> None:
    """/remind ৫ <টেক্সট> -> ৫ মিনিট পর রিমাইন্ডার (বটের নিজের অ্যালার্ম)।"""
    import re as _re
    raw = args or ""
    m = _re.match(r"\s*(\d+)\s*(?:মিনিট|minutes?|min)?\s*(.*)$", _bn_digits(raw), _re.I)
    if not m or not m.group(1):
        TG.send(chat_id, "ব্যবহার: <code>/remind ৫ দুধ আনতে বলো</code>")
        return
    mins = int(m.group(1))
    rest = (m.group(2) or "").strip() or "হাই ভাই! 👋"
    due = time.time() + mins * 60
    add_job(chat_id, due, "msg", md_to_telegram(f"⏰ রিমাইন্ডার ({mins} মিনিট পর):\n{rest}"))
    TG.send(chat_id, f"⏰ সেট হলো — {time.strftime('%H:%M', time.localtime(due))}-এ: {rest}")


COMMANDS["remind"] = cmd_remind
COMMANDS["alarm"] = cmd_remind


def handle_update(update: dict) -> None:
    _kb_lazy()
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return

    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user = msg.get("from") or {}
    if not chat_id:
        return

    if not allowed(user):
        TG.send(chat_id, "⛔ দুঃখিত, আপনি এই বট ব্যবহারের অনুমতি পাওয়া নেই।\n"
                         f"আপনার ID: <code>{user.get('id')}</code> — অ্যাডমিনকে ALLOWED_TELEGRAM_IDS এ যোগ করতে বলুন।")
        return

    st = get_state(chat_id)

    # ছবি/ডকুমেন্ট
    photo_b64 = None
    photo_ext = "jpg"
    doc_name = None
    doc_data = None
    if msg.get("photo"):
        try:
            photo = msg["photo"][-1]
            name, data = TG.download(photo["file_id"])
            if not data:
                raise RuntimeError("empty")
            data, photo_ext = shrink_photo(data, CFG.photo_max_px, CFG.photo_quality)
            photo_b64 = base64.b64encode(data).decode()
            if len(photo_b64) > CFG.photo_max_b64:
                photo_b64 = None
                TG.send(chat_id, "⚠️ ছবিটা অনেক বড় — একটু ছোট/কম রেজল্যুশনের ছবি পাঠান।")
                return
        except Exception as e:
            log("photo download failed:", e)
            TG.send(chat_id, "⚠️ ছবি ডাউনলোড করা যায়নি।")
            return
    elif msg.get("document"):
        # ছবি "ফাইল" হিসেবে পাঠালেও এজেন্টকে দেখানো হবে
        doc = msg["document"]
        mime = str(doc.get("mime_type") or "")
        if mime.startswith("image/") and int(doc.get("file_size") or 0) < 12_000_000:
            try:
                name, data = TG.download(doc["file_id"])
                if data:
                    data, photo_ext = shrink_photo(data, CFG.photo_max_px, CFG.photo_quality)
                    b64 = base64.b64encode(data).decode()
                    if len(b64) <= CFG.photo_max_b64:
                        photo_b64 = b64
                    else:
                        TG.send(chat_id, "⚠️ ছবিটা অনেক বড় — ছোট করে পাঠান।")
                        return
            except Exception as e:
                log("document-image download failed:", e)
                TG.send(chat_id, "⚠️ ছবিটা ডাউনলোড করা যায়নি।")
                return
        else:
            # যেকোনো ফাইল (txt/pdf/zip/…) -> এজেন্টের inbox-এ পৌঁছে দেব
            try:
                name, data = TG.download(doc["file_id"])
                if data:
                    doc_name, doc_data = name, data
            except Exception as e:
                log("document download failed:", e)
                TG.send(chat_id, "⚠️ ফাইলটা ডাউনলোড করা যায়নি।")
                return

    text = msg.get("text") or msg.get("caption") or ""

    # 📚 /kb — ফাইল নলেজ-বেসে সংরক্ষণ (ভল্ট + ইনডেক্স)
    if doc_data and (text or "").strip().lower().startswith("/kb"):
        threading.Thread(target=kb_add, args=(chat_id, doc_name or "file.txt", doc_data),
                         daemon=True).start()
        return

    # 📝 সংশোধন ডিটেক্ট -> নিয়ম ব্রেইনে (এজেন্টের পরের উত্তরটা নিয়ম হিসেবে ধরা হবে)
    if (text and len(text) < 60
            and re.search(r"(ভুল|ঠিক না|হয়নি|এটা না|সত্যি না)", text)
            and not re.search(r"(ঠিক কর|রিমুভ|বানা|করো|fix|remove|build|সরা)", text)):
        with WATCHER_LOCK:
            w = WATCHERS.get(chat_id)
        if w is not None and not w.capture_mode:
            w.capture_mode = "rule"
            w.capture_deadline = time.time() + 150
            try:
                oh().send_message(w.cid, _RULE_TEXT.format(user=text[:150]), run=True)
                TG.send(chat_id, "📝 বুঝেছি ভাই — নিয়মটা ব্রেইনে লিখিয়ে নিচ্ছি, যাতে ভুলটা আর না হয়। "
                                 "আবার করাতে চাইলে বলো \"আবার করো\"।")
                return
            except Exception as e:
                log("rule capture send failed:", str(e)[:120])
                w.capture_mode = ""

    # কমান্ড?
    if text.startswith("/"):
        first_line = text.split("\n", 1)[0]
        head = first_line.split(" ", 1)[0]
        cmd = head[1:].split("@")[0].lower()
        # কমান্ডের নাম বাদে বাকি পুরো টেক্সটই আর্গুমেন্ট (নতুন লাইনসহ)
        rest = text[len(head):].strip()

        if cmd == "new":
            PENDING_NEW.pop(chat_id, None)
            cmd_new(chat_id, rest, st)
            return
        if cmd == "id":
            cmd_id(chat_id, rest, st, user=user)
            return
        handler = COMMANDS.get(cmd)
        if handler:
            try:
                handler(chat_id, rest, st)
            except Exception as e:
                log("command crash:", traceback.format_exc()[:600])
                TG.send(chat_id, f"⚠️ কমান্ড ব্যর্থ: {str(e)[:300]}")
        else:
            TG.send(chat_id, f"অচেনা কমান্ড <code>/{cmd}</code>। /help দেখুন।")
        return

    # /new এর পরের মেসেজ = টাস্ক
    pend = PENDING_NEW.pop(chat_id, None)
    if pend and time.time() - pend["ts"] < 600:
        _do_start_conversation(chat_id, st, text, pend.get("repo"), pend.get("branch"))
        return

    if not text.strip() and not photo_b64:
        return

    # ⚡ তাৎক্ষণিক ব্রেইন-উত্তর: সহজ চ্যাটে এজেন্ট-লুপের অপেক্ষা নয়
    if not photo_b64 and doc_data is None:
        quick = brain_quick_answer(text)
        if quick:
            TG.send(chat_id, md_to_telegram(quick))
            return

        # ⏰ সময়-নির্ভর অনুরোধ (রিমাইন্ডার/পরে বলো) -> বটের নিজের অ্যালার্ম
        import re as _re
        _t = text or ""
        mm = _re.search(r"(\d+)\s*(মিনিট|minutes?|min|ঘণ্টা?|hours?|hr)", _bn_digits(_t), _re.I)
        rem = _re.search(r"(রিমাইন্ড|মনে করি|বলিও|বোলো|হাই বল|খবর দিও|জানিও|reminder|remind)", _t, _re.I)
        if mm and rem and not _t.lstrip().startswith("/"):
            mins = int(mm.group(1))
            if _bn_digits(mm.group(2) or "").lower().startswith(("ঘ", "h")):
                mins *= 60
            if 0 < mins <= 720:
                due = time.time() + mins * 60
                rest = (text or "")[mm.end():].strip(" -–,।")
                payload = f"⏰ রিমাইন্ডার ({mins} মিনিট পর, আপনার কথামতো):\n" + (rest or "হাই ভাই! 👋")
                add_job(chat_id, due, "msg", md_to_telegram(payload))
                TG.send(chat_id, f"⏰ ঠিক আছে ভাই — <b>{time.strftime('%H:%M', time.localtime(due))}</b>-এ "
                                 "মনিয়ে করিয়ে দেব (বটের নিজের অ্যালার্ম — স্যান্ডবক্স ঘুমালেও থামবে না)")
                return

    text = enrich_outgoing(chat_id, text)
    try:
        TG.typing(chat_id)
        st = get_state(chat_id)
        if st.get("conversation_id"):
            wake_sandbox(st["conversation_id"])
    except Exception as e:
        log("pre-wake failed:", str(e)[:120])
    send_to_agent(chat_id, text, photo_b64=photo_b64, photo_ext=photo_ext,
                  doc_name=doc_name, doc_data=doc_data)


def handle_callback(cq: dict) -> None:
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    user = cq.get("from") or {}
    if not chat_id:
        return
    if not allowed(user):
        TG.call("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                         "text": "অনুমতি নেই", "show_alert": True})
        return
    TG.call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
    st = get_state(chat_id)

    if data.startswith("use:"):
        cmd_use(chat_id, data[4:], st)
    elif data.startswith("repo:"):
        cmd_repo(chat_id, data[5:], st)
    elif data == "help":
        cmd_help(chat_id, "", st)


# ====================================================================== #
#  মেইন লুপ (long polling)
# ====================================================================== #


# ====================================================================== #
#  Health endpoint (Hugging Face Spaces / Koyeb-এর মতো প্ল্যাটফর্মের জন্য)
# ====================================================================== #


def _health_port() -> int:
    port = _env_int("HEALTH_PORT", 0)
    if port:
        return port
    # Hugging Face Space হলে SPACE_ID সেট থাকে, পোর্ট PORT env এ আসে
    if _env("SPACE_ID") or _env("HF_SPACE"):
        return _env_int("PORT", 7860)
    return 0


def start_health_server() -> None:
    port = _health_port()
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({
                "status": "ok",
                "bot": "openhands-telegram-bridge",
                "uptime_sec": int(time.time() - START_TIME),
                "watchers": len(WATCHERS),
                "hint": "এই বট টেলিগ্রামে চলে — এখানে কিছু নেই।",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def run():
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            log(f"health endpoint চালু: 0.0.0.0:{port}")
            srv.serve_forever()
        except Exception as e:
            log("health server ব্যর্থ:", e)

    threading.Thread(target=run, daemon=True).start()


START_TIME = time.time()


def restore_watchers() -> None:
    """বট রিস্টার্ট হলে আগের সক্রিয় কনভারসেশনগুলোর ওয়াচার আবার চালু করা।"""
    if _env_bool("AUTORESTORE", True) is False:
        return
    try:
        rows = db().execute("SELECT chat_id, conversation_id, verbosity, live_card FROM kv "
                            "WHERE conversation_id IS NOT NULL").fetchall()
    except Exception:
        rows = []
    for chat_id, cid, verbosity, live in rows:
        if CFG.allowed_ids and int(chat_id) not in CFG.allowed_ids:
            continue
        st = get_state(int(chat_id))
        try:
            conv = oh().get_conversation(cid) or {}
        except Exception:
            continue
        if (conv.get("execution_status") or "") == "running":
            start_watcher(int(chat_id), cid, st)
            log(f"restored watcher for chat {chat_id}")


def sync_kv_with_env() -> None:
    """env/config.env-তে VERBOSITY বা LIVE_CARD স্পষ্ট দেওয়া থাকলে সব সংরক্ষিত
    চ্যাটেও তা বসিয়ে দিই — নইলে পুরনো row (verbosity=normal, live_card=1)
    নয়েজ/ডুপ্লিকেট দেখাতেই থাকে। /quiet কমান্ড রানটা চলার সময় আবার কাজ করবে।"""
    sets, vals = [], []
    if "VERBOSITY" in _ENV_KEYS_EXPLICIT:
        sets.append("verbosity=?")
        vals.append(CFG.default_verbosity)
    if "LIVE_CARD" in _ENV_KEYS_EXPLICIT:
        sets.append("live_card=?")
        vals.append(1 if CFG.use_live_card else 0)
    if not sets:
        return
    try:
        db().execute(f"UPDATE kv SET {', '.join(sets)}", tuple(vals))
        db().commit()
        log("saved chats synced with env config:", ", ".join(sets))
    except Exception as e:
        log("kv sync failed:", e)



def _start_keepalive() -> None:
    port = _env("PORT")
    if port:
        import http.server

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        def _serve():
            try:
                http.server.ThreadingHTTPServer(("0.0.0.0", int(port)), _H).serve_forever()
            except Exception as e:
                log("keepalive server err:", str(e)[:120])

        threading.Thread(target=_serve, daemon=True, name="keepalive").start()
        log(f"keepalive HTTP listening on :{port}")
    url = _env("KEEPALIVE_URL")
    if url:
        def _ping():
            while not STOP.wait(360):
                try:
                    urllib.request.urlopen(
                        urllib.request.Request(url, headers={"User-Agent": "self-ping"}),
                        timeout=20).read()
                except Exception:
                    pass
        threading.Thread(target=_ping, daemon=True, name="self-ping").start()
        log("self-ping keepalive scheduled (6 min)")


def main() -> int:
    if not CFG.telegram_token:
        print("❌ TELEGRAM_BOT_TOKEN নেই। BotFather থেকে টোকেন নিয়ে config.env বা env এ সেট করুন।")
        return 2
    if not CFG.oh_api_key:
        print("❌ OPENHANDS_API_KEY নেই। app.all-hands.dev → Settings → API Keys থেকে কী নিয়ে সেট করুন।")
        return 2
    if TG is None:
        print("❌ Telegram ক্লায়েন্ট তৈরি হয়নি।")
        return 2

    # 🌐 Render-স্টাইল ফ্রি হোস্ট: PORT থাকলে ছোট্ট HTTP সার্ভার (ঘুম-প্রতিরোধ)
    # + KEEPALIVE_URL থাকলে নিজেই নিজেকে প্রতি ৬ মিনিটে ping করে জাগিয়ে রাখে
    _start_keepalive()


    def _sig(_signum, _frame):
        log("শাটডাউন সিগনাল পেয়েছি…")
        STOP.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _sig)
        except Exception:
            pass

    # API key যাচাই
    try:
        convs = oh().search_conversations(limit=1)
        log(f"OpenHands API সংযুক্ত ✅  (আগের কনভারসেশন: {len(convs)}টা দেখা যাচ্ছে)")
    except OpenHandsError as e:
        log(f"⚠️ OpenHands API যাচাই ব্যর্থ: {str(e)[:300]}")
        if "401" in str(e) or "403" in str(e):
            print("❌ API key কাজ করছে না — config.env চেক করুন।")
            return 2
    except Exception as e:
        log("⚠️ API যাচাইয়ে সমস্যা:", e)

    try:
        info = TG.call("getMe", {}, timeout=30)
        log(f"Telegram বট সংযুক্ত ✅  @{info.get('username')}")
    except Exception as e:
        print(f"❌ টেলিগ্রাম টোকেন কাজ করছে না: {e}")
        return 2

    sync_kv_with_env()
    start_health_server()
    threading.Thread(target=_scheduler_loop, daemon=True, name='scheduler').start()
    restore_watchers()
    threading.Thread(target=_keep_awake_loop, daemon=True, name="keep-awake").start()

    offset = int(misc_get("tg_offset") or 0)
    log("বট চালু হয়েছে — মেসেজের অপেক্ষায়…  (বন্ধ করতে Ctrl+C)")
    backoff = 1.0
    while not STOP.is_set():
        try:
            updates = TG.call("getUpdates", {
                "offset": offset, "timeout": 50,
                "allowed_updates": ["message", "edited_message", "callback_query"],
            }, timeout=60)
            backoff = 1.0
            if not isinstance(updates, list):
                updates = []
            for upd in updates:
                offset = max(offset, int(upd.get("update_id") or 0) + 1)
                threading.Thread(target=_safe_handle, args=(upd,), daemon=True).start()
            misc_set("tg_offset", str(offset))
            if not updates:
                continue
        except TelegramAPIError as e:
            log("getUpdates error:", e.description[:200])
            if STOP.wait(backoff):
                break
            backoff = min(backoff * 2, 30)
        except Exception as e:
            log("poll error:", e)
            if STOP.wait(backoff):
                break
            backoff = min(backoff * 2, 30)

    for w in list(WATCHERS.values()):
        w.stop()
    log("বাই 👋")
    return 0


def wake_sandbox(cid: str) -> None:
    """স্যান্ডবক্স ঘুমিয়ে থাকলে জাগাও — যাতে এজেন্ট সাথে সাথে কাজ শুরু করে।"""
    try:
        conv = oh().get_conversation(cid) or {}
        if (conv.get("sandbox_status") or "") == "PAUSED":
            sid = conv.get("sandbox_id")
            if sid:
                oh().resume_sandbox(sid)
                log(f"[keep-awake] sandbox resumed for {cid[:8]}")
    except Exception as e:
        log("wake_sandbox failed:", str(e)[:120])


def _keep_awake_loop() -> None:
    """প্রতি ৪ মিনিটে অ্যাকটিভ কনভারসেশনের স্যান্ডবক্স জাগিয়ে রাখি — ইউজার মেসেজ
    দিলে এজেন্ট যেন ঘুম থেকে না জাগে, আগে থেকেই জেগে থাকে।"""
    while not STOP.is_set():
        if STOP.wait(240):
            return
        try:
            rows = list(db().execute("select conversation_id from kv"))
            for (cid,) in rows:
                if not cid:
                    continue
                wake_sandbox(cid)
        except Exception as e:
            log("keep-awake loop err:", str(e)[:120])


def _safe_handle(upd: dict) -> None:
    try:
        handle_update(upd)
    except Exception:
        log("handler crash:", traceback.format_exc()[:800])
        try:
            m = (upd or {}).get("message") or {}
            cid_chat = m.get("chat", {}).get("id")
            if cid_chat:
                TG.send(cid_chat, "⚠️ অভ্যন্তরীণ গলিচ ছিল — মেসেজটা ধরা হয়েছে, আবার পাঠাতে হবে না।")
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
