from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

Channel = Literal["group", "dm"]
Mode = Literal["ask", "summarize", "follow_up"]
State = Literal["idle", "working"]
ChangeKind = Literal["snapshot", "draft"]


@dataclass(frozen=True)
class ActivitySnapshot:
    state: State
    channel: Channel | None = None
    mode: Mode | None = None
    target_aci: str | None = None
    target_display_name: str | None = None
    started_at: int | None = None


@dataclass(frozen=True)
class Subscription:
    event: asyncio.Event
    snapshot_gen: int
    draft_gen: int


_state: State = "idle"
_channel: Channel | None = None
_mode: Mode | None = None
_target_aci: str | None = None
_target_display_name: str | None = None
_started_at: int | None = None
_draft_text: str = ""
_preview_text: str = ""
_preview_aci: str | None = None
_preview_dismissed: set[str] = set()
_snapshot_gen: int = 0
_draft_gen: int = 0
_waiters: list[Subscription] = []


def subscribe() -> Subscription:
    sub = Subscription(asyncio.Event(), _snapshot_gen, _draft_gen)
    _waiters.append(sub)
    return sub


def unsubscribe(sub: Subscription) -> None:
    _waiters[:] = [w for w in _waiters if w is not sub]


def snapshot_generation() -> int:
    return _snapshot_gen


def draft_generation() -> int:
    return _draft_gen


def pending_change(since_snapshot: int, since_draft: int) -> ChangeKind | Literal["none"]:
    if _snapshot_gen != since_snapshot:
        return "snapshot"
    if _draft_gen != since_draft:
        return "draft"
    return "none"


def notify(kind: ChangeKind) -> None:
    global _snapshot_gen, _draft_gen
    if kind == "snapshot":
        _snapshot_gen += 1
    else:
        _draft_gen += 1
    if not _waiters:
        return
    for sub in _waiters:
        sub.event.set()


def set_working(
    *,
    channel: Channel,
    mode: Mode,
    target_aci: str,
    target_display_name: str,
    started_at: int,
) -> None:
    global _state, _channel, _mode, _target_aci, _target_display_name, _started_at, _draft_text
    _state = "working"
    _channel = channel
    _mode = mode
    _target_aci = target_aci
    _target_display_name = target_display_name
    _started_at = started_at
    _draft_text = ""
    notify("snapshot")


def append_draft(text: str) -> None:
    global _draft_text
    if text:
        _draft_text += text
        notify("draft")


def dismiss_preview(viewer_aci: str) -> None:
    if _preview_aci == viewer_aci and _preview_text:
        _preview_dismissed.add(viewer_aci)
        notify("snapshot")


def draft_for_viewer(viewer_aci: str) -> str | None:
    if _state == "working" and _target_aci == viewer_aci and _draft_text:
        return _draft_text
    if _preview_aci == viewer_aci and _preview_text and viewer_aci not in _preview_dismissed:
        return _preview_text
    return None


def clear() -> None:
    global _state, _channel, _mode, _target_aci, _target_display_name, _started_at, _draft_text
    global _preview_text, _preview_aci
    if _draft_text and _target_aci:
        _preview_text = _draft_text
        _preview_aci = _target_aci
        _preview_dismissed.discard(_target_aci)
    _state = "idle"
    _channel = None
    _mode = None
    _target_aci = None
    _target_display_name = None
    _started_at = None
    _draft_text = ""
    notify("snapshot")


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
    global _state, _channel, _mode, _target_aci, _target_display_name, _started_at, _draft_text
    global _preview_text, _preview_aci, _preview_dismissed
    global _snapshot_gen, _draft_gen, _waiters
    _state = "idle"
    _channel = None
    _mode = None
    _target_aci = None
    _target_display_name = None
    _started_at = None
    _draft_text = ""
    _preview_text = ""
    _preview_aci = None
    _preview_dismissed = set()
    _snapshot_gen = 0
    _draft_gen = 0
    for sub in _waiters:
        sub.event.set()
    _waiters.clear()


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
