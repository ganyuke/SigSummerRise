# SigSummerRise — design handoff

Read this before changing behavior. It records **what was decided, why, and what must not regress**. Operator install steps live in [`deploy/debian.md`](deploy/debian.md); this file is the product/architecture brief.

## What this is

A **friend-group Signal bot** that can summarize recent chat (and answer quote-reply follow-ups) via **OpenRouter Zero Data Retention** providers, plus a **single-page dashboard** of per-user **counts and metadata only**.

Use case: a small group wants a Grok-like summarizer **without** a shared password, **without** sending opted-out people’s text to a model, and **without** putting Signal identifiers on the public web.

There is **no official Signal bot API**. The bot is a **linked `signal-cli` device** of a **dedicated** Signal account (spare phone is primary). It only sees messages sent **after** it was linked. No history backfill.

## Operator choices (locked in conversation)

| Topic | Decision |
| --- | --- |
| Signal attach | Linked device (`signal-cli link` + QR), not SMS-register-as-primary in this repo |
| Identity | Dedicated bot account, **not** the operator’s personal Signal |
| Groups | Exactly one configured group (`SIGNAL_GROUP_ID`, required at process start) |
| Dashboard audience | Any **opted-in** member who redeems a magic link sees **everyone’s** counts (friend-group trust) |
| Dashboard fields | Display name, opt-in status, body count, opt-in time |
| Stack | Python (FastAPI + asyncio bot) + `signal-cli`. No Svelte/SPA |
| Opt-in | Bot **DMs** consent; user replies **Yes** in the DM |
| Summarize scope | Last N **kept** messages: opted-in bodies + unlabeled `[redacted]` holes |
| Follow-ups | Any **opted-in** member quote-replies a bot summary |
| Model | Configurable cheap/capable (`OPENROUTER_MODEL`); always ZDR |
| At-rest crypto | SQLCipher (`sqlcipher3-binary`) + passphrase `DB_KEY` |
| Retention | Keep opted-in bodies until that user opts out (or operator wipes DB) |
| Site exposure | Public HTTPS (Caddy); **no** shared basic auth |
| Max N | `MAX_N` env, default 200 |

Later refinements (also locked):

- Do **not** put Signal ACI/UUID (or even a prefix) on the dashboard or in LLM prompts.
- Disappearing messages are **never collected** (not stored then deleted when the timer fires).
- Dashboard auth is a **single-use magic link DMed on intent**, not a password and not a bare `@mention`.

## Architecture

One Python process: FastAPI (dashboard) + asyncio task (Signal event loop). Separate process: `signal-cli daemon --http 127.0.0.1:8080`. Caddy terminates TLS and reverse-proxies `127.0.0.1:8000`.

```
Group/DM  →  signal-cli (JSON-RPC + SSE)  →  sigsummerrise
                                              ├─ SQLCipher
                                              ├─ OpenRouter (summarize / follow-up / ask)
                                              └─ GET /  and  GET /a/{token}
```

| Path | Role |
| --- | --- |
| `src/sigsummerrise/bot.py` | Event handling, consent, commands, summarize/follow-up |
| `src/sigsummerrise/signal_rpc.py` | Parse `receive` envelopes; send DM/group; **never persist `sourceNumber`** |
| `src/sigsummerrise/commands.py` | Regex/keyword intents (no LLM for command parsing) |
| `src/sigsummerrise/collect.py` | skip / body / hole; unlabeled redaction |
| `src/sigsummerrise/consent.py` | Consent timing + roast picker (copy lives in JSON) |
| `src/sigsummerrise/auth.py` | Magic tokens + session cookies |
| `src/sigsummerrise/db.py` | SQLCipher schema and deletes-on-opt-out |
| `src/sigsummerrise/llm.py` | OpenRouter chat; fail closed; do not log prompt/body |
| `src/sigsummerrise/prompts.py` | LLM system prompts loaded from `copy/prompts.json` |
| `src/sigsummerrise/web.py` | Two HTML pages, inline CSS only (templates packaged with the module) |
| `tests/` | Parser, consent, disappearing, holes, opt-out isolation, magic-link single-use |

## Invariants (do not regress)

1. **ACI/UUID and phone numbers never leave SQLCipher except to Signal itself.** Not in HTML, cookies, logs, group/DM command replies, or OpenRouter payloads. Dashboard identity is **display name only**.
2. **ZDR is not “the model never sees it.”** Summarize/follow-up still sends plaintext of the window to OpenRouter + a ZDR provider. Minimize that payload (window only, no IDs, unlabeled holes).
3. **Magic links only in DMs.** Posting `/a/{token}` in the group lets anyone burn the one-time token.
4. **A user can only mutate their own data.** Opt-out/status/dashboard-link issuance are caller-scoped. Summarize **reads** all kept messages but cannot delete others.
5. **No bodies, tokens, or OpenRouter prompts in journald/stdout.** Caddy must not log `/a/` tokens or query strings (`deploy/Caddyfile.example`). The app disables uvicorn access logs so tokens never hit the unit journal.
6. **No third-party JS/fonts/CDNs** on the dashboard (would phone home for every viewer).
7. **OpenRouter plugins/tools are off.** They sit outside ZDR.
8. **Fail closed** if the model has no ZDR endpoint: in-group error **without** echoing the prompt.
9. **Group commands require a Signal mention of the bot ACI.** Plain-text `@username` is not a mention.

## Consent and commands

Commands are **regex/keywords**, not an LLM, so asking for a dashboard does not upload the command to OpenRouter.

**Not opted in + group Signal mention of the bot:** ignore the command in-group; DM consent (at most once per 24h). Reply **Yes** / **No** in the DM (whole message). After No, they can DM Yes later. Typed `@bot` as plain text, without a Signal mention, is ignored. The first group mention in 24h always gets a clear “not opted in” notice in the group; later mentions in that window may get a random roast (`group_roast_chance`, default 1-in-4) instead.

Consent DM must mention: encrypted local storage, OpenRouter ZDR, **summaries are visible to the whole group (including opted-out members)**, disappearing messages never kept, opt-out deletes their stored messages.

**Opted in:**

| Intent | Trigger (after stripping leading `@name`) | Effect |
| --- | --- | --- |
| summarize | `summarize the past/last N messages` | Cap N at `MAX_N`; reply in group |
| opt-out | `opt out` / `opt-out` / `stop collecting` | Delete **caller** bodies, threads, tokens, sessions; redact summaries whose window included those bodies |
| dashboard | `dashboard`, `website`, `login`, `magic link`, `my stats` | DM one-time link; group only gets “I DMed you” |
| status | `status` | Caller’s count + opt-in time only |
| help | `help` / `commands` / `what can you do` (also a bare Signal mention with no extra text) | List commands; **do not** mint a link. Works even if not opted in. |
| ask | any other @mention text (opted-in) | LLM answer; recent kept chat attached silently (default 50, `ASK_CONTEXT_N`) |
| follow-up | quote-reply a stored summary or any message in that thread (opted-in, even without mention) | LLM over window + summary + thread |

Priority: opt-out > summarize > dashboard > status > help > ask. Bare typed `@bot` is **not** a mention. A summarize line must not also issue a magic link.

DMs: Yes/No for consent (whole message). **No after opt-in is opt-out** (deletes stored messages). After No, copy tells them they can DM Yes later. `help` in a DM works even if not opted in. A DM that is not Yes/No/help, after the consent text was already sent, gets `CONSENT_CLARIFY` — not silence. Dashboard/opt-out/status/ask allowed if already opted in. Summarize is **group-only**; a DM summarize tells them to mention the bot in the group.

Commands are only parsed in-group when the message’s `mentions[]` includes the bot ACI (`SIGNAL_BOT_ACI` or `listAccounts`). There is no username/text fallback.

## Collection

Evaluate in `collect.classify_inbound`. **Skip entirely** (no body, no hole, not in N):

- `expiresInSeconds` / `expireTimer` > 0 (disappearing)
- reactions, bot’s own messages
- empty text, media-only

**Do not** store then scrub when the timer fires — that still writes plaintext to disk/backups.

If kept:

- Opted in → body + `sender_aci` (DB only) + display name + timestamp
- Not opted in → **anonymous hole**: timestamp only, `sender_aci` NULL, no body. LLM/UI: `[redacted]` **without a name** (named placeholders would send non-consenting identities to OpenRouter)

Command messages from opted-in users **are** collected (they are group messages). Status counts include them.

Opt-out: `DELETE` that user’s bodies and thread rows; holes stay anonymous. Summaries whose window included that user’s message ids have `summary_text` replaced with `[redacted]` so follow-ups do not resend paraphrased deleted text. Re-hydrating a summary window after opt-out treats missing ids as `[redacted]`.

Media is out of scope; if there is text plus an attachment, keep text only.

## Magic-link auth

Replaced the original shared-password idea.

1. Opted-in member asks for dashboard (keywords above).
2. Unused tokens for that user are invalidated; existing 24h cookie stays.
3. New token: `secrets.token_urlsafe(32)`, stored **hashed**, ~15 min unused TTL, **one GET then deleted**. Rate limit 3/hour/user (`link_issuance`).
4. `GET /a/{token}` → new **session** id (also hashed) as cookie → 302 `/` with **no** token in the URL.
5. Cookie: `HttpOnly`, `SameSite=strict`, `Path=/`, `Max-Age=86400`. Name `__Host-ssr_session` when `PUBLIC_BASE_URL` is `https://`, else `ssr_session` (so localhost HTTP works). `Secure` only on HTTPS.
6. Unauthenticated `/`: login instructions only — **no counts, no names, no group name, no model**.
7. Each request re-checks the user is still `opted_in` (opt-out drops sessions).

The cookie value is random, not an ACI.

## Dashboard

Magic-link members (`/`): consent stats, model name, per-member message counts, LLM usage (24h/7d), estimated spend (7d), weekly rank, not-opted-in table, personal hourly quota, FAQ. **No** per-user last-seen, **no** UUIDs, **no** phones, **no** message bodies. Logged-out visitors see none of the above.

## Operator UI (`/ops`)

Disabled when `OPERATOR_TOKEN` is empty. Separate HttpOnly cookie (`ssr_ops` / `__Host-ssr_ops`). Hot-reloads model, temperature, max tokens, rate limits, write-only OpenRouter key, and system prompts (DB overrides; reset to file). ZDR cannot be disabled in code. Static CSS only; no CDN.

Runtime config in SQLCipher `runtime_config` overrides env/file for LLM settings and prompts. Env still bootstraps first install.

`llm_issuance` rows kept 30 days for leaderboards; hourly rate limit still uses the last hour only. Optional token/cost columns filled from OpenRouter usage when present.

App binds `127.0.0.1` only. Caddy in front; member dashboard has no shared password.

## OpenRouter

Every completion:

```json
"provider": { "zdr": true, "data_collection": "deny" }
```

Prompt: `[timestamp] Name: text` for opted-in lines, `[timestamp] [redacted]` for holes. Window preamble includes message count, time span, and redaction count. No group ids, quote internals, or ACIs. Follow-up resends the **stored window ids** re-hydrated at call time (so opt-out redacts). Operators must disable OpenRouter **account** prompt logging; request flags cannot turn that off if it is already on.

Default model `deepseek/deepseek-v4-flash-0731` — change via env or `/ops`. Summarize, ask, and follow-up share a per-user cap (`LLM_CALLS_PER_HOUR`, default 10; hot-reloadable). While the model is running, the bot sends group typing (`sendTyping`) and refreshes it every 10s (Signal drops the indicator after 15s). Typing is stopped before the reply is posted. Instant replies (empty window, rate limit, help, status) do not type.

## Encryption and process security

- `signal-cli` already encrypts its protocol store (`/var/lib/signal-cli`).
- App DB is SQLCipher; empty `DB_KEY` is refused. Secrets in `/etc/sigsummerrise/secrets.env` mode `0600`. All DB calls share one connection behind an `RLock`, with WAL and `busy_timeout`.
- Compromise of the host **is** compromise of the Signal account (linked-device keys). Dedicated UNIX user, `0700` data dirs, loopback-only `signal-cli` HTTP (that daemon has **no** auth).
- `sqlcipher3-binary` **vendors** SQLCipher. Debian `libsqlcipher-dev` / `python3-dev` are **not** required when the wheel installs; they are only a compile fallback. Runtime does not need the `sqlcipher` CLI either.

## Deploy targets

**Debian** is the intended host (`systemd/` + Caddy). Dev happened on Fedora 44 (Python 3.14, Java 25); a 3.12/3.13 venv is the fallback if wheels fail.

**Alpine is harder, not smaller in practice:** musl, so `sqlcipher3-binary` manylinux wheels typically do not install. You would compile `sqlcipher3` (gcc, musl-dev, openssl-dev, sqlcipher-dev) or switch to a musllinux wheel (e.g. `sqlcipher3-wheels`). No systemd — OpenRC (or similar) would replace the unit files. Prefer JVM `signal-cli` over glibc GraalVM natives (`gcompat` if you must). Not implemented; do not assume the Debian units work there.

## Out of scope (on purpose)

Media, multiple groups, history backfill, acting on another user’s data, OpenRouter prompt logging, shared-password auth, Svelte/SPA, collecting disappearing messages “until they expire.”

## Known limitations

- Linked device sees traffic only while online and only after link time.
- A group with disappearing messages **on** yields an empty summarize window; consent/help text should keep saying that.
- Display-name collisions on the dashboard are accepted; do **not** disambiguate with ACI.
- Summaries are public to the Signal group, including people who never opted in.
- Host root can read the DB (with `DB_KEY`) and impersonate the bot account.

## Tests

`pytest` from repo root with `.venv`. Coverage that must stay green when touching privacy/auth: `tests/test_collect.py`, `tests/test_optout.py`, `tests/test_auth.py`, `tests/test_commands.py`, `tests/test_bot.py`, `tests/test_signal_parse.py`, `tests/test_db.py`.

SQLCipher row factory must be **sqlcipher’s** `Row`, not stdlib `sqlite3.Row`.

## If you change something

Prefer tightening data exposure over adding fields. If a feature needs UUIDs, phones, last-seen, named redaction, group-posted magic links, LLM command parsing, or storing disappearing messages, that is a product change — update this file in the same PR and do not treat it as a drive-by.
