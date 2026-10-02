"""Chatwork message notation <-> plain text / Markdown.

Pure functions, no Hermes imports, so they are easy to test and reason about.

Inbound (Chatwork -> agent): ``parse_inbound`` finds whether the bot was
addressed ([To:<bot>] or a reply to the bot) and turns Chatwork tags into
readable text ([qt] -> "> " lines, [code] -> fenced block, [info] -> plain).

Outbound (agent -> Chatwork): ``render_outbound`` turns the agent's Markdown
into what Chatwork can show (fenced code -> [code], headings -> 【...】, no
**bold**) and defuses notification tags the model must not send on its own
([toall], [To:...], [rp ...]). Addressing the reply is the adapter's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# --- inbound -------------------------------------------------------------

# [To:id] and the reply tag; the reply tag also in the legacy Japanese
# spelling still found in old rooms.
_ADDRESS_RE = re.compile(r"\[To:(\d+)\]|\[(?:rp|返信) aid=(\d+) to=(\d+)-(\d+)\]")
_QT_RE = re.compile(r"\[qt\](?:\[qtmeta[^\]]*\])?(.*?)\[/qt\]", re.S)
_CODE_RE = re.compile(r"\[code\](.*?)\[/code\]", re.S)
_TITLE_RE = re.compile(r"\[title\](.*?)\[/title\]", re.S)
_INFO_TAG_RE = re.compile(r"\[/?info\]")
_DOWNLOAD_RE = re.compile(r"\[download:\d+\](.*?)\[/download\]", re.S)
_URL_TAG_RE = re.compile(r"\[url\](.*?)\[/url\]", re.S)
_DROP_TAG_RE = re.compile(
    r"\[(?:toall|hr|preview[^\]]*|picon:\d+|piconname:\d+|pname:\d+|deleted)\]")
# What Chatwork's UI inserts after a [To:]/[rp] tag: the addressee's display
# name, optionally an honorific, then a line break.
_HONORIFIC_RE = r"[ \t\u3000]*(?:さん|様|さま|殿|san)?"


@dataclass
class Inbound:
    """What the adapter needs to know about one inbound message."""

    text: str  # cleaned text for the agent
    to_bot: bool  # [To:<bot>] present
    reply_to_account: Optional[str] = None  # [rp aid=...]
    reply_to_message: Optional[str] = None  # [rp to=room-<msg>]
    is_system: bool = False  # Chatwork system notice ([dtext:...]) — never answered

    def addresses_bot(self, bot_id: str) -> bool:
        return self.to_bot or self.reply_to_account == bot_id


def _strip_name_after(text: str, start: int, names: tuple) -> int:
    """Index where real content starts after a [To:<bot>]/[rp aid=<bot>] tag.

    Removes only what the To/Reply button inserts: the bot's exact display name
    (plus an honorific) and the line break after it. Anything else is the
    asker's words and is kept, so "[To:1] 宛名は山田商事 山田様" keeps its text.
    """
    for name in sorted((n for n in names if n), key=len, reverse=True):
        m = re.compile(r"[ \t\u3000]*" + re.escape(name) + _HONORIFIC_RE + r"[ \t\u3000]*(?:\n|$)").match(text, start)
        if m:
            return m.end()
    m = re.compile(r"[ \t\u3000]*\n").match(text, start)  # tag alone on its line
    return m.end() if m else start


def parse_inbound(body: str, bot_id: str, bot_names: tuple = ()) -> Inbound:
    """Parse a Chatwork message body.

    ``bot_id`` is the account id the adapter runs as; ``bot_names`` are names
    that may follow a [To:<bot>] tag (the account's display name).
    """
    body = body or ""
    if "[dtext:" in body:
        return Inbound(text="", to_bot=False, is_system=True)

    to_bot = False
    reply_acc = reply_msg = None
    names = tuple(n for n in bot_names if n)

    # Pass 1: addressing tags. Walk left to right so we can drop the name line
    # that follows each tag.
    out = []
    pos = 0
    for m in _ADDRESS_RE.finditer(body):
        out.append(body[pos:m.start()])
        if m.group(1) is not None:  # [To:id]
            acc = m.group(1)
            if acc == bot_id:
                to_bot = True
                pos = _strip_name_after(body, m.end(), names)
            else:
                out.append("@")  # keep "@山田さん" so the agent knows who else was addressed
                pos = m.end()
        else:  # [rp aid=.. to=room-msg]
            acc, msg = m.group(2), m.group(4)
            if reply_acc is None:
                reply_acc, reply_msg = acc, msg
            if acc == bot_id:
                pos = _strip_name_after(body, m.end(), names)
            else:
                out.append("@")
                pos = m.end()
    out.append(body[pos:])
    text = "".join(out)

    # Pass 2: block notation -> readable text.
    text = _CODE_RE.sub(lambda m: "```\n" + m.group(1).strip("\n") + "\n```", text)
    text = _QT_RE.sub(lambda m: "\n".join("> " + ln for ln in m.group(1).strip("\n").splitlines()), text)
    text = _TITLE_RE.sub(lambda m: m.group(1).strip() + "\n", text)
    text = _INFO_TAG_RE.sub("", text)
    text = _DOWNLOAD_RE.sub(lambda m: f"(attached file: {m.group(1).strip()})", text)
    text = _URL_TAG_RE.sub(lambda m: m.group(1).strip(), text)
    text = _DROP_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Inbound(text=text, to_bot=to_bot, reply_to_account=reply_acc, reply_to_message=reply_msg)


def quote_preview(body: str, limit: int = 500) -> str:
    """Short readable version of a replied-to message, for reply context."""
    cleaned = parse_inbound(body, bot_id="", bot_names=()).text
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


# --- outbound ------------------------------------------------------------

# Tags that notify people or speak in someone else's name. The model must not
# emit them: a prompt-injected "[toall]" would ping a whole company room, and
# "[qt][qtmeta aid=..]" / "[piconname:..]" would show a colleague's name and
# icon on words they never wrote.
_NOTIFY_TAG_RE = re.compile(
    r"\[(toall|To:\d+|rp aid=[^\]]*|返信 aid=[^\]]*|qtmeta[^\]]*|piconname:\d+|picon:\d+|pname:\d+|dtext:[^\]]*)\]",
    re.I)
_FENCE_RE = re.compile(r"^```[^\n`]*\n(.*?)^```[ \t]*$", re.S | re.M)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.M)
_OPEN_FENCE_RE = re.compile(r"^```[^\n`]*(?:\n|$)", re.M)
_BOLD_RE = re.compile(r"(\*\*)(?=\S)(.+?)(?<=\S)\1")  # not __x__: that is usually code (__init__)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((\S+?)\)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HR_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)
_CODE_PLACEHOLDER = "\x00CWCODE{}\x00"


def defuse_notify_tags(text: str) -> str:
    """Make [toall]/[To:..]/[rp ..]/[qtmeta ..]/[picon..]/[pname..]/[dtext..] inert with full-width brackets."""
    return _NOTIFY_TAG_RE.sub(lambda m: "［" + m.group(1) + "］", text)


def render_outbound(markdown: str) -> str:
    """Agent Markdown -> Chatwork body (without the reply header)."""
    text = defuse_notify_tags(markdown or "")

    blocks: list[str] = []

    def _stash(m: re.Match) -> str:
        blocks.append(m.group(1).rstrip("\n"))
        return _CODE_PLACEHOLDER.format(len(blocks) - 1)

    text = _FENCE_RE.sub(_stash, text)
    # An unterminated fence at the start of a line (a long answer cut inside a
    # code block) still becomes a code block. ``` in the middle of a line is text.
    open_fence = _OPEN_FENCE_RE.search(text)
    if open_fence:
        tail = text[open_fence.end():]
        blocks.append(tail.rstrip("\n"))
        text = text[:open_fence.start()] + _CODE_PLACEHOLDER.format(len(blocks) - 1)

    text = _HEADING_RE.sub(lambda m: "【" + m.group(1).strip() + "】", text)
    text = _BOLD_RE.sub(lambda m: m.group(2), text)
    text = _IMAGE_RE.sub(lambda m: m.group(2), text)
    text = _LINK_RE.sub(lambda m: m.group(2) if m.group(1).strip() == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    text = _HR_RE.sub("[hr]", text)

    for i, code in enumerate(blocks):
        text = text.replace(_CODE_PLACEHOLDER.format(i), "[code]" + code + "[/code]")
    return text.strip("\n")


def reply_header(account_id: str, room_id: str, message_id: str, name: str) -> str:
    """The header Chatwork's own "Reply" button produces (notifies the asker)."""
    who = (name or "").strip()
    suffix = f"{who}さん" if who else ""
    return f"[rp aid={account_id} to={room_id}-{message_id}]{suffix}\n"
