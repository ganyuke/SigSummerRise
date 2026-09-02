#!/usr/bin/env bash
# SigSummerRise — automated Debian install (bare metal or LXC).
#
# Run inside the target host/container as root, from a repo checkout:
#   git clone <this-repo> /opt/sigsummerrise
#   cd /opt/sigsummerrise
#   sudo ./deploy/install-debian.sh --hostname bot.example.com
#
# LXC notes:
#   - Use a Debian template with systemd (Proxmox: unprivileged Debian 12+ works).
#   - Forward TCP 80 and 443 to the container for Caddy/Let's Encrypt.
#   - signal-cli device linking is interactive; complete it before enabling services.
#
set -euo pipefail

APP_USER=sigsummerrise
APP_GROUP=sigsummerrise
APP_HOME=/var/lib/sigsummerrise
APP_OPT=/opt/sigsummerrise
SIGNAL_CLI_CONFIG=/var/lib/signal-cli
SIGNAL_CLI_OPT=/opt/signal-cli
SECRETS_FILE=/etc/sigsummerrise/secrets.env
CADDY_LOG_DIR=/var/log/caddy

SOURCE_DIR=""
HOSTNAME=""
SIGNAL_CLI_VERSION=""
SKIP_CADDY=0
LINK_ONLY=0

usage() {
  cat <<'EOF'
SigSummerRise — automated Debian install (bare metal or LXC).

Run inside the target host/container as root, from a repo checkout:
  git clone <this-repo> /opt/sigsummerrise
  cd /opt/sigsummerrise
  sudo ./deploy/install-debian.sh --hostname bot.example.com

LXC: Debian template with systemd; forward TCP 80/443 for Caddy/Let's Encrypt.

Options:
  --hostname HOST       Public hostname for Caddy TLS (required unless --no-caddy)
  --no-caddy            Skip Caddy install/config (reverse proxy elsewhere)
  --source-dir PATH     Repo root to install (default: parent of deploy/)
  --signal-cli-version  Pin signal-cli release (default: latest GitHub release)
  --link-only           Only run the interactive signal-cli link step
  -h, --help            Show this help

After install:
  1. Finish signal-cli link (QR) if not done yet.
  2. Edit secrets:  sudo nano /etc/sigsummerrise/secrets.env
  3. Join the group on the bot phone, then:
       sudo -u sigsummerrise signal-cli --config /var/lib/signal-cli -a +E164 listGroups -d
  4. Start services: sudo systemctl enable --now signal-cli sigsummerrise caddy
EOF
}

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "run as root (sudo $0 …)"
}

script_dir() {
  cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --hostname)
        [[ $# -ge 2 ]] || die "--hostname requires a value"
        HOSTNAME=$2
        shift 2
        ;;
      --no-caddy)
        SKIP_CADDY=1
        shift
        ;;
      --source-dir)
        [[ $# -ge 2 ]] || die "--source-dir requires a value"
        SOURCE_DIR=$2
        shift 2
        ;;
      --signal-cli-version)
        [[ $# -ge 2 ]] || die "--signal-cli-version requires a value"
        SIGNAL_CLI_VERSION=$2
        shift 2
        ;;
      --link-only)
        LINK_ONLY=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1 (try --help)"
        ;;
    esac
  done
}

latest_signal_cli_version() {
  curl -fsSL https://api.github.com/repos/AsamK/signal-cli/releases/latest \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'].lstrip('v'))"
}

signal_cli_native_asset() {
  local version=$1 arch
  arch=$(uname -m)
  case "$arch" in
    x86_64|amd64) echo "signal-cli-${version}-Linux-native.tar.gz" ;;
    aarch64|arm64) echo "signal-cli-${version}-Linux-aarch64-native.tar.gz" ;;
    *) die "unsupported architecture for native signal-cli: $arch (install Java and use JVM tarball manually)" ;;
  esac
}

install_signal_cli() {
  local version asset url tmp
  version=${SIGNAL_CLI_VERSION:-$(latest_signal_cli_version)}
  asset=$(signal_cli_native_asset "$version")
  url="https://github.com/AsamK/signal-cli/releases/download/v${version}/${asset}"
  log "installing signal-cli v${version} (${asset})"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN
  if ! curl -fsSL "$url" -o "${tmp}/signal-cli.tar.gz"; then
    log "native tarball missing; falling back to JVM build"
    asset="signal-cli-${version}.tar.gz"
    url="https://github.com/AsamK/signal-cli/releases/download/v${version}/${asset}"
    curl -fsSL "$url" -o "${tmp}/signal-cli.tar.gz"
    apt-get install -y --no-install-recommends openjdk-21-jre-headless
  fi
  rm -rf "$SIGNAL_CLI_OPT"
  tar -xzf "${tmp}/signal-cli.tar.gz" -C /opt
  if [[ -d "/opt/signal-cli-${version}" ]]; then
    mv "/opt/signal-cli-${version}" "$SIGNAL_CLI_OPT"
  elif [[ ! -d "$SIGNAL_CLI_OPT" ]]; then
  # Some archives unpack to signal-cli/ directly.
    for candidate in /opt/signal-cli*; do
      [[ -d "$candidate" ]] || continue
      mv "$candidate" "$SIGNAL_CLI_OPT"
      break
    done
  fi
  [[ -x "${SIGNAL_CLI_OPT}/bin/signal-cli" ]] \
    || die "signal-cli binary not found under ${SIGNAL_CLI_OPT}/bin"
  ln -sf "${SIGNAL_CLI_OPT}/bin/signal-cli" /usr/local/bin/signal-cli
}

ensure_user_and_dirs() {
  if ! id "$APP_USER" &>/dev/null; then
    log "creating system user ${APP_USER}"
    useradd --system --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
  fi
  install -d -m 0755 "$APP_OPT"
  install -d -m 0700 -o "$APP_USER" -g "$APP_GROUP" \
    "$APP_HOME" "$SIGNAL_CLI_CONFIG" /etc/sigsummerrise
}

install_packages() {
  log "installing apt packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  local packages=(
    python3 python3-venv python3-dev
    curl ca-certificates qrencode git rsync
  )
  if [[ "$SKIP_CADDY" -eq 0 ]]; then
    packages+=(caddy)
  fi
  apt-get install -y --no-install-recommends "${packages[@]}"
}

sync_application() {
  [[ -n "$SOURCE_DIR" ]] || SOURCE_DIR="$(cd "$(script_dir)/.." && pwd)"
  [[ -f "${SOURCE_DIR}/pyproject.toml" ]] \
    || die "source dir does not look like SigSummerRise: ${SOURCE_DIR}"
  log "syncing application from ${SOURCE_DIR} to ${APP_OPT}"
  rsync -a \
    --exclude .git \
    --exclude .venv \
    --exclude __pycache__ \
    --exclude .pytest_cache \
    --exclude '*.pyc' \
    "${SOURCE_DIR}/" "${APP_OPT}/"
  chown -R "${APP_USER}:${APP_GROUP}" "$APP_OPT"
}

install_python_env() {
  log "creating Python venv and installing package"
  if [[ ! -d "${APP_OPT}/.venv" ]]; then
    python3 -m venv "${APP_OPT}/.venv"
  fi
  "${APP_OPT}/.venv/bin/pip" install --upgrade pip
  if ! "${APP_OPT}/.venv/bin/pip" install -e "${APP_OPT}"; then
    log "default python failed; retrying with python3.12 venv"
    apt-get install -y --no-install-recommends python3.12 python3.12-venv
    rm -rf "${APP_OPT}/.venv"
    python3.12 -m venv "${APP_OPT}/.venv"
    "${APP_OPT}/.venv/bin/pip" install --upgrade pip
    "${APP_OPT}/.venv/bin/pip" install -e "${APP_OPT}"
  fi
  chown -R "${APP_USER}:${APP_GROUP}" "${APP_OPT}/.venv"
}

ensure_responses_file() {
  local responses="${APP_OPT}/copy/responses.json"
  if [[ -f "$responses" ]]; then
    log "keeping existing ${responses}"
    return
  fi
  log "creating ${responses} from responses.example.json"
  cp "${APP_OPT}/copy/responses.example.json" "$responses"
  chown "${APP_USER}:${APP_GROUP}" "$responses"
  chmod 0640 "$responses"
}

ensure_secrets_file() {
  if [[ -f "$SECRETS_FILE" ]]; then
    log "keeping existing ${SECRETS_FILE}"
    return
  fi
  log "creating ${SECRETS_FILE} from .env.example"
  cp "${APP_OPT}/.env.example" "$SECRETS_FILE"
  chmod 0600 "$SECRETS_FILE"
  chown root:"$APP_GROUP" "$SECRETS_FILE"
  local db_key
  db_key=$(openssl rand -base64 48)
  sed -i "s/^DB_KEY=.*/DB_KEY=${db_key}/" "$SECRETS_FILE"
}

install_systemd_units() {
  log "installing systemd units"
  install -m 0644 "${APP_OPT}/systemd/signal-cli.service" /etc/systemd/system/signal-cli.service
  install -m 0644 "${APP_OPT}/systemd/sigsummerrise.service" /etc/systemd/system/sigsummerrise.service
  systemctl daemon-reload
}

configure_caddy() {
  [[ "$SKIP_CADDY" -eq 1 ]] && return
  [[ -n "$HOSTNAME" ]] || die "--hostname is required unless --no-caddy"
  log "configuring Caddy for ${HOSTNAME}"
  install -d -m 0755 "$CADDY_LOG_DIR"
  if id caddy &>/dev/null; then
    chown caddy:caddy "$CADDY_LOG_DIR"
  fi
  sed "s/sigsummerrise.example.com/${HOSTNAME}/g" \
    "${APP_OPT}/deploy/Caddyfile.example" > /etc/caddy/Caddyfile
  systemctl enable caddy
}

signal_cli_linked() {
  [[ -f "${SIGNAL_CLI_CONFIG}/data/accounts.json" ]] && return 0
  find "$SIGNAL_CLI_CONFIG" -name 'accounts.json' -print -quit 2>/dev/null | grep -q .
}

run_signal_link() {
  log "linking signal-cli as a dedicated bot device"
  printf '\nScan the QR code on the bot account phone: Settings → Linked devices → Link device.\n\n'
  sudo -u "$APP_USER" -H signal-cli --config "$SIGNAL_CLI_CONFIG" link -n SigSummerRise \
    | tee /dev/stderr \
    | qrencode -t utf8
  printf '\n'
}

print_next_steps() {
  cat <<EOF

Install finished.

Required manual steps:
  1. Edit secrets:
       sudo nano ${SECRETS_FILE}
     Set at least SIGNAL_ACCOUNT, SIGNAL_GROUP_ID, SIGNAL_BOT_ACI,
     PUBLIC_BASE_URL, OPENROUTER_API_KEY, OPENROUTER_MODEL.

  2. List groups (after joining on the phone):
       sudo -u ${APP_USER} signal-cli --config ${SIGNAL_CLI_CONFIG} -a +E164 listGroups -d

  3. Enable services (after secrets and link are complete):
       sudo systemctl enable --now signal-cli sigsummerrise
EOF
  if [[ "$SKIP_CADDY" -eq 0 ]]; then
    cat <<EOF
       sudo systemctl enable --now caddy

  Ensure DNS for ${HOSTNAME} points at this host and ports 80/443 reach the container.
EOF
  fi
  cat <<EOF

Check:
  ss -ltnp | grep -E '8000|8080'
  systemctl status signal-cli sigsummerrise
EOF
}

main() {
  parse_args "$@"
  require_root

  if [[ "$LINK_ONLY" -eq 1 ]]; then
    ensure_user_and_dirs
    command -v signal-cli >/dev/null || die "signal-cli not installed; run full install first"
    command -v qrencode >/dev/null || apt-get install -y --no-install-recommends qrencode
    run_signal_link
    exit 0
  fi

  if [[ "$SKIP_CADDY" -eq 0 && -z "$HOSTNAME" ]]; then
    die "--hostname is required unless --no-caddy"
  fi

  install_packages
  ensure_user_and_dirs
  install_signal_cli
  sync_application
  install_python_env
  ensure_responses_file
  ensure_secrets_file
  install_systemd_units
  configure_caddy

  if ! signal_cli_linked; then
    if [[ -t 0 ]]; then
      run_signal_link
    else
      log "no TTY; skipping interactive signal-cli link"
      log "run later: sudo $0 --link-only"
    fi
  else
    log "signal-cli already linked; skipping link step"
  fi

  print_next_steps
}

main "$@"
