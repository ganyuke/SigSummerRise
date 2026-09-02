from sigsummerrise.collect import classify_inbound, format_line, format_window
from sigsummerrise.db import StoredMessage


def test_skip_disappearing():
    assert (
        classify_inbound(
            expires_in_seconds=30,
            text="secret",
            is_bot=False,
            opted_in=True,
            is_reaction=False,
            has_attachments_only=False,
        )
        == "skip"
    )


def test_opted_in_body():
    assert (
        classify_inbound(
            expires_in_seconds=0,
            text="hello",
            is_bot=False,
            opted_in=True,
            is_reaction=False,
            has_attachments_only=False,
        )
        == "body"
    )


def test_not_opted_in_hole():
    assert (
        classify_inbound(
            expires_in_seconds=0,
            text="hello",
            is_bot=False,
            opted_in=False,
            is_reaction=False,
            has_attachments_only=False,
        )
        == "hole"
    )


def test_skip_empty_and_media_only():
    assert (
        classify_inbound(
            expires_in_seconds=0,
            text="  ",
            is_bot=False,
            opted_in=True,
            is_reaction=False,
            has_attachments_only=True,
        )
        == "skip"
    )


def test_redaction_is_unlabeled():
    hole = StoredMessage(id=1, sender_aci=None, ts=1, body=None, is_hole=True, display_name="Alice")
    body = StoredMessage(id=2, sender_aci="aaa", ts=2, body="hi", is_hole=False, display_name="Bob")
    lines = format_window([hole, body])
    assert lines == ["[redacted]", "Bob: hi"]
    assert "Alice" not in format_line(hole)
