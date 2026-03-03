#!/bin/bash
set -e

# Start bgutil YouTube PO token provider server in background (port 4416)
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
