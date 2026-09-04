from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urljoin

import httpx

from sigsummerrise.signal_format import markdown_to_signal

log = logging.getLogger("sigsummerrise.signal")

# Signal shows typing for 15s unless STOP is sent; refresh before that.
TYPING_REFRESH_SECONDS = 10.0


@dataclass
class IncomingMessage:
    sender_aci: str
    display_name: str
    timestamp: int
    text: str
    group_id: str | None
    expires_in_seconds: int
    mentioned_uuids: list[str]
    quote_timestamp: int | None
    is_reaction: bool
    has_attachments: bool
    quote_author_aci: str | None = None
    remote_delete_timestamp: int | None = None
    admin_delete_target_aci: str | None = None
    admin_delete_timestamp: int | None = None

    @property
    def is_dm(self) -> bool:
        return not self.group_id

    @property
    def deleted_message(self) -> tuple[str, int] | None:
        """Author ACI and original message timestamp for delete-for-everyone."""
        if self.remote_delete_timestamp:
            return (self.sender_aci, self.remote_delete_timestamp)
        if self.admin_delete_target_aci and self.admin_delete_timestamp:
            return (self.admin_delete_target_aci, self.admin_delete_timestamp)
        return None

    @property
    def has_attachments_only(self) -> bool:
        return self.has_attachments and not (self.text or "").strip()

    def mentions_bot(self, bot_aci: str) -> bool:
        if not bot_aci:
            return False
        needle = bot_aci.strip().lower()
        return needle in self.mentioned_uuids


def normalize_group_id(value: str | None) -> str:
    if not value:
        return ""
    g = value.strip()
    if g.lower().startswith("group."):
        g = g[6:]
    return g.rstrip("=").replace(" ", "+")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def quote_preview(text: str, max_len: int = 200) -> str:
    preview = (text or "").strip().replace("\n", " ")
    if len(preview) <= max_len:
        return preview
    return preview[: max_len - 1] + "…"


def parse_receive(payload: dict[str, Any]) -> IncomingMessage | None:
    params = payload.get("params") if payload.get("method") == "receive" else payload
    params = _as_dict(params)
    result = _as_dict(params.get("result"))
    envelope = _as_dict(params.get("envelope") or result.get("envelope"))
    if not envelope:
        return None
    data = _as_dict(envelope.get("dataMessage"))
    if not data:
        return None
    is_reaction = bool(data.get("reaction"))
    sender_aci = str(envelope.get("sourceUuid") or "").strip().lower()
    if not sender_aci:
        return None
    group = _as_dict(data.get("groupInfo") or data.get("groupContext"))
    raw_gid = group.get("groupId") or group.get("group_id")
    mentions = []
    for item in data.get("mentions") or []:
        if isinstance(item, dict):
            uid = item.get("uuid") or item.get("aci")
            if uid:
                mentions.append(str(uid).strip().lower())
    quote = _as_dict(data.get("quote"))
    quote_ts = quote.get("id") or quote.get("timestamp")
    quote_author_raw = (
        quote.get("authorUuid")
        or quote.get("author")
        or quote.get("authorAci")
        or quote.get("quoteAuthor")
    )
    quote_author_aci = str(quote_author_raw).strip().lower() if quote_author_raw else None
    expires = data.get("expiresInSeconds")
    if expires is None:
        expires = data.get("expireTimer") or 0
    try:
        expires_in = int(expires or 0)
    except (TypeError, ValueError):
        expires_in = 0
    attachments = data.get("attachments") or []
    text = data.get("message") or data.get("text") or ""
    if text is None:
        text = ""
    try:
        timestamp = int(data.get("timestamp") or envelope.get("timestamp") or 0)
    except (TypeError, ValueError):
        timestamp = 0
    display = str(envelope.get("sourceName") or "").strip()
    remote_delete = _as_dict(data.get("remoteDelete"))
    remote_delete_ts = None
    if remote_delete.get("timestamp") is not None:
        try:
            remote_delete_ts = int(remote_delete["timestamp"])
        except (TypeError, ValueError):
            remote_delete_ts = None
    admin_delete = _as_dict(data.get("adminDelete"))
    admin_target_aci = None
    admin_delete_ts = None
    target_raw = admin_delete.get("targetAuthorUuid") or admin_delete.get("targetAuthor")
    if target_raw:
        admin_target_aci = str(target_raw).strip().lower()
    if admin_delete.get("targetSentTimestamp") is not None:
        try:
            admin_delete_ts = int(admin_delete["targetSentTimestamp"])
        except (TypeError, ValueError):
            admin_delete_ts = None
    return IncomingMessage(
        sender_aci=sender_aci,
        display_name=display,
        timestamp=timestamp,
        text=str(text),
        group_id=str(raw_gid) if raw_gid else None,
        expires_in_seconds=expires_in,
        mentioned_uuids=mentions,
        quote_timestamp=int(quote_ts) if quote_ts else None,
        quote_author_aci=quote_author_aci,
        is_reaction=is_reaction,
        has_attachments=bool(attachments),
        remote_delete_timestamp=remote_delete_ts,
        admin_delete_target_aci=admin_target_aci,
        admin_delete_timestamp=admin_delete_ts,
    )


def extract_aci_from_accounts(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, dict):
        for key in ("aci", "uuid", "numberUuid"):
            if result.get(key):
                return str(result[key]).strip().lower()
        accounts = result.get("accounts") or result.get("result")
        return extract_aci_from_accounts(accounts)
    if isinstance(result, list):
        for item in result:
            found = extract_aci_from_accounts(item)
            if found:
                return found
    if isinstance(result, str) and "-" in result and len(result) >= 32:
        return result.strip().lower()
    return None


class SignalClient:
    def __init__(self, base_url: str, account: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.account = account
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": str(uuid.uuid4()),
        }
        payload_params = dict(params or {})
        if self.account and "account" not in payload_params:
            payload_params["account"] = self.account
        if payload_params:
            body["params"] = payload_params
        url = urljoin(self.base_url + "/", "api/v1/rpc")
        response = await self._http().post(url, json=body)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise RuntimeError("signal-cli rpc error")
        return data.get("result")

    async def get_bot_aci(self) -> str | None:
        try:
            result = await self.rpc("listAccounts")
        except Exception:
            log.exception("listAccounts failed")
            return None
        return extract_aci_from_accounts(result)

    async def send_dm(self, recipient_aci: str, message: str) -> int | None:
        plain, text_styles = markdown_to_signal(message)
        params: dict[str, Any] = {"recipient": [recipient_aci], "message": plain}
        if text_styles:
            params["textStyles"] = text_styles
        result = await self.rpc("send", params)
        return _send_timestamp(result)

    async def send_group(
        self,
        group_id: str,
        message: str,
        *,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
    ) -> int | None:
        plain, text_styles = markdown_to_signal(message)
        params: dict[str, Any] = {"groupId": group_id, "message": plain}
        if text_styles:
            params["textStyles"] = text_styles
        if quote_timestamp is not None and quote_author:
            params["quoteTimestamp"] = quote_timestamp
            params["quoteAuthor"] = quote_author
            if quote_message:
                quote_plain, quote_styles = markdown_to_signal(quote_message)
                params["quoteMessage"] = quote_plain
                if quote_styles:
                    params["quoteTextStyles"] = quote_styles
        result = await self.rpc("send", params)
        return _send_timestamp(result)

    async def send_typing(
        self,
        *,
        group_id: str | None = None,
        recipient: str | None = None,
        stop: bool = False,
    ) -> None:
        params: dict[str, Any] = {}
        if group_id:
            params["groupId"] = group_id
        elif recipient:
            params["recipient"] = [recipient]
        else:
            return
        if stop:
            params["stop"] = True
        try:
            await self.rpc("sendTyping", params)
        except Exception:
            log.exception("sendTyping failed")

    @asynccontextmanager
    async def keep_typing(
        self,
        *,
        group_id: str | None = None,
        recipient: str | None = None,
    ) -> AsyncIterator[None]:
        if not group_id and not recipient:
            yield
            return
        await self.send_typing(group_id=group_id, recipient=recipient, stop=False)
        done = asyncio.Event()

        async def refresh() -> None:
            while True:
                try:
                    await asyncio.wait_for(done.wait(), timeout=TYPING_REFRESH_SECONDS)
                    return
                except asyncio.TimeoutError:
                    await self.send_typing(group_id=group_id, recipient=recipient, stop=False)

        task = asyncio.create_task(refresh())
        try:
            yield
        finally:
            done.set()
            await task
            await self.send_typing(group_id=group_id, recipient=recipient, stop=True)

    async def events(self):
        url = urljoin(self.base_url + "/", "api/v1/events")
        async with self._http().stream("GET", url, timeout=None) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                data_line = line
                if line.startswith("data:"):
                    data_line = line[5:].strip()
                elif line.startswith(":"):
                    continue
                try:
                    payload = json.loads(data_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                incoming = parse_receive(payload)
                if incoming is not None:
                    yield incoming


def _send_timestamp(result: Any) -> int | None:
    if result is None:
        return None
    if isinstance(result, bool):
        return None
    if isinstance(result, int):
        return result if result > 0 else None
    if isinstance(result, dict):
        for key in ("timestamp", "messageTimestamp", "sentTimestamp"):
            value = result.get(key)
            if value is not None and not isinstance(value, bool):
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    parsed = 0
                if parsed > 0:
                    return parsed
        inner = result.get("result")
        if inner is not None:
            found = _send_timestamp(inner)
            if found is not None:
                return found
        results = result.get("results")
        if isinstance(results, list):
            for item in results:
                found = _send_timestamp(item)
                if found is not None:
                    return found
        return None
    if isinstance(result, list):
        for item in result:
            found = _send_timestamp(item)
            if found is not None:
                return found
    return None
