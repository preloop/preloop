#!/usr/bin/env sh

set -eu

PRELOOP_REPO="${PRELOOP_REPO:-preloop/preloop}"
PRELOOP_DEFAULT_VERSION="${PRELOOP_DEFAULT_VERSION:-}"
PRELOOP_VERSION="${PRELOOP_VERSION:-$PRELOOP_DEFAULT_VERSION}"
INSTALL_DIR="${INSTALL_DIR:-${HOME}/.preloop-oss}"

# Public base URL this instance is reached at. Everything (console, API, MCP,
# gateway) is served through one origin: the console container reverse-proxies
# /api, /mcp, /openai, /anthropic and /gemini to the backend services.
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

DEFAULT_URL="http://localhost:3000"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
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
has_tty() {
  { : < /dev/tty; } 2>/dev/null
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

issue_certificate() {
  host="$1"
  if [ -f "${INSTALL_DIR}/certbot/conf/live/${host}/fullchain.pem" ]; then
    echo "TLS certificate for ${host} already present; skipping issuance."
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
    # shellcheck disable=SC2086
    docker compose -f docker-compose.yaml -f docker-compose.tls.yaml run --rm \
      --entrypoint certbot certbot certonly \
      --webroot -w /var/www/certbot \
      -d "$host" $email_arg $staging_arg \
      --agree-tos --no-eff-email --non-interactive
  ) || certbot_status=$?

  if [ "$certbot_status" -ne 0 ]; then
    echo ""
    echo "Certificate issuance FAILED. Preloop is still running over HTTP on port 80."
    echo "Common causes:"
    echo "  - ${host} does not resolve to this machine's public IP"
    echo "  - port 80 is blocked by a firewall or already in use"
    echo "  - Let's Encrypt rate limits (retry with PRELOOP_TLS_STAGING=1)"
    echo "Fix the cause and re-run:"
    echo "  cd ${INSTALL_DIR} && docker compose -f docker-compose.yaml -f docker-compose.tls.yaml run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot -d ${host} --agree-tos"
    return 1
  fi

  # Swap the proxy to the TLS server block.
  cp "${INSTALL_DIR}/tls/https.conf" "${INSTALL_DIR}/tls/active.conf"
  (
    cd "$INSTALL_DIR"
    docker compose -f docker-compose.yaml -f docker-compose.tls.yaml up -d proxy certbot
    docker compose -f docker-compose.yaml -f docker-compose.tls.yaml exec -T proxy nginx -s reload 2>/dev/null || true
  )
  echo "TLS is active for https://${host}"
  return 0
}

require_command curl
require_command docker

VERSION="$(resolve_version)"
TAG="v${VERSION}"
COMPOSE_URL="https://github.com/${PRELOOP_REPO}/releases/download/${TAG}/docker-compose.release.yaml"

mkdir -p "$INSTALL_DIR"
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
if [ "$SCHEME" = "https" ] && [ -z "$PRELOOP_SKIP_TLS" ]; then
  if is_public_hostname "$HOST"; then
    WANT_TLS=1
  else
    echo "PRELOOP_URL is https but '${HOST}' is not a public DNS name;" >&2
    echo "skipping certificate issuance (bring your own TLS)." >&2
  fi
fi

prompt_smtp

if [ ! -f "${INSTALL_DIR}/.env" ]; then
  cat > "${INSTALL_DIR}/.env" <<EOF
PRELOOP_VERSION=${VERSION}
SECRET_KEY=$(generate_secret)
POSTGRES_PASSWORD=$(generate_secret)
PRELOOP_URL=${PRELOOP_URL}
ALLOWED_ORIGINS=${PRELOOP_URL}
# Email (approval requests, invitations, password resets). Leave SMTP_HOST
# empty to run without email; re-run the installer or edit these values, then
# `docker compose up -d` to apply.
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
fi

COMPOSE_ARGS="-f docker-compose.yaml"
if [ "$WANT_TLS" -eq 1 ]; then
  prompt_tls_email
  write_tls_assets "$HOST"
  COMPOSE_ARGS="-f docker-compose.yaml -f docker-compose.tls.yaml"
elif [ -n "$PRELOOP_TLS_ENABLED" ] && [ -f "${INSTALL_DIR}/docker-compose.tls.yaml" ]; then
  # TLS was configured previously but this run did not ask for it (e.g. an
  # upgrade run without PRELOOP_URL). Keep managing the proxy/certbot services
  # rather than orphaning them.
  echo "Keeping the existing TLS proxy (set PRELOOP_SKIP_TLS=1 to stop using it)."
  COMPOSE_ARGS="-f docker-compose.yaml -f docker-compose.tls.yaml"
fi

# Record what this install is, so the next run reproduces the same topology.
if [ "$WANT_TLS" -eq 1 ] || [ "$COMPOSE_ARGS" != "-f docker-compose.yaml" ]; then
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
      pg_dump -U postgres preloop
  ) > "$backup_file" 2>/dev/null; then
    echo "  saved ${backup_file}"
  else
    rm -f "$backup_file"
    echo "  WARNING: backup failed (is the old stack running?). Continuing;"
    echo "  stop now with Ctrl-C if you want to back up manually first."
  fi
fi

compose_status=0
(
  cd "$INSTALL_DIR"
  # Pull first so an upgrade fails BEFORE tearing down the running stack, and
  # --remove-orphans drops containers for services a new version deleted.
  # shellcheck disable=SC2086
  docker compose $COMPOSE_ARGS pull --quiet 2>/dev/null || true
  # shellcheck disable=SC2086
  docker compose $COMPOSE_ARGS up -d --remove-orphans
) || compose_status=$?

tls_status=0
if [ "$WANT_TLS" -eq 1 ] && [ "$compose_status" -eq 0 ]; then
  issue_certificate "$HOST" || tls_status=$?
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
echo "  1. Open ${PRELOOP_URL} and create the first user"
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
echo "To stop it later:"
echo "  cd ${INSTALL_DIR} && docker compose ${COMPOSE_ARGS} down"

if [ "$compose_status" -ne 0 ]; then
  echo ""
  echo "Docker Compose failed. Inspect logs with:"
  echo "  cd ${INSTALL_DIR} && docker compose ${COMPOSE_ARGS} logs"
  exit "$compose_status"
fi

if [ "$tls_status" -ne 0 ]; then
  exit "$tls_status"
fi
