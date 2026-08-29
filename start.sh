#!/bin/bash
# Start Preloop REST API server

set -e

activate_virtualenv_if_present() {
  if [ -n "$VIRTUAL_ENV" ]; then
    return
  fi

  local script_dir
  script_dir="$(dirname "$0")"
  local candidates=(
    "$script_dir/.venv/bin/activate"
    "$script_dir/../.venv/bin/activate"
  )

  for candidate in "${candidates[@]}"; do
    if [ -f "$candidate" ]; then
      echo "Activating virtual environment..."
      # shellcheck disable=SC1090
      source "$candidate"
      return
    fi
  done

  echo "No virtual environment found, using system Python."
}

# Load .env file if it exists and variables are not already set
ENV_FILE=".env"
if [ -f "$ENV_FILE" ]; then
  echo "Loading environment variables from $ENV_FILE"
  # Read line by line, ignore comments and empty lines
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Remove comments and leading/trailing whitespace
    line=$(echo "$line" | sed 's/#.*//' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    # Skip empty lines
    if [ -z "$line" ]; then
      continue
    fi
    # Remove potential 'export ' prefix
    line=$(echo "$line" | sed 's/^export //')
    # Split KEY=VALUE, handle values with '='
    KEY=$(echo "$line" | cut -d '=' -f 1)
    VALUE=$(echo "$line" | cut -d '=' -f 2-)
    # Check if variable is already set in the environment
    # Using indirect expansion: ${!KEY}
    # The condition [ -z "${!KEY}" ] checks if the variable named by KEY is unset or empty.
    if [ -z "${!KEY}" ]; then
      # Export if not set
      export "$KEY=$VALUE"
      # echo "Exported $KEY from $ENV_FILE" # Optional: uncomment for debugging
    # else
      # echo "Skipping $KEY, already set in environment" # Optional: uncomment for debugging
    fi
  done < "$ENV_FILE"
else
  echo "Warning: $ENV_FILE not found. Skipping."
fi
echo "" # Add a blank line for separation

activate_virtualenv_if_present

wait_for_database() {
  echo "Waiting for the database to accept connections..."
  python - <<'PY'
import os
import socket
import sys
import time
from urllib.parse import urlparse

raw = os.environ.get("DATABASE_URL", "")
if not raw:
    sys.exit(0)

parsed = urlparse(raw)
host = parsed.hostname or "localhost"
port = parsed.port or 5432
deadline = time.time() + 60
last_error = None
while time.time() < deadline:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        connect_error = None
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(3)
                sock.connect(sockaddr)
                sock.close()
                sys.exit(0)
            except OSError as exc:
                connect_error = exc
            finally:
                sock.close()
        last_error = connect_error
    except OSError as exc:
        last_error = exc
    time.sleep(1)

print(
    f"Database not reachable at {host}:{port}: {last_error}",
    file=sys.stderr,
)
sys.exit(1)
PY
}

wait_for_database

# Initialize database tables, embedding model, and AI model using Alembic
echo "Initializing database schema, embedding model, and AI model..."
python scripts/init_db.py --force
# Default parameters
API_PORT=8000
DEBUG="true"
INIT_TEST_DATA="false"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --port)
      API_PORT="$2"
      shift 2
      ;;
    --debug)
      DEBUG="true"
      shift
      ;;
    --init-test-data)
      INIT_TEST_DATA="true"
      shift
      ;;
    --help)
      echo "Preloop Startup Script"
      echo ""
      echo "Usage: $0 [options]"
      echo ""
      echo "Options:"
      echo "  --port PORT          Port for the REST API server (default: 8000)"
      echo "  --debug              Enable debug mode"
      echo "  --init-test-data     Initialize test data on startup"
      echo "  --help               Show this help message"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Set environment variables
export DEBUG="$DEBUG"
export INIT_TEST_DATA="$INIT_TEST_DATA"
export PORT="$API_PORT"

# Function to stop the process when the script exits
function cleanup {
  echo "Stopping service..."
  if [ -n "$API_PID" ]; then
    kill $API_PID 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Debug and test data flags
debug_flag=""
test_data_flag=""
if [ "$DEBUG" = "true" ]; then
  debug_flag="--debug"
fi
if [ "$INIT_TEST_DATA" = "true" ]; then
  test_data_flag="--init-test-data"
fi


echo ""
echo "Starting Preloop REST API with configuration:"
echo " - API Server: http://localhost:$API_PORT"
echo " - API Documentation (Swagger): http://localhost:$API_PORT/docs/api"
echo " - User Documentation: http://localhost:$API_PORT/docs"
echo " - Debug mode: $DEBUG"
echo " - Init test data: $INIT_TEST_DATA"

activate_virtualenv_if_present

# Start the API server in the foreground
python -m preloop.server --port "$API_PORT" $debug_flag $test_data_flag
