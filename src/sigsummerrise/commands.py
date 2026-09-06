from __future__ import annotations

import re
from dataclasses import dataclass

from sigsummerrise.responses import get_responses

YES_RE = re.compile(r"^\s*(yes|y|agree|i agree|ok)\s*[.!]*\s*$", re.IGNORECASE)
NO_RE = re.compile(r"^\s*(no|n|nope|decline|disagree)\s*[.!]*\s*$", re.IGNORECASE)
SUMMARIZE_RE = re.compile(
    r"^summarize\s+(?:the\s+)?(?:past|last)\s+(\d+)\s+messages?$",
    re.IGNORECASE,
)
OPT_OUT_RE = re.compile(r"^(?:opt[-\s]?out|stop collecting)$", re.IGNORECASE)
STATUS_RE = re.compile(r"^status$", re.IGNORECASE)
DASHBOARD_RE = re.compile(
    r"^(?:dashboard|website|web\s*site|login|magic\s*link|my\s+stats)$",
    re.IGNORECASE,
)
HELP_RE = re.compile(r"^(?:help|commands|what can you do)$", re.IGNORECASE)
OPTIONAL_PREFIX_RE = re.compile(
    r"^(?:please|pls|can you|could you|would you|hey|hi|ok|okay)[,:]?\s+",
    re.IGNORECASE,
)
OPTIONAL_SUFFIX_RE = re.compile(
    r"\s+(?:please|pls|thanks|thank you)[.!]*$",
    re.IGNORECASE,
)
TRAILING_PUNCT_RE = re.compile(r"[\s.!?]+$")
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


def command_phrase(text: str) -> str:
    t = text.strip()
    while True:
        stripped = OPTIONAL_PREFIX_RE.sub("", t)
        if stripped == t:
            break
        t = stripped.strip()
    t = OPTIONAL_SUFFIX_RE.sub("", t).strip()
    t = TRAILING_PUNCT_RE.sub("", t).strip()
    return t


def parse_commands(text: str, *, max_n: int) -> Intent:
    t = normalize_command_text(text)
    if not t:
        return Intent("help")
    phrase = command_phrase(t)
    if OPT_OUT_RE.fullmatch(phrase):
        return Intent("opt_out")
    match = SUMMARIZE_RE.fullmatch(phrase)
    if match:
        n = int(match.group(1))
        if n < 1:
            n = 1
        if n > max_n:
            n = max_n
        return Intent("summarize", n=n)
    if DASHBOARD_RE.fullmatch(phrase):
        return Intent("dashboard")
    if STATUS_RE.fullmatch(phrase):
        return Intent("status")
    if HELP_RE.fullmatch(phrase):
        return Intent("help")
    return Intent("ask")


def matches_opt_out_confirm(text: str, bot_name: str) -> bool:
    phrase = TRAILING_PUNCT_RE.sub("", normalize_command_text(text)).strip()
    expected = f"please forget me, {bot_name.strip()}".lower()
    return phrase.lower() == expected


_COMMAND_INTENTS = frozenset({"opt_out", "dashboard", "status", "summarize"})


def is_command_intent(intent: Intent) -> bool:
    return intent.name in _COMMAND_INTENTS


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
