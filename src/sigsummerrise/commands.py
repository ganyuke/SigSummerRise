from __future__ import annotations

import re
from dataclasses import dataclass

from sigsummerrise.responses import get_responses

YES_RE = re.compile(r"^\s*(yes|y|agree|i agree|ok)\s*[.!]*\s*$", re.IGNORECASE)
NO_RE = re.compile(r"^\s*(no|n|nope|decline|disagree)\s*[.!]*\s*$", re.IGNORECASE)
SUMMARIZE_RE = re.compile(
    r"\bsummarize\s+(?:the\s+)?(?:past|last)\s+(\d+)\s+messages?\b",
    re.IGNORECASE,
)
OPT_OUT_RE = re.compile(r"\b(?:opt[-\s]?out|stop collecting)\b", re.IGNORECASE)
STATUS_RE = re.compile(r"\bstatus\b", re.IGNORECASE)
DASHBOARD_RE = re.compile(
    r"\b(?:dashboard|website|web\s*site|login|magic\s*link|my\s+stats)\b",
    re.IGNORECASE,
)
HELP_RE = re.compile(r"\b(?:help|commands|what can you do)\b", re.IGNORECASE)
MENTION_OBJECT = re.compile(r"\ufffc")
LEADING_AT = re.compile(r"^@\S+\s*")


def help_text() -> str:
    return get_responses().help_text


def pick_unknown_reply() -> str:
    return get_responses().pick_unknown_reply()


@dataclass(frozen=True)
class Intent:
    name: str
    n: int | None = None


def normalize_command_text(text: str) -> str:
    t = MENTION_OBJECT.sub(" ", text or "")
    t = t.replace("\u200b", "")
    t = re.sub(r"\s+", " ", t).strip()
    while True:
        stripped = LEADING_AT.sub("", t).strip()
        if stripped == t:
            break
        t = stripped
    return t


def parse_commands(text: str, *, max_n: int) -> Intent:
    t = normalize_command_text(text)
    if not t:
        return Intent("help")
    if OPT_OUT_RE.search(t):
        return Intent("opt_out")
    match = SUMMARIZE_RE.search(t)
    if match:
        n = int(match.group(1))
        if n < 1:
            n = 1
        if n > max_n:
            n = max_n
        return Intent("summarize", n=n)
    if DASHBOARD_RE.search(t):
        return Intent("dashboard")
    if STATUS_RE.search(t):
        return Intent("status")
    if HELP_RE.search(t):
        return Intent("help")
    return Intent("ask")


def parse_intent(text: str, *, mentioned: bool, in_dm: bool, max_n: int) -> Intent:
    t = normalize_command_text(text)
    if in_dm:
        if YES_RE.match(t):
            return Intent("yes")
        if NO_RE.match(t):
            return Intent("no")
        return parse_commands(t, max_n=max_n)
    if not mentioned:
        return Intent("none")
    return parse_commands(t, max_n=max_n)
