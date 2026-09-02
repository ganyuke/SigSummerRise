from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sigsummerrise import auth, collect, commands, consent, llm
from sigsummerrise.commands import Intent, help_text, pick_unknown_reply
from sigsummerrise.config import Settings
from sigsummerrise.db import Database
from sigsummerrise.responses import get_responses, init_responses
from sigsummerrise.signal_rpc import IncomingMessage, SignalClient, normalize_group_id

log = logging.getLogger("sigsummerrise.bot")


class Bot:
    def __init__(self, settings: Settings, db: Database, signal: SignalClient | None = None) -> None:
        init_responses(settings.responses_path)
        self.settings = settings
        self.db = db
        self.signal = signal or SignalClient(settings.signal_http_url, settings.signal_account)
        self.bot_aci = (settings.signal_bot_aci or "").strip().lower()
        self.configured_group = normalize_group_id(settings.signal_group_id)
        self.copy = get_responses()

    async def run(self) -> None:
        if not self.configured_group:
            log.error("SIGNAL_GROUP_ID is required; bot not starting")
            return
        delay = 2.0
        while True:
            try:
                if not self.bot_aci:
                    aci = await self.signal.get_bot_aci()
                    if aci:
                        self.bot_aci = aci.strip().lower()
                    else:
                        log.error("SIGNAL_BOT_ACI is not set and listAccounts returned nothing")
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 30.0)
                        continue
                log.info("connecting to signal-cli event stream")
                async for incoming in self.signal.events():
                    delay = 2.0
                    try:
                        await self.handle(incoming)
                    except Exception:
                        log.exception("failed to handle inbound event")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("signal-cli event stream disconnected")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def handle(self, incoming: IncomingMessage) -> None:
        if self.bot_aci and incoming.sender_aci == self.bot_aci:
            return
        now = int(time.time())
        if incoming.is_dm:
            await self._handle_dm(incoming, now)
            return
        if not self.configured_group:
            return
        if normalize_group_id(incoming.group_id) != self.configured_group:
            return
        await self._handle_group(incoming, now)

    async def _handle_dm(self, incoming: IncomingMessage, now: int) -> None:
        user = self.db.upsert_user(incoming.sender_aci, incoming.display_name)
        intent = commands.parse_intent(
            incoming.text, mentioned=True, in_dm=True, max_n=self.settings.max_n
        )
        if intent.name == "yes":
            self.db.opt_in(incoming.sender_aci, now)
            await self.signal.send_dm(incoming.sender_aci, self.copy.opted_in)
            return
        if intent.name == "no":
            if user.opted_in:
                self.db.opt_out(incoming.sender_aci)
                await self.signal.send_dm(incoming.sender_aci, self.copy.opted_out)
            else:
                self.db.decline(incoming.sender_aci)
                await self.signal.send_dm(incoming.sender_aci, self.copy.declined)
            return
        if intent.name == "help":
            await self.signal.send_dm(incoming.sender_aci, help_text())
            return
        if not user.opted_in:
            if consent.should_send_consent_dm(user.consent_state, user.last_consent_dm_at, now):
                await self._maybe_consent_dm(
                    incoming.sender_aci, user.consent_state, user.last_consent_dm_at, now
                )
            else:
                await self.signal.send_dm(incoming.sender_aci, self.copy.consent_clarify)
            return
        await self._run_intent(intent, incoming, now, in_group=False)

    async def _handle_group(self, incoming: IncomingMessage, now: int) -> None:
        user = self.db.upsert_user(incoming.sender_aci, incoming.display_name)
        mentioned = incoming.mentions_bot(self.bot_aci)
        action = collect.classify_inbound(
            expires_in_seconds=incoming.expires_in_seconds,
            text=incoming.text,
            is_bot=False,
            opted_in=user.opted_in,
            is_reaction=incoming.is_reaction,
            has_attachments_only=incoming.has_attachments_only,
        )
        if action == "body":
            self.db.insert_body(incoming.sender_aci, incoming.timestamp, incoming.text.strip())
        elif action == "hole":
            self.db.insert_hole(incoming.timestamp)

        if (
            incoming.quote_timestamp
            and user.opted_in
            and (incoming.text or "").strip()
            and not incoming.is_reaction
        ):
            summary = self.db.get_summary_by_timestamp(incoming.quote_timestamp)
            if summary is not None:
                await self._follow_up(summary, incoming)
                return

        if not mentioned:
            return
        intent = commands.parse_intent(
            incoming.text, mentioned=True, in_dm=False, max_n=self.settings.max_n
        )
        if not user.opted_in:
            if intent.name == "help":
                await self._reply(incoming, help_text(), True)
                return
            await self._maybe_consent_dm(
                incoming.sender_aci, user.consent_state, user.last_consent_dm_at, now
            )
            if consent.should_roast_unopted_mention():
                await self.signal.send_group(
                    incoming.group_id or self.settings.signal_group_id,
                    consent.pick_group_roast(),
                )
            return
        await self._run_intent(intent, incoming, now, in_group=True)

    async def _maybe_consent_dm(self, aci: str, state: str, last_dm_at: int | None, now: int) -> None:
        if not consent.should_send_consent_dm(state, last_dm_at, now):
            return
        await self.signal.send_dm(aci, self.copy.consent_dm)
        self.db.set_consent_dm_at(aci, now)

    async def _run_intent(self, intent: Intent, incoming: IncomingMessage, now: int, in_group: bool) -> None:
        if intent.name == "opt_out":
            self.db.opt_out(incoming.sender_aci)
            await self._reply(incoming, self.copy.opted_out, in_group)
            return
        if intent.name == "status":
            user = self.db.get_user(incoming.sender_aci)
            count = self.db.count_bodies(incoming.sender_aci)
            when = _format_ts(user.opted_in_at if user else None)
            await self._reply(
                incoming,
                self.copy.format_status(count, when),
                in_group,
            )
            return
        if intent.name == "dashboard":
            await self._send_dashboard_link(incoming, now, in_group)
            return
        if intent.name == "summarize":
            if not in_group:
                await self.signal.send_dm(incoming.sender_aci, self.copy.summarize_in_dm)
                return
            await self._summarize(incoming, intent.n or 1)
            return
        if intent.name == "help":
            await self._reply(incoming, help_text(), in_group)
            return
        await self._reply(incoming, pick_unknown_reply(), in_group)

    async def _send_dashboard_link(self, incoming: IncomingMessage, now: int, in_group: bool) -> None:
        url = auth.issue_magic_link(self.db, self.settings, incoming.sender_aci, now)
        if url is None:
            await self._reply(incoming, self.copy.dashboard_rate, in_group)
            return
        await self.signal.send_dm(
            incoming.sender_aci,
            self.copy.dashboard_dm.format(url=url),
        )
        if in_group:
            await self.signal.send_group(
                incoming.group_id or self.settings.signal_group_id,
                self.copy.dashboard_group,
            )

    def _allow_llm(self, aci: str, now: int) -> bool:
        if not auth.can_issue_link(self.db.llm_count(aci, now), self.settings.llm_calls_per_hour):
            return False
        self.db.record_llm_call(aci, now)
        return True

    async def _summarize(self, incoming: IncomingMessage, n: int) -> None:
        kept = self.db.last_n_kept(n)
        if not kept:
            await self.signal.send_group(
                incoming.group_id or self.settings.signal_group_id,
                self.copy.empty_window,
            )
            return
        now = int(time.time())
        if not self._allow_llm(incoming.sender_aci, now):
            await self.signal.send_group(
                incoming.group_id or self.settings.signal_group_id,
                self.copy.llm_rate,
            )
            return
        lines = collect.format_window(kept)
        user_block = "\n".join(lines)
        group_id = incoming.group_id or self.settings.signal_group_id
        try:
            async with self.signal.keep_typing(group_id=group_id):
                text = await llm.complete(self.settings, llm.SUMMARIZE_SYSTEM, user_block)
        except llm.LlmError:
            await self.signal.send_group(group_id, self.copy.llm_fail)
            return
        ts = await self.signal.send_group(
            incoming.group_id or self.settings.signal_group_id,
            text,
        )
        if ts is None:
            log.warning("summary sent but signal-cli returned no timestamp; follow-ups will not bind")
            return
        self.db.save_summary(
            incoming.group_id or self.settings.signal_group_id,
            ts,
            [m.id for m in kept],
            text,
        )

    async def _follow_up(self, summary, incoming: IncomingMessage) -> None:
        now = int(time.time())
        if not self._allow_llm(incoming.sender_aci, now):
            await self.signal.send_group(
                incoming.group_id or self.settings.signal_group_id,
                self.copy.llm_rate,
            )
            return
        question = incoming.text.strip()
        self.db.add_thread(summary.id, incoming.sender_aci, question, incoming.timestamp)
        by_id = self.db.get_messages_by_ids(summary.window_ids)
        excerpt = "\n".join(collect.format_window_from_ids(summary.window_ids, by_id))
        thread_lines = collect.format_thread(self.db.get_thread(summary.id))
        user_block = (
            "Summary:\n"
            f"{summary.summary_text}\n\n"
            "Chat excerpt:\n"
            f"{excerpt}\n\n"
            "Follow-up:\n"
            + "\n".join(thread_lines)
        )
        group_id = incoming.group_id or self.settings.signal_group_id
        try:
            async with self.signal.keep_typing(group_id=group_id):
                answer = await llm.complete(self.settings, llm.FOLLOWUP_SYSTEM, user_block)
        except llm.LlmError:
            await self.signal.send_group(group_id, self.copy.llm_fail)
            return
        ts = await self.signal.send_group(
            incoming.group_id or self.settings.signal_group_id,
            answer,
        )
        self.db.add_thread(summary.id, None, answer, ts or int(time.time() * 1000))

    async def _reply(self, incoming: IncomingMessage, text: str, in_group: bool) -> None:
        if in_group:
            await self.signal.send_group(
                incoming.group_id or self.settings.signal_group_id,
                text,
            )
        else:
            await self.signal.send_dm(incoming.sender_aci, text)


def _format_ts(ts: int | None) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
