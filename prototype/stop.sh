#!/usr/bin/env bash
# Stop the endpoint process. The reverse tunnel keeps running (it forwards to the port and
# tolerates the server restarting); pass --all to stop the tunnel too.
cd "$(dirname "$0")"
[ -f logs/serve.pid ] && kill "$(cat logs/serve.pid)" 2>/dev/null || true
pkill -f "^/home/mattshax/amsc-design/activate-iri/.venv/bin/python .*activate-iri serve" || true
if [ "${1:-}" = "--all" ]; then pkill -f "^/home/mattshax/pw/pw endpoints http --name activate-iri" || true; fi
echo stopped
