import uuid
from contextlib import asynccontextmanager

import pytest

from sigsummerrise import llm
from sigsummerrise.bot import Bot
from sigsummerrise.commands import help_text
from sigsummerrise.db import REDACTED_SUMMARY
from sigsummerrise.prompts import format_current_time, get_prompts, render_system_prompt
from sigsummerrise.responses import get_responses
from sigsummerrise.signal_rpc import IncomingMessage


class FakeSignal:
    def __init__(self) -> None:
        self.dms: list[tuple[str, str]] = []
        self.groups: list[tuple[str, str]] = []
        self.group_quotes: list[tuple[int, str, str | None]] = []
        self.typing: list[tuple[str | None, bool]] = []
        self.ts = 5000

    async def send_dm(self, recipient_aci: str, message: str) -> int | None:
        self.dms.append((recipient_aci, message))
        return None

    async def send_group(
        self,
        group_id: str,
        message: str,
        *,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> int | None:
        self.groups.append((group_id, message))
        if quote_timestamp is not None and quote_author:
            self.group_quotes.append((quote_timestamp, quote_author, quote_message))
        self.ts += 1
        return self.ts

    async def send_typing(
        self,
        *,
        group_id: str | None = None,
        recipient: str | None = None,
        stop: bool = False,
    ) -> None:
        self.typing.append((group_id or recipient, stop))

    def keep_typing(self, *, group_id: str | None = None, recipient: str | None = None):
        @asynccontextmanager
        async def _ctx():
            await self.send_typing(group_id=group_id, recipient=recipient, stop=False)
            try:
                yield
            finally:
                await self.send_typing(group_id=group_id, recipient=recipient, stop=True)

        return _ctx()

    async def aclose(self) -> None:
        return None


def _msg(**kwargs) -> IncomingMessage:
    base = dict(
        sender_aci="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        display_name="Suisei",
        timestamp=100,
        text="hello",
        group_id="abc123",
        expires_in_seconds=0,
        mentioned_uuids=[],
        quote_timestamp=None,
        is_reaction=False,
        has_attachments=False,
    )
    base.update(kwargs)
    return IncomingMessage(**base)


@pytest.mark.asyncio
async def test_disappearing_not_collected(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    await bot.handle(_msg(text="gone soon", expires_in_seconds=60))
    count = tmp_db.connect().execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert count == 0


@pytest.mark.asyncio
async def test_non_consent_is_anonymous_hole(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await bot.handle(_msg(text="private chatter"))
    row = tmp_db.connect().execute("SELECT sender_aci, body, is_hole FROM messages").fetchone()
    assert row["is_hole"] == 1
    assert row["sender_aci"] is None
    assert row["body"] is None
    assert tmp_db.count_bodies(aci) == 0


@pytest.mark.asyncio
async def test_mention_sends_consent_dm_and_group_notice(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    await bot.handle(
        _msg(
            text="@grok summarize the past 5 messages",
            mentioned_uuids=[settings.signal_bot_aci],
        )
    )
    assert len(signal.dms) == 1
    assert signal.dms[0][1] == get_responses().consent_dm
    assert signal.groups[0][1] == get_responses().unopted_group_notice
    assert "http" not in signal.dms[0][1]


@pytest.mark.asyncio
async def test_unopted_first_mention_notices_then_may_roast(tmp_db, settings, monkeypatch):
    monkeypatch.setattr("sigsummerrise.consent.should_roast_unopted_mention", lambda: True)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await bot.handle(
        _msg(
            sender_aci=aci,
            text="@grok status",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=100,
        )
    )
    assert signal.groups[0][1] == get_responses().unopted_group_notice
    assert signal.dms[0][1] == get_responses().consent_dm

    await bot.handle(
        _msg(
            sender_aci=aci,
            text="@grok status",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=101,
        )
    )
    assert signal.groups[1][1] in get_responses().group_roasts


@pytest.mark.asyncio
async def test_opted_in_collects_body_and_status(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    await bot.handle(_msg(text="hello friends"))
    assert tmp_db.count_bodies(aci) == 1
    await bot.handle(
        _msg(
            text="@grok status",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=101,
        )
    )
    assert tmp_db.count_bodies(aci) == 2
    assert any("2 of your messages" in text for _, text in signal.groups)


@pytest.mark.asyncio
async def test_dashboard_is_dm_only(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    await bot.handle(
        _msg(text="@grok dashboard", mentioned_uuids=[settings.signal_bot_aci])
    )
    assert any("/a/" in text for _, text in signal.dms)
    assert any("one-time" in text.lower() for _, text in signal.groups)
    assert not any("/a/" in text for _, text in signal.groups)


@pytest.mark.asyncio
async def test_dm_yes_opts_in(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = str(uuid.uuid4())
    await bot.handle(_msg(sender_aci=aci, text="Yes", group_id=None))
    user = tmp_db.get_user(aci)
    assert user is not None and user.opted_in


@pytest.mark.asyncio
async def test_dm_non_yes_no_asks_again(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    await bot.handle(
        _msg(
            text="@grok status",
            mentioned_uuids=[settings.signal_bot_aci],
        )
    )
    assert signal.dms[-1][1] == get_responses().consent_dm
    await bot.handle(_msg(sender_aci=aci, text="Yeah no bro", group_id=None))
    assert signal.dms[-1][1] == get_responses().consent_clarify
    await bot.handle(_msg(sender_aci=aci, text="I don't know", group_id=None))
    assert signal.dms[-1][1] == get_responses().consent_clarify
    user = tmp_db.get_user(aci)
    assert user is not None and not user.opted_in


@pytest.mark.asyncio
async def test_dm_no_after_opt_in_deletes(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    tmp_db.insert_body(aci, 1, "keep me? no")
    await bot.handle(_msg(sender_aci=aci, text="No", group_id=None))
    user = tmp_db.get_user(aci)
    assert user is not None and not user.opted_in
    assert tmp_db.count_bodies(aci) == 0
    assert any("deleted" in text.lower() for _, text in signal.dms)


@pytest.mark.asyncio
async def test_text_mention_without_signal_mention_is_ignored(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    await bot.handle(_msg(text="@grok status", mentioned_uuids=[]))
    assert signal.groups == []
    assert signal.dms == []


@pytest.mark.asyncio
async def test_other_group_is_ignored(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    await bot.handle(
        _msg(
            text="@grok status",
            mentioned_uuids=[settings.signal_bot_aci],
            group_id="some-other-group",
        )
    )
    assert signal.groups == []
    assert tmp_db.count_bodies(aci) == 0


@pytest.mark.asyncio
async def test_empty_group_id_ignores_groups(tmp_db, settings):
    settings.signal_group_id = ""
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    await bot.handle(
        _msg(text="@grok status", mentioned_uuids=[settings.signal_bot_aci])
    )
    assert signal.groups == []
    assert tmp_db.count_bodies(aci) == 0


@pytest.mark.asyncio
async def test_llm_rate_limit(tmp_db, settings):
    settings.llm_calls_per_hour = 1
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    tmp_db.insert_body(aci, 10, "hello")
    mention = dict(
        mentioned_uuids=[settings.signal_bot_aci],
        text="@grok summarize the past 5 messages",
    )
    await bot.handle(_msg(**mention, timestamp=200))
    first = list(signal.groups)
    await bot.handle(_msg(**mention, timestamp=201))
    extra = signal.groups[len(first) :]
    assert extra
    assert any(text in get_responses().llm_rate_replies for _, text in extra)


@pytest.mark.asyncio
async def test_summarize_types_while_waiting(tmp_db, settings, monkeypatch):
    async def fake_complete(settings, db, system, user, **kwargs):
        return "short recap"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    tmp_db.insert_body(aci, 10, "hello")
    await bot.handle(
        _msg(
            text="@grok summarize the past 5 messages",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=200,
        )
    )
    assert signal.typing == [("abc123", False), ("abc123", True)]
    assert signal.groups[-1][1] == "short recap"


@pytest.mark.asyncio
async def test_help_works_when_opted_out(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.decline(aci)
    await bot.handle(_msg(sender_aci=aci, text="help", group_id=None))
    assert signal.dms[-1][1] == help_text()
    await bot.handle(
        _msg(text="@grok help", mentioned_uuids=[settings.signal_bot_aci], timestamp=200)
    )
    assert any(text == help_text() for _, text in signal.groups)


@pytest.mark.asyncio
async def test_follow_up_chains_on_quoted_reply(tmp_db, settings, monkeypatch):
    async def fake_complete(settings, db, system, user, **kwargs):
        return "answer 2"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    summary_id = tmp_db.save_summary("abc123", 5000, [1], "first summary")
    tmp_db.add_thread(summary_id, aci, "question 1", 5001)
    tmp_db.add_thread(summary_id, None, "answer 1", 5002)
    signal.ts = 5100

    await bot.handle(
        _msg(
            text="question 2",
            quote_timestamp=5002,
            timestamp=5003,
        )
    )

    thread = tmp_db.get_thread(summary_id)
    assert [entry.body for entry in thread] == [
        "question 1",
        "answer 1",
        "question 2",
        "answer 2",
    ]
    assert signal.groups[-1][1] == "answer 2"
    assert thread[-1].ts == 5101
    assert signal.group_quotes[-1] == (5003, aci, "question 2")


@pytest.mark.asyncio
async def test_follow_up_still_works_on_original_summary(tmp_db, settings, monkeypatch):
    async def fake_complete(settings, db, system, user, **kwargs):
        return "follow-up answer"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    summary_id = tmp_db.save_summary("abc123", 5000, [1], "first summary")

    await bot.handle(
        _msg(
            text="first question",
            quote_timestamp=5000,
            timestamp=5001,
        )
    )

    assert tmp_db.get_thread(summary_id)[-1].body == "follow-up answer"
    assert signal.group_quotes[-1][0] == 5001
    assert signal.group_quotes[-1][1] == aci


@pytest.mark.asyncio
async def test_summarize_quotes_command_message(tmp_db, settings, monkeypatch):
    async def fake_complete(settings, db, system, user, **kwargs):
        return "short recap"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    tmp_db.insert_body(aci, 10, "hello")
    await bot.handle(
        _msg(
            text="@grok summarize the past 5 messages",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=200,
        )
    )
    assert signal.group_quotes[-1] == (
        200,
        aci,
        "@grok summarize the past 5 messages",
    )


@pytest.mark.asyncio
async def test_mention_triggers_ask(tmp_db, settings, monkeypatch):
    async def fake_complete(settings, db, system, user, **kwargs):
        assert "Question:" in user
        assert "island" in user.lower()
        return "definitely musk"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    await bot.handle(
        _msg(
            text="@grok trump or musk on the island who wins",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=300,
        )
    )
    assert signal.groups
    assert signal.groups[-1][1] == "definitely musk"
    assert signal.group_quotes[-1][0] == 300


@pytest.mark.asyncio
async def test_ask_includes_recent_chat_when_available(tmp_db, settings, monkeypatch):
    captured: list[str] = []

    async def fake_complete(settings, db, system, user, **kwargs):
        captured.append(user)
        return "answer"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    tmp_db.insert_body(aci, 10, "we should do pizza")
    await bot.handle(
        _msg(
            text="@grok what food did we want",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=301,
        )
    )
    assert "Recent chat (2 kept messages):" in captured[0]
    assert "Asked by: Suisei" in captured[0]
    assert "Channel: group chat" in captured[0]
    assert "pizza" in captured[0]


@pytest.mark.asyncio
async def test_dm_ask_omits_group_context(tmp_db, settings, monkeypatch):
    captured: list[str] = []

    async def fake_complete(settings, db, system, user, **kwargs):
        captured.append(user)
        return "dm answer"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    tmp_db.insert_body(aci, 10, "secret group pizza talk")
    await bot.handle(_msg(sender_aci=aci, text="what did we want for food", group_id=None))
    assert captured
    assert "Channel: private DM" in captured[0]
    assert "Recent chat" not in captured[0]
    assert "pizza" not in captured[0]
    assert signal.dms[-1][1] == "dm answer"


@pytest.mark.asyncio
async def test_ask_quote_reply_without_mention(tmp_db, settings, monkeypatch):
    calls: list[str] = []
    fixed_now = 1_700_000_000

    async def fake_complete(settings, db, system, user, **kwargs):
        calls.append(system)
        return "answer one" if len(calls) == 1 else "answer two"

    monkeypatch.setattr("sigsummerrise.bot.time.time", lambda: fixed_now)
    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    await bot.handle(
        _msg(
            text="@bot who wins",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=100,
        )
    )
    bot_ts = signal.ts
    await bot.handle(
        _msg(
            text="why though",
            quote_timestamp=bot_ts,
            timestamp=101,
            mentioned_uuids=[],
        )
    )
    assert len(calls) == 2
    assert calls[1] == render_system_prompt(
        get_prompts().followup_system,
        bot_name=settings.bot_name,
        current_time=format_current_time(fixed_now, settings.bot_timezone),
        group_name=settings.group_name,
    )
    assert signal.groups[-1][1] == "answer two"


@pytest.mark.asyncio
async def test_quote_reply_to_user_thread_ignored(tmp_db, settings, monkeypatch):
    calls = 0

    async def fake_complete(settings, db, system, user, **kwargs):
        nonlocal calls
        calls += 1
        return "should not run"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    summary_id = tmp_db.save_summary("abc123", 5000, [1], "first summary")
    tmp_db.add_thread(summary_id, aci, "question 1", 5001)

    await bot.handle(
        _msg(
            text="follow up to user",
            quote_timestamp=5001,
            timestamp=5003,
        )
    )
    assert calls == 0
    assert signal.groups == []


@pytest.mark.asyncio
async def test_kill_switch_sends_paused(tmp_db, settings, monkeypatch):
    from sigsummerrise.runtime import save_runtime_config

    save_runtime_config(tmp_db, {"responses_enabled": False})

    async def fake_complete(settings, db, system, user, **kwargs):
        raise AssertionError("LLM should not be called when paused")

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    await bot.handle(
        _msg(
            text="@grok who wins",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=300,
        )
    )
    assert signal.groups[-1][1] == get_responses().llm_paused


@pytest.mark.asyncio
async def test_busy_reply_while_llm_locked(tmp_db, settings, monkeypatch):
    import asyncio

    gate = asyncio.Event()

    async def slow_complete(settings, db, system, user, **kwargs):
        gate.set()
        await asyncio.sleep(0.2)
        return "slow answer"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", slow_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    other = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.upsert_user(other, "Bob")
    tmp_db.opt_in(aci, 1)
    tmp_db.opt_in(other, 1)

    first = asyncio.create_task(
        bot.handle(
            _msg(
                sender_aci=aci,
                text="@grok think hard",
                mentioned_uuids=[settings.signal_bot_aci],
                timestamp=400,
            )
        )
    )
    await gate.wait()
    await bot.handle(
        _msg(
            sender_aci=other,
            text="@grok another question",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=401,
        )
    )
    await first
    assert any(get_responses().llm_busy_group.format(name="Suisei") in text for _, text in signal.groups)


@pytest.mark.asyncio
async def test_remote_delete_removes_stored_message(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    tmp_db.insert_body(aci, 100, "please forget this")
    assert tmp_db.count_bodies(aci) == 1
    await bot.handle(
        _msg(
            text="",
            timestamp=200,
            remote_delete_timestamp=100,
        )
    )
    assert tmp_db.count_bodies(aci) == 0


@pytest.mark.asyncio
async def test_admin_delete_removes_target_message(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    alice = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    admin = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    tmp_db.upsert_user(alice, "Alice")
    tmp_db.opt_in(alice, 1)
    tmp_db.insert_body(alice, 150, "removed by admin")
    await bot.handle(
        _msg(
            sender_aci=admin,
            display_name="Admin",
            text="",
            timestamp=300,
            admin_delete_target_aci=alice,
            admin_delete_timestamp=150,
        )
    )
    assert tmp_db.count_bodies(alice) == 0


@pytest.mark.asyncio
async def test_remote_delete_redacts_summary_window(tmp_db, settings):
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    aci = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    tmp_db.upsert_user(aci, "Suisei")
    tmp_db.opt_in(aci, 1)
    msg_id = tmp_db.insert_body(aci, 100, "gone now")
    sid = tmp_db.save_summary("abc123", 5000, [msg_id], "mentioned gone now")
    await bot.handle(_msg(text="", timestamp=200, remote_delete_timestamp=100))
    assert tmp_db.count_bodies(aci) == 0
    summary = tmp_db.get_summary_by_id(sid)
    assert summary is not None
    assert summary.summary_text == REDACTED_SUMMARY


@pytest.mark.asyncio
async def test_summarize_hides_excluded_member_from_other_requester(tmp_db, settings, monkeypatch):
    captured: list[str] = []

    async def fake_complete(settings, db, system, user, **kwargs):
        captured.append(user)
        return "summary"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    alice = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bob = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    tmp_db.upsert_user(alice, "Alice")
    tmp_db.upsert_user(bob, "Bob")
    tmp_db.opt_in(alice, 1)
    tmp_db.opt_in(bob, 1)
    tmp_db.set_privacy_flags(alice, exclude_from_summaries=True, exclude_from_questions=False)
    tmp_db.insert_body(alice, 10, "alice secret")
    tmp_db.insert_body(bob, 11, "bob public")

    await bot.handle(
        _msg(
            sender_aci=bob,
            text="@grok summarize the past 5 messages",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=300,
        )
    )
    assert "alice secret" not in captured[0]
    assert "bob public" in captured[0]

    captured.clear()
    await bot.handle(
        _msg(
            sender_aci=alice,
            text="@grok summarize the past 5 messages",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=301,
        )
    )
    assert "alice secret" in captured[0]


@pytest.mark.asyncio
async def test_ask_hides_excluded_member_from_other_requester(tmp_db, settings, monkeypatch):
    captured: list[str] = []

    async def fake_complete(settings, db, system, user, **kwargs):
        captured.append(user)
        return "answer"

    monkeypatch.setattr("sigsummerrise.bot.llm.complete", fake_complete)
    signal = FakeSignal()
    bot = Bot(settings, tmp_db, signal)
    alice = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    bob = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    tmp_db.upsert_user(alice, "Alice")
    tmp_db.upsert_user(bob, "Bob")
    tmp_db.opt_in(alice, 1)
    tmp_db.opt_in(bob, 1)
    tmp_db.set_privacy_flags(alice, exclude_from_summaries=False, exclude_from_questions=True)
    tmp_db.insert_body(alice, 10, "alice pizza")
    tmp_db.insert_body(bob, 11, "bob tacos")

    await bot.handle(
        _msg(
            sender_aci=bob,
            text="@grok what food",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=300,
        )
    )
    assert "alice pizza" not in captured[0]
    assert "bob tacos" in captured[0]

    captured.clear()
    await bot.handle(
        _msg(
            sender_aci=alice,
            text="@grok what food",
            mentioned_uuids=[settings.signal_bot_aci],
            timestamp=301,
        )
    )
    assert "alice pizza" in captured[0]
