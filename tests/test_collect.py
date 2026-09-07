from sigsummerrise.collect import (
    LlmFormatContext,
    classify_inbound,
    format_ask_user_block,
    format_followup_user_block,
    format_line,
    format_summarize_user_block,
    format_transcript_preamble,
    format_window,
    format_window_from_ids,
    resolve_mentions,
    signal_ts_seconds,
)
from sigsummerrise.db import Mention, StoredMessage, ThreadEntry


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


def test_format_line_hides_aci():
    body = StoredMessage(id=2, sender_aci="aaa", ts=2, body="hi", is_hole=False, display_name="Bob")
    line = format_line(body, hide_acis=frozenset({"aaa"}))
    assert line.endswith("[redacted]")
    assert "Bob" not in line
    visible = format_line(body, hide_acis=frozenset())
    assert visible.endswith("Bob: hi")


def test_format_summarize_user_block_hides_aci():
    messages = [
        StoredMessage(id=1, sender_aci="aaa", ts=100, body="secret", is_hole=False, display_name="Alice"),
        StoredMessage(id=2, sender_aci="bbb", ts=101, body="public", is_hole=False, display_name="Bob"),
    ]
    block = format_summarize_user_block(messages, ctx=LlmFormatContext(), hide_acis=frozenset({"aaa"}))
    assert "secret" not in block
    assert "Bob: public" in block
    assert "Redacted: 1 of 2" in block


def test_redaction_is_unlabeled():
    hole = StoredMessage(id=1, sender_aci=None, ts=1, body=None, is_hole=True, display_name="Alice")
    body = StoredMessage(id=2, sender_aci="aaa", ts=2, body="hi", is_hole=False, display_name="Bob")
    lines = format_window([hole, body])
    assert lines[0].endswith("[redacted]")
    assert lines[1].endswith("Bob: hi")
    assert "Alice" not in format_line(hole)


def test_format_window_collapses_consecutive_holes():
    holes = [StoredMessage(id=i, sender_aci=None, ts=i * 1000, body=None, is_hole=True) for i in (1, 2, 3)]
    body = StoredMessage(id=4, sender_aci="a", ts=4000, body="hi", is_hole=False, display_name="Bob")
    lines = format_window([*holes, body])
    assert lines == [
        "[1970-01-01 00:16 → 00:50] (3 redacted messages)",
        "[1970-01-01 01:06] Bob: hi",
    ]


def test_format_window_keeps_single_hole_per_line():
    hole = StoredMessage(id=1, sender_aci=None, ts=1000, body=None, is_hole=True)
    body = StoredMessage(id=2, sender_aci="a", ts=2000, body="hi", is_hole=False, display_name="Bob")
    lines = format_window([hole, body])
    assert lines == [
        "[1970-01-01 00:16] [redacted]",
        "[1970-01-01 00:33] Bob: hi",
    ]


def test_format_window_range_spans_days():
    # > 100_000_000_000 means milliseconds; these are two days apart
    holes = [
        StoredMessage(id=1, sender_aci=None, ts=1_631_458_508_000, body=None, is_hole=True),
        StoredMessage(id=2, sender_aci=None, ts=1_631_544_908_000, body=None, is_hole=True),
    ]
    lines = format_window(holes)
    assert lines == ["[2021-09-12 14:55 → 2021-09-13 14:55] (2 redacted messages)"]


def test_format_window_collapses_hidden_aci_runs():
    messages = [
        StoredMessage(id=1, sender_aci="aaa", ts=1000, body="secret one", is_hole=False, display_name="Alice"),
        StoredMessage(id=2, sender_aci="aaa", ts=2000, body="secret two", is_hole=False, display_name="Alice"),
        StoredMessage(id=3, sender_aci="bbb", ts=3000, body="public", is_hole=False, display_name="Bob"),
    ]
    lines = format_window(messages, hide_acis=frozenset({"aaa"}))
    assert lines == [
        "[1970-01-01 00:16 → 00:33] (2 redacted messages)",
        "[1970-01-01 00:50] Bob: public",
    ]
    assert "secret" not in "\n".join(lines)
    assert "Alice" not in "\n".join(lines)


def test_format_window_from_ids_collapses_missing_ids():
    body = StoredMessage(id=3, sender_aci="a", ts=3000, body="hi", is_hole=False, display_name="Bob")
    lines = format_window_from_ids([1, 2, 3], {3: body})
    assert lines == [
        "(2 redacted messages)",
        "[1970-01-01 00:50] Bob: hi",
    ]


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


def test_resolve_mentions_renders_opted_in_names():
    ctx = LlmFormatContext(mention_names={"uuid-1": "Bob", "bot-uuid": "grok"})
    body = f"\ufffc and \ufffc say hi"
    resolved = resolve_mentions(
        body,
        [Mention(uuid="uuid-1", start=0), Mention(uuid="bot-uuid", start=6)],
        ctx=ctx,
    )
    assert resolved == "Bob and grok say hi"


def test_resolve_mentions_redacts_unopted_and_unknown():
    ctx = LlmFormatContext(mention_names={})
    body = "\ufffc waved"
    resolved = resolve_mentions(body, [Mention(uuid="uuid-1", start=0)], ctx=ctx)
    assert resolved == "[redacted] waved"
    assert "\ufffc" not in resolved
    # No metadata at all -> still redacted, never raw placeholder
    assert resolve_mentions(body, [], ctx=ctx) == "[redacted] waved"


def test_resolve_mentions_placeholder_without_position_metadata():
    ctx = LlmFormatContext(mention_names={"uuid-1": "Bob"})
    resolved = resolve_mentions("\ufffc waved", [], ctx=ctx)
    assert resolved == "[redacted] waved"


def test_resolve_mentions_ignores_stale_positions():
    ctx = LlmFormatContext(mention_names={"uuid-1": "Bob"})
    body = "xx\ufffc"
    resolved = resolve_mentions(body, [Mention(uuid="uuid-1", start=0)], ctx=ctx)
    assert resolved == "xx[redacted]"


def test_resolve_mentions_handles_astral_characters():
    # Mention offset was measured in UTF-16 units and converted at parse time
    ctx = LlmFormatContext(mention_names={"uuid-1": "Bob"})
    body = "\U0001f4a9\ufffc"
    resolved = resolve_mentions(body, [Mention(uuid="uuid-1", start=1)], ctx=ctx)
    assert resolved == "\U0001f4a9Bob"


def test_format_line_renders_mentions():
    message = StoredMessage(
        id=1,
        sender_aci="a",
        ts=10,
        body="\ufffc hi",
        is_hole=False,
        display_name="Ann",
        mentions=(Mention(uuid="uuid-1", start=0),),
    )
    ctx = LlmFormatContext(mention_names={"uuid-1": "Bob"})
    assert format_line(message, ctx=ctx).endswith("Ann: Bob hi")
    assert format_line(message, ctx=LlmFormatContext()).endswith("Ann: [redacted] hi")


def test_format_line_without_mentions_unchanged():
    message = StoredMessage(id=1, sender_aci="a", ts=10, body="plain hi", is_hole=False, display_name="Ann")
    assert format_line(message, ctx=LlmFormatContext()).endswith("Ann: plain hi")
