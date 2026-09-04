import asyncio

import pytest

from sigsummerrise.signal_rpc import (
    SignalClient,
    extract_aci_from_accounts,
    parse_receive,
    quote_preview,
    _send_timestamp,
)


SAMPLE = {
    "jsonrpc": "2.0",
    "method": "receive",
    "params": {
        "envelope": {
            "sourceNumber": "+15555550123",
            "sourceUuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "sourceName": "Suisei",
            "timestamp": 1631458508784,
            "dataMessage": {
                "timestamp": 1631458508784,
                "message": "@grok summarize the past 5 messages",
                "expiresInSeconds": 0,
                "mentions": [{"uuid": "11111111-1111-1111-1111-111111111111", "start": 0, "length": 5}],
                "attachments": [],
                "groupInfo": {"groupId": "abc123=="},
                "quote": {"id": 42},
            },
        }
    },
}


def test_parse_strips_phone_and_keeps_aci():
    incoming = parse_receive(SAMPLE)
    assert incoming is not None
    assert incoming.sender_aci == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert incoming.display_name == "Suisei"
    assert incoming.expires_in_seconds == 0
    assert incoming.quote_timestamp == 42
    assert incoming.mentions_bot("11111111-1111-1111-1111-111111111111")
    assert not incoming.mentions_bot("not-the-bot")
    assert incoming.group_id == "abc123=="
    dumped = repr(incoming)
    assert "+15555550123" not in dumped
    assert not hasattr(incoming, "sourceNumber")


def test_parse_disappearing_and_dm():
    payload = {
        "method": "receive",
        "params": {
            "envelope": {
                "sourceUuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "sourceName": "Sam",
                "timestamp": 2,
                "dataMessage": {"message": "Yes", "expiresInSeconds": 30, "timestamp": 2},
            }
        },
    }
    incoming = parse_receive(payload)
    assert incoming is not None
    assert incoming.is_dm
    assert incoming.expires_in_seconds == 30


def test_extract_aci():
    assert extract_aci_from_accounts([{"number": "+1", "uuid": "cccccc"}]) == "cccccc"


def test_text_at_name_is_not_a_mention():
    payload = {
        "method": "receive",
        "params": {
            "envelope": {
                "sourceUuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "sourceName": "Suisei",
                "timestamp": 3,
                "dataMessage": {
                    "message": "@grok status",
                    "timestamp": 3,
                    "mentions": [],
                    "groupInfo": {"groupId": "abc123"},
                },
            }
        },
    }
    incoming = parse_receive(payload)
    assert incoming is not None
    assert not incoming.mentions_bot("11111111-1111-1111-1111-111111111111")


def test_parse_remote_delete():
    payload = {
        "method": "receive",
        "params": {
            "envelope": {
                "sourceUuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "sourceName": "Suisei",
                "timestamp": 200,
                "dataMessage": {
                    "timestamp": 200,
                    "message": None,
                    "remoteDelete": {"timestamp": 100},
                    "groupInfo": {"groupId": "abc123=="},
                },
            }
        },
    }
    incoming = parse_receive(payload)
    assert incoming is not None
    assert incoming.deleted_message == ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 100)
    assert incoming.text == ""


def test_parse_admin_delete():
    payload = {
        "method": "receive",
        "params": {
            "envelope": {
                "sourceUuid": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "sourceName": "Admin",
                "timestamp": 300,
                "dataMessage": {
                    "timestamp": 300,
                    "adminDelete": {
                        "targetAuthorUuid": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        "targetSentTimestamp": 150,
                    },
                    "groupInfo": {"groupId": "abc123=="},
                },
            }
        },
    }
    incoming = parse_receive(payload)
    assert incoming is not None
    assert incoming.deleted_message == ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", 150)


def test_send_timestamp_nested_shapes():
    assert _send_timestamp({"timestamp": 99}) == 99
    assert _send_timestamp({"results": [{"timestamp": 77}]}) == 77
    assert _send_timestamp({"result": {"timestamp": 55}}) == 55
    assert _send_timestamp(12345) == 12345
    assert _send_timestamp(None) is None
    assert _send_timestamp({}) is None


def test_quote_preview_truncates():
    assert quote_preview("hello") == "hello"
    assert quote_preview("x" * 250).endswith("…")
    assert len(quote_preview("x" * 250)) == 200


@pytest.mark.asyncio
async def test_send_group_includes_quote_params(monkeypatch):
    client = SignalClient("http://127.0.0.1:9", "+15555550100")
    captured: list[dict] = []

    async def fake_rpc(method, params=None):
        captured.append({"method": method, "params": params or {}})
        return {"timestamp": 999}

    client.rpc = fake_rpc  # type: ignore[method-assign]
    ts = await client.send_group(
        "abc123",
        "reply text",
        quote_timestamp=42,
        quote_author="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        quote_message="original",
    )
    assert ts == 999
    assert captured[0]["params"]["quoteTimestamp"] == 42
    assert captured[0]["params"]["quoteAuthor"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert captured[0]["params"]["quoteMessage"] == "original"


@pytest.mark.asyncio
async def test_keep_typing_refreshes_then_stops(monkeypatch):
    monkeypatch.setattr("sigsummerrise.signal_rpc.TYPING_REFRESH_SECONDS", 0.05)
    client = SignalClient("http://127.0.0.1:9", "+15555550100")
    events: list[bool] = []

    async def fake_send_typing(*, group_id=None, recipient=None, stop=False):
        events.append(stop)

    client.send_typing = fake_send_typing  # type: ignore[method-assign]
    async with client.keep_typing(group_id="abc123"):
        await asyncio.sleep(0.12)
    assert events[0] is False
    assert events[-1] is True
    assert events.count(False) >= 2
