#!/usr/bin/env bash
# Install dependencies, start the server, and open it in the browser.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8777}"
URL="http://127.0.0.1:${PORT}"

PYTHON="$(command -v python3 || command -v python)"
if [ -z "$PYTHON" ]; then
    echo "Python was not found on PATH." >&2
    exit 1
fi

echo "Installing/checking dependencies..."
"$PYTHON" -m pip install -q -r requirements.txt

echo "Starting Broadlink SmartIR Studio on ${URL} ..."
PORT="$PORT" "$PYTHON" server.py &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

echo "Waiting for the server to come up..."
for _ in $(seq 1 30); do
    if curl -s -o /dev/null "${URL}/api/platforms"; then
        break
    fi
    sleep 1
done

if command -v open >/dev/null; then
    open "$URL"                 # macOS
elif command -v xdg-open >/dev/null; then
    xdg-open "$URL"             # Linux
else
    echo "Open ${URL} in your browser."
fi

echo
echo "Server is running (PID $SERVER_PID). Press Ctrl+C to stop it."
wait "$SERVER_PID"
