#!/usr/bin/env bash
# Start the prototype IRI endpoint detached on this host. Credential comes from the pw CLI's
# current "activate" context (a 24-hour platform token today; replace with a service API key
# from the ACTIVATE UI for anything longer-lived).
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh
mkdir -p logs
VENV=/home/mattshax/amsc-design/activate-iri/.venv
export ACTIVATE_SERVICE_API_KEY="$(python3 - <<'PY'
import json, os
d = json.load(open(os.path.expanduser("~/.config/pw/credentials")))
ident = d["identities"]["activate"]
print(ident.get("apikey") or ident.get("token"))
PY
)"
if [ -f logs/serve.pid ] && kill -0 "$(cat logs/serve.pid)" 2>/dev/null; then echo "already running"; exit 0; fi
nohup setsid env ACTIVATE_IRI_TAG=activate-iri-prototype "$VENV/bin/activate-iri" serve --host 127.0.0.1 --port "$ACTIVATE_IRI_PORT" > logs/serve.out 2>&1 &
echo $! > logs/serve.pid
sleep 3
curl -fsS "http://127.0.0.1:$ACTIVATE_IRI_PORT/api/v2/facility" >/dev/null && echo "endpoint up on 127.0.0.1:$ACTIVATE_IRI_PORT (pid $(cat logs/serve.pid))"
./publish.sh >/dev/null 2>&1 || true   # re-publish the tunnel if it is not running
