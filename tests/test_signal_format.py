from sigsummerrise.signal_format import markdown_to_signal


def test_bold_and_italic():
    plain, styles = markdown_to_signal("This is **bold** and *italic*.")
    assert plain == "This is bold and italic."
    assert "8:4:BOLD" in styles
    assert "17:6:ITALIC" in styles


def test_spoiler_and_monospace():
    plain, styles = markdown_to_signal("secret: ||nope|| and `code`")
    assert plain == "secret: nope and code"
    assert any(s.endswith(":SPOILER") for s in styles)
    assert any(s.endswith(":MONOSPACE") for s in styles)


def test_utf16_offsets_with_emoji():
    plain, styles = markdown_to_signal("hi 👋 **there**")
    assert plain == "hi 👋 there"
    assert styles == ["6:5:BOLD"]


def test_send_applies_styles(monkeypatch):
    from sigsummerrise.signal_rpc import SignalClient

    client = SignalClient("http://127.0.0.1:9", "+15555550100")
    captured: list[dict] = []

    async def fake_rpc(method, params=None):
        captured.append({"method": method, "params": params or {}})
        return {"timestamp": 999}

    client.rpc = fake_rpc  # type: ignore[method-assign]

    import asyncio

    asyncio.run(
        client.send_group(
            "abc123",
            "Hello **world**",
            quote_timestamp=42,
            quote_author="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            quote_message="*original*",
        )
    )
    params = captured[0]["params"]
    assert params["message"] == "Hello world"
    assert params["textStyles"] == ["6:5:BOLD"]
    assert params["quoteMessage"] == "original"
    assert params["quoteTextStyles"] == ["0:8:ITALIC"]
