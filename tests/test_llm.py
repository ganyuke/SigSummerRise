import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from sigsummerrise.config import Settings
from sigsummerrise.llm import (
    LlmTimeoutError,
    _build_provider_payload,
    _parse_stream_line,
    complete,
)
from sigsummerrise.runtime import ResolvedLlmConfig


def test_parse_stream_line_delta():
    line = 'data: {"choices":[{"delta":{"content":"hello"}}],"model":"m","provider":"p"}'
    chunk, meta = _parse_stream_line(line)
    assert chunk == "hello"
    assert meta is not None
    assert meta["provider"] == "p"


def test_build_provider_payload_merges_ops_fields():
    cfg = ResolvedLlmConfig(
        openrouter_api_key="k",
        openrouter_model="m",
        llm_temperature=None,
        llm_max_tokens=None,
        llm_calls_per_hour=10,
        ask_context_n=0,
        max_n=50,
        api_key_configured=True,
        api_key_suffix="k",
        responses_enabled=True,
        llm_timeout_seconds=60,
        llm_read_idle_seconds=30,
        provider_order=("a", "b"),
        provider_ignore=("c",),
        provider_sort="latency",
    )
    payload = _build_provider_payload(cfg)
    assert payload == {
        "zdr": True,
        "data_collection": "deny",
        "order": ["a", "b"],
        "ignore": ["c"],
        "sort": "latency",
    }


@pytest.mark.asyncio
async def test_complete_accumulates_stream_chunks(tmp_db, settings, monkeypatch):
    settings = settings.model_copy(update={"openrouter_api_key": "test-key"})
    chunks = [
        'data: {"choices":[{"delta":{"content":"hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}],"model":"test/model","provider":"Acme","usage":{"prompt_tokens":1,"completion_tokens":2,"cost":0.01}}',
        "data: [DONE]",
    ]

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        @property
        def status_code(self):
            return 200

        async def aiter_lines(self):
            for line in chunks:
                yield line

        async def aread(self):
            return b""

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=FakeStream())
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    seen: list[str] = []

    monkeypatch.setattr(
        "sigsummerrise.llm.httpx.AsyncClient",
        lambda *args, **kwargs: fake_client,
    )

    row_id = tmp_db.record_llm_call("user-aci", 1_700_000_000)
    text = await complete(
        settings,
        tmp_db,
        "system",
        "user",
        issuance_id=row_id,
        on_chunk=seen.append,
    )
    assert text == "hello"
    assert seen == ["hel", "lo"]
    row = tmp_db.connect().execute(
        "SELECT latency_ms, model, provider, outcome FROM llm_issuance WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row["outcome"] == "ok"
    assert row["model"] == "test/model"
    assert row["provider"] == "Acme"
    assert row["latency_ms"] is not None


@pytest.mark.asyncio
async def test_complete_wall_clock_timeout(tmp_db, settings, monkeypatch):
    settings = settings.model_copy(update={"openrouter_api_key": "test-key"})

    async def slow_stream(*args, **kwargs):
        await asyncio.sleep(0.2)
        return ("never", {})

    monkeypatch.setattr("sigsummerrise.llm._stream_completion", slow_stream)
    cfg = ResolvedLlmConfig(
        openrouter_api_key="test-key",
        openrouter_model="m",
        llm_temperature=None,
        llm_max_tokens=None,
        llm_calls_per_hour=10,
        ask_context_n=0,
        max_n=50,
        api_key_configured=True,
        api_key_suffix="key",
        responses_enabled=True,
        llm_timeout_seconds=0.05,
        llm_read_idle_seconds=30,
        provider_order=(),
        provider_ignore=(),
        provider_sort="",
    )
    row_id = tmp_db.record_llm_call("user-aci", 1_700_000_000)
    with pytest.raises(LlmTimeoutError):
        await complete(
            settings,
            tmp_db,
            "system",
            "user",
            issuance_id=row_id,
            config=cfg,
        )
    row = tmp_db.connect().execute(
        "SELECT outcome FROM llm_issuance WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row["outcome"] == "timeout"
