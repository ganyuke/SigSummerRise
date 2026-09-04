from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

from sigsummerrise.db import StoredMessage, ThreadEntry


@dataclass(frozen=True)
class LlmFormatContext:
    tz_name: str = "UTC"
    bot_name: str = "Assistant"


def classify_inbound(
    *,
    expires_in_seconds: int,
    text: str,
    is_bot: bool,
    opted_in: bool,
    is_reaction: bool,
    has_attachments_only: bool,
) -> str:
    """Return skip, body, or hole. Never store disappearing messages."""
    if is_bot or is_reaction:
        return "skip"
    if expires_in_seconds and expires_in_seconds > 0:
        return "skip"
    body = (text or "").strip()
    if not body or has_attachments_only:
        return "skip"
    if opted_in:
        return "body"
    return "hole"


def signal_ts_seconds(ts: int) -> float:
    if ts > 100_000_000_000:
        return ts / 1000.0
    return float(ts)


def format_message_timestamp(ts: int, tz_name: str = "UTC") -> str:
    if not ts:
        return "unknown time"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    dt = datetime.fromtimestamp(signal_ts_seconds(ts), tz=tz)
    return dt.strftime("%Y-%m-%d %H:%M")


def _display_name(name: str | None) -> str:
    return (name or "").strip() or "Someone"


def _is_redacted(
    message: StoredMessage | None,
    *,
    hide_acis: frozenset[str] | None = None,
) -> bool:
    if message is None or message.is_hole or not message.body:
        return True
    if hide_acis and message.sender_aci and message.sender_aci in hide_acis:
        return True
    return False


def format_line(
    message: StoredMessage,
    *,
    ctx: LlmFormatContext | None = None,
    hide_acis: frozenset[str] | None = None,
) -> str:
    ctx = ctx or LlmFormatContext()
    stamp = f"[{format_message_timestamp(message.ts, ctx.tz_name)}] "
    if _is_redacted(message, hide_acis=hide_acis):
        return f"{stamp}[redacted]"
    return f"{stamp}{_display_name(message.display_name)}: {message.body}"


def format_window(
    messages: list[StoredMessage],
    *,
    ctx: LlmFormatContext | None = None,
    hide_acis: frozenset[str] | None = None,
) -> list[str]:
    return [format_line(message, ctx=ctx, hide_acis=hide_acis) for message in messages]


def format_window_from_ids(
    ids: list[int],
    by_id: dict[int, StoredMessage],
    *,
    ctx: LlmFormatContext | None = None,
    hide_acis: frozenset[str] | None = None,
) -> list[str]:
    ctx = ctx or LlmFormatContext()
    lines: list[str] = []
    for message_id in ids:
        message = by_id.get(message_id)
        if message is None:
            lines.append("[redacted]")
        else:
            lines.append(format_line(message, ctx=ctx, hide_acis=hide_acis))
    return lines


def format_thread(entries: list[ThreadEntry], *, ctx: LlmFormatContext | None = None) -> list[str]:
    ctx = ctx or LlmFormatContext()
    lines: list[str] = []
    for entry in entries:
        stamp = f"[{format_message_timestamp(entry.ts, ctx.tz_name)}] "
        if entry.sender_aci is None:
            lines.append(f"{stamp}{ctx.bot_name}: {entry.body}")
        else:
            lines.append(f"{stamp}{_display_name(entry.display_name)}: {entry.body}")
    return lines


def _count_redacted(
    messages: Sequence[StoredMessage | None],
    *,
    hide_acis: frozenset[str] | None = None,
) -> int:
    count = 0
    for message in messages:
        if _is_redacted(message, hide_acis=hide_acis):
            count += 1
    return count


def format_transcript_preamble(
    messages: Sequence[StoredMessage | None],
    *,
    ctx: LlmFormatContext,
    task_line: str,
    hide_acis: frozenset[str] | None = None,
) -> str:
    count = len(messages)
    redacted = _count_redacted(messages, hide_acis=hide_acis)
    lines = [task_line]
    if count:
        timestamps = [message.ts for message in messages if message is not None and message.ts]
        if timestamps:
            first = format_message_timestamp(min(timestamps), ctx.tz_name)
            last = format_message_timestamp(max(timestamps), ctx.tz_name)
            lines.append(f"Messages: {count} ({first} → {last})")
        else:
            lines.append(f"Messages: {count}")
    else:
        lines.append("Messages: 0")
    if redacted:
        lines.append(
            f"Redacted: {redacted} of {count} lines are [redacted] "
            "(some members have not opted in; do not guess their content)."
        )
    return "\n".join(lines)


def format_summarize_user_block(
    messages: list[StoredMessage],
    *,
    ctx: LlmFormatContext,
    hide_acis: frozenset[str] | None = None,
) -> str:
    preamble = format_transcript_preamble(
        messages,
        ctx=ctx,
        task_line=f"Summarize the following {len(messages)} kept messages.",
        hide_acis=hide_acis,
    )
    transcript = "\n".join(format_window(messages, ctx=ctx, hide_acis=hide_acis))
    return f"{preamble}\n\n{transcript}"


def format_ask_user_block(
    *,
    question: str,
    messages: list[StoredMessage],
    asker_name: str,
    in_group: bool,
    ctx: LlmFormatContext,
    hide_acis: frozenset[str] | None = None,
) -> str:
    channel = "group chat" if in_group else "private DM"
    header = [f"Channel: {channel}", f"Asked by: {_display_name(asker_name)}"]
    if messages:
        preamble = format_transcript_preamble(
            messages,
            ctx=ctx,
            task_line=f"Recent chat ({len(messages)} kept messages):",
            hide_acis=hide_acis,
        )
        header.append(preamble)
        header.append("\n".join(format_window(messages, ctx=ctx, hide_acis=hide_acis)))
    header.append(f"Question:\n{question}")
    return "\n\n".join(header)


def format_followup_user_block(
    *,
    summary_text: str,
    window_ids: list[int],
    by_id: dict[int, StoredMessage],
    thread_entries: list[ThreadEntry],
    asker_name: str,
    ctx: LlmFormatContext,
    hide_acis: frozenset[str] | None = None,
) -> str:
    ordered = [by_id.get(message_id) for message_id in window_ids]
    preamble = format_transcript_preamble(
        ordered,
        ctx=ctx,
        task_line="Continuing a thread from a prior summary or answer.",
        hide_acis=hide_acis,
    )
    excerpt = "\n".join(format_window_from_ids(window_ids, by_id, ctx=ctx, hide_acis=hide_acis))
    thread_lines = "\n".join(format_thread(thread_entries, ctx=ctx))
    return (
        f"{preamble}\n\n"
        f"Asked by: {_display_name(asker_name)}\n\n"
        f"Summary:\n{summary_text}\n\n"
        f"Chat excerpt:\n{excerpt}\n\n"
        f"Follow-up thread:\n{thread_lines}"
    )
