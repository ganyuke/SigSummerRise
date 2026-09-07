from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sigsummerrise import activity, auth, health, llm
from sigsummerrise.config import Settings
from sigsummerrise.db import Database, hash_secret
from sigsummerrise.runtime import (
    resolve_llm_config,
    runtime_config_for_ops,
    save_runtime_config,
    validate_prompt_fields,
)


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def static_dir() -> Path:
    return Path(__file__).resolve().parent / "static"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(["html"]),
    )


def _format_opted_in(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ops_cookie_name(settings: Settings) -> str:
    if settings.cookie_secure:
        return "__Host-ssr_ops"
    return "ssr_ops"


def _ops_cookie_kwargs(settings: Settings, value: str) -> dict[str, Any]:
    return {
        "key": _ops_cookie_name(settings),
        "value": value,
        "max_age": 7 * 86400,
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "strict",
        "path": "/",
    }


def _ops_authenticated(request: Request, settings: Settings) -> bool:
    token = (settings.operator_token or "").strip()
    if not token:
        return False
    cookie = request.cookies.get(_ops_cookie_name(settings))
    return cookie == hash_secret(token)


def session_aci(request: Request, db: Database, settings: Settings, now: int) -> str | None:
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    return db.get_session_aci(raw, now)


def _group_label(settings: Settings) -> str:
    name = (settings.group_name or "").strip()
    return name or "SigSummerRise"


def _fmt_cost(value: float) -> str:
    if value < 0.01:
        return "<$0.01"
    return f"${value:.2f}"


def _event_age(now: int) -> str | None:
    last = health.last_signal_event_at()
    if last is None:
        return None
    age = now - last
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{age // 60}m ago"
    return f"{age // 3600}h ago"


def _signal_cli_ok(url: str) -> bool:
    try:
        with httpx.Client(timeout=2.0) as client:
            response = client.post(
                url.rstrip("/") + "/api/v1/rpc",
                json={"jsonrpc": "2.0", "method": "version", "id": 1},
            )
            return response.status_code < 500
    except httpx.HTTPError:
        return False


def _parse_optional_float(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    return float(text)


def _parse_optional_int(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    return int(text)


def _member_row_dict(row, *, viewer_aci: str) -> dict[str, Any]:
    opted_in = row.consent_state == "opted_in"
    if opted_in:
        rank_cell = "—"
        if row.rank_7d == 1 and row.llm_calls_7d > 0:
            rank_cell = f"#{row.rank_7d} this week's hog"
        elif row.rank_7d:
            rank_cell = f"#{row.rank_7d}"
        return {
            "display_name": row.display_name,
            "opted_in": True,
            "body_count": row.body_count,
            "llm_calls_24h": row.llm_calls_24h,
            "llm_calls_7d": row.llm_calls_7d,
            "cost_display": _fmt_cost(row.cost_usd_7d),
            "rank_display": rank_cell,
            "rank_7d": row.rank_7d,
            "is_you": row.aci == viewer_aci,
        }
    return {
        "display_name": row.display_name,
        "opted_in": False,
        "body_count": "—",
        "llm_calls_24h": "—",
        "llm_calls_7d": "—",
        "cost_display": "—",
        "rank_display": "—",
        "rank_7d": 0,
        "is_you": row.aci == viewer_aci,
    }


def _live_status_payload(settings: Settings, db: Database, aci: str, now: int) -> dict[str, Any]:
    runtime = resolve_llm_config(settings, db)
    stats = db.dashboard_stats(now, window_n=runtime.max_n)
    snap = activity.snapshot()
    message, elapsed = activity.format_status_message(
        bot_name=settings.bot_name,
        viewer_aci=aci,
        snap=snap,
        now=now,
    )
    members_raw = db.member_usage_rows(now)
    members = [_member_row_dict(row, viewer_aci=aci) for row in members_raw]
    draft = activity.draft_for_viewer(aci)
    payload: dict[str, Any] = {
        "bot_name": settings.bot_name,
        "model": runtime.openrouter_model,
        "last_provider": db.last_llm_provider(),
        "status": {
            "state": snap.state,
            "message": message,
            "elapsed_seconds": elapsed,
        },
        "stats": {
            "opted_in": stats.opted_in,
            "not_opted_in": stats.not_opted_in,
            "body_messages": stats.body_messages,
            "holes": stats.holes,
            "redaction_pct_last_n": stats.redaction_pct_last_n,
            "messages_24h": stats.messages_24h,
            "messages_7d": stats.messages_7d,
            "summaries_7d": stats.summaries_7d,
            "llm_calls_24h": stats.llm_calls_24h,
            "cost_7d": _fmt_cost(stats.cost_usd_7d),
        },
        "quota": {
            "used": db.llm_count(aci, now),
            "limit": runtime.llm_calls_per_hour,
        },
        "members": members,
    }
    if draft:
        payload["draft"] = draft
    return payload


def _format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _live_activity_payload(settings: Settings, aci: str, now: int) -> dict[str, Any]:
    snap = activity.snapshot()
    message, elapsed = activity.format_status_message(
        bot_name=settings.bot_name,
        viewer_aci=aci,
        snap=snap,
        now=now,
    )
    payload: dict[str, Any] = {
        "status": {
            "state": snap.state,
            "message": message,
            "elapsed_seconds": elapsed,
        },
    }
    draft = activity.draft_for_viewer(aci)
    if draft:
        payload["draft"] = draft
    return payload


_SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}
_SSE_HEARTBEAT_SECONDS = 60.0
_SSE_RECONNECT_DELAY_MS = 5000
_DISCONNECT_POLL_SECONDS = 0.25


def _reconnect_sse() -> str:
    return _format_sse("reconnect", {"delay_ms": _SSE_RECONNECT_DELAY_MS})


async def _watch_disconnect(request: Request, disconnect: asyncio.Event) -> None:
    while not disconnect.is_set():
        if await request.is_disconnected():
            disconnect.set()
            return
        await asyncio.sleep(_DISCONNECT_POLL_SECONDS)


async def _wait_activity(
    sub: activity.Subscription,
    timeout: float,
    *,
    disconnect: asyncio.Event | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> Literal["activity", "shutdown", "heartbeat"]:
    if activity.is_shutting_down():
        return "shutdown"
    sub.event.clear()
    if disconnect is not None and disconnect.is_set():
        return "shutdown"
    if shutdown_event is not None and shutdown_event.is_set():
        return "shutdown"
    activity_task = asyncio.create_task(sub.event.wait())
    extra: list[asyncio.Task[None]] = []
    if disconnect is not None:
        extra.append(asyncio.create_task(disconnect.wait()))
    if shutdown_event is not None:
        extra.append(asyncio.create_task(shutdown_event.wait()))
    tasks = [activity_task, *extra]
    try:
        done, _ = await asyncio.wait(set(tasks), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
    if activity.is_shutting_down():
        return "shutdown"
    if disconnect is not None and disconnect.is_set():
        return "shutdown"
    if shutdown_event is not None and shutdown_event.is_set():
        return "shutdown"
    if activity_task in done:
        return "activity"
    return "heartbeat"

async def _live_stream(
    settings: Settings,
    db: Database,
    aci: str,
    *,
    request: Request | None = None,
    shutdown_event: asyncio.Event | None = None,  # Inject global shutdown_event
) -> AsyncIterator[str]:
    disconnect = asyncio.Event()
    watcher: asyncio.Task[None] | None = None
    if request is not None:
        watcher = asyncio.create_task(_watch_disconnect(request, disconnect))
    
    sub = activity.subscribe()
    snap_gen = sub.snapshot_gen
    draft_gen = sub.draft_gen
    
    try:
        now = int(time.time())
        snap_gen = activity.snapshot_generation()
        draft_gen = activity.draft_generation()
        yield _format_sse("snapshot", _live_status_payload(settings, db, aci, now))
        
        heartbeat_at = time.monotonic()
        
        while True:
            if shutdown_event is not None and shutdown_event.is_set() or activity.is_shutting_down():
                yield _reconnect_sse()
                break
            if disconnect.is_set():
                break

            kind = activity.pending_change(snap_gen, draft_gen)
            if kind == "none":
                result = await _wait_activity(
                    sub,
                    timeout=_SSE_HEARTBEAT_SECONDS,  # Set timeout to heartbeat interval directly
                    disconnect=disconnect,
                    shutdown_event=shutdown_event,
                )

                if result == "shutdown":
                    yield _reconnect_sse()
                    break
                if result == "disconnect":
                    break
                if result == "activity":
                    kind = activity.pending_change(snap_gen, draft_gen)
                    if kind == "none":
                        continue
                else:  # timeout
                    if time.monotonic() - heartbeat_at >= _SSE_HEARTBEAT_SECONDS:
                        yield ": ping\n\n"
                        heartbeat_at = time.monotonic()
                    continue

            now = int(time.time())
            if kind == "snapshot":
                snap_gen = activity.snapshot_generation()
                draft_gen = activity.draft_generation()
                yield _format_sse("snapshot", _live_status_payload(settings, db, aci, now))
            else:
                draft_gen = activity.draft_generation()
                yield _format_sse("update", _live_activity_payload(settings, aci, now))

    except asyncio.CancelledError:
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        activity.unsubscribe(sub)

def mount_routes(app: FastAPI) -> None:
    jinja = _env()
    app.mount("/static", StaticFiles(directory=str(static_dir())), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, sort: str = "name", saved: str = "") -> HTMLResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        aci = session_aci(request, db, settings, now)
        if aci is None:
            return HTMLResponse(jinja.get_template("login.html").render())
        user = db.get_user(aci)
        viewer_name = (user.display_name if user else "") or "Member"
        runtime = resolve_llm_config(settings, db)
        stats = db.dashboard_stats(now, window_n=runtime.max_n)
        members_raw = db.member_usage_rows(now, sort=sort if sort in ("name", "llm7d") else "name")
        members = [_member_row_dict(row, viewer_aci=aci) for row in members_raw]
        flash_ok = "Privacy settings saved." if saved == "privacy" else ""
        html = jinja.get_template("index.html").render(
            page_title=f"{_group_label(settings)} — dashboard",
            group_label=_group_label(settings),
            bot_name=settings.bot_name,
            status_message=activity.idle_message(settings.bot_name),
            model=runtime.openrouter_model,
            last_provider=db.last_llm_provider(),
            viewer_name=viewer_name,
            stats=stats,
            window_n=runtime.max_n,
            cost_7d=_fmt_cost(stats.cost_usd_7d),
            quota_used=db.llm_count(aci, now),
            quota_limit=runtime.llm_calls_per_hour,
            members=members,
            privacy={
                "exclude_from_summaries": bool(user and user.exclude_from_summaries),
                "exclude_from_questions": bool(user and user.exclude_from_questions),
            },
            flash_ok=flash_ok,
            limits={
                "max_n": runtime.max_n,
                "ask_context_n": runtime.ask_context_n,
                "llm_calls_per_hour": runtime.llm_calls_per_hour,
            },
        )
        return HTMLResponse(html)

    @app.get("/api/live")
    def api_live(request: Request) -> JSONResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        aci = session_aci(request, db, settings, now)
        if aci is None:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        payload = _live_status_payload(settings, db, aci, now)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/api/live/stream")
    async def api_live_stream(request: Request):
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        shutdown_event: asyncio.Event | None = getattr(
            request.app.state, "shutdown_event", None
        )

        now = int(time.time())
        aci = session_aci(request, db, settings, now)
        if aci is None:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        return StreamingResponse(
            _live_stream(
                settings,
                db,
                aci,
                request=request,
                shutdown_event=shutdown_event,
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    @app.post("/api/live/preview/dismiss")
    def api_live_preview_dismiss(request: Request) -> JSONResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        aci = session_aci(request, db, settings, now)
        if aci is None:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        activity.dismiss_preview(aci)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/privacy")
    def save_privacy(
        request: Request,
        exclude_from_summaries: str = Form(""),
        exclude_from_questions: str = Form(""),
    ) -> RedirectResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        aci = session_aci(request, db, settings, now)
        if aci is None:
            return RedirectResponse("/", status_code=302)
        db.set_privacy_flags(
            aci,
            exclude_from_summaries=exclude_from_summaries == "on",
            exclude_from_questions=exclude_from_questions == "on",
        )
        return RedirectResponse("/?saved=privacy", status_code=302)

    @app.post("/opt-out")
    def dashboard_opt_out(request: Request) -> RedirectResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        aci = session_aci(request, db, settings, now)
        if aci is None:
            return RedirectResponse("/", status_code=302)
        db.opt_out(aci)
        activity.notify("snapshot")
        response = RedirectResponse("/", status_code=302)
        auth.revoke_session(request, db, settings)
        auth.clear_session_cookie(response, settings)
        return response

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        response = RedirectResponse("/", status_code=302)
        auth.revoke_session(request, db, settings)
        auth.clear_session_cookie(response, settings)
        return response

    @app.get("/a/{token}", response_class=HTMLResponse)
    def redeem(token: str, request: Request) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        raw_session = auth.redeem_and_session(db, settings, token, now)
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if raw_session is None:
            html = jinja.get_template("login.html").render(
                error="This link is invalid or already used. Mention the bot in Signal and say dashboard for a new one."
            )
            return HTMLResponse(html, status_code=400, headers=headers)
        response = RedirectResponse("/", status_code=302, headers=headers)
        kwargs = auth.cookie_kwargs(settings, raw_session)
        response.set_cookie(**kwargs)
        return response

    @app.get("/ops", response_class=HTMLResponse)
    def ops_get(request: Request) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        if not (settings.operator_token or "").strip():
            return HTMLResponse("Not found", status_code=404)
        if not _ops_authenticated(request, settings):
            return HTMLResponse(jinja.get_template("ops_login.html").render())
        return _render_ops(request, jinja)

    @app.post("/ops/login")
    def ops_login(request: Request, token: str = Form(...)) -> RedirectResponse:
        settings: Settings = request.app.state.settings
        if not (settings.operator_token or "").strip():
            return RedirectResponse("/", status_code=302)
        expected = settings.operator_token.strip()
        if token.strip() != expected:
            html = jinja.get_template("ops_login.html").render(error="Invalid token.")
            return HTMLResponse(html, status_code=401)
        response = RedirectResponse("/ops", status_code=302)
        response.set_cookie(**_ops_cookie_kwargs(settings, hash_secret(expected)))
        return response

    @app.post("/ops/logout")
    def ops_logout(request: Request) -> RedirectResponse:
        settings: Settings = request.app.state.settings
        response = RedirectResponse("/ops", status_code=302)
        response.delete_cookie(
            _ops_cookie_name(settings),
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/ops")
    async def ops_save(
        request: Request,
        action: str = Form("save"),
        openrouter_model: str = Form(...),
        llm_temperature: str = Form(""),
        llm_max_tokens: str = Form(""),
        llm_calls_per_hour: int = Form(...),
        llm_queue_cap: int = Form(...),
        ask_context_n: int = Form(...),
        max_n: int = Form(...),
        openrouter_api_key: str = Form(""),
        summarize_system: str = Form(...),
        followup_system: str = Form(...),
        ask_system: str = Form(""),
        responses_enabled: str = Form(""),
        llm_timeout_seconds: int = Form(...),
        llm_read_idle_seconds: int = Form(...),
        provider_order: str = Form(""),
        provider_ignore: str = Form(""),
        provider_sort: str = Form(""),
    ) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        if not (settings.operator_token or "").strip() or not _ops_authenticated(request, settings):
            return HTMLResponse("Not found", status_code=404)
        if action == "reset_prompts":
            save_runtime_config(db, {}, clear_prompts=True)
            activity.notify("snapshot")
            return _render_ops(request, jinja, flash_ok="Prompts reset to file defaults.")
        patch: dict[str, Any] = {
            "openrouter_model": openrouter_model.strip(),
            "llm_calls_per_hour": llm_calls_per_hour,
            "llm_queue_cap": llm_queue_cap,
            "ask_context_n": ask_context_n,
            "max_n": max_n,
            "summarize_system": summarize_system.strip(),
            "followup_system": followup_system.strip(),
            "ask_system": ask_system.strip(),
            "responses_enabled": responses_enabled == "on",
            "llm_timeout_seconds": llm_timeout_seconds,
            "llm_read_idle_seconds": llm_read_idle_seconds,
            "provider_order": provider_order.strip(),
            "provider_ignore": provider_ignore.strip(),
            "provider_sort": provider_sort.strip(),
        }
        temp = _parse_optional_float(llm_temperature)
        tokens = _parse_optional_int(llm_max_tokens)
        if temp is not None:
            patch["llm_temperature"] = temp
        else:
            patch["llm_temperature"] = None
        if tokens is not None:
            patch["llm_max_tokens"] = tokens
        else:
            patch["llm_max_tokens"] = None
        key = openrouter_api_key.strip()
        if key:
            patch["openrouter_api_key"] = key
        try:
            validate_prompt_fields(patch)
            save_runtime_config(db, patch)
            activity.notify("snapshot")
        except ValueError as exc:
            return _render_ops(request, jinja, flash_err=str(exc))
        return _render_ops(request, jinja, flash_ok="Saved.")

    @app.post("/ops/test")
    async def ops_test(request: Request) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        if not (settings.operator_token or "").strip() or not _ops_authenticated(request, settings):
            return HTMLResponse("Not found", status_code=404)
        try:
            text = await llm.complete(
                settings,
                db,
                "Reply with exactly OK.",
                "Say OK.",
            )
            return _render_ops(request, jinja, test_ok=True, test_result=f"OK — model replied: {text[:80]}")
        except llm.LlmError:
            return _render_ops(request, jinja, test_ok=False, test_result="Test call failed.")

    def _render_ops(
        request: Request,
        jinja_env: Environment,
        *,
        flash_ok: str = "",
        flash_err: str = "",
        test_ok: bool = False,
        test_result: str = "",
    ) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        runtime = resolve_llm_config(settings, db)
        stats = db.dashboard_stats(now)
        bodies, holes = db.total_message_counts()
        summaries_total = db.connect().execute("SELECT COUNT(*) AS n FROM summaries").fetchone()["n"]
        form = runtime_config_for_ops(settings, db)
        count, median_ms, p95_ms = db.llm_latency_stats(runtime.openrouter_model, now)
        html = jinja_env.get_template("ops.html").render(
            flash_ok=flash_ok,
            flash_err=flash_err,
            test_ok=test_ok,
            test_result=test_result,
            health={
                "last_event_age": _event_age(now),
                "signal_ok": _signal_cli_ok(settings.signal_http_url),
                "key_ok": runtime.api_key_configured,
                "key_suffix": runtime.api_key_suffix,
                "bot_aci_ok": bool((settings.signal_bot_aci or "").strip()),
                "group_ok": bool((settings.signal_group_id or "").strip()),
            },
            agg={
                "opted_in": stats.opted_in,
                "not_opted_in": stats.not_opted_in,
                "body_messages": bodies,
                "holes": holes,
                "summaries_total": int(summaries_total),
                "llm_calls_24h": stats.llm_calls_24h,
                "latency_count": count,
                "latency_median_ms": median_ms,
                "latency_p95_ms": p95_ms,
            },
            form=form,
        )
        return HTMLResponse(html)
