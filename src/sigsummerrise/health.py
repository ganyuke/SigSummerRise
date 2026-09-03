from __future__ import annotations

_last_signal_event_at: int | None = None


def stamp_signal_event(now: int) -> None:
    global _last_signal_event_at
    _last_signal_event_at = now


def last_signal_event_at() -> int | None:
    return _last_signal_event_at


def reset_health_state() -> None:
    global _last_signal_event_at
    _last_signal_event_at = None
