from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Channel = Literal["group", "dm"]
Mode = Literal["ask", "summarize", "follow_up"]
State = Literal["idle", "working"]


@dataclass(frozen=True)
class ActivitySnapshot:
    state: State
    channel: Channel | None = None
    mode: Mode | None = None
    target_aci: str | None = None
    target_display_name: str | None = None
    started_at: int | None = None


_state: State = "idle"
_channel: Channel | None = None
_mode: Mode | None = None
_target_aci: str | None = None
_target_display_name: str | None = None
_started_at: int | None = None


def set_working(
    *,
    channel: Channel,
    mode: Mode,
    target_aci: str,
    target_display_name: str,
    started_at: int,
) -> None:
    global _state, _channel, _mode, _target_aci, _target_display_name, _started_at
    _state = "working"
    _channel = channel
    _mode = mode
    _target_aci = target_aci
    _target_display_name = target_display_name
    _started_at = started_at


def clear() -> None:
    global _state, _channel, _mode, _target_aci, _target_display_name, _started_at
    _state = "idle"
    _channel = None
    _mode = None
    _target_aci = None
    _target_display_name = None
    _started_at = None


def snapshot() -> ActivitySnapshot:
    return ActivitySnapshot(
        state=_state,
        channel=_channel,
        mode=_mode,
        target_aci=_target_aci,
        target_display_name=_target_display_name,
        started_at=_started_at,
    )


def reset_activity_state() -> None:
    clear()


def format_elapsed(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s"


def idle_message(bot_name: str) -> str:
    return f"{bot_name} is awaiting messages."


def format_status_message(
    *,
    bot_name: str,
    viewer_aci: str,
    snap: ActivitySnapshot,
    now: int,
) -> tuple[str, int | None]:
    if snap.state != "working" or snap.started_at is None:
        return idle_message(bot_name), None
    elapsed = now - snap.started_at
    elapsed_text = format_elapsed(elapsed)
    name = (snap.target_display_name or "").strip() or "Someone"
    if snap.channel == "dm":
        if snap.target_aci == viewer_aci:
            return (
                f"{bot_name} is working on a reply to you (elapsed: {elapsed_text}).",
                elapsed,
            )
        return (
            f"{bot_name} is working on a private reply (elapsed: {elapsed_text}).",
            elapsed,
        )
    if snap.mode == "summarize":
        return (
            f"{bot_name} is summarizing recent group chat (elapsed: {elapsed_text}).",
            elapsed,
        )
    return (
        f"{bot_name} is working on a reply to {name} in the group chat (elapsed: {elapsed_text}).",
        elapsed,
    )
