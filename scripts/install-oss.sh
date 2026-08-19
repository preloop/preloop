#!/usr/bin/env sh

# Everything below is function definitions; execution starts at the single
# `main "$@"` call on the LAST line. That structure is deliberate: this script
# is documented to run as `curl ... | sh`, where sh parses its input
# incrementally from a pipe. Without the main() wrapper, a truncated download
# executes half a script, and any child process that reads stdin (docker
# compose exec, etc.) steals unparsed script bytes from the pipe and corrupts
# the parse mid-file ("Unterminated quoted string"). With it, nothing runs
# until the whole script has been read. Belt and braces: every command that
# does not need stdin also gets an explicit `< /dev/null`.

set -eu

PRELOOP_REPO="${PRELOOP_REPO:-preloop/preloop}"
PRELOOP_DEFAULT_VERSION="${PRELOOP_DEFAULT_VERSION:-}"
PRELOOP_VERSION="${PRELOOP_VERSION:-$PRELOOP_DEFAULT_VERSION}"
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.preloop-oss}"

# Full docker output (pull progress, compose up chatter) goes here instead of
# the terminal; the happy path prints a few curated lines and this path.
LOG_FILE="${INSTALL_DIR}/install.log"

# Public base URL this instance is reached at. Console, API and MCP share one
# origin: the TLS proxy (or the console nginx on plain HTTP) reverse-proxies
# /api and /mcp. Model-gateway routes (/openai, /anthropic, /gemini) go
# straight to the gateway container so streaming TTFB does not pay an extra
# hop through the console.
#
#   PRELOOP_URL=https://preloop.example.com  -> public deploy, TLS via certbot
#   PRELOOP_URL=http://localhost:3000        -> local (default)
#
# TLS knobs:
#   PRELOOP_TLS_EMAIL     contact address for Let's Encrypt (recommended)
#   PRELOOP_TLS_STAGING=1 use the Let's Encrypt staging CA (rehearsals; the
#                         issued cert is NOT trusted by browsers)
#   PRELOOP_SKIP_TLS=1    keep the https URL but do not touch certificates
#                         (you terminate TLS yourself, e.g. behind a LB)
#
# SMTP (approval emails, invitations, password resets). Set these to skip the
# interactive prompts; leave SMTP_HOST empty to run without email.
#   SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_PASSWORD SMTP_FROM SMTP_FROM_NAME
#   PRELOOP_SKIP_SMTP=1   never prompt for SMTP
PRELOOP_URL="${PRELOOP_URL:-}"
PRELOOP_TLS_EMAIL="${PRELOOP_TLS_EMAIL:-}"
PRELOOP_TLS_STAGING="${PRELOOP_TLS_STAGING:-}"
PRELOOP_SKIP_TLS="${PRELOOP_SKIP_TLS:-}"
SMTP_HOST="${SMTP_HOST:-}"
SMTP_PORT="${SMTP_PORT:-587}"
SMTP_USERNAME="${SMTP_USERNAME:-}"
SMTP_PASSWORD="${SMTP_PASSWORD:-}"
SMTP_FROM="${SMTP_FROM:-}"
SMTP_FROM_NAME="${SMTP_FROM_NAME:-Preloop}"
PRELOOP_SKIP_SMTP="${PRELOOP_SKIP_SMTP:-}"

# First user + signup lockdown. Setting username AND password runs this
# unattended (email is validated up front: create_first_user.py requires it);
# PRELOOP_SKIP_ADMIN=1 keeps signups open and asks nothing.
PRELOOP_ADMIN_USERNAME="${PRELOOP_ADMIN_USERNAME:-}"
PRELOOP_ADMIN_EMAIL="${PRELOOP_ADMIN_EMAIL:-}"
PRELOOP_ADMIN_PASSWORD="${PRELOOP_ADMIN_PASSWORD:-}"
PRELOOP_SKIP_ADMIN="${PRELOOP_SKIP_ADMIN:-}"
CREATE_ADMIN=0

# First-user setup token. While the instance has zero users, /register only
# accepts signups that carry this token (the "setup link" in the footer), so
# a freshly reachable instance can never be claimed by a stranger. Provide
# your own via the environment to skip generation; the generated one is kept
# across re-runs (load_existing_env).
PRELOOP_BOOTSTRAP_TOKEN="${PRELOOP_BOOTSTRAP_TOKEN:-}"

DEFAULT_URL="http://localhost:3000"

# Replace (or append) a single KEY=value line in the install's .env.
set_env_value() {
  key="$1"
  value="$2"
  env_file="${INSTALL_DIR}/.env"
  tmp_file="${env_file}.set"
  if [ -f "$env_file" ]; then
    grep -v "^${key}=" "$env_file" > "$tmp_file" || true
  else
    : > "$tmp_file"
  fi
  echo "${key}=${value}" >> "$tmp_file"
  mv "$tmp_file" "$env_file"
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

# Verify docker exists AND its daemon actually answers, BEFORE any other work.
# A present binary with a wedged daemon (the classic stuck-Docker-Desktop
# case) would otherwise sail through `command -v docker` and then hang
# indefinitely at the first pull with no message at all. `timeout` bounds the
# probe where available (Linux coreutils, modern macOS); without it, fall back
# to an unbounded probe rather than skipping the check.
docker_preflight() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed (the 'docker' command was not found)." >&2
    echo "  Install Docker Engine or Docker Desktop: https://docs.docker.com/get-docker/" >&2
    echo "  then re-run this installer." >&2
    exit 1
  fi

  probe_status=0
  if command -v timeout >/dev/null 2>&1; then
    timeout 10 docker info < /dev/null >/dev/null 2>&1 || probe_status=$?
  else
    docker info < /dev/null >/dev/null 2>&1 || probe_status=$?
  fi

  if [ "$probe_status" -eq 124 ]; then
    # 124 is timeout(1)'s exit code for "command still running at the deadline".
    echo "Docker is installed but its daemon did not respond within 10 seconds." >&2
    echo "The daemon looks WEDGED (running but unresponsive). Restart it:" >&2
    echo "  - Docker Desktop (macOS):   killall Docker && open -a Docker" >&2
    echo "    then wait until it reports 'running' and re-run this installer." >&2
    echo "  - Docker Desktop (Windows): quit Docker Desktop fully and reopen it." >&2
    echo "  - Linux:                    sudo systemctl restart docker" >&2
    echo "Verify with: docker info" >&2
    exit 1
  elif [ "$probe_status" -ne 0 ]; then
    echo "Docker is installed but the daemon is not running." >&2
    echo "  - Docker Desktop (Mac/Windows): open Docker Desktop and wait until" >&2
    echo "    it says 'running', then re-run this installer." >&2
    echo "  - Linux: sudo systemctl start docker" >&2
    echo "Verify with: docker info" >&2
    exit 1
  fi

  if ! docker compose version < /dev/null >/dev/null 2>&1; then
    echo "Docker Compose v2 is required (the 'docker compose' subcommand failed)." >&2
    echo "  Upgrade Docker, or install the compose plugin:" >&2
    echo "  https://docs.docker.com/compose/install/" >&2
    exit 1
  fi
}

resolve_version() {
  if [ -n "$PRELOOP_VERSION" ]; then
    echo "$PRELOOP_VERSION"
    return
  fi

  latest_json="$(curl -fsSL "https://api.github.com/repos/${PRELOOP_REPO}/releases/latest")"
  version="$(printf '%s' "$latest_json" | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  if [ -z "$version" ]; then
    echo "Could not determine the latest Preloop release" >&2
    exit 1
  fi
  echo "${version#v}"
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  fi
}

generate_bootstrap_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
  fi
}

# Load the settings of an existing install so a re-run UPGRADES it instead of
# silently reconfiguring it: prior values become the defaults for every prompt
# and for anything not overridden on this run.
PREVIOUS_VERSION=""
PRELOOP_TLS_ENABLED=""
load_existing_env() {
  env_file="${INSTALL_DIR}/.env"
  [ -f "$env_file" ] || return 0

  PREVIOUS_VERSION="$(env_value PRELOOP_VERSION "$env_file")"
  PRELOOP_TLS_ENABLED="$(env_value PRELOOP_TLS_ENABLED "$env_file")"

  # Existing values win over the built-in defaults, but NOT over anything the
  # caller explicitly passed in the environment on this run.
  [ -n "$PRELOOP_URL" ] || PRELOOP_URL="$(env_value PRELOOP_URL "$env_file")"
  [ -n "$PRELOOP_BOOTSTRAP_TOKEN" ] \
    || PRELOOP_BOOTSTRAP_TOKEN="$(env_value PRELOOP_BOOTSTRAP_TOKEN "$env_file")"
  [ -n "$SMTP_HOST" ] || SMTP_HOST="$(env_value SMTP_HOST "$env_file")"
  [ -n "$SMTP_USERNAME" ] || SMTP_USERNAME="$(env_value SMTP_USERNAME "$env_file")"
  [ -n "$SMTP_PASSWORD" ] || SMTP_PASSWORD="$(env_value SMTP_PASSWORD "$env_file")"
  [ -n "$SMTP_FROM" ] || SMTP_FROM="$(env_value SMTP_FROM "$env_file")"
  existing_port="$(env_value SMTP_PORT "$env_file")"
  [ -z "$existing_port" ] || SMTP_PORT="$existing_port"
  existing_from_name="$(env_value SMTP_FROM_NAME "$env_file")"
  [ -z "$existing_from_name" ] || SMTP_FROM_NAME="$existing_from_name"
}

env_value() {
  # last assignment wins, value may contain '='
  sed -n "s/^$1=//p" "$2" 2>/dev/null | tail -n 1
}

# True only when a real terminal is attached. `curl ... | sh` leaves stdin as
# the pipe, so prompts must go through /dev/tty — but /dev/tty merely EXISTING
# is not enough (in CI / cloud-init it opens and then fails), so probe it.
# The probe must run in a subshell: `:` is a POSIX special builtin, and a
# failed redirection on a special builtin is FATAL under sh implementations
# like dash — a bare `{ : < /dev/tty; }` kills the whole script the moment
# there is no controlling tty, which is exactly the unattended case. The
# subshell contains the failure and turns it into a plain non-zero status.
has_tty() {
  [ -e /dev/tty ] || return 1
  ( : < /dev/tty; ) 2>/dev/null
}

# Plausibility check only (something@something.tld, no whitespace). The goal
# is to reject, AT THE PROMPT, values that scripts/create_first_user.py would
# refuse after the entire install has run — not to implement RFC 5322.
valid_email() {
  case "$1" in
    *[[:space:]]*) return 1 ;;
    ?*@?*.?*) return 0 ;;
  esac
  return 1
}

# Ask for the public URL. On an existing install the current URL is already
# loaded into PRELOOP_URL, so a re-run keeps it (a plain `curl | sh` upgrade
# must never silently reset a public instance back to localhost).
prompt_url() {
  if [ -n "$PRELOOP_URL" ]; then
    return
  fi
  if ! has_tty; then
    PRELOOP_URL="$DEFAULT_URL"
    return
  fi
  printf 'Public URL for this Preloop instance [%s]: ' "$DEFAULT_URL" > /dev/tty
  read -r answer < /dev/tty || answer=""
  PRELOOP_URL="${answer:-$DEFAULT_URL}"
}

prompt_tls_email() {
  if [ -n "$PRELOOP_TLS_EMAIL" ] || ! has_tty; then
    return
  fi
  printf 'Email for Let'\''s Encrypt expiry notices (optional): ' > /dev/tty
  read -r answer < /dev/tty || answer=""
  PRELOOP_TLS_EMAIL="$answer"
}

# A fresh public instance with open signup is a land-grab risk: whoever finds it
# first can register. Offer to create the operator's account now and close
# signup, so the instance is never reachable-and-open.
prompt_admin() {
  if [ -n "$PRELOOP_SKIP_ADMIN" ]; then
    return
  fi
  if [ -n "$PRELOOP_ADMIN_USERNAME" ] && [ -n "$PRELOOP_ADMIN_PASSWORD" ]; then
    # Unattended path: fail fast, BEFORE the install does any work, on an
    # email that first-user creation would reject at the very end.
    if ! valid_email "$PRELOOP_ADMIN_EMAIL"; then
      echo "PRELOOP_ADMIN_EMAIL must be a plausible address (name@domain.tld)" >&2
      echo "when PRELOOP_ADMIN_USERNAME and PRELOOP_ADMIN_PASSWORD are set:" >&2
      echo "first-user creation requires an email and would otherwise fail at" >&2
      echo "the very end of the install. Got: '${PRELOOP_ADMIN_EMAIL}'" >&2
      exit 1
    fi
    CREATE_ADMIN=1
    return
  fi
  # Only ever bootstrap a brand-new install; a re-run/upgrade already has users.
  if [ -n "$PREVIOUS_VERSION" ] || ! has_tty; then
    return
  fi

  printf '\nAnyone who can reach this instance can sign up unless you close registration.\n' > /dev/tty
  printf 'Create the first user now and disable public signups? [Y/n]: ' > /dev/tty
  read -r answer < /dev/tty || answer=""
  case "$answer" in
    n | N | no | NO)
      printf 'Leaving signups OPEN. Create the first user in the browser, then set\n' > /dev/tty
      printf 'REGISTRATION_ENABLED=false in %s/.env and re-run docker compose up -d.\n' "$INSTALL_DIR" > /dev/tty
      return
      ;;
  esac

  printf 'Admin username: ' > /dev/tty
  read -r PRELOOP_ADMIN_USERNAME < /dev/tty || PRELOOP_ADMIN_USERNAME=""
  if [ -z "$PRELOOP_ADMIN_USERNAME" ]; then
    printf 'No username given; leaving signups open.\n' > /dev/tty
    return
  fi

  # The email is REQUIRED: scripts/create_first_user.py refuses to run without
  # one, so accepting an empty/garbled value here would only surface as a
  # failure after the whole install. Loop until it is plausible, or the user
  # explicitly abandons first-user creation.
  while :; do
    printf 'Admin email (required; type "skip" to abandon first-user creation): ' > /dev/tty
    if ! read -r PRELOOP_ADMIN_EMAIL < /dev/tty; then
      PRELOOP_ADMIN_EMAIL=""
      printf '\nNo input; leaving signups open.\n' > /dev/tty
      return
    fi
    case "$PRELOOP_ADMIN_EMAIL" in
      skip | SKIP)
        PRELOOP_ADMIN_EMAIL=""
        printf 'Skipping first-user creation; signups stay OPEN. Create the first user\n' > /dev/tty
        printf 'in the browser, then set REGISTRATION_ENABLED=false in %s/.env\n' "$INSTALL_DIR" > /dev/tty
        printf 'and re-run docker compose up -d.\n' > /dev/tty
        return
        ;;
    esac
    if valid_email "$PRELOOP_ADMIN_EMAIL"; then
      break
    fi
    printf 'That does not look like an email address (expected name@domain.tld).\n' > /dev/tty
  done

  # Read the password twice, without echo where the shell supports it. A typo
  # here is unrecoverable without SMTP: there is no password-reset mail.
  while :; do
    if (stty -echo 2>/dev/null < /dev/tty); then
      printf 'Admin password (min 8 chars, hidden): ' > /dev/tty
      read -r PRELOOP_ADMIN_PASSWORD < /dev/tty || PRELOOP_ADMIN_PASSWORD=""
      printf '\nConfirm password: ' > /dev/tty
      read -r admin_password_confirm < /dev/tty || admin_password_confirm=""
      stty echo < /dev/tty 2>/dev/null || true
      printf '\n' > /dev/tty
    else
      printf 'Admin password (min 8 chars): ' > /dev/tty
      read -r PRELOOP_ADMIN_PASSWORD < /dev/tty || PRELOOP_ADMIN_PASSWORD=""
      admin_password_confirm="$PRELOOP_ADMIN_PASSWORD"
    fi

    if [ "${#PRELOOP_ADMIN_PASSWORD}" -lt 8 ]; then
      printf 'Password must be at least 8 characters.\n' > /dev/tty
      continue
    fi
    if [ "$PRELOOP_ADMIN_PASSWORD" != "$admin_password_confirm" ]; then
      printf 'Passwords did not match.\n' > /dev/tty
      continue
    fi
    break
  done

  CREATE_ADMIN=1
}

# `up -d` returns as soon as containers are created, which is earlier than the
# api container being able to run a command. Give it a bounded grace period.
wait_for_api() {
  i=0
  while [ "$i" -lt 30 ]; do
    if (
      cd "$INSTALL_DIR"
      # shellcheck disable=SC2086
      docker compose $COMPOSE_ARGS exec -T api true < /dev/null
    ) >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  return 1
}

# Create the operator's account, then close registration. In this order on
# purpose: if user creation fails we leave signup open rather than locking the
# operator out of their own instance. On failure the caller prints a loud
# banner at the very END of the install output — a warning printed here would
# scroll away behind the next-steps footer.
create_admin_user() {
  echo ""
  echo "Creating the first user ..."
  admin_status=0
  (
    cd "$INSTALL_DIR"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS exec -T \
      -e PRELOOP_ADMIN_USERNAME="$PRELOOP_ADMIN_USERNAME" \
      -e PRELOOP_ADMIN_EMAIL="$PRELOOP_ADMIN_EMAIL" \
      -e PRELOOP_ADMIN_PASSWORD="$PRELOOP_ADMIN_PASSWORD" \
      api python scripts/create_first_user.py < /dev/null
  ) || admin_status=$?

  if [ "$admin_status" -ne 0 ]; then
    echo "  WARNING: could not create the first user — details at the END of this output."
    return 1
  fi

  set_env_value REGISTRATION_ENABLED false
  (
    cd "$INSTALL_DIR"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS up -d api < /dev/null > /dev/null 2>&1
  ) || true
  echo "Public signups are DISABLED. Invite teammates from the console."
  return 0
}

# REGISTRATION_ENABLED=false means "this instance already has its first user;
# stay invite-only". That is a property of the DATABASE, not of the .env file:
# when the database was recreated (e.g. `docker compose down --volumes`, a
# teardown, a lost data volume) while .env survived, the inherited false would
# produce an instance with zero users that nobody can ever sign in to. So when
# the flag is false, verify the claim against the database and reopen signup
# if it verifiably has no users; the normal lockdown (create_admin_user, or
# the operator after their first browser signup) closes it again afterwards.
# On any doubt — api not reachable, query fails, count unreadable — the flag
# is left untouched: a flaky upgrade must never reopen a locked-down instance.
reset_registration_if_fresh_db() {
  if [ "$(env_value REGISTRATION_ENABLED "${INSTALL_DIR}/.env")" != "false" ]; then
    return 0
  fi
  # The api container starts only after migrations complete, so once it
  # answers, the user table exists and the count below is authoritative.
  wait_for_api || return 0
  user_count="$(
    cd "$INSTALL_DIR"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS exec -T postgres \
      psql -U postgres -d preloop -tAc 'SELECT count(*) FROM "user"' < /dev/null 2>/dev/null \
      | tr -d '[:space:]'
  )" || user_count=""
  if [ "$user_count" != "0" ]; then
    return 0
  fi
  echo ""
  echo "The existing configuration disables public signup, but the database is"
  echo "fresh (no users): re-enabling registration so the first user can sign up."
  set_env_value REGISTRATION_ENABLED true
  (
    cd "$INSTALL_DIR"
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS up -d api < /dev/null > /dev/null 2>&1
  ) || {
    echo "  WARNING: could not restart the API container;" >&2
    echo "  REGISTRATION_ENABLED=true may not be applied. Run manually:" >&2
    echo "  cd ${INSTALL_DIR} && docker compose ${COMPOSE_ARGS} up -d api" >&2
  }
}

# Email is how approvals, invitations and password resets reach people. Ask
# once, up front; skip silently when unattended or already configured.
prompt_smtp() {
  if [ -n "$PRELOOP_SKIP_SMTP" ] || [ -n "$SMTP_HOST" ] || ! has_tty; then
    return
  fi
  printf '\nApproval requests, invitations and password resets are sent by email.\n' > /dev/tty
  printf 'Configure SMTP now? [y/N]: ' > /dev/tty
  read -r answer < /dev/tty || answer=""
  case "$answer" in
    y | Y | yes | YES) ;;
    *)
      printf 'Skipping SMTP. Email features stay disabled until you set it up.\n' > /dev/tty
      return
      ;;
  esac

  printf 'SMTP host (e.g. smtp.gmail.com): ' > /dev/tty
  read -r SMTP_HOST < /dev/tty || SMTP_HOST=""
  if [ -z "$SMTP_HOST" ]; then
    printf 'No host given; skipping SMTP.\n' > /dev/tty
    return
  fi
  printf 'SMTP port [%s]: ' "$SMTP_PORT" > /dev/tty
  read -r answer < /dev/tty || answer=""
  SMTP_PORT="${answer:-$SMTP_PORT}"
  printf 'SMTP username: ' > /dev/tty
  read -r SMTP_USERNAME < /dev/tty || SMTP_USERNAME=""
  # Password is read without echo when the shell supports it.
  if (stty -echo 2>/dev/null < /dev/tty); then
    printf 'SMTP password (hidden): ' > /dev/tty
    read -r SMTP_PASSWORD < /dev/tty || SMTP_PASSWORD=""
    stty echo < /dev/tty 2>/dev/null || true
    printf '\n' > /dev/tty
  else
    printf 'SMTP password: ' > /dev/tty
    read -r SMTP_PASSWORD < /dev/tty || SMTP_PASSWORD=""
  fi
  default_from="${SMTP_USERNAME:-preloop@${HOST}}"
  printf 'From address [%s]: ' "$default_from" > /dev/tty
  read -r answer < /dev/tty || answer=""
  SMTP_FROM="${answer:-$default_from}"
}

url_scheme() {
  printf '%s' "$1" | sed -n 's,^\([a-zA-Z][a-zA-Z0-9+.-]*\)://.*,\1,p'
}

url_host() {
  # strip scheme, then path, then any :port and userinfo
  printf '%s' "$1" | sed -e 's,^[a-zA-Z][a-zA-Z0-9+.-]*://,,' -e 's,/.*$,,' \
    -e 's,^.*@,,' -e 's,:.*$,,'
}

# A certificate can only be issued for a public DNS name that resolves to this
# machine. Anything else (localhost, bare IPs, .local/.internal, single-label
# names) is refused rather than failing deep inside certbot.
is_public_hostname() {
  host="$1"
  case "$host" in
    localhost | localhost.* | *.local | *.localhost | *.internal | *.lan)
      return 1
      ;;
  esac
  # IPv4 literal / IPv6 literal
  if printf '%s' "$host" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'; then
    return 1
  fi
  case "$host" in
    *:*) return 1 ;;
  esac
  # must contain a dot (a registrable domain)
  case "$host" in
    *.*) return 0 ;;
    *) return 1 ;;
  esac
}

# A CAA record on any parent zone can forbid Let's Encrypt from issuing at all,
# no matter how correct the DNS and firewall are. The cloud providers' ephemeral
# hostnames do exactly this (googleusercontent.com publishes `issue "pki.goog"`),
# so a cert for one of those machine-generated names can NEVER be obtained.
# Catching it here turns ten minutes of certbot debugging into one honest line.
#
# Prints the offending zone on stdout and returns 1 when issuance is impossible;
# returns 0 when allowed OR when we cannot tell (never block on a failed lookup).

# CAA records for one domain, one per line. Uses dig when present and falls back
# to DNS-over-HTTPS, because a stock cloud image has curl but no dig — which is
# exactly the machine this check exists to protect.
caa_records() {
  domain="$1"
  if command -v dig >/dev/null 2>&1; then
    dig +short CAA "$domain" 2>/dev/null
    return 0
  fi
  # Split the JSON into one object per line, keep the CAA answers (type 257),
  # print their rdata. The question/authority sections are filtered out by the
  # type match, so a domain with no CAA yields nothing.
  curl -fsS --max-time 5 "https://dns.google/resolve?name=${domain}&type=257" 2>/dev/null \
    | tr '}' '\n' \
    | grep '"type":[[:space:]]*257' \
    | sed -n 's/.*"data":[[:space:]]*"\(.*\)".*/\1/p'
}

caa_forbids_letsencrypt() {
  host="$1"

  domain="$host"
  while [ -n "$domain" ]; do
    case "$domain" in
      *.*) ;;
      *) break ;;   # single label: stop before querying the TLD
    esac

    records="$(caa_records "$domain")"
    if [ -n "$records" ]; then
      # The closest zone with any CAA record set is authoritative for issuance;
      # parents above it are not consulted.
      if printf '%s' "$records" | grep -q 'issue[a-z]*[[:space:]]*"[^"]*letsencrypt\.org'; then
        return 0
      fi
      printf '%s' "$domain"
      return 1
    fi
    domain="${domain#*.}"
  done
  return 0
}

# Nginx locations that terminate /openai /anthropic /gemini on the gateway
# container (port 8000 on the compose network). Used by both the HTTP bootstrap
# config and the HTTPS config so streaming traffic never hairpins through the
# console. proxy_buffering must stay off (SSE).
gateway_proxy_locations() {
  cat <<'GATE'
    location ^~ /openai/ {
        proxy_pass http://gateway:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_send_timeout 3600s;
        proxy_read_timeout 3600s;
        proxy_buffering off;
        proxy_cache off;
        client_max_body_size 32m;
    }

    location ^~ /anthropic/ {
        proxy_pass http://gateway:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_send_timeout 3600s;
        proxy_read_timeout 3600s;
        proxy_buffering off;
        proxy_cache off;
        client_max_body_size 32m;
    }

    location ^~ /gemini/ {
        proxy_pass http://gateway:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 10s;
        proxy_send_timeout 3600s;
        proxy_read_timeout 3600s;
        proxy_buffering off;
        proxy_cache off;
        client_max_body_size 32m;
    }
GATE
}

write_tls_assets() {
  host="$1"
  mkdir -p "${INSTALL_DIR}/tls" "${INSTALL_DIR}/certbot/www" "${INSTALL_DIR}/certbot/conf"

  # Phase 1: HTTP only. Serves the ACME challenge and proxies everything else,
  # so the instance is usable while the certificate is being issued.
  cat > "${INSTALL_DIR}/tls/http.conf" <<EOF
server {
    listen 80;
    server_name ${host};

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

$(gateway_proxy_locations)

    location / {
        proxy_pass http://console:80;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
EOF

  # Phase 2: HTTPS, with HTTP kept for ACME renewal + redirect.
  cat > "${INSTALL_DIR}/tls/https.conf" <<EOF
server {
    listen 80;
    server_name ${host};

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    http2 on;
    server_name ${host};

    ssl_certificate     /etc/letsencrypt/live/${host}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${host}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;

    # Streaming (SSE) and WebSocket traffic must not be buffered or cut short.
    # Gateway routes are listed first so /openai /anthropic /gemini skip the
    # console hop (measured extra TTFB on the hairpin path).
$(gateway_proxy_locations)

    location / {
        proxy_pass http://console:80;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
EOF

  cp "${INSTALL_DIR}/tls/http.conf" "${INSTALL_DIR}/tls/active.conf"

  # Compose overlay: a TLS-terminating proxy in front of the console, plus a
  # certbot sidecar that renews every 12h and reloads nginx.
  cat > "${INSTALL_DIR}/docker-compose.tls.yaml" <<'EOF'
services:
  proxy:
    image: nginx:1.27-alpine
    restart: unless-stopped
    depends_on:
      - console
      - gateway
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./tls/active.conf:/etc/nginx/conf.d/default.conf:ro
      - ./certbot/conf:/etc/letsencrypt:ro
      - ./certbot/www:/var/www/certbot:ro
    command: >-
      sh -c "while :; do sleep 6h & wait $${!}; nginx -s reload; done & nginx -g 'daemon off;'"

  certbot:
    image: certbot/certbot:latest
    restart: unless-stopped
    volumes:
      - ./certbot/conf:/etc/letsencrypt
      - ./certbot/www:/var/www/certbot
    entrypoint: >-
      sh -c "trap exit TERM; while :; do certbot renew --webroot -w /var/www/certbot --quiet; sleep 12h & wait $${!}; done"

  # Public traffic reaches the console through the TLS proxy; keep 3000 for
  # local debugging only.
  console:
    ports: !override
      - "127.0.0.1:3000:80"
EOF

  # Remove a docker-compose.override.yaml written by older installers: compose
  # loads that file IMPLICITLY, so it would keep applying (binding the console
  # to loopback) even on a run that does not use the TLS overlay.
  rm -f "${INSTALL_DIR}/docker-compose.override.yaml"
}

# Switch the proxy to the HTTPS server block and (re)load it. Must run on
# EVERY install where a usable certificate exists — freshly issued or reused —
# because write_tls_assets always resets tls/active.conf to the HTTP-only
# bootstrap config; skipping this on the reuse path leaves a certified
# instance serving plain HTTP.
activate_https() {
  host="$1"
  cp "${INSTALL_DIR}/tls/https.conf" "${INSTALL_DIR}/tls/active.conf"
  (
    cd "$INSTALL_DIR"
    docker compose -f docker-compose.yaml -f docker-compose.tls.yaml up -d proxy certbot < /dev/null >> "$LOG_FILE" 2>&1
    docker compose -f docker-compose.yaml -f docker-compose.tls.yaml exec -T proxy nginx -s reload < /dev/null 2>/dev/null || true
  )
  echo "TLS is active for https://${host}"
}

issue_certificate() {
  host="$1"
  if [ -f "${INSTALL_DIR}/certbot/conf/live/${host}/fullchain.pem" ]; then
    echo "TLS certificate for ${host} already present; skipping issuance."
    activate_https "$host"
    return 0
  fi

  echo "Requesting a Let's Encrypt certificate for ${host} ..."
  echo "  (${host} must resolve to this machine and ports 80/443 must be free)"

  staging_arg=""
  if [ -n "$PRELOOP_TLS_STAGING" ]; then
    staging_arg="--staging"
  fi
  if [ -n "$PRELOOP_TLS_EMAIL" ]; then
    email_arg="--email ${PRELOOP_TLS_EMAIL}"
  else
    email_arg="--register-unsafely-without-email"
  fi

  certbot_status=0
  (
    cd "$INSTALL_DIR"
    # MSYS_NO_PATHCONV stops Git Bash / MSYS2 on Windows from rewriting the
    # container-side webroot into a host path ("C:/Program Files/Git/var/www/
    # certbot"). It is an ordinary unused variable on macOS and Linux.
    export MSYS_NO_PATHCONV=1
    # shellcheck disable=SC2086
    docker compose -f docker-compose.yaml -f docker-compose.tls.yaml run --rm \
      --entrypoint certbot certbot certonly \
      --webroot -w /var/www/certbot \
      -d "$host" $email_arg $staging_arg \
      --agree-tos --no-eff-email --non-interactive < /dev/null
  ) || certbot_status=$?

  if [ "$certbot_status" -ne 0 ]; then
    echo ""
    echo "Certificate issuance FAILED. Preloop is still running over HTTP on port 80."
    echo "Common causes:"
    echo "  - ${host} does not resolve to this machine's public IP"
    echo "  - port 80 is blocked by a firewall or already in use"
    echo "  - a CAA record on the domain forbids Let's Encrypt from issuing"
    echo "    (check: dig CAA ${host}; cloud-provider hostnames such as"
    echo "     *.googleusercontent.com can never be certified — use your own domain)"
    echo "  - Let's Encrypt rate limits (retry with PRELOOP_TLS_STAGING=1)"
    echo "Fix the cause and re-run:"
    echo "  cd ${INSTALL_DIR} && MSYS_NO_PATHCONV=1 docker compose -f docker-compose.yaml -f docker-compose.tls.yaml run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot -d ${host} --agree-tos"
    return 1
  fi

  activate_https "$host"
  return 0
}

# Carry REGISTRATION_ENABLED into the API. Kept in a script-owned overlay rather
# than relying on the released compose file, so signup lockdown works even on
# releases whose compose file predates the setting.
write_auth_overlay() {
  cat > "${INSTALL_DIR}/docker-compose.auth.yaml" <<'EOF'
services:
  api:
    environment:
      REGISTRATION_ENABLED: ${REGISTRATION_ENABLED:-true}
      PRELOOP_BOOTSTRAP_TOKEN: ${PRELOOP_BOOTSTRAP_TOKEN:-}
EOF
}

main() {
  require_command curl
  docker_preflight

  VERSION="$(resolve_version)"
  TAG="v${VERSION}"
  COMPOSE_URL="https://github.com/${PRELOOP_REPO}/releases/download/${TAG}/docker-compose.release.yaml"

  mkdir -p "$INSTALL_DIR"
  # If the log file cannot be written (exotic permissions), fall back to
  # /dev/null rather than failing the install over logging.
  { echo "== Preloop install $(date) ==" >> "$LOG_FILE"; } 2>/dev/null \
    || LOG_FILE=/dev/null
  load_existing_env

  if [ -n "$PREVIOUS_VERSION" ] && [ "$PREVIOUS_VERSION" != "$VERSION" ]; then
    echo "Upgrading Preloop ${PREVIOUS_VERSION} -> ${VERSION} in ${INSTALL_DIR}"
  elif [ -n "$PREVIOUS_VERSION" ]; then
    echo "Re-applying Preloop ${VERSION} in ${INSTALL_DIR}"
  fi

  curl -fsSL "$COMPOSE_URL" -o "${INSTALL_DIR}/docker-compose.yaml"

  prompt_url

  SCHEME="$(url_scheme "$PRELOOP_URL")"
  HOST="$(url_host "$PRELOOP_URL")"
  if [ -z "$SCHEME" ] || [ -z "$HOST" ]; then
    echo "PRELOOP_URL must be an absolute URL, e.g. https://preloop.example.com" >&2
    exit 1
  fi

  # An instance that was installed WITH TLS keeps TLS on every later run, so an
  # upgrade cannot leave the proxy/certbot containers orphaned (still holding
  # ports 80/443) while compose manages only the plain stack.
  WANT_TLS=0
  # Serve on port 80 through the bundled proxy even when there is no certificate,
  # so the instance is still reachable at the URL the user typed.
  HTTP_PROXY_ONLY=0
  if [ "$SCHEME" = "https" ] && [ -z "$PRELOOP_SKIP_TLS" ]; then
    if ! is_public_hostname "$HOST"; then
      echo "PRELOOP_URL is https but '${HOST}' is not a public DNS name;" >&2
      echo "skipping certificate issuance (bring your own TLS)." >&2
    elif caa_zone="$(caa_forbids_letsencrypt "$HOST")"; then
      WANT_TLS=1
    else
      # Not a transient failure: no CA that Let's Encrypt controls can ever sign
      # for this name, so retrying or fixing DNS/firewall will not help.
      echo ""
      echo "Cannot obtain a TLS certificate for ${HOST}."
      echo "The DNS zone '${caa_zone}' publishes a CAA record that forbids Let's"
      echo "Encrypt from issuing certificates for it. This is permanent — it is not"
      echo "a DNS, firewall or rate-limit problem, and re-running will not fix it."
      echo ""
      echo "Cloud providers set this on their machine-generated hostnames"
      echo "(*.googleusercontent.com, *.compute.amazonaws.com, ...). To serve"
      echo "Preloop over HTTPS, point a domain you control at this machine and"
      echo "re-run with PRELOOP_URL=https://your-domain."
      echo ""
      echo "Continuing over plain HTTP at http://${HOST} — usable for evaluation,"
      echo "but do not put real credentials or agent traffic through it."
      echo ""
      PRELOOP_URL="http://${HOST}"
      SCHEME="http"
      HTTP_PROXY_ONLY=1
    fi
  fi

  prompt_smtp
  prompt_admin

  # First-user setup token: generate one unless the caller provided it via
  # the environment or a previous run already stored it in .env. Printed
  # ONLY in the terminal footer (as part of the setup link), never into
  # ${LOG_FILE}.
  if [ -z "$PRELOOP_BOOTSTRAP_TOKEN" ]; then
    PRELOOP_BOOTSTRAP_TOKEN="$(generate_bootstrap_token)"
  fi

  if [ ! -f "${INSTALL_DIR}/.env" ]; then
    cat > "${INSTALL_DIR}/.env" <<EOF
PRELOOP_VERSION=${VERSION}
SECRET_KEY=$(generate_secret)
POSTGRES_PASSWORD=$(generate_secret)
PRELOOP_URL=${PRELOOP_URL}
ALLOWED_ORIGINS=${PRELOOP_URL}
# Public signup. Turned off after the first user is created; set to true to
# reopen registration, then run 'docker compose up -d api'.
REGISTRATION_ENABLED=true
# First-user setup token: while the instance has zero users, signing up
# requires the setup link printed at the end of the install (which carries
# this token). Ignored once any user exists.
PRELOOP_BOOTSTRAP_TOKEN=${PRELOOP_BOOTSTRAP_TOKEN}
# Email (approval requests, invitations, password resets). Leave SMTP_HOST
# empty to run without email; re-run the installer or edit these values, then
# run 'docker compose up -d' to apply.
SMTP_HOST=${SMTP_HOST}
SMTP_PORT=${SMTP_PORT}
SMTP_USERNAME=${SMTP_USERNAME}
SMTP_PASSWORD=${SMTP_PASSWORD}
SMTP_FROM=${SMTP_FROM}
SMTP_FROM_NAME=${SMTP_FROM_NAME}
EOF
  else
    # Keep an existing install's secrets, but refresh the public URL.
    tmp_env="${INSTALL_DIR}/.env.tmp"
    grep -v -e '^PRELOOP_VERSION=' -e '^PRELOOP_URL=' -e '^ALLOWED_ORIGINS=' \
      "${INSTALL_DIR}/.env" > "$tmp_env" || true
    {
      echo "PRELOOP_VERSION=${VERSION}"
      echo "PRELOOP_URL=${PRELOOP_URL}"
      echo "ALLOWED_ORIGINS=${PRELOOP_URL}"
    } >> "$tmp_env"
    # Only rewrite SMTP when this run supplied it, so an existing configuration
    # is never silently wiped.
    if [ -n "$SMTP_HOST" ]; then
      grep -v -e '^SMTP_' "$tmp_env" > "${tmp_env}.2" || true
      mv "${tmp_env}.2" "$tmp_env"
      {
        echo "SMTP_HOST=${SMTP_HOST}"
        echo "SMTP_PORT=${SMTP_PORT}"
        echo "SMTP_USERNAME=${SMTP_USERNAME}"
        echo "SMTP_PASSWORD=${SMTP_PASSWORD}"
        echo "SMTP_FROM=${SMTP_FROM}"
        echo "SMTP_FROM_NAME=${SMTP_FROM_NAME}"
      } >> "$tmp_env"
    fi
    mv "$tmp_env" "${INSTALL_DIR}/.env"
    # Persist the setup token (idempotent: keeps the loaded/provided value).
    set_env_value PRELOOP_BOOTSTRAP_TOKEN "$PRELOOP_BOOTSTRAP_TOKEN"
  fi

  write_auth_overlay
  COMPOSE_ARGS="-f docker-compose.yaml -f docker-compose.auth.yaml"
  if [ "$WANT_TLS" -eq 1 ]; then
    prompt_tls_email
    write_tls_assets "$HOST"
    COMPOSE_ARGS="-f docker-compose.yaml -f docker-compose.auth.yaml -f docker-compose.tls.yaml"
  elif [ "$HTTP_PROXY_ONLY" -eq 1 ]; then
    # Same proxy, HTTP server block only: the console answers on port 80 at the
    # hostname the user gave, we just never ask for a certificate.
    write_tls_assets "$HOST"
    COMPOSE_ARGS="-f docker-compose.yaml -f docker-compose.auth.yaml -f docker-compose.tls.yaml"
  elif [ -n "$PRELOOP_TLS_ENABLED" ] && [ -f "${INSTALL_DIR}/docker-compose.tls.yaml" ]; then
    # TLS was configured previously but this run did not ask for it (e.g. an
    # upgrade run without PRELOOP_URL). Keep managing the proxy/certbot services
    # rather than orphaning them.
    echo "Keeping the existing TLS proxy (set PRELOOP_SKIP_TLS=1 to stop using it)."
    COMPOSE_ARGS="-f docker-compose.yaml -f docker-compose.auth.yaml -f docker-compose.tls.yaml"
  fi

  # Record what this install is, so the next run reproduces the same topology.
  if [ "$WANT_TLS" -eq 1 ] || [ "$HTTP_PROXY_ONLY" -eq 1 ] || [ -n "$PRELOOP_TLS_ENABLED" ]; then
    grep -v '^PRELOOP_TLS_ENABLED=' "${INSTALL_DIR}/.env" > "${INSTALL_DIR}/.env.tls" || true
    echo "PRELOOP_TLS_ENABLED=1" >> "${INSTALL_DIR}/.env.tls"
    mv "${INSTALL_DIR}/.env.tls" "${INSTALL_DIR}/.env"
  fi

  # Back up the database before an upgrade touches the schema. Migrations run
  # automatically (the `migrate` service runs `alembic upgrade head`), and some
  # are irreversible, so a pre-upgrade dump is the cheap safety net.
  if [ -n "$PREVIOUS_VERSION" ] && [ "$PREVIOUS_VERSION" != "$VERSION" ]; then
    backup_dir="${INSTALL_DIR}/backups"
    mkdir -p "$backup_dir"
    backup_file="${backup_dir}/preloop-${PREVIOUS_VERSION}-$(date +%Y%m%d%H%M%S).sql"
    echo "Backing up the database before upgrading ..."
    if (
      cd "$INSTALL_DIR"
      # shellcheck disable=SC2086
      docker compose $COMPOSE_ARGS exec -T postgres \
        pg_dump -U postgres preloop < /dev/null
    ) > "$backup_file" 2>/dev/null; then
      echo "  saved ${backup_file}"
    else
      rm -f "$backup_file"
      echo "  WARNING: backup failed (is the old stack running?). Continuing;"
      echo "  stop now with Ctrl-C if you want to back up manually first."
    fi
  fi

  # Docker's layer-by-layer progress output is thousands of lines that drown
  # the few that matter; keep it in the log file and print curated status
  # lines instead.
  echo ""
  echo "Pulling Preloop ${VERSION} images (a first install downloads ~2 GB) ..."
  echo "  full docker output: ${LOG_FILE}"
  compose_status=0
  (
    cd "$INSTALL_DIR"
    # Pull first so an upgrade fails BEFORE tearing down the running stack, and
    # --remove-orphans drops containers for services a new version deleted.
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS pull --quiet < /dev/null >> "$LOG_FILE" 2>&1 || true
    echo "Starting containers ..."
    # shellcheck disable=SC2086
    docker compose $COMPOSE_ARGS up -d --remove-orphans --quiet-pull < /dev/null >> "$LOG_FILE" 2>&1
  ) || compose_status=$?

  tls_status=0
  if [ "$WANT_TLS" -eq 1 ] && [ "$compose_status" -eq 0 ]; then
    issue_certificate "$HOST" || tls_status=$?
  fi

  # Before any first-user handling: an inherited REGISTRATION_ENABLED=false over
  # a fresh database would lock everyone out (see reset_registration_if_fresh_db).
  if [ "$compose_status" -eq 0 ]; then
    reset_registration_if_fresh_db
  fi

  admin_created=0
  admin_failed=0
  if [ "$CREATE_ADMIN" -eq 1 ] && [ "$compose_status" -eq 0 ]; then
    if wait_for_api && create_admin_user; then
      admin_created=1
    else
      admin_failed=1
    fi
  fi

  echo ""
  echo "Preloop OSS ${VERSION} is starting in ${INSTALL_DIR}"
  if [ "$WANT_TLS" -eq 1 ] && [ "$tls_status" -eq 0 ]; then
    echo "Console + API: ${PRELOOP_URL}"
  else
    echo "Console: ${PRELOOP_URL}"
    echo "API (direct): http://localhost:8000"
  fi
  echo ""
  echo "Next steps:"
  if [ "$admin_created" -eq 1 ]; then
    echo "  1. Open ${PRELOOP_URL} and sign in as '${PRELOOP_ADMIN_USERNAME}'"
  elif [ -n "$PRELOOP_BOOTSTRAP_TOKEN" ]; then
    # The token travels in the URL FRAGMENT (never sent to servers or logged
    # in access logs) and this footer goes to the terminal only, not to
    # ${LOG_FILE}.
    echo "  1. Create the first user (it becomes the admin account) with this"
    echo "     setup link:"
    echo "       ${PRELOOP_URL}/register#bootstrap=${PRELOOP_BOOTSTRAP_TOKEN}"
  else
    echo "  1. Open ${PRELOOP_URL} and create the first user"
  fi
  echo "  2. Install the CLI (if you haven't):"
  echo "       curl -fsSL https://preloop.ai/install/cli | sh"
  echo "  3. Connect the CLI to THIS instance (not preloop.ai):"
  echo "       preloop login --url ${PRELOOP_URL}"
  echo "  4. Onboard your local agents:"
  echo "       preloop agents discover"
  echo ""
  if [ -n "$SMTP_HOST" ]; then
    echo "Email: sending via ${SMTP_HOST}:${SMTP_PORT} as ${SMTP_FROM}"
  else
    echo "Email: NOT configured — approval emails, invitations and password"
    echo "  resets will not be delivered. To enable, set SMTP_HOST/SMTP_PORT/"
    echo "  SMTP_USERNAME/SMTP_PASSWORD/SMTP_FROM in ${INSTALL_DIR}/.env"
  fi
  echo "  (change any setting: edit ${INSTALL_DIR}/.env, then re-run 'docker compose up -d')"
  echo ""
  echo "Full install log: ${LOG_FILE}"
  echo "To stop it later:"
  echo "  cd ${INSTALL_DIR} && docker compose ${COMPOSE_ARGS} down"

  if [ "$compose_status" -ne 0 ]; then
    echo ""
    echo "Docker Compose FAILED. Last lines of ${LOG_FILE}:"
    tail -n 15 "$LOG_FILE" 2>/dev/null || true
    echo ""
    echo "Inspect further with:"
    echo "  cd ${INSTALL_DIR} && docker compose ${COMPOSE_ARGS} logs"
    exit "$compose_status"
  fi

  # Deliberately the LAST thing printed: this failure previously hid above the
  # footer (and behind thousands of docker lines) while the instance silently
  # kept public signups open and the admin the user typed a password for did
  # not exist.
  if [ "$admin_failed" -eq 1 ]; then
    echo ""
    echo "!!! =================================================================="
    echo "!!!  FIRST USER WAS NOT CREATED"
    echo "!!! =================================================================="
    echo "!!!"
    echo "!!! The install itself succeeded and Preloop is running at:"
    echo "!!!   ${PRELOOP_URL}"
    echo "!!! but the admin account '${PRELOOP_ADMIN_USERNAME}' does NOT exist and"
    echo "!!! public signups are still ENABLED: anyone who can reach the instance"
    echo "!!! can register right now."
    echo "!!!"
    echo "!!! Fix it with either:"
    if [ -n "$PRELOOP_BOOTSTRAP_TOKEN" ]; then
      echo "!!!  a) Sign up in the browser with this setup link (that becomes"
      echo "!!!     the first account):"
      echo "!!!       ${PRELOOP_URL}/register#bootstrap=${PRELOOP_BOOTSTRAP_TOKEN}"
      echo "!!!     then set REGISTRATION_ENABLED=false in"
    else
      echo "!!!  a) Open ${PRELOOP_URL} and sign up in the browser (that becomes the"
      echo "!!!     first account), then set REGISTRATION_ENABLED=false in"
    fi
    echo "!!!     ${INSTALL_DIR}/.env and run:"
    echo "!!!       cd ${INSTALL_DIR} && docker compose ${COMPOSE_ARGS} up -d api"
    echo "!!!  b) Retry from the terminal (use the password you chose):"
    echo "!!!       cd ${INSTALL_DIR} && docker compose ${COMPOSE_ARGS} exec -T \\"
    echo "!!!         -e PRELOOP_ADMIN_USERNAME='${PRELOOP_ADMIN_USERNAME}' \\"
    echo "!!!         -e PRELOOP_ADMIN_EMAIL='${PRELOOP_ADMIN_EMAIL}' \\"
    echo "!!!         -e PRELOOP_ADMIN_PASSWORD='<your password>' \\"
    echo "!!!         api python scripts/create_first_user.py"
    echo "!!!     then close signups as in (a)."
    echo "!!! =================================================================="
    exit 1
  fi

  if [ "$tls_status" -ne 0 ]; then
    exit "$tls_status"
  fi
}

main "$@"
