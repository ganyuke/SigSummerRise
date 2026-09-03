# Debian install (SigSummerRise)

The bot is a linked Signal device plus a local FastAPI process. Caddy terminates TLS. Nothing but Caddy should be reachable from the network.

## Automated install (Debian / LXC)

Inside a fresh Debian 12+ host or LXC container (systemd, root):

```bash
git clone https://github.com/ganyuke/SigSummerRise.git /opt/sigsummerrise
cd /opt/sigsummerrise
sudo ./deploy/install-debian.sh --hostname your.bot.example.com
```

The script installs packages, `signal-cli`, the Python app, systemd units, and Caddy. It generates `DB_KEY` in `/etc/sigsummerrise/secrets.env` but leaves Signal/OpenRouter values for you to fill in. If stdin is not a TTY, run the QR link step afterward:

```bash
sudo /opt/sigsummerrise/deploy/install-debian.sh --link-only
```

**LXC:** use a Debian template with systemd. Forward host TCP **80** and **443** to the container so Caddy can obtain certificates. No nesting or Docker is required.

Options: `--no-caddy` (reverse proxy elsewhere), `--source-dir PATH`, `--signal-cli-version X.Y.Z`. See `./deploy/install-debian.sh --help`.

## Manual install

The steps below match what the script automates. Use them if you prefer hand-installing or need to debug.

## Packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev sqlcipher libsqlcipher-dev \
    openjdk-21-jre-headless curl qrencode caddy
```

If your Debian release lacks Java 21, install Temurin 21 or use the native `signal-cli` tarball from GitHub (no JVM).

## Dedicated UNIX user and directories

```bash
sudo install -d -m 0755 /opt/sigsummerrise
sudo useradd --system --home-dir /var/lib/sigsummerrise --shell /usr/sbin/nologin sigsummerrise
sudo install -d -m 0700 -o sigsummerrise -g sigsummerrise \
    /var/lib/sigsummerrise /var/lib/signal-cli /etc/sigsummerrise
```

## signal-cli

Install the latest release from https://github.com/AsamK/signal-cli/releases into `/opt/signal-cli` and symlink:

```bash
sudo ln -sf /opt/signal-cli/bin/signal-cli /usr/local/bin/signal-cli
```

Link the **dedicated bot account** (spare phone is the Signal primary):

```bash
sudo -u sigsummerrise -H signal-cli --config /var/lib/signal-cli link -n SigSummerRise
```

Render the `sgnl://linkdevice?...` URI as a QR code (`qrencode -t utf8`) and scan it on the spare phone: Settings → Linked devices. Set a Signal username on that account so group `@mentions` work.

Join the friend group with that account, then:

```bash
sudo -u sigsummerrise -H signal-cli --config /var/lib/signal-cli -a +E164 listGroups -d
```

Copy the group id into `SIGNAL_GROUP_ID` (required; the process will not start without it). Copy the account ACI into `SIGNAL_BOT_ACI` so group commands can detect a real Signal mention of the bot. A Signal username on the account is still useful so people can pick the bot from the mention picker; typed `@name` without a mention object is ignored.

## Application

```bash
sudo git clone https://github.com/ganyuke/SigSummerRise.git /opt/sigsummerrise
cd /opt/sigsummerrise
sudo python3 -m venv /opt/sigsummerrise/.venv
sudo /opt/sigsummerrise/.venv/bin/pip install -e ".[dev]"
sudo chown -R sigsummerrise:sigsummerrise /opt/sigsummerrise
```

If `sqlcipher3` fails to install on a very new Python, use a 3.12 or 3.13 venv.

## Secrets

```bash
sudo cp /opt/sigsummerrise/.env.example /etc/sigsummerrise/secrets.env
sudo chmod 0600 /etc/sigsummerrise/secrets.env
sudo chown root:sigsummerrise /etc/sigsummerrise/secrets.env
```

Set at least:

- `SIGNAL_ACCOUNT` (E.164)
- `SIGNAL_GROUP_ID` (required)
- `SIGNAL_BOT_ACI` (bot account UUID; required for group command mentions)
- `PUBLIC_BASE_URL` (`https://your.hostname`, no trailing slash required)
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL` (must have a ZDR endpoint)
- `DB_PATH=/var/lib/sigsummerrise/sigsummerrise.db`
- `DB_KEY` (long random passphrase)
- `RESPONSES_PATH=copy/responses.json` (optional; copy from `copy/responses.example.json` and customize)
- `PROMPTS_PATH=copy/prompts.json` (optional; copy from `copy/prompts.example.json` and customize)

Bot reply text (consent messages, roasts, help, errors) lives in JSON under `copy/`. The repo ships a tame `responses.example.json`; operators copy it to `responses.json` (gitignored) for private customization.

LLM system prompts (summarize, follow-up, ask) live in `copy/prompts.example.json` → `copy/prompts.json` with the same pattern. Prompts support `{bot_name}`, `{current_time}`, and `{group_name}` placeholders (see `BOT_NAME`, `BOT_TIMEZONE`, `GROUP_NAME` in `.env`). Chat lines sent to the model include timestamps and window metadata.

Set `OPERATOR_TOKEN` in secrets to enable `/ops` (model, limits, write-only API key, system prompt editor). Leave empty to disable the operator UI entirely.

In the OpenRouter account, disable prompt logging. Every request already sends `provider.zdr=true` and `data_collection=deny`. Do not enable OpenRouter plugins.

## systemd

```bash
sudo cp /opt/sigsummerrise/systemd/signal-cli.service /etc/systemd/system/
sudo cp /opt/sigsummerrise/systemd/sigsummerrise.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signal-cli.service sigsummerrise.service
```

The app binds `127.0.0.1:8000`. Confirm with `ss -ltnp | grep 8000`.

## Caddy

```bash
sudo cp /opt/sigsummerrise/deploy/Caddyfile.example /etc/caddy/Caddyfile
# edit the hostname
sudo systemctl reload caddy
```

No HTTP basic auth. Access is a single-use magic link the bot DMs after an opted-in member asks for the dashboard.

The example Caddyfile redacts `/a/...` paths and query strings in access logs. The app disables uvicorn access logs. Do not log tokens.

## Behaviour checklist

- First Signal mention of the bot from someone who has not opted in: ignored in the group; they get a consent DM. They reply **Yes** or **No** in that DM. After No they can DM **Yes** anytime. Typed `@bot` without a mention object does nothing.
- Disappearing messages (`expiresInSeconds` > 0) are never stored.
- `@bot summarize the past N messages` uses only kept messages (opted-in bodies + anonymous `[redacted]` holes).
- Any other `@mention` from an opted-in member is treated as a question; the bot attaches recent chat silently and answers in the group.
- Quote-reply a summary or any message in that thread to ask a follow-up.
- `@bot dashboard` DMs a one-time link (15 minutes, one GET). Cookie lasts 24 hours.
- `@bot opt out` deletes that user's stored bodies, sessions, and unused links only.

A linked device only receives messages sent after it was linked. There is no backfill of older history.
