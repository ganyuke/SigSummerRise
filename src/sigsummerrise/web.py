from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sigsummerrise import auth
from sigsummerrise.config import Settings
from sigsummerrise.db import Database


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(["html"]),
    )


def _format_opted_in(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def session_aci(request: Request, db: Database, settings: Settings, now: int) -> str | None:
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    return db.get_session_aci(raw, now)


def mount_routes(app: FastAPI) -> None:
    jinja = _env()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        settings: Settings = request.app.state.settings
        db: Database = request.app.state.db
        now = int(time.time())
        if session_aci(request, db, settings, now) is None:
            html = jinja.get_template("login.html").render()
            return HTMLResponse(html)
        rows = [
            {
                "display_name": row.display_name,
                "consent_state": "opted in",
                "body_count": row.body_count,
                "opted_in_at": _format_opted_in(row.opted_in_at),
            }
            for row in db.dashboard_rows()
        ]
        html = jinja.get_template("index.html").render(rows=rows)
        return HTMLResponse(html)

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
