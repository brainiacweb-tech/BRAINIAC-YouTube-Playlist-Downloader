#!/bin/bash
set -e

# Debug: check if bgutil-yt-dlp-pot-provider is in PATH and print version
echo "[startup] Checking for bgutil-yt-dlp-pot-provider in PATH..."
which bgutil-yt-dlp-pot-provider || { echo '[startup] ERROR: bgutil-yt-dlp-pot-provider not found in PATH!'; exit 1; }
echo "[startup] bgutil-yt-dlp-pot-provider found: $(which bgutil-yt-dlp-pot-provider)"
bgutil-yt-dlp-pot-provider --version || echo '[startup] WARNING: Could not get bgutil version.'
echo "[startup] Launching bgutil-yt-dlp-pot-provider on port 4416..."
bgutil-yt-dlp-pot-provider &
BGUTIL_PID=$!
echo "[startup] bgutil PO token provider started (PID $BGUTIL_PID)"

# Give it 2 seconds to initialize before gunicorn starts
sleep 2

# Start gunicorn
exec gunicorn web_app:app \
  --workers=1 \
  --threads=8 \
  --timeout=600 \
  --worker-class=gthread \
  --keep-alive=5 \
  --bind 0.0.0.0:$PORT
