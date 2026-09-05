#!/usr/bin/env bash
# Start PostgreSQL and NATS on a native (no Docker) host. Idempotent.
set -euo pipefail

if command -v pg_ctlcluster >/dev/null 2>&1; then
  if ! pg_isready -q -h 127.0.0.1 -p 5432; then
    if [ "$(id -u)" -eq 0 ]; then
      pg_ctlcluster 16 main start
    else
      sudo pg_ctlcluster 16 main start
    fi
  fi
else
  echo "pg_ctlcluster not found; start PostgreSQL 16 yourself on port 5432." >&2
fi

if ! command -v nats-server >/dev/null 2>&1; then
  echo "nats-server not found; install NATS Server and re-run." >&2
  exit 1
fi

if ! pgrep -x nats-server >/dev/null 2>&1; then
  nats-server -js -m 8222 -l /tmp/nats-server.log &
  echo "Started nats-server (JetStream, client 4222, monitor 8222)."
fi
