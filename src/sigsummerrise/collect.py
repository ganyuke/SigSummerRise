from __future__ import annotations

from sigsummerrise.db import StoredMessage, ThreadEntry


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


def format_line(message: StoredMessage) -> str:
    if message.is_hole or not message.body:
        return "[redacted]"
    name = (message.display_name or "").strip() or "Someone"
    return f"{name}: {message.body}"


def format_window(messages: list[StoredMessage]) -> list[str]:
    return [format_line(m) for m in messages]


def format_window_from_ids(
    ids: list[int],
    by_id: dict[int, StoredMessage],
) -> list[str]:
    lines: list[str] = []
    for message_id in ids:
        message = by_id.get(message_id)
        if message is None:
            lines.append("[redacted]")
        else:
            lines.append(format_line(message))
    return lines


def format_thread(entries: list[ThreadEntry]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        if entry.sender_aci is None:
            lines.append(f"Assistant: {entry.body}")
        else:
            name = (entry.display_name or "").strip() or "Someone"
            lines.append(f"{name}: {entry.body}")
    return lines
