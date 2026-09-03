from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from sigsummerrise import activity, auth, collect, commands, consent, health, llm
from sigsummerrise.activity import Mode
from sigsummerrise.commands import Intent, help_text, normalize_command_text
from sigsummerrise.config import Settings
from sigsummerrise.db import Database, User
from sigsummerrise.prompts import format_current_time, init_prompts, render_system_prompt
from sigsummerrise.responses import get_responses, init_responses
from sigsummerrise.runtime import resolve_llm_config, resolve_prompts
from sigsummerrise.signal_rpc import IncomingMessage, SignalClient, normalize_group_id, quote_preview

log = logging.getLogger("sigsummerrise.bot")


class Bot:
    def __init__(self, settings: Settings, db: Database, signal: SignalClient | None = None) -> None:
        init_responses(settings.responses_path)
        init_prompts(settings.prompts_path)
        self.settings = settings
        self.db = db
        self.signal = signal or SignalClient(settings.signal_http_url, settings.signal_account)
        self.bot_aci = (settings.signal_bot_aci or "").strip().lower()
        self.configured_group = normalize_group_id(settings.signal_group_id)
        self.copy = get_responses()

    def _runtime(self):
        return resolve_llm_config(self.settings, self.db)

    def _prompts(self):
        return resolve_prompts(self.settings, self.db)

    def _max_n(self) -> int:
        return self._runtime().max_n

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
        health.stamp_signal_event(now)
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
            incoming.text, mentioned=True, in_dm=True, max_n=self._max_n()
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
            summary = self.db.get_summary_for_quote(incoming.quote_timestamp)
            if summary is not None:
                await self._follow_up(summary, incoming)
                return

        if not mentioned:
            return
        intent = commands.parse_intent(
            incoming.text, mentioned=True, in_dm=False, max_n=self._max_n()
        )
        if not user.opted_in:
            if intent.name == "help":
                await self._reply(incoming, help_text(), True)
                return
            await self._maybe_consent_dm(
                incoming.sender_aci, user.consent_state, user.last_consent_dm_at, now
            )
            await self._maybe_unopted_group_reply(incoming, user, now)
            return
        await self._run_intent(intent, incoming, now, in_group=True)

    async def _maybe_unopted_group_reply(
        self,
        incoming: IncomingMessage,
        user: User,
        now: int,
    ) -> None:
        if consent.should_send_unopted_group_notice(user.last_unopted_group_notice_at, now):
            await self._reply(incoming, self.copy.unopted_group_notice, True)
            self.db.set_unopted_group_notice_at(incoming.sender_aci, now)
            return
        if consent.should_roast_unopted_mention():
            await self._reply(incoming, consent.pick_group_roast(), True)

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
        if intent.name == "ask":
            await self._ask(incoming, now)
            return
        await self._reply(incoming, help_text(), in_group)

    def _llm_ctx(self) -> collect.LlmFormatContext:
        return collect.LlmFormatContext(
            tz_name=self.settings.bot_timezone,
            bot_name=self.settings.bot_name,
        )

    def _llm_system(self, template: str, now: int) -> str:
        return render_system_prompt(
            template,
            bot_name=self.settings.bot_name,
            current_time=format_current_time(now, self.settings.bot_timezone),
            group_name=self.settings.group_name,
        )

    def _asker_name(self, incoming: IncomingMessage) -> str:
        return (incoming.display_name or "").strip() or "Someone"

    @asynccontextmanager
    async def _activity(self, incoming: IncomingMessage, mode: Mode) -> AsyncIterator[None]:
        activity.set_working(
            channel="group" if incoming.group_id else "dm",
            mode=mode,
            target_aci=incoming.sender_aci,
            target_display_name=self._asker_name(incoming),
            started_at=int(time.time()),
        )
        try:
            yield
        finally:
            activity.clear()

    def _ask_context_n(self) -> int:
        n = self._runtime().ask_context_n
        if n < 0:
            return 0
        return min(n, self._max_n())

    async def _complete_llm(self, system_template: str, user_block: str, now: int, aci: str) -> str:
        issuance_id = self.db.record_llm_call(aci, now)
        system = self._llm_system(system_template, now)
        return await llm.complete(
            self.settings,
            self.db,
            system,
            user_block,
            issuance_id=issuance_id,
        )

    async def _ask(self, incoming: IncomingMessage, now: int) -> None:
        if not self._allow_llm(incoming.sender_aci, now):
            await self._reply_rate_limited(incoming, True)
            return
        question = normalize_command_text(incoming.text)
        if not question:
            await self._reply(incoming, help_text(), bool(incoming.group_id))
            return
        context_n = self._ask_context_n()
        kept = self.db.last_n_kept(context_n) if context_n else []
        ctx = self._llm_ctx()
        user_block = collect.format_ask_user_block(
            question=question,
            messages=kept,
            asker_name=self._asker_name(incoming),
            in_group=bool(incoming.group_id),
            ctx=ctx,
        )
        group_id = incoming.group_id or self.settings.signal_group_id
        in_group = bool(incoming.group_id)
        prompts = self._prompts()
        try:
            async with self._activity(incoming, "ask"):
                if in_group:
                    async with self.signal.keep_typing(group_id=group_id):
                        answer = await self._complete_llm(
                            prompts.ask_system, user_block, now, incoming.sender_aci
                        )
                else:
                    answer = await self._complete_llm(
                        prompts.ask_system, user_block, now, incoming.sender_aci
                    )
        except llm.LlmError:
            await self._reply(incoming, self.copy.llm_fail, in_group)
            return
        ts = await self._reply(incoming, answer, in_group)
        if not in_group or ts is None:
            if in_group and ts is None:
                log.warning("ask reply sent but signal-cli returned no timestamp; follow-ups will not bind")
            return
        summary_id = self.db.save_summary(
            incoming.group_id or self.settings.signal_group_id,
            ts,
            [m.id for m in kept],
            answer,
        )
        self.db.add_thread(summary_id, incoming.sender_aci, question, incoming.timestamp)
        self.db.add_thread(summary_id, None, answer, ts)

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
            await self._reply(incoming, self.copy.dashboard_group, True)

    def _allow_llm(self, aci: str, now: int) -> bool:
        limit = self._runtime().llm_calls_per_hour
        return auth.can_issue_link(self.db.llm_count(aci, now), limit)

    async def _summarize(self, incoming: IncomingMessage, n: int) -> None:
        kept = self.db.last_n_kept(n)
        if not kept:
            await self._reply(incoming, self.copy.empty_window, True)
            return
        now = int(time.time())
        if not self._allow_llm(incoming.sender_aci, now):
            await self._reply_rate_limited(incoming, True)
            return
        ctx = self._llm_ctx()
        user_block = collect.format_summarize_user_block(kept, ctx=ctx)
        group_id = incoming.group_id or self.settings.signal_group_id
        prompts = self._prompts()
        try:
            async with self._activity(incoming, "summarize"):
                async with self.signal.keep_typing(group_id=group_id):
                    text = await self._complete_llm(
                        prompts.summarize_system, user_block, now, incoming.sender_aci
                    )
        except llm.LlmError:
            await self._reply(incoming, self.copy.llm_fail, True)
            return
        ts = await self._reply(incoming, text, True)
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
            await self._reply_rate_limited(incoming, True)
            return
        question = incoming.text.strip()
        self.db.add_thread(summary.id, incoming.sender_aci, question, incoming.timestamp)
        by_id = self.db.get_messages_by_ids(summary.window_ids)
        thread_entries = self.db.get_thread(summary.id)
        user_block = collect.format_followup_user_block(
            summary_text=summary.summary_text,
            window_ids=summary.window_ids,
            by_id=by_id,
            thread_entries=thread_entries,
            asker_name=self._asker_name(incoming),
            ctx=self._llm_ctx(),
        )
        group_id = incoming.group_id or self.settings.signal_group_id
        prompts = self._prompts()
        try:
            async with self._activity(incoming, "follow_up"):
                async with self.signal.keep_typing(group_id=group_id):
                    answer = await self._complete_llm(
                        prompts.followup_system, user_block, now, incoming.sender_aci
                    )
        except llm.LlmError:
            await self._reply(incoming, self.copy.llm_fail, True)
            return
        ts = await self._reply(incoming, answer, True)
        self.db.add_thread(summary.id, None, answer, ts or int(time.time() * 1000))

    async def _reply(self, incoming: IncomingMessage, text: str, in_group: bool) -> int | None:
        if in_group:
            return await self.signal.send_group(
                incoming.group_id or self.settings.signal_group_id,
                text,
                quote_timestamp=incoming.timestamp,
                quote_author=incoming.sender_aci,
                quote_message=quote_preview(incoming.text),
            )
        await self.signal.send_dm(incoming.sender_aci, text)
        return None

    async def _reply_rate_limited(self, incoming: IncomingMessage, in_group: bool) -> None:
        await self._reply(incoming, self.copy.pick_llm_rate_reply(), in_group)


def _format_ts(ts: int | None) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
