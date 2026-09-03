from sigsummerrise.collect import (
    LlmFormatContext,
    classify_inbound,
    format_ask_user_block,
    format_followup_user_block,
    format_line,
    format_summarize_user_block,
    format_transcript_preamble,
    format_window,
    signal_ts_seconds,
)
from sigsummerrise.db import StoredMessage, ThreadEntry


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
    assert lines[0].endswith("[redacted]")
    assert lines[1].endswith("Bob: hi")
    assert "Alice" not in format_line(hole)


def test_signal_ts_seconds_handles_milliseconds():
    assert signal_ts_seconds(1_631_458_508_784) == 1_631_458_508.784
    assert signal_ts_seconds(100) == 100.0


def test_format_transcript_preamble_includes_redaction_count():
    messages = [
        StoredMessage(id=1, sender_aci=None, ts=10, body=None, is_hole=True),
        StoredMessage(id=2, sender_aci="a", ts=20, body="hi", is_hole=False, display_name="Bob"),
    ]
    preamble = format_transcript_preamble(
        messages,
        ctx=LlmFormatContext(tz_name="UTC"),
        task_line="Summarize the following 2 kept messages.",
    )
    assert "Messages: 2" in preamble
    assert "Redacted: 1 of 2" in preamble


def test_format_summarize_user_block_includes_task_header():
    messages = [
        StoredMessage(id=1, sender_aci="a", ts=100, body="hello", is_hole=False, display_name="Bob"),
    ]
    block = format_summarize_user_block(messages, ctx=LlmFormatContext())
    assert "Summarize the following 1 kept messages." in block
    assert "Bob: hello" in block
    assert block.startswith("Summarize")


def test_format_ask_user_block_includes_asker_and_channel():
    block = format_ask_user_block(
        question="why though",
        messages=[],
        asker_name="Suisei",
        in_group=True,
        ctx=LlmFormatContext(bot_name="grok"),
    )
    assert "Channel: group chat" in block
    assert "Asked by: Suisei" in block
    assert "Question:\nwhy though" in block


def test_format_ask_user_block_dm_omits_transcript():
    block = format_ask_user_block(
        question="personal question",
        messages=[],
        asker_name="Suisei",
        in_group=False,
        ctx=LlmFormatContext(bot_name="grok"),
    )
    assert "Channel: private DM" in block
    assert "Recent chat" not in block
    assert "Question:\npersonal question" in block


def test_format_followup_user_block_uses_bot_name():
    block = format_followup_user_block(
        summary_text="they wanted pizza",
        window_ids=[1],
        by_id={
            1: StoredMessage(id=1, sender_aci="a", ts=50, body="pizza", is_hole=False, display_name="Bob"),
        },
        thread_entries=[
            ThreadEntry(
                id=1,
                summary_id=1,
                sender_aci="a",
                body="why?",
                ts=60,
                display_name="Suisei",
            ),
            ThreadEntry(id=2, summary_id=1, sender_aci=None, body="because", ts=70),
        ],
        asker_name="Suisei",
        ctx=LlmFormatContext(bot_name="grok"),
    )
    assert "Continuing a thread" in block
    assert "Asked by: Suisei" in block
    assert "grok: because" in block
    assert "Follow-up thread:" in block
