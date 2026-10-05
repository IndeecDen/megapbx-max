#!/usr/bin/env bash
# MegaPBX -> MAX installer for Debian/Ubuntu.
# It copies an explicit manifest and never copies .env, virtualenvs, SQLite or backups.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

SCRIPT_NAME="megapbx-max installer"
REPO_URL="${MEGAPBX_MAX_REPO_URL:-https://github.com/IndeecDen/megapbx-max}"
RELEASE_REF="${MEGAPBX_MAX_RELEASE_REF:-main}"
REF_KIND="${MEGAPBX_MAX_REF_KIND:-auto}"
INSTALL_ROOT="${MEGAPBX_MAX_INSTALL_DIR:-/opt/megapbx-max}"
STATE_ROOT="${MEGAPBX_MAX_STATE_DIR:-/var/lib/megapbx-max}"
SERVICE_NAME="${MEGAPBX_MAX_SERVICE_NAME:-megapbx-max}"
SERVICE_USER="${MEGAPBX_MAX_SERVICE_USER:-megapbx-max}"
CONFIG_FILE="${MEGAPBX_MAX_CONFIG_FILE:-/etc/megapbx-max.env}"
BACKUP_ROOT="${MEGAPBX_MAX_BACKUP_DIR:-/var/backups/megapbx-max}"
BACKEND_HOST="127.0.0.1"
BIND_HOST="$BACKEND_HOST"
BACKEND_PORT="${MEGAPBX_MAX_BACKEND_PORT:-8000}"
PUBLIC_BIND_HOST="${MEGAPBX_MAX_PUBLIC_BIND:-127.0.0.1}"
PYTHON_BIN="${MEGAPBX_MAX_PYTHON:-}"
PIP_INDEX_URL="${MEGAPBX_MAX_PIP_INDEX_URL:-https://pypi.org/simple}"
SOURCE_DIR="${MEGAPBX_MAX_SOURCE_DIR:-}"
GITHUB_TOKEN_FILE="${MEGAPBX_MAX_GITHUB_TOKEN_FILE:-}"
GITHUB_TOKEN=""
DOMAIN="${MEGAPBX_MAX_DOMAIN:-}"
TLS_EMAIL="${MEGAPBX_MAX_TLS_EMAIL:-}"
INSTALL_NGINX="${MEGAPBX_MAX_INSTALL_NGINX:-0}"
ENABLE_TLS="${MEGAPBX_MAX_ENABLE_TLS:-0}"
ALLOW_ALL_DESTINATIONS="${MEGAPBX_MAX_ALLOW_ALL_DESTINATIONS:-0}"
ALLOW_HTTP_API="${MEGAPBX_MAX_ALLOW_HTTP_API:-0}"
NON_INTERACTIVE=0
DRY_RUN=0
NO_START=0
NO_SUBSCRIBE=0
REPLACE_CONFIG=0
REPLACE_NGINX=0
ASSUME_YES=0
ENV_FILE=""
TRANSACTION_ACTIVE=0
STAGE_DIR=""
FINAL_DIR=""
TX_DIR=""
SERVICE_WAS_ACTIVE=0
SERVICE_WAS_ENABLED=0
NGINX_WAS_ACTIVE=0
NGINX_WAS_ENABLED=0
SERVICE_GROUP=""
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
NGINX_LINK="/etc/nginx/sites-enabled/${SERVICE_NAME}"
CURL_AUTH_ARGS=()
CURL_CONFIG_FILE=""
LOCK_DIR=""

refresh_service_paths() {
    UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
    NGINX_SITE="/etc/nginx/sites-available/${SERVICE_NAME}"
    NGINX_LINK="/etc/nginx/sites-enabled/${SERVICE_NAME}"
}

log() {
    printf '[%s] %s\n' "$SCRIPT_NAME" "$*"
}

warn() {
    printf '[%s] WARNING: %s\n' "$SCRIPT_NAME" "$*" >&2
}

die() {
    printf '[%s] ERROR: %s\n' "$SCRIPT_NAME" "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: sudo bash install.sh [options]

Options:
  --ref REF                 Git ref to install (legacy auto detection; default: main)
  --tag REF                 immutable Git tag to install (recommended for production)
  --branch REF              Git branch to install (development only)
  --commit SHA              Git commit/archive ref to install (immutable)
  --env-file FILE           read a simple KEY=VALUE file without executing it
  --source-dir DIR          install from a local checkout instead of GitHub
  --github-token-file FILE  read-only PAT for a private repository
  --non-interactive         never prompt; all required values must be supplied
  --with-nginx              install/configure Nginx
  --no-nginx                do not install/configure Nginx (default)
  --domain DOMAIN           public domain for Nginx
  --enable-tls              issue a Let's Encrypt certificate (requires --with-nginx)
  --tls-email EMAIL         Let's Encrypt email
  --replace-config          replace /etc/megapbx-max.env after a backup
  --replace-nginx           replace an existing Nginx site after a backup
  --no-subscribe            do not call POST /subscriptions after setup
  --no-start                install/enable but do not start or health-check
  --dry-run                 validate and show the plan without changing the system
  --yes                     accept the installation plan
  -h, --help                show help

Example (development; use --tag or --commit in production):
  sudo bash install.sh --ref main --with-nginx --domain bot.example.com \
    --enable-tls --tls-email admin@example.com
EOF
}

is_yes() {
    case "${1,,}" in
        1|y|yes|true|on) return 0 ;;
        *) return 1 ;;
    esac
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "Run this installer as root"
}

check_os() {
    [[ -r /etc/os-release ]] || die "Cannot detect the operating system"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        debian|ubuntu) ;;
        *) die "Only Debian and Ubuntu are supported (detected: ${ID:-unknown})" ;;
    esac
    command -v systemctl >/dev/null 2>&1 || die "systemd is required"
    [[ -d /run/systemd/system ]] || die "The system must be booted with systemd"
    command -v apt-get >/dev/null 2>&1 || die "apt-get is required"
}

check_tty() {
    if (( NON_INTERACTIVE == 0 )) && [[ ! -t 0 || ! -t 1 ]]; then
        die "Interactive installation needs a TTY; use --non-interactive --env-file FILE"
    fi
}

acquire_lock() {
    mkdir -p /run/lock
    if command -v flock >/dev/null 2>&1; then
        exec 9>"/run/lock/${SERVICE_NAME}-install.lock"
        flock -n 9 || die "Another installation is already running"
        return
    fi
    # util-linux is installed below on minimal Debian/Ubuntu images.  Keep a
    # mkdir-based fallback so the installer can bootstrap without flock.
    LOCK_DIR="/run/lock/${SERVICE_NAME}-install.lock.d"
    mkdir "$LOCK_DIR" 2>/dev/null || die "Another installation is already running"
}

parse_args() {
    while (($#)); do
        case "$1" in
            --ref|--version)
                [[ $# -ge 2 ]] || die "$1 requires a value"
                RELEASE_REF="$2"
                REF_KIND=auto
                shift 2
                ;;
            --tag)
                [[ $# -ge 2 ]] || die "--tag requires a value"
                RELEASE_REF="$2"
                REF_KIND=tag
                shift 2
                ;;
            --branch)
                [[ $# -ge 2 ]] || die "--branch requires a value"
                RELEASE_REF="$2"
                REF_KIND=branch
                shift 2
                ;;
            --commit)
                [[ $# -ge 2 ]] || die "--commit requires a value"
                RELEASE_REF="$2"
                REF_KIND=commit
                shift 2
                ;;
            --env-file)
                [[ $# -ge 2 ]] || die "--env-file requires a path"
                ENV_FILE="$2"
                shift 2
                ;;
            --source-dir)
                [[ $# -ge 2 ]] || die "--source-dir requires a path"
                SOURCE_DIR="$2"
                shift 2
                ;;
            --github-token-file)
                [[ $# -ge 2 ]] || die "--github-token-file requires a path"
                GITHUB_TOKEN_FILE="$2"
                shift 2
                ;;
            --domain)
                [[ $# -ge 2 ]] || die "--domain requires a hostname"
                DOMAIN="$2"
                shift 2
                ;;
            --tls-email)
                [[ $# -ge 2 ]] || die "--tls-email requires an email"
                TLS_EMAIL="$2"
                shift 2
                ;;
            --non-interactive|--unattended) NON_INTERACTIVE=1; shift ;;
            --with-nginx) INSTALL_NGINX=1; shift ;;
            --no-nginx) INSTALL_NGINX=0; shift ;;
            --enable-tls) ENABLE_TLS=1; shift ;;
            --replace-config) REPLACE_CONFIG=1; shift ;;
            --replace-nginx) REPLACE_NGINX=1; shift ;;
            --no-subscribe) NO_SUBSCRIBE=1; shift ;;
            --no-start) NO_START=1; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            --yes) ASSUME_YES=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "Unknown option: $1" ;;
        esac
    done
}

load_github_token() {
    [[ -n "$GITHUB_TOKEN_FILE" ]] || return 0
    [[ -f "$GITHUB_TOKEN_FILE" && -r "$GITHUB_TOKEN_FILE" ]] || die "GitHub token file is not readable"
    local mode
    mode="$(stat -c '%a' "$GITHUB_TOKEN_FILE" 2>/dev/null || printf '600')"
    if (( (8#$mode & 077) != 0 )); then
        die "GitHub token file must not be accessible by group/others"
    fi
    IFS= read -r GITHUB_TOKEN < "$GITHUB_TOKEN_FILE" || true
    [[ -n "$GITHUB_TOKEN" ]] || die "GitHub token file is empty"
    [[ "$GITHUB_TOKEN" =~ ^[A-Za-z0-9_.-]+$ ]] || die "GitHub token contains unsupported characters"
    CURL_CONFIG_FILE="$(mktemp /tmp/megapbx-max-curl.XXXXXX)"
    chmod 600 "$CURL_CONFIG_FILE"
    printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" > "$CURL_CONFIG_FILE"
    CURL_AUTH_ARGS=(--config "$CURL_CONFIG_FILE")
}

load_env_file() {
    local file="$1"
    [[ -f "$file" && -r "$file" ]] || die "Environment file is not readable: $file"
    local line key raw
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "${line//[[:space:]]/}" ]] && continue
        [[ "${line:0:1}" == "#" ]] && continue
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || \
            die "Only simple KEY=VALUE lines are supported"
        key="${BASH_REMATCH[1]}"
        raw="${BASH_REMATCH[2]}"
        case "$key" in
            MAX_BOT_TOKEN|MAX_CHAT_ID|MAX_API_BASE|MAX_WEBHOOK_SECRET|MAX_WEBHOOK_URL| \
            MAX_WEBHOOK_UPDATE_TYPES|MAX_API_MAX_RETRIES|MAX_API_RETRY_BASE_SEC| \
            MAX_API_RETRY_MAX_SEC|MAX_API_TIMEOUT_SEC|MAX_WEBHOOK_BODY_BYTES| \
            MEGAPBX_CRM_TOKEN|MEGAPBX_ALLOWED_GROUP|MEGAPBX_ALLOWED_DID|MEGAPBX_DID_NAMES| \
            MEGAPBX_API_BASE|MEGAPBX_API_TOKEN|MEGAPBX_ALLOW_QUERY_TOKEN|MEGAPBX_ALLOW_ALL| \
            MEGAPBX_ALLOW_HTTP_API|MEGAPBX_WEBHOOK_BODY_BYTES|MEGAPBX_ENRICHMENT_REFRESH_SEC| \
            TZ_OFFSET_HOURS|MISSED_MAX_AGE_SEC|MISSED_CLEANUP_INTERVAL_SEC|MISSED_DEDUP_TTL_SEC| \
            JOB_MAX_ATTEMPTS|JOB_RETRY_BASE_SEC|JOB_RETRY_MAX_SEC|JOB_LEASE_SEC| \
            STATE_DB_PATH|MEGAPBX_MAX_RELEASE_REF|MEGAPBX_MAX_INSTALL_DIR|MEGAPBX_MAX_STATE_DIR| \
            MEGAPBX_MAX_SERVICE_NAME|MEGAPBX_MAX_SERVICE_USER|MEGAPBX_MAX_CONFIG_FILE| \
            MEGAPBX_MAX_BACKUP_DIR|MEGAPBX_MAX_BACKEND_PORT|MEGAPBX_MAX_PUBLIC_BIND| \
            MEGAPBX_MAX_PYTHON|MEGAPBX_MAX_PIP_INDEX_URL|MEGAPBX_MAX_SOURCE_DIR| \
            MEGAPBX_MAX_DOMAIN|MEGAPBX_MAX_TLS_EMAIL|MEGAPBX_MAX_INSTALL_NGINX| \
            MEGAPBX_MAX_ENABLE_TLS|MEGAPBX_MAX_GITHUB_TOKEN_FILE|MEGAPBX_MAX_REF_KIND| \
            MEGAPBX_MAX_ALLOW_ALL_DESTINATIONS|MEGAPBX_MAX_ALLOW_HTTP_API) ;;
            *) die "Unsupported key '$key' in $file" ;;
        esac
        if (( ${#raw} >= 2 )) && [[ "${raw:0:1}" == '"' && "${raw: -1}" == '"' ]]; then
            raw="${raw:1:${#raw}-2}"
            raw="${raw//\\\\/\\}"
            raw="${raw//\\\"/\"}"
            raw="${raw//\\$/\$}"
        elif (( ${#raw} >= 2 )) && [[ "${raw:0:1}" == "'" && "${raw: -1}" == "'" ]]; then
            raw="${raw:1:${#raw}-2}"
        fi
        [[ "$raw" != *$'\n'* && "$raw" != *$'\r'* ]] || die "Multiline values are not supported"
        local target="$key"
        case "$key" in
            MEGAPBX_MAX_RELEASE_REF) target=RELEASE_REF ;;
            MEGAPBX_MAX_INSTALL_DIR) target=INSTALL_ROOT ;;
            MEGAPBX_MAX_STATE_DIR) target=STATE_ROOT ;;
            MEGAPBX_MAX_SERVICE_NAME) target=SERVICE_NAME ;;
            MEGAPBX_MAX_SERVICE_USER) target=SERVICE_USER ;;
            MEGAPBX_MAX_CONFIG_FILE) target=CONFIG_FILE ;;
            MEGAPBX_MAX_BACKUP_DIR) target=BACKUP_ROOT ;;
            MEGAPBX_MAX_BACKEND_PORT) target=BACKEND_PORT ;;
            MEGAPBX_MAX_PUBLIC_BIND) target=PUBLIC_BIND_HOST ;;
            MEGAPBX_MAX_PYTHON) target=PYTHON_BIN ;;
            MEGAPBX_MAX_PIP_INDEX_URL) target=PIP_INDEX_URL ;;
            MEGAPBX_MAX_SOURCE_DIR) target=SOURCE_DIR ;;
            MEGAPBX_MAX_DOMAIN) target=DOMAIN ;;
            MEGAPBX_MAX_TLS_EMAIL) target=TLS_EMAIL ;;
            MEGAPBX_MAX_INSTALL_NGINX) target=INSTALL_NGINX ;;
            MEGAPBX_MAX_ENABLE_TLS) target=ENABLE_TLS ;;
            MEGAPBX_MAX_GITHUB_TOKEN_FILE) target=GITHUB_TOKEN_FILE ;;
            MEGAPBX_MAX_REF_KIND) target=REF_KIND ;;
            MEGAPBX_MAX_ALLOW_ALL_DESTINATIONS) target=ALLOW_ALL_DESTINATIONS ;;
            MEGAPBX_MAX_ALLOW_HTTP_API) target=MEGAPBX_ALLOW_HTTP_API ;;
        esac
        printf -v "$target" '%s' "$raw"
    done < "$file"
}

set_defaults() {
    MAX_API_BASE="${MAX_API_BASE:-https://platform-api2.max.ru}"
    MAX_WEBHOOK_UPDATE_TYPES="${MAX_WEBHOOK_UPDATE_TYPES:-message_callback,bot_started,bot_added,bot_removed,bot_admin_permissions_changed}"
    MAX_API_MAX_RETRIES="${MAX_API_MAX_RETRIES:-2}"
    MAX_API_RETRY_BASE_SEC="${MAX_API_RETRY_BASE_SEC:-0.5}"
    MAX_API_RETRY_MAX_SEC="${MAX_API_RETRY_MAX_SEC:-8}"
    MAX_API_TIMEOUT_SEC="${MAX_API_TIMEOUT_SEC:-8}"
    MAX_WEBHOOK_BODY_BYTES="${MAX_WEBHOOK_BODY_BYTES:-1048576}"
    MEGAPBX_ALLOW_QUERY_TOKEN="${MEGAPBX_ALLOW_QUERY_TOKEN:-0}"
    MEGAPBX_ALLOW_ALL="${MEGAPBX_ALLOW_ALL:-0}"
    MEGAPBX_ALLOW_HTTP_API="${MEGAPBX_ALLOW_HTTP_API:-0}"
    MEGAPBX_WEBHOOK_BODY_BYTES="${MEGAPBX_WEBHOOK_BODY_BYTES:-1048576}"
    MEGAPBX_ENRICHMENT_REFRESH_SEC="${MEGAPBX_ENRICHMENT_REFRESH_SEC:-600}"
    TZ_OFFSET_HOURS="${TZ_OFFSET_HOURS:-3}"
    MISSED_MAX_AGE_SEC="${MISSED_MAX_AGE_SEC:-3600}"
    MISSED_CLEANUP_INTERVAL_SEC="${MISSED_CLEANUP_INTERVAL_SEC:-3600}"
    MISSED_DEDUP_TTL_SEC="${MISSED_DEDUP_TTL_SEC:-86400}"
    JOB_MAX_ATTEMPTS="${JOB_MAX_ATTEMPTS:-20}"
    JOB_RETRY_BASE_SEC="${JOB_RETRY_BASE_SEC:-1}"
    JOB_RETRY_MAX_SEC="${JOB_RETRY_MAX_SEC:-300}"
    JOB_LEASE_SEC="${JOB_LEASE_SEC:-300}"
}

read_secret() {
    local var_name="$1" prompt="$2" value="${!1-}" confirmation
    if [[ -z "$value" ]]; then
        (( NON_INTERACTIVE == 0 )) || die "$var_name is required in non-interactive mode"
        read -r -s -p "$prompt" value
        printf '\n'
        [[ -n "$value" ]] || die "$var_name cannot be empty"
        read -r -s -p "Repeat $var_name: " confirmation
        printf '\n'
        [[ "$value" == "$confirmation" ]] || die "$var_name values do not match"
    fi
    printf -v "$var_name" '%s' "$value"
}

read_value() {
    local var_name="$1" prompt="$2" default="${3:-}" value="${!1-}"
    [[ -n "$value" ]] || value="$default"
    if [[ -z "$value" ]]; then
        (( NON_INTERACTIVE == 0 )) || die "$var_name is required in non-interactive mode"
        read -r -p "$prompt" value
    fi
    printf -v "$var_name" '%s' "$value"
}

read_optional() {
    local var_name="$1" prompt="$2" value="${!1-}"
    if (( NON_INTERACTIVE == 0 )) && [[ -z "$value" ]]; then
        read -r -p "$prompt" value || true
    fi
    printf -v "$var_name" '%s' "$value"
}

ask_yes_no() {
    local prompt="$1"
    local default="${2:-y}"
    local answer="$default"
    if (( NON_INTERACTIVE == 0 )); then
        read -r -p "$prompt [y/n]: " answer || true
        [[ -n "$answer" ]] || answer="$default"
    fi
    is_yes "$answer"
}

collect_config() {
    if [[ -n "$ENV_FILE" ]]; then
        load_env_file "$ENV_FILE"
    elif [[ -f "$CONFIG_FILE" && "$REPLACE_CONFIG" -eq 0 ]]; then
        load_env_file "$CONFIG_FILE"
        log "Existing configuration will be reused: $CONFIG_FILE"
    fi
    set_defaults

    read_secret MAX_BOT_TOKEN "MAX bot token: "
    read_value MAX_CHAT_ID "MAX chat ID (positive int64): "
    read_secret MAX_WEBHOOK_SECRET "MAX webhook secret (5-256 chars): "
    read_secret MEGAPBX_CRM_TOKEN "MegaPBX CRM token: "
    read_optional MEGAPBX_ALLOWED_GROUP "Allowed groups, comma-separated (optional): "
    read_optional MEGAPBX_ALLOWED_DID "Allowed DIDs, comma-separated (optional): "
    if [[ -z "$MEGAPBX_ALLOWED_GROUP" && -z "$MEGAPBX_ALLOWED_DID" ]]; then
        if is_yes "$MEGAPBX_ALLOW_ALL" || is_yes "$ALLOW_ALL_DESTINATIONS"; then
            MEGAPBX_ALLOW_ALL=1
            ALLOW_ALL_DESTINATIONS=1
        elif (( NON_INTERACTIVE == 0 )) && ask_yes_no "Allow every MegaPBX direction?" "n"; then
            MEGAPBX_ALLOW_ALL=1
            ALLOW_ALL_DESTINATIONS=1
        else
            die "Configure at least one group/DID or set MEGAPBX_ALLOW_ALL=1"
        fi
    fi
    read_optional MEGAPBX_DID_NAMES "DID=name mapping, comma-separated (optional): "
    read_optional MEGAPBX_API_BASE "MegaPBX API HTTPS base (optional): "
    if [[ -n "$MEGAPBX_API_BASE" ]]; then
        read_secret MEGAPBX_API_TOKEN "MegaPBX API token: "
    fi
    read_value TZ_OFFSET_HOURS "UTC offset for display (default 3): " "3"
    read_value MISSED_MAX_AGE_SEC "Missed correlation TTL seconds: " "3600"
    read_value MISSED_DEDUP_TTL_SEC "Deduplication TTL seconds: " "86400"
    read_value MISSED_CLEANUP_INTERVAL_SEC "Cleanup interval seconds: " "3600"

    if (( NON_INTERACTIVE == 0 )); then
        if [[ -z "$DOMAIN" ]]; then
            read -r -p "Public domain for Nginx/TLS (empty to skip): " DOMAIN || true
        fi
        if [[ -n "$DOMAIN" ]] && ask_yes_no "Install Nginx for $DOMAIN?" "y"; then
            INSTALL_NGINX=1
        fi
        if is_yes "$INSTALL_NGINX" && [[ -z "$TLS_EMAIL" ]] && ask_yes_no "Enable Let's Encrypt TLS?" "y"; then
            ENABLE_TLS=1
        fi
        if is_yes "$ENABLE_TLS"; then
            read -r -p "Let's Encrypt email: " TLS_EMAIL
        fi
    fi
    if is_yes "$ENABLE_TLS"; then
        [[ -n "$DOMAIN" ]] || die "TLS requires --domain"
        [[ "$TLS_EMAIL" == *@* ]] || die "A valid TLS email is required"
        MAX_WEBHOOK_URL="https://${DOMAIN}/max/webhook"
    else
        read_optional MAX_WEBHOOK_URL "Public MAX webhook HTTPS URL (optional): "
    fi
    validate_config
}

validate_webhook_port() {
    local authority="$1" hostport port
    authority="${1#https://}"
    hostport="${authority%%/*}"
    if [[ "$hostport" =~ ^\[[^]]+\](:[0-9]+)?$ ]]; then
        port="${BASH_REMATCH[2]#:}"
        [[ -z "$port" || "$port" == 443 ]] || die "MAX_WEBHOOK_URL must use port 443"
        return 0
    fi
    [[ "$hostport" == *:* ]] || return 0
    port="${hostport##*:}"
    [[ "$port" =~ ^[0-9]+$ ]] || die "MAX_WEBHOOK_URL contains an invalid port"
    [[ "$port" == 443 ]] || die "MAX_WEBHOOK_URL must use port 443"
}

validate_url_shape() {
    local scheme="$1" name="$2" value="$3" authority hostport
    [[ "$value" == "${scheme}://"* ]] || die "${name} must use ${scheme}"
    authority="${value#*://}"
    hostport="${authority%%/*}"
    [[ -n "$hostport" && "$hostport" != *'?'* && "$hostport" != *'#'* && "$hostport" != *'@'* ]] || \
        die "${name} contains an invalid authority"
    [[ "$authority" != *'#'* && "$authority" != *'?'* && "$authority" != *[[:space:]]* ]] || \
        die "${name} must not contain whitespace, query or fragment"
}

validate_config() {
    local LC_ALL=C
    [[ "$RELEASE_REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die "Invalid Git ref"
    [[ "$REF_KIND" == auto || "$REF_KIND" == tag || "$REF_KIND" == branch || "$REF_KIND" == commit ]] || \
        die "REF_KIND must be auto, tag, branch or commit"
    [[ "$SERVICE_NAME" =~ ^[a-zA-Z0-9_.-]+$ ]] || die "Invalid service name"
    [[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "Invalid service user"
    [[ "$INSTALL_ROOT" == /* && "$STATE_ROOT" == /* && "$CONFIG_FILE" == /* && "$BACKUP_ROOT" == /* ]] || \
        die "Install/config/state/backup paths must be absolute"
    if [[ ! "$BACKEND_PORT" =~ ^[0-9]+$ ]] || (( ${#BACKEND_PORT} > 5 )); then
        die "BACKEND_PORT is invalid"
    fi
    if (( 10#$BACKEND_PORT < 1 || 10#$BACKEND_PORT > 65535 )); then
        die "BACKEND_PORT is invalid"
    fi
    [[ "$PUBLIC_BIND_HOST" =~ ^[A-Za-z0-9.:-]+$ ]] || die "Invalid PUBLIC_BIND_HOST"
    [[ "$PUBLIC_BIND_HOST" == "127.0.0.1" || "$PUBLIC_BIND_HOST" == "0.0.0.0" || "$PUBLIC_BIND_HOST" == "::" ]] || \
        die "PUBLIC_BIND_HOST must be 127.0.0.1, 0.0.0.0 or ::"
    if [[ ! "$MAX_CHAT_ID" =~ ^[0-9]+$ ]]; then
        die "MAX_CHAT_ID must be a positive integer"
    fi
    local normalized_chat_id="$MAX_CHAT_ID"
    while [[ ${#normalized_chat_id} -gt 1 && "$normalized_chat_id" == 0* ]]; do
        normalized_chat_id="${normalized_chat_id#0}"
    done
    [[ "$normalized_chat_id" != "0" ]] || die "MAX_CHAT_ID must be a positive integer"
    # The string comparison is intentional: Bash arithmetic would overflow for long IDs.
    # shellcheck disable=SC2071
    if (( ${#normalized_chat_id} > 19 )) || \
        { (( ${#normalized_chat_id} == 19 )) && [[ "$normalized_chat_id" > "9223372036854775807" ]]; }; then
        die "MAX_CHAT_ID exceeds int64"
    fi
    [[ "$MAX_WEBHOOK_SECRET" =~ ^[A-Za-z0-9_-]{5,256}$ ]] || die "Invalid MAX_WEBHOOK_SECRET"
    validate_url_shape https MAX_API_BASE "$MAX_API_BASE"
    if [[ -n "$MAX_WEBHOOK_URL" ]]; then
        validate_url_shape https MAX_WEBHOOK_URL "$MAX_WEBHOOK_URL"
        validate_webhook_port "$MAX_WEBHOOK_URL"
    fi
    [[ -n "$MEGAPBX_CRM_TOKEN" ]] || die "MEGAPBX_CRM_TOKEN is required"
    if [[ -z "$MEGAPBX_ALLOWED_GROUP" && -z "$MEGAPBX_ALLOWED_DID" ]]; then
        if is_yes "$ALLOW_ALL_DESTINATIONS" || is_yes "$MEGAPBX_ALLOW_ALL"; then
            MEGAPBX_ALLOW_ALL=1
            ALLOW_ALL_DESTINATIONS=1
        else
            die "No group/DID allowlist and all destinations are not explicitly allowed"
        fi
    fi
    if [[ -n "$MEGAPBX_API_BASE" ]]; then
        if [[ "$MEGAPBX_API_BASE" == https://* ]]; then
            validate_url_shape https MEGAPBX_API_BASE "$MEGAPBX_API_BASE"
        elif [[ "$MEGAPBX_API_BASE" == http://* ]]; then
            validate_url_shape http MEGAPBX_API_BASE "$MEGAPBX_API_BASE"
        else
            die "MEGAPBX_API_BASE must be an absolute HTTP(S) URL"
        fi
        if [[ "$MEGAPBX_API_BASE" == http://* ]] && ! is_yes "$ALLOW_HTTP_API" && ! is_yes "$MEGAPBX_ALLOW_HTTP_API"; then
            die "HTTP MegaPBX API requires explicit MEGAPBX_MAX_ALLOW_HTTP_API=1"
        fi
        [[ -n "$MEGAPBX_API_TOKEN" ]] || die "MEGAPBX_API_TOKEN is required with MEGAPBX_API_BASE"
    elif [[ -n "$MEGAPBX_API_TOKEN" ]]; then
        die "MEGAPBX_API_TOKEN requires MEGAPBX_API_BASE"
    fi
    if is_yes "$ENABLE_TLS"; then
        is_yes "$INSTALL_NGINX" || die "--enable-tls requires --with-nginx"
    fi
    if is_yes "$INSTALL_NGINX"; then
        [[ -n "$DOMAIN" ]] || die "--with-nginx requires --domain"
        [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "Invalid domain"
    fi
}

write_env_line() {
    local key="$1" value="$2"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\\\$}"
    printf '%s="%s"\n' "$key" "$value" >> "$CONFIG_FILE"
}

write_config() {
    local tmp
    tmp="$(mktemp "${CONFIG_FILE}.tmp.XXXXXX")"
    : > "$tmp"
    write_env_line MAX_BOT_TOKEN "$MAX_BOT_TOKEN"
    write_env_line MAX_CHAT_ID "$MAX_CHAT_ID"
    write_env_line MAX_API_BASE "$MAX_API_BASE"
    write_env_line MAX_WEBHOOK_SECRET "$MAX_WEBHOOK_SECRET"
    write_env_line MAX_WEBHOOK_URL "$MAX_WEBHOOK_URL"
    write_env_line MAX_WEBHOOK_UPDATE_TYPES "$MAX_WEBHOOK_UPDATE_TYPES"
    write_env_line MAX_API_MAX_RETRIES "$MAX_API_MAX_RETRIES"
    write_env_line MAX_API_RETRY_BASE_SEC "$MAX_API_RETRY_BASE_SEC"
    write_env_line MAX_API_RETRY_MAX_SEC "$MAX_API_RETRY_MAX_SEC"
    write_env_line MAX_API_TIMEOUT_SEC "$MAX_API_TIMEOUT_SEC"
    write_env_line MAX_WEBHOOK_BODY_BYTES "$MAX_WEBHOOK_BODY_BYTES"
    write_env_line MEGAPBX_CRM_TOKEN "$MEGAPBX_CRM_TOKEN"
    write_env_line MEGAPBX_ALLOWED_GROUP "$MEGAPBX_ALLOWED_GROUP"
    write_env_line MEGAPBX_ALLOWED_DID "$MEGAPBX_ALLOWED_DID"
    write_env_line MEGAPBX_DID_NAMES "$MEGAPBX_DID_NAMES"
    write_env_line MEGAPBX_API_BASE "$MEGAPBX_API_BASE"
    write_env_line MEGAPBX_API_TOKEN "$MEGAPBX_API_TOKEN"
    write_env_line MEGAPBX_ALLOW_QUERY_TOKEN "$MEGAPBX_ALLOW_QUERY_TOKEN"
    write_env_line MEGAPBX_ALLOW_ALL "$MEGAPBX_ALLOW_ALL"
    write_env_line MEGAPBX_ALLOW_HTTP_API "$MEGAPBX_ALLOW_HTTP_API"
    write_env_line MEGAPBX_WEBHOOK_BODY_BYTES "$MEGAPBX_WEBHOOK_BODY_BYTES"
    write_env_line MEGAPBX_ENRICHMENT_REFRESH_SEC "$MEGAPBX_ENRICHMENT_REFRESH_SEC"
    write_env_line TZ_OFFSET_HOURS "$TZ_OFFSET_HOURS"
    write_env_line MISSED_MAX_AGE_SEC "$MISSED_MAX_AGE_SEC"
    write_env_line MISSED_CLEANUP_INTERVAL_SEC "$MISSED_CLEANUP_INTERVAL_SEC"
    write_env_line MISSED_DEDUP_TTL_SEC "$MISSED_DEDUP_TTL_SEC"
    write_env_line JOB_MAX_ATTEMPTS "$JOB_MAX_ATTEMPTS"
    write_env_line JOB_RETRY_BASE_SEC "$JOB_RETRY_BASE_SEC"
    write_env_line JOB_RETRY_MAX_SEC "$JOB_RETRY_MAX_SEC"
    write_env_line JOB_LEASE_SEC "$JOB_LEASE_SEC"
    write_env_line STATE_DB_PATH "$STATE_ROOT/state.sqlite3"
    chown root:root "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$CONFIG_FILE"
}

secure_config_file() {
    [[ -f "$CONFIG_FILE" ]] || return 0
    chown root:root "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
}

validate_runtime_config() {
    set -a
    load_env_file "$CONFIG_FILE"
    set +a
    "$FINAL_DIR/.venv/bin/python" -c 'from megapbx_max.config import Settings; Settings.from_env()'
}

show_plan() {
    log "Git ref: $RELEASE_REF ($REF_KIND)"
    log "Install directory: $INSTALL_ROOT"
    log "State directory: $STATE_ROOT"
    log "Service: $SERVICE_NAME ($SERVICE_USER)"
    log "Config: $CONFIG_FILE (0600)"
    log "Backend: $BIND_HOST:$BACKEND_PORT"
    if is_yes "$INSTALL_NGINX"; then
        log "Nginx/TLS: enabled for https://$DOMAIN/"
    else
        log "Nginx: disabled"
    fi
    (( DRY_RUN == 1 )) && return 0
    (( ASSUME_YES == 1 )) || ask_yes_no "Continue?" "y" || die "Installation cancelled"
}

install_packages() {
    (( DRY_RUN == 0 )) || return 0
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    local packages=(python3 python3-venv ca-certificates coreutils curl tar util-linux)
    if is_yes "$INSTALL_NGINX"; then
        packages+=(nginx)
        if is_yes "$ENABLE_TLS"; then
            packages+=(certbot python3-certbot-nginx)
        fi
    fi
    apt-get install -y --no-install-recommends "${packages[@]}"
}

select_python() {
    if [[ -n "$PYTHON_BIN" ]]; then
        command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "Python binary not found: $PYTHON_BIN"
    else
        local candidate
        for candidate in python3.11 python3; do
            if command -v "$candidate" >/dev/null 2>&1 && \
                "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
                PYTHON_BIN="$candidate"
                break
            fi
        done
    fi
    [[ -n "$PYTHON_BIN" ]] || die "Python 3.11+ is required"
    "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || \
        die "Python 3.11+ is required"
    log "Python: $($PYTHON_BIN --version 2>&1)"
}

ensure_service_user() {
    (( DRY_RUN == 0 )) || return 0
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin --user-group "$SERVICE_USER"
    fi
    SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
    install -d -o root -g "$SERVICE_GROUP" -m 750 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 700 "$STATE_ROOT"
    install -d -o root -g "$SERVICE_GROUP" -m 750 "$BACKUP_ROOT"
    chmod 700 "$BACKUP_ROOT"
}

download_release() {
    STAGE_DIR="$(mktemp -d "$INSTALL_ROOT/releases/.stage.XXXXXX")"
    local extracted
    if [[ -n "$SOURCE_DIR" ]]; then
        [[ -d "$SOURCE_DIR" ]] || die "SOURCE_DIR does not exist: $SOURCE_DIR"
        cp -- "$SOURCE_DIR/pyproject.toml" "$SOURCE_DIR/requirements.txt" "$SOURCE_DIR/requirements.lock" \
            "$SOURCE_DIR/LICENSE" "$STAGE_DIR/"
        cp -a -- "$SOURCE_DIR/src" "$STAGE_DIR/src"
        [[ -f "$SOURCE_DIR/README.md" ]] && cp -- "$SOURCE_DIR/README.md" "$STAGE_DIR/"
    else
        extracted="$(mktemp -d)"
        local archive="$extracted/source.tar.gz"
        local ref_path
        case "$REF_KIND" in
            tag) ref_path="tags/$RELEASE_REF" ;;
            branch) ref_path="heads/$RELEASE_REF" ;;
            commit) ref_path="$RELEASE_REF" ;;
            auto)
                if [[ "$RELEASE_REF" == v* || "$RELEASE_REF" == *-v* ]]; then
                    ref_path="tags/$RELEASE_REF"
                else
                    ref_path="heads/$RELEASE_REF"
                fi
                ;;
        esac
        curl --fail --silent --show-error --location --retry 3 --connect-timeout 20 \
            "${CURL_AUTH_ARGS[@]}" --proto '=https' --tlsv1.2 \
            "$REPO_URL/archive/$ref_path.tar.gz" -o "$archive"
        tar -xzf "$archive" -C "$extracted"
        local root
        root="$(find "$extracted" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
        [[ -n "$root" ]] || die "Downloaded archive is empty"
        cp -- "$root/pyproject.toml" "$root/requirements.txt" "$root/requirements.lock" "$root/LICENSE" "$STAGE_DIR/"
        cp -a -- "$root/src" "$STAGE_DIR/src"
        [[ -f "$root/README.md" ]] && cp -- "$root/README.md" "$STAGE_DIR/"
        if [[ -f "$root/SHA256SUMS" ]]; then
            cp -- "$root/SHA256SUMS" "$STAGE_DIR/"
            (cd "$STAGE_DIR" && sha256sum -c SHA256SUMS) || die "Checksum verification failed"
        else
            die "SHA256SUMS is missing; refusing an unverified remote release"
        fi
        rm -rf -- "$extracted"
    fi
    [[ -s "$STAGE_DIR/pyproject.toml" && -d "$STAGE_DIR/src" ]] || die "Release manifest is incomplete"
    "$PYTHON_BIN" -m compileall -q "$STAGE_DIR/src"
    find "$STAGE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +
}

prepare_backup_root() {
    (( DRY_RUN == 0 )) || return 0
    install -d -o root -g root -m 700 "$BACKUP_ROOT"
}


begin_transaction() {
    (( DRY_RUN == 0 )) || return 0
    TX_DIR="$(mktemp -d "$BACKUP_ROOT/transaction.XXXXXX")"
    chmod 700 "$TX_DIR"
    [[ -L "$INSTALL_ROOT/current" ]] && readlink "$INSTALL_ROOT/current" > "$TX_DIR/current-target"
    [[ -f "$UNIT_PATH" ]] && { cp -p "$UNIT_PATH" "$TX_DIR/unit"; touch "$TX_DIR/unit-existed"; }
    [[ -f "$NGINX_SITE" ]] && { cp -p "$NGINX_SITE" "$TX_DIR/nginx"; touch "$TX_DIR/nginx-existed"; }
    [[ -L "$NGINX_LINK" ]] && { readlink "$NGINX_LINK" > "$TX_DIR/nginx-link"; touch "$TX_DIR/nginx-link-existed"; }
    if [[ -f "$CONFIG_FILE" ]]; then
        cp -p "$CONFIG_FILE" "$TX_DIR/config"
        touch "$TX_DIR/config-existed"
    fi
    if systemctl is-active --quiet "$SERVICE_NAME"; then SERVICE_WAS_ACTIVE=1; fi
    if systemctl is-enabled --quiet "$SERVICE_NAME"; then SERVICE_WAS_ENABLED=1; fi
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
        NGINX_WAS_ACTIVE=1
    fi
    if command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet nginx; then
        NGINX_WAS_ENABLED=1
    fi
    TRANSACTION_ACTIVE=1
    log "Transaction backup: $TX_DIR"
}

restore_file() {
    local saved="$1" target="$2"
    if [[ -f "$saved" ]]; then cp -p "$saved" "$target"; else rm -f "$target"; fi
}

rollback_transaction() {
    (( TRANSACTION_ACTIVE == 1 )) || return 0
    TRANSACTION_ACTIVE=0
    warn "Installation failed; restoring previous files (SQLite state is preserved)"
    set +e
    systemctl stop "$SERVICE_NAME" >/dev/null 2>&1
    if [[ -f "$TX_DIR/current-target" ]]; then
        rm -f "$INSTALL_ROOT/current"
        ln -s "$(cat "$TX_DIR/current-target")" "$INSTALL_ROOT/current"
    else
        rm -f "$INSTALL_ROOT/current"
    fi
    if [[ -f "$TX_DIR/unit-existed" ]]; then restore_file "$TX_DIR/unit" "$UNIT_PATH"; else rm -f "$UNIT_PATH"; fi
    if [[ -f "$TX_DIR/nginx-existed" ]]; then restore_file "$TX_DIR/nginx" "$NGINX_SITE"; else rm -f "$NGINX_SITE"; fi
    if [[ -f "$TX_DIR/nginx-link-existed" ]]; then
        rm -f "$NGINX_LINK"
        ln -s "$(cat "$TX_DIR/nginx-link")" "$NGINX_LINK"
    else
        rm -f "$NGINX_LINK"
    fi
    if [[ -f "$TX_DIR/config-existed" ]]; then
        restore_file "$TX_DIR/config" "$CONFIG_FILE"
    else
        rm -f "$CONFIG_FILE"
    fi
    systemctl daemon-reload >/dev/null 2>&1
    if (( SERVICE_WAS_ENABLED == 1 )); then
        systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    else
        systemctl disable "$SERVICE_NAME" >/dev/null 2>&1
    fi
    if (( SERVICE_WAS_ACTIVE == 1 )); then
        systemctl start "$SERVICE_NAME" >/dev/null 2>&1
    fi
    if (( NGINX_WAS_ENABLED == 1 )); then
        systemctl enable nginx >/dev/null 2>&1
    else
        systemctl disable nginx >/dev/null 2>&1 || true
    fi
    if (( NGINX_WAS_ACTIVE == 1 )); then
        systemctl reload nginx >/dev/null 2>&1 || systemctl restart nginx >/dev/null 2>&1 || true
    fi
    set -e
}

on_exit() {
    local status=$?
    trap - EXIT
    if (( status != 0 && TRANSACTION_ACTIVE == 1 )); then rollback_transaction || true; fi
    [[ -n "$STAGE_DIR" && -d "$STAGE_DIR" ]] && rm -rf "$STAGE_DIR"
    [[ -n "$CURL_CONFIG_FILE" && -f "$CURL_CONFIG_FILE" ]] && rm -f "$CURL_CONFIG_FILE"
    if [[ -n "$LOCK_DIR" && -d "$LOCK_DIR" ]]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
    exit "$status"
}
trap on_exit EXIT

render_unit() {
    local tmp
    tmp="$(mktemp "/tmp/${SERVICE_NAME}.service.XXXXXX")"
    cat > "$tmp" <<EOF
[Unit]
Description=MegaPBX MAX missed-call notifier
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_ROOT/current
EnvironmentFile=$CONFIG_FILE
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
StateDirectory=$SERVICE_NAME
StateDirectoryMode=0700
ReadWritePaths=$STATE_ROOT
ExecStart=$INSTALL_ROOT/current/.venv/bin/megapbx-max serve --host $BIND_HOST --port $BACKEND_PORT
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
KillSignal=SIGINT
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemd-analyze verify "$tmp" >/dev/null
    install -o root -g root -m 644 "$tmp" "$UNIT_PATH"
    rm -f "$tmp"
}

nginx_server_name_in_file() {
    local file="$1"
    local domain="${2,,}"
    awk -v domain="$domain" '
        /server_name[[:space:]]/ {
            line=$0
            sub(/^.*server_name[[:space:]]+/, "", line)
            sub(/[[:space:]]*;.*$/, "", line)
            gsub(/"/, "", line)
            count=split(line, names, /[[:space:]]+/)
            for (i=1; i<=count; i++) {
                if (tolower(names[i]) == domain || tolower(names[i]) == "*." domain) {
                    found=1
                }
            }
        }
        END { exit(found ? 0 : 1) }
    ' "$file"
}

check_nginx_server_name_conflicts() {
    local root candidate own_real candidate_real
    [[ -n "$DOMAIN" ]] || return 0
    own_real="$(readlink -f "$NGINX_SITE" 2>/dev/null || true)"
    for root in /etc/nginx/sites-enabled /etc/nginx/conf.d; do
        [[ -d "$root" ]] || continue
        for candidate in "$root"/*; do
            [[ -f "$candidate" ]] || continue
            candidate_real="$(readlink -f "$candidate" 2>/dev/null || true)"
            if [[ -n "$own_real" && "$candidate_real" == "$own_real" ]]; then
                continue
            fi
            if nginx_server_name_in_file "$candidate" "$DOMAIN"; then
                die "Nginx server_name '$DOMAIN' is already configured in $candidate"
            fi
        done
    done
}

render_nginx() {
    is_yes "$INSTALL_NGINX" || return 0
    check_nginx_server_name_conflicts
    if [[ "$REPLACE_NGINX" -eq 0 && -e "$NGINX_SITE" ]]; then
        die "Nginx site exists; use --replace-nginx after review"
    fi
    local max_body_bytes="$MAX_WEBHOOK_BODY_BYTES"
    if (( MEGAPBX_WEBHOOK_BODY_BYTES > max_body_bytes )); then
        max_body_bytes="$MEGAPBX_WEBHOOK_BODY_BYTES"
    fi
    local limit_kb=$(((max_body_bytes + 1023) / 1024))
    (( limit_kb < 1 )) && limit_kb=1
    cat > "$NGINX_SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location = / { return 404; }

    location = /healthz {
        limit_except GET { deny all; }
        access_log off;
        proxy_pass http://127.0.0.1:$BACKEND_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /readyz {
        limit_except GET { deny all; }
        access_log off;
        proxy_pass http://127.0.0.1:$BACKEND_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location = /max/webhook {
        limit_except POST { deny all; }
        access_log off;
        client_max_body_size ${limit_kb}k;
        proxy_pass http://127.0.0.1:$BACKEND_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Max-Bot-Api-Secret \$http_x_max_bot_api_secret;
        proxy_connect_timeout 5s;
        proxy_read_timeout 35s;
    }

    location = /megapbx/webhook {
        limit_except POST { deny all; }
        access_log off;
        client_max_body_size ${limit_kb}k;
        proxy_pass http://127.0.0.1:$BACKEND_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-CRM-Token \$http_x_crm_token;
        proxy_connect_timeout 5s;
        proxy_read_timeout 35s;
    }
}
EOF
    chown root:root "$NGINX_SITE"
    chmod 644 "$NGINX_SITE"
    ln -sfn "$NGINX_SITE" "$NGINX_LINK"
    nginx -t
    systemctl enable --now nginx
    systemctl reload nginx
    if is_yes "$ENABLE_TLS"; then
        certbot --nginx --non-interactive --agree-to-tos --email "$TLS_EMAIL" --redirect -d "$DOMAIN"
        nginx -t
        systemctl reload nginx
    elif [[ -n "$MAX_WEBHOOK_URL" ]]; then
        warn "Local Nginx TLS is disabled; ensure the configured external HTTPS endpoint is reachable"
    else
        warn "TLS is disabled; configure an external HTTPS endpoint before using MAX Webhook"
    fi
}

health_check() {
    (( NO_START == 0 )) || return 0
    systemctl is-active --quiet "$SERVICE_NAME" || die "Service is not active"
    local attempt
    for ((attempt = 1; attempt <= 20; attempt++)); do
        if curl --fail --silent --max-time 3 "http://127.0.0.1:${BACKEND_PORT}/readyz" >/dev/null; then
            log "Health check passed"
            return 0
        fi
        sleep 1
    done
    journalctl -u "$SERVICE_NAME" -n 40 --no-pager >&2 || true
    die "Readiness check failed"
}

configure_subscription() {
    (( NO_START == 0 )) || return 0
    (( NO_SUBSCRIBE == 0 )) || return 0
    [[ -n "$MAX_WEBHOOK_URL" ]] || { warn "MAX_WEBHOOK_URL is empty; subscription skipped"; return 0; }
    set -a
    load_env_file "$CONFIG_FILE"
    set +a
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt "$FINAL_DIR/.venv/bin/megapbx-max" check
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt "$FINAL_DIR/.venv/bin/megapbx-max" subscribe
    log "MAX webhook subscription configured"
}

print_result() {
    log "Installation completed"
    log "Service: systemctl status $SERVICE_NAME"
    log "Config: $CONFIG_FILE"
    log "SQLite: $STATE_ROOT/state.sqlite3 (preserved on rollback/update)"
    if [[ -n "$MAX_WEBHOOK_URL" ]]; then
        log "MAX webhook: $MAX_WEBHOOK_URL"
    fi
    if is_yes "$INSTALL_NGINX" && is_yes "$ENABLE_TLS"; then
        log "MegaPBX webhook: https://${DOMAIN}/megapbx/webhook"
    fi
    log "Logs: journalctl -u $SERVICE_NAME -f"
}

main() {
    parse_args "$@"
    require_root
    check_os
    check_tty
    acquire_lock
    [[ -z "$ENV_FILE" || -f "$ENV_FILE" ]] || die "Environment file not found: $ENV_FILE"
    collect_config
    refresh_service_paths
    if is_yes "$INSTALL_NGINX"; then
        check_nginx_server_name_conflicts
        BIND_HOST="$BACKEND_HOST"
    else
        BIND_HOST="$PUBLIC_BIND_HOST"
    fi
    show_plan
    (( DRY_RUN == 1 )) && return 0

    prepare_backup_root
    begin_transaction
    install_packages
    select_python
    ensure_service_user
    load_github_token
    download_release

    FINAL_DIR="$INSTALL_ROOT/releases/${RELEASE_REF//\//_}-$(date -u +%Y%m%d%H%M%S)-$$"
    mv "$STAGE_DIR" "$FINAL_DIR"
    STAGE_DIR=""
    "$PYTHON_BIN" -m venv "$FINAL_DIR/.venv"
    "$FINAL_DIR/.venv/bin/python" -m ensurepip --upgrade
    PIP_INDEX_URL="$PIP_INDEX_URL" "$FINAL_DIR/.venv/bin/python" -m pip install \
        --disable-pip-version-check --no-cache-dir -r "$FINAL_DIR/requirements.lock"
    PIP_INDEX_URL="$PIP_INDEX_URL" "$FINAL_DIR/.venv/bin/python" -m pip install \
        --disable-pip-version-check --no-cache-dir --no-deps "$FINAL_DIR"
    (cd "$FINAL_DIR" && "$FINAL_DIR/.venv/bin/python" -c 'import megapbx_max; print(megapbx_max.__version__)')

    chown -R root:"$SERVICE_GROUP" "$FINAL_DIR"
    chmod -R u=rwX,g=rX,o= "$FINAL_DIR"
    if [[ "$REPLACE_CONFIG" -eq 1 || ! -f "$CONFIG_FILE" ]]; then write_config; fi
    secure_config_file
    validate_runtime_config
    render_unit
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    rm -f "$INSTALL_ROOT/current"
    ln -s "$FINAL_DIR" "$INSTALL_ROOT/current"

    if (( NO_START == 0 )); then
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            systemctl restart "$SERVICE_NAME"
        else
            systemctl start "$SERVICE_NAME"
        fi
    fi
    render_nginx
    health_check
    configure_subscription
    TRANSACTION_ACTIVE=0
    print_result
}

main "$@"
