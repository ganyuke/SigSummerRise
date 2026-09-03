import pytest

from sigsummerrise import activity


@pytest.fixture(autouse=True)
def _reset_activity():
    activity.reset_activity_state()
    yield
    activity.reset_activity_state()


def test_idle_message():
    snap = activity.snapshot()
    msg, elapsed = activity.format_status_message(
        bot_name="TestBot",
        viewer_aci="viewer",
        snap=snap,
        now=1000,
    )
    assert msg == "TestBot is awaiting messages."
    assert elapsed is None


def test_group_ask_shows_name_to_any_viewer():
    activity.set_working(
        channel="group",
        mode="ask",
        target_aci="alice-aci",
        target_display_name="Alice",
        started_at=900,
    )
    snap = activity.snapshot()
    msg, elapsed = activity.format_status_message(
        bot_name="TestBot",
        viewer_aci="bob-aci",
        snap=snap,
        now=942,
    )
    assert "reply to Alice in the group chat" in msg
    assert elapsed == 42


def test_group_summarize_omits_name():
    activity.set_working(
        channel="group",
        mode="summarize",
        target_aci="alice-aci",
        target_display_name="Alice",
        started_at=100,
    )
    snap = activity.snapshot()
    msg, _elapsed = activity.format_status_message(
        bot_name="TestBot",
        viewer_aci="bob-aci",
        snap=snap,
        now=130,
    )
    assert msg == "TestBot is summarizing recent group chat (elapsed: 0m 30s)."
    assert "Alice" not in msg


def test_dm_target_sees_you():
    activity.set_working(
        channel="dm",
        mode="ask",
        target_aci="alice-aci",
        target_display_name="Alice",
        started_at=0,
    )
    snap = activity.snapshot()
    msg, _elapsed = activity.format_status_message(
        bot_name="TestBot",
        viewer_aci="alice-aci",
        snap=snap,
        now=5,
    )
    assert msg == "TestBot is working on a reply to you (elapsed: 0m 5s)."
    assert "Alice" not in msg


def test_dm_other_viewer_sees_private_reply():
    activity.set_working(
        channel="dm",
        mode="ask",
        target_aci="alice-aci",
        target_display_name="Alice",
        started_at=0,
    )
    snap = activity.snapshot()
    msg, _elapsed = activity.format_status_message(
        bot_name="TestBot",
        viewer_aci="bob-aci",
        snap=snap,
        now=5,
    )
    assert msg == "TestBot is working on a private reply (elapsed: 0m 5s)."
    assert "Alice" not in msg


def test_format_elapsed():
    assert activity.format_elapsed(0) == "0m 0s"
    assert activity.format_elapsed(42) == "0m 42s"
    assert activity.format_elapsed(125) == "2m 5s"


def test_clear_returns_idle():
    activity.set_working(
        channel="group",
        mode="ask",
        target_aci="x",
        target_display_name="X",
        started_at=1,
    )
    activity.clear()
    assert activity.snapshot().state == "idle"
