"""ঐচ্ছিক state.git-sync: GitHub Actions-এর বাইরে (যেমন HF Spaces) চললে
state.db একটা প্রাইভেট রিপোতে ব্যাকআপ হয় — রিস্টার্টে সেশন/অফসেট হারায় না।"""
from __future__ import annotations

import atexit
import base64
import json
import threading
import time
import urllib.error
import urllib.request

_PAT = ""
_REPO = ""
_FILES: list[tuple[str, str]] = []
_STOP = threading.Event()
_LAST: dict[str, str] = {}


def configure(pat: str, repo: str, db_path: str) -> None:
    global _PAT, _REPO, _FILES
    _PAT = pat or ""
    _REPO = repo or ""
    _FILES = [("state.db", db_path or "state.db"), ("BRAIN.md", "BRAIN.md")]


def enabled() -> bool:
    return bool(_PAT and _REPO)


def _req(method: str, path: str, body: dict | None = None):
    r = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        headers={"Authorization": f"Bearer {_PAT}", "User-Agent": "oh-tg-sync",
                 "Accept": "application/vnd.github+json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(r, timeout=40) as resp:
        return json.load(resp)


def pull() -> None:
    if not enabled():
        return
    for path, local in _FILES:
        try:
            d = _req("GET", f"/repos/{_REPO}/contents/{path}")
            data = base64.b64decode(d.get("content") or "")
            if data:
                with open(local, "wb") as f:
                    f.write(data)
                print(f"[state-sync] pulled {path} ({len(data)}B)")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                print("[state-sync] pull err:", path, e.code)
        except Exception as e:
            print("[state-sync] pull err:", path, str(e)[:120])


def push() -> None:
    if not enabled():
        return
    import hashlib, os
    for path, local in _FILES:
        try:
            if not os.path.exists(local):
                continue
            with open(local, "rb") as f:
                raw = f.read()
            h = hashlib.md5(raw).hexdigest()
            if _LAST.get(path) == h:
                continue
            data = base64.b64encode(raw).decode()
            sha = None
            try:
                sha = (_req("GET", f"/repos/{_REPO}/contents/{path}") or {}).get("sha")
            except urllib.error.HTTPError:
                sha = None
            body = {"message": f"chore: sync {path} [skip ci]", "content": data}
            if sha:
                body["sha"] = sha
            _req("PUT", f"/repos/{_REPO}/contents/{path}", body)
            _LAST[path] = h
        except Exception as e:
            print("[state-sync] push err:", path, str(e)[:120])


def _loop() -> None:
    while not _STOP.wait(300):
        push()


def start() -> None:
    if not enabled():
        return
    pull()
    push()          # স্টার্টআপেই একবার -> "বট জীবিত" প্রমাণ
    threading.Thread(target=_loop, daemon=True, name="state-sync").start()
    atexit.register(push)
