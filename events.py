"""
events.py
---------
OpenHands Cloud থেকে আসা ইভেন্ট (JSON) -> টেলিগ্রামে পাঠানোর মতো সুন্দর HTML টেক্সট।

ইভেন্টগুলোর shape OpenHands SDK-এর event মডেল অনুযায়ী:
  MessageEvent                  -> এজেন্টের কথা / ইউজারের কথা
  ActionEvent                   -> এজেন্ট কোন টুল কল করল (thought, tool_name, tool_call.arguments, summary)
  ObservationEvent              -> টুলের ফলাফল (টার্মিনাল আউটপুট, ফাইল কনটেন্ট ইত্যাদি)
  AgentErrorEvent               -> এজেন্ট লেভেল এরর
  ConversationErrorEvent        -> কনভারসেশন লেভেল এরর
  ServerErrorEvent              -> সার্ভার/ইনফ্রা এরর
  ConversationStateUpdateEvent  -> স্টেট আপডেট (verbose মোডে দেখানো হয়)
  CondensationSummaryEvent      -> কনটেক্সট সংক্ষিপ্তকরণের সারাংশ

verbosity:
  "quiet"   -> শুধু এজেন্টের মেসেজ + এরর
  "normal"  -> + প্রতিটি টুল কলের এক-লাইন সামারি ও ছোট করে আউটপুট
  "verbose" -> + পূর্ণ আর্গুমেন্ট, পূর্ণ আউটপুট (কাটা), স্টেট আপডেট
"""

from __future__ import annotations

import html
import json

# টেলিগ্রামে এক মেসেজে সর্বোচ্চ 4096 ক্যারেক্টার
TG_LIMIT = 4096

TRUNC = {
    "quiet": 0,
    "normal": 900,
    "verbose": 3000,
}


def esc(text) -> str:
    """Telegram HTML parse mode অনুযায়ী escape।"""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    return html.escape(text, quote=False)


def split_message(text: str, limit: int = TG_LIMIT) -> list:
    """লম্বা টেক্সটকে টেলিগ্রাম-সীমার মধ্যে টুকরো করে (লাইন ভাঙা বাঁচিয়ে)।"""
    text = (text or "").rstrip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks or [text[:limit]]


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… (+{len(text) - limit} chars)"


# ---------------------------------------------------------------------- #
#  content এক্সট্রাক্টর — নানা shape সামলায়
# ---------------------------------------------------------------------- #
def extract_text(obj) -> str:
    """
    OpenHands ইভেন্টের কনটেন্ট থেকে প্লেইন টেক্সট বের করে।
    সম্ভাব্য shape:
      "hello"
      [{"type":"text","text":"hello"}, {"type":"image", ...}]
      {"role":"assistant","content":[...]}
      {"outputs":[{"content":[...]}]}
      {"content":"..."} / {"text":"..."} / {"stdout":"..."}
    """
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        parts = []
        for item in obj:
            t = extract_text(item)
            if t:
                parts.append(t)
        return "\n".join(parts)
    if isinstance(obj, dict):
        kind = obj.get("kind") or obj.get("type")
        if kind == "image" or "image_urls" in obj:
            return ""
        # LLM কনটেন্ট-ব্লক: {"type":"text","text":""} জাতীয় -> খালি হলে খালিই,
        # কখনো raw JSON ডেলিভারি দেওয়া যাবে না
        if "type" in obj:
            if obj.get("type") == "text":
                return str(obj.get("text") or "")
            return ""
        for key in ("text", "content", "outputs", "output", "stdout", "result",
                    "observation", "llm_message", "message", "detail", "error",
                    "summary", "thought", "response", "file_text", "old_str"):
            if key in obj:
                t = extract_text(obj[key])
                if t:
                    return t
        return ""
    return str(obj)


def _code_block(text: str, limit: int) -> str:
    text = _truncate((text or "").strip("\n"), limit)
    if not text:
        return ""
    # ভেতরে </pre> থাকলে ভেঙে যাবে না
    text = text.replace("</pre>", "<\\/pre>")
    return f"<pre>{esc(text)}</pre>"


def _pretty_args(raw: str, limit: int) -> str:
    """tool_call.arguments হচ্ছে JSON স্ট্রিং — সুন্দর করে দেখানো।"""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except Exception:
        return _code_block(raw, limit)
    if isinstance(parsed, dict):
        # খুব লম্বা ভ্যালু সংক্ষিপ্ত করা
        short = {}
        for k, v in parsed.items():
            if isinstance(v, str) and len(v) > 400:
                short[k] = v[:400] + f"…(+{len(v) - 400})"
            else:
                short[k] = v
        body = json.dumps(short, ensure_ascii=False, indent=2)
    else:
        body = json.dumps(parsed, ensure_ascii=False, indent=2)
    return _code_block(body, limit)


# ---------------------------------------------------------------------- #
#  মেইন রেন্ডারার
# ---------------------------------------------------------------------- #
def render_event(ev: dict, verbosity: str = "normal"):
    """
    একটি ইভেন্ট -> list[str] (টেলিগ্রামে পাঠানোর মতো HTML টেক্সট)।
    দেখানোর মতো না হলে খালি list রিটার্ন করে।
    """
    if not isinstance(ev, dict):
        return []
    kind = ev.get("kind") or ev.get("type") or ""
    source = (ev.get("source") or "").lower()
    limit = TRUNC.get(verbosity, 900)

    # ---- এজেন্টের/ইউজারের মেসেজ ----
    if kind == "MessageEvent":
        text = extract_text(ev.get("llm_message")) or extract_text(ev.get("extended_content"))
        text = text.strip()
        if not text:
            return []
        if source == "agent":
            body = esc(text)
            return split_message(f"🤖 <b>Agent</b>\n{body}")
        if source == "user":
            # ইউজারের মেসেজ টেলিগ্রাম থেকেই গেছে, ইকো করার দরকার নেই
            return []
        if source == "environment":
            if verbosity == "quiet":
                return []
            return split_message(f"ℹ️ {_code_block(text, limit)}")
        return split_message(f"ℹ️ {esc(text)}")

    # ---- টুল কল (এজেন্ট কী করছে) ----
    if kind == "ActionEvent":
        if verbosity == "quiet":
            return []
        tool = ev.get("tool_name") or "action"
        summary = (ev.get("summary") or "").strip()
        thought = extract_text(ev.get("thought")).strip()
        tc = ev.get("tool_call") or {}
        args_raw = tc.get("arguments") if isinstance(tc, dict) else None

        header = f"🔧 <code>{esc(tool)}</code>"
        if summary:
            header += f" — {esc(summary)}"
        risk = ev.get("security_risk")
        if risk and risk not in ("UNKNOWN", "LOW"):
            header += f" ⚠️<i>{esc(risk)}</i>"

        parts = [header]
        if thought and verbosity == "verbose":
            parts.append("💭 " + esc(_truncate(thought, 600)))
        if args_raw and verbosity == "verbose":
            args_block = _pretty_args(args_raw, limit)
            if args_block:
                parts.append(args_block)
        return split_message("\n".join(parts))

    # ---- টুলের ফলাফল ----
    if kind == "ObservationEvent":
        if verbosity == "quiet":
            return []
        tool = ev.get("tool_name") or "observation"
        obs = ev.get("observation")
        text = extract_text(obs).strip()
        is_error = bool(isinstance(obs, dict) and obs.get("is_error"))
        if not text:
            return []
        icon = "❌" if is_error else "📄"
        block = _code_block(text, limit)
        if not block:
            return []
        return split_message(f"{icon} <i>{esc(tool)}</i>\n{block}")

    # ---- এরর ----
    if kind in ("AgentErrorEvent", "ConversationErrorEvent", "ServerErrorEvent"):
        msg = ev.get("error") or ev.get("detail") or ""
        code = ev.get("code") or ""
        head = {"AgentErrorEvent": "🚫 Agent error",
                "ConversationErrorEvent": "🛑 Conversation error",
                "ServerErrorEvent": "💥 Server error"}.get(kind, "❗ Error")
        out = f"<b>{head}</b>"
        if code:
            out += f" (<code>{esc(code)}</code>)"
        if msg:
            out += "\n" + _code_block(str(msg), max(limit, 1200))
        return split_message(out)

    # ---- কনটেক্সট সংক্ষিপ্তকরণ ----
    if kind == "CondensationSummaryEvent":
        if verbosity == "quiet":
            return []
        text = extract_text(ev.get("summary")) or extract_text(ev.get("condensed_content"))
        if not text.strip():
            return []
        return split_message(f"🗜 <b>Context condensed</b>\n{_code_block(text, limit)}")
    if kind in ("Condensation", "CondensationRequest"):
        return [ "🗜 <i>Condensing conversation history…</i>" ] if verbosity == "verbose" else []

    # ---- স্টেট আপডেট ----
    if kind == "ConversationStateUpdateEvent":
        if verbosity != "verbose":
            return []
        key = ev.get("key")
        # full_state ইভেন্টে পুরো এজেন্ট কনফিগ থাকে — বিশাল ও অদরকারি
        if key in ("full_state", "last_user_message_id", "stats", "agent"):
            return []
        val = ev.get("value")
        if key is None and val is None:
            return []
        sval = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        return split_message(f"🔄 <code>{esc(str(key))}</code> = <code>{esc(_truncate(sval, 400))}</code>")

    # ---- বিরতি / স্টপ ----
    if kind == "InterruptEvent":
        return ["⏹ <i>Interrupt received</i>"] if verbosity != "quiet" else []
    if kind == "PauseEvent":
        return ["⏸ <i>Agent paused</i>"] if verbosity != "quiet" else []
    if kind == "UserRejectObservation":
        return ["🙅 <i>Action rejected</i>"] if verbosity != "quiet" else []

    # ---- বাকিগুলো (SystemPrompt, Token, LLMCompletionLog, ACPToolCall, Hook...) ----
    return []


# ---------------------------------------------------------------------- #
#  লাইভ কার্ড: চলমান ধাপগুলো এক মেসেজে এডিট করে দেখানোর জন্য
# ---------------------------------------------------------------------- #
def build_live_card(steps: list, last_agent_text: str, status_line: str, max_len: int = 3600) -> str:
    """
    steps: ["🔧 execute_bash — running tests", "📄 ...", ...] (ছোট ছোট লাইন, প্লেইন টেক্সট)
    """
    head = f"⚡ <b>Working…</b>  {esc(status_line)}\n"
    tail = ""
    body_steps = [esc(s) for s in steps]

    # শেষ এজেন্ট টেক্সট থাকলে সেটা দেখানো
    if last_agent_text:
        tail = "\n" + esc(_truncate(last_agent_text.strip(), 1200))

    # সীমার মধ্যে রাখতে পুরনো ধাপ বাদ
    while body_steps and len(head) + sum(len(s) + 1 for s in body_steps) + len(tail) > max_len:
        body_steps.pop(0)
        if body_steps:
            body_steps[0] = "… " + body_steps[0]

    body = "\n".join(body_steps)
    return split_message(head + body + tail, TG_LIMIT)[0]


def plain_step(ev: dict, max_len: int = 140) -> str:
    """ইভেন্ট থেকে লাইভ কার্ডের জন্য এক-লাইনের প্লেইন টেক্সট।"""
    kind = ev.get("kind") or ""
    if kind == "ActionEvent":
        tool = ev.get("tool_name") or "action"
        summary = (ev.get("summary") or "").strip()
        line = f"🔧 {tool}"
        if summary:
            line += f" — {summary}"
    elif kind == "ObservationEvent":
        tool = ev.get("tool_name") or "observation"
        text = extract_text(ev.get("observation")).strip().replace("\n", " ")
        line = f"📄 {tool}: {text[:80]}"
    elif kind == "MessageEvent":
        return ""
    else:
        return ""
    line = line.replace("<", "").replace(">", "")
    return line[:max_len]


# ====================================================================== #
#  বড় আউটপুট -> ফাইল (txt / html) বানানোর হেল্পার
# ====================================================================== #
import re as _re

_MD_FENCE = _re.compile(r"```(\w*)\n(.*?)```", _re.S)


def has_code(text: str) -> bool:
    return "```" in (text or "")


def _md_inline(s: str) -> str:
    s = _re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = _re.sub(r"__([^_\n]+)__", r"<b>\1</b>", s)
    s = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    s = _re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    return s


_MD_TABLE_SEP = _re.compile(r"^\s*\|?\s*:?-{2,}[\s:|\-]*$")


def _table_cells(ln: str) -> list:
    return [c.strip() for c in ln.strip().strip("|").split("|")]



def _md_to_html(text: str) -> str:
    """ছোট্ট নিরাপদ markdown->HTML (আগে escape, তারপর ট্যাগ)।"""
    out = []
    pos = 0
    for m in _MD_FENCE.finditer(text):
        out.append(_md_block(text[pos:m.start()]))
        lang = m.group(1) or "code"
        body = esc(m.group(2).strip("\n"))
        out.append(
            f'<div class="codehead">{esc(lang)}</div>'
            f'<pre class="code">{body}</pre>'
        )
        pos = m.end()
    out.append(_md_block(text[pos:]))
    return "\n".join(out)


def _md_block(chunk: str) -> str:
    """Markdown ব্লক -> HTML: heading, ul/ol, quote, টেবিল, hr, p (লিস্ট গ্রুপিং সঠিক)।"""
    out: list = []
    buf: list = []
    kind = None  # "ul" | "ol" | "quote" | "table"

    def flush() -> None:
        nonlocal kind, buf
        if not buf:
            kind = None
            return
        if kind in ("ul", "ol"):
            out.append(f"<{kind}>" + "".join(buf) + f"</{kind}>")
        elif kind == "quote":
            out.append("<blockquote>" + "<br>".join(buf) + "</blockquote>")
        elif kind == "table":
            rows = []
            for r in buf:
                if _MD_TABLE_SEP.match(r):
                    continue
                cells = [c for c in _table_cells(r) if c]
                if not cells:
                    continue
                if not rows:
                    rows.append("<tr>" + "".join(f"<th>{_md_inline(esc(c))}</th>" for c in cells) + "</tr>")
                else:
                    rows.append("<tr>" + "".join(f"<td>{_md_inline(esc(c))}</td>" for c in cells) + "</tr>")
            if rows:
                out.append('<div class="tw"><table>' + "".join(rows) + "</table></div>")
        buf = []
        kind = None

    for ln in chunk.split("\n"):
        st = ln.strip()
        if not st:
            flush()
            continue
        if st.startswith("|") and st.endswith("|"):
            if kind != "table":
                flush()
                kind = "table"
            buf.append(st)
            continue
        hm = _re.match(r"^(#{1,6})\s+(.*)$", ln)
        if hm:
            flush()
            lvl = len(hm.group(1))
            out.append(f"<h{lvl}>{_md_inline(esc(hm.group(2)))}</h{lvl}>")
            continue
        if _re.match(r"^(-{3,}|\*{3,}|_{3,})$", st):
            flush()
            out.append("<hr>")
            continue
        bm = _re.match(r"^[-*+\u2022]\s+(.*)$", st)
        if bm:
            if kind != "ul":
                flush()
                kind = "ul"
            buf.append(f"<li>{_md_inline(esc(bm.group(1)))}</li>")
            continue
        om = _re.match(r"^\d+[.)]\s+(.*)$", st)
        if om:
            if kind != "ol":
                flush()
                kind = "ol"
            buf.append(f"<li>{_md_inline(esc(om.group(1)))}</li>")
            continue
        qm = _re.match(r"^>\s?(.*)$", st)
        if qm:
            if kind != "quote":
                flush()
                kind = "quote"
            buf.append(_md_inline(esc(qm.group(1))))
            continue
        flush()
        out.append(f"<p>{_md_inline(esc(ln))}</p>")
    flush()
    return "\n".join(out)


def _tg_block(chunk: str) -> str:
    """Markdown ব্লক -> Telegram-নিরাপদ HTML (b/i/code/a, টেবিল->বুলেট)।"""
    lines = []
    for ln in (chunk or "").split("\n"):
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if s.startswith("|") and s.endswith("|"):
            if _MD_TABLE_SEP.match(s):
                continue
            cells = [c for c in _table_cells(s) if c]
            if cells:
                lines.append("• " + _md_inline(" — ".join(esc(c) for c in cells)))
            continue
        e = esc(ln.rstrip())
        hm = _re.match(r"^#{1,6}\s+(.*)$", e)
        if hm:
            lines.append(f"<b>{_md_inline(hm.group(1))}</b>")
        elif _re.match(r"^[-*+]\s+", s):
            lines.append("• " + _md_inline(_re.sub(r"^[-*+]\s+", "", e)))
        elif _re.match(r"^&gt;\s?", e):
            lines.append(_md_inline(_re.sub(r"^&gt;\s?", "", e)))
        else:
            lines.append(_md_inline(e))
    return "\n".join(lines).strip("\n")


def md_to_telegram(text: str, limit: int = 4000) -> str:
    """এজেন্টের markdown -> Telegram HTML: raw ** বা | টেবিল আর দেখাবে না।"""
    parts, pos, src = [], 0, text or ""
    for m in _MD_FENCE.finditer(src):
        parts.append(_tg_block(src[pos:m.start()]))
        parts.append("<pre>" + esc(m.group(2).strip("\n")) + "</pre>")
        pos = m.end()
    parts.append(_tg_block(src[pos:]))
    out = "\n".join(p for p in parts if p.strip())
    out = _re.sub(r"\n{3,}", "\n\n", out).strip()
    return out[:limit]


def _plain_inline(s: str) -> str:
    s = _re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1: \2", s)
    s = _re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = _re.sub(r"__([^_\n]+)__", r"\1", s)
    s = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = _re.sub(r"`([^`\n]+)`", r"\1", s)
    return s.strip()


def _plain_block(chunk: str) -> str:
    lines = []
    for ln in (chunk or "").split("\n"):
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if s.startswith("|") and s.endswith("|"):
            if _MD_TABLE_SEP.match(s):
                continue
            cells = [c for c in _table_cells(s) if c]
            if cells:
                lines.append("• " + " — ".join(_plain_inline(c) for c in cells))
            continue
        s = _re.sub(r"^#{1,6}\s*", "", s)
        s = _re.sub(r"^[-*+]\s+", "• ", s)
        s = _re.sub(r"^>\s?", "", s)
        lines.append(_plain_inline(s))
    return "\n".join(lines).strip("\n")


def md_to_plain(text: str) -> str:
    """Markdown -> পরিষ্কার plain text (.txt ফাইলের জন্য):
    **bold** -> সাধারণ শব্দ, | টেবিল -> বুলেট লাইন, [লিংক](url) -> লিংক: url।"""
    parts, pos, src = [], 0, text or ""
    for m in _MD_FENCE.finditer(src):
        parts.append(_plain_block(src[pos:m.start()]))
        lang = (m.group(1) or "").strip()
        body = m.group(2).strip("\n")
        parts.append((f"[{lang}]\n" if lang else "") + body)
        pos = m.end()
    parts.append(_plain_block(src[pos:]))
    out = "\n\n".join(p for p in parts if p.strip())
    return _re.sub(r"\n{3,}", "\n\n", out).strip() + "\n"


def _txt_header(title: str, when: str) -> str:
    bar = "=" * max(24, len(title) + 4)
    line = "-" * 60
    return (f"{title}\n{bar}\n"
            f"সময় : {when}\n"
            f"উৎস : OpenHands এজেন্ট (Telegram bridge)\n\n"
            f"{line}\n\n")


def build_txt_file(text: str, title: str = "এজেন্টের উত্তর") -> bytes:
    import datetime as _dt
    when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = _txt_header(title, when)
    return (head + md_to_plain(text)).encode("utf-8")


_HTML_TMPL = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{
    margin: 0; padding: 30px 16px 70px;
    background: radial-gradient(1200px 500px at 80% -10%, #17203a 0%, #0b0e14 55%) fixed, #0b0e14;
    color: #dfe7f3;
    font: 19px/1.85 -apple-system, "SF Pro Text", "Segoe UI", Roboto,
          "Noto Sans Bengali", "Hind Siliguri", "SolaimanLipi", sans-serif;
  }}
  ::selection {{ background: #7c5cff66; }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  .accent {{
    height: 6px; border-radius: 999px; margin: 0 0 22px;
    background: linear-gradient(90deg, #7c5cff, #22d3ee 45%, #34d399 80%, #fbbf24);
  }}
  header {{ padding: 4px 6px 20px; margin-bottom: 24px; border-bottom: 1px solid #1f2937; }}
  header h1 {{
    margin: 0 0 12px; color: #ffffff; font-weight: 800; letter-spacing: .2px;
    font-size: clamp(26px, 5.6vw, 36px); line-height: 1.32;
  }}
  .chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .chip {{
    display: inline-block; padding: 4px 12px; border-radius: 999px;
    background: #161d2c; border: 1px solid #263145; color: #9fb0c7; font-size: 13px;
  }}
  section.card {{
    background: #121722d9; border: 1px solid #222b3a; border-radius: 18px;
    padding: 22px 24px; margin: 0 0 20px; box-shadow: 0 10px 28px rgba(0,0,0,.28);
  }}
  h1, h2, h3, h4 {{ color: #fff; line-height: 1.35; margin: .95em 0 .5em; }}
  section.card > h1:first-child, section.card > h2:first-child, section.card > h3:first-child {{ margin-top: .1em; }}
  h1 {{ font-size: clamp(24px, 5vw, 31px); font-weight: 800; }}
  h2 {{
    font-size: clamp(21px, 4.4vw, 26px); font-weight: 800;
    background: linear-gradient(180deg, #7c5cff, #22d3ee) left center / 5px 78% no-repeat;
    padding-left: 15px;
  }}
  h3 {{ font-size: clamp(19px, 3.9vw, 22px); font-weight: 700; color: #e8f0ff; }}
  h4 {{ font-size: 18px; font-weight: 700; color: #dbe7ff; }}
  p {{ margin: .7em 0; }}
  b, strong {{ color: #ffffff; font-weight: 750; }}
  a {{ color: #6cb6ff; text-decoration: underline; text-underline-offset: 3px; }}
  ul, ol {{ margin: .5em 0 1em 1.4em; padding: 0; }}
  li {{ margin: .42em 0; }}
  ul li::marker {{ color: #7c5cff; }}
  ol li::marker {{ color: #22d3ee; font-weight: 700; }}
  blockquote {{
    margin: .9em 0; padding: 12px 18px; background: #141a26;
    border-left: 4px solid #7c5cff; border-radius: 10px; color: #c7d3e8;
  }}
  code {{
    background: #1b2230; border: 1px solid #2a3446; border-radius: 6px;
    padding: 2px 7px; font: .88em ui-monospace, "SF Mono", Consolas, monospace; color: #ffc069;
  }}
  .codehead {{
    margin: 20px 0 0; padding: 7px 14px; font-size: 12.5px; letter-spacing: .07em;
    text-transform: uppercase; color: #9fb0c7;
    background: #161d2c; border: 1px solid #263145; border-bottom: none;
    border-radius: 14px 14px 0 0;
  }}
  pre.code {{
    margin: 0 0 20px; padding: 16px 18px; overflow-x: auto;
    background: #0a0d13; border: 1px solid #232b38; border-radius: 0 0 14px 14px;
    font: 14px/1.62 ui-monospace, "SF Mono", Consolas, monospace;
    color: #c9d1d9; white-space: pre;
  }}
  pre.code code {{ background: none; border: none; padding: 0; color: inherit; font: inherit; }}
  .tw {{ overflow-x: auto; margin: .9em 0; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 17px; }}
  th, td {{ border: 1px solid #263042; padding: 9px 13px; text-align: left; vertical-align: top; }}
  th {{ background: #1a2130; color: #fff; font-weight: 700; }}
  hr {{ border: none; height: 1px; margin: 26px 0;
       background: linear-gradient(90deg, transparent, #2a3446, transparent); }}
  footer {{ margin-top: 42px; padding-top: 14px; border-top: 1px solid #1f2937;
            font-size: 13px; color: #7484a0; }}
  @media print {{
    body {{ background: #fff; color: #111; }}
    section.card {{ background: #fff; border-color: #ddd; box-shadow: none; }}
    header h1, h1, h2, h3, h4, b, strong {{ color: #111; }}
    h2 {{ background: none; padding-left: 0; border-left: 5px solid #7c5cff; padding-left: 12px; }}
    .chip {{ background: #f3f4f6; border-color: #e5e7eb; color: #4b5563; }}
    a {{ color: #1d4ed8; }}
    th {{ background: #f3f4f6; color: #111; }}
    th, td {{ border-color: #d1d5db; }}
    footer {{ color: #6b7280; }}
  }}
</style>
</head>
<body>
<div class="wrap">
<div class="accent"></div>
<header>
  <h1>{title}</h1>
  <div class="chips">
    <span class="chip">🕒 {when}</span>
    <span class="chip">🤖 OpenHands এজেন্ট</span>
    <span class="chip">📨 Telegram bridge</span>
  </div>
</header>
{body}
<footer>এই ডকুমেন্টটি OpenHands এজেন্টের উত্তর থেকে স্বয়ংক্রিয়ভাবে তৈরি — প্রিমিয়াম ফরম্যাট।</footer>
</div>
</body>
</html>
"""


def _wrap_cards(body: str) -> str:
    """প্রতিটা h1/h2 সেকশনকে আলাদা কার্ডে মুড়ি — গুছানো প্রিমিয়াম লুক।"""
    parts = [p for p in _re.split(r"(?=<h[12][ >])", body) if p.strip()]
    if not parts:
        return f'<section class="card">{body}</section>'
    return "\n".join(f'<section class="card">{p.strip()}</section>' for p in parts)


def build_html_file(text: str, title: str = "এজেন্টের উত্তর") -> bytes:
    import datetime as _dt
    when = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _HTML_TMPL.format(title=esc(title), when=when, body=_wrap_cards(_md_to_html(text))).encode("utf-8")


def first_line(text: str, limit: int = 90) -> str:
    for ln in (text or "").split("\n"):
        ln = ln.strip().lstrip("#*`>- ").strip()
        if ln:
            return ln[:limit]
    return (text or "")[:limit]
