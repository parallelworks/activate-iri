#!/usr/bin/env bash
# Publish the local endpoint through the ACTIVATE reverse tunnel at https://activate-iri.activate.pw/.
# --public is required by the IRI specification (facility and status groups are unauthenticated);
# if the organization disallows public sessions the tunnel falls back to login-gated access.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh
mkdir -p logs
if pgrep -f "^/home/mattshax/pw/pw endpoints http --name activate-iri" >/dev/null; then echo "already published"; exit 0; fi
nohup setsid /home/mattshax/pw/pw endpoints http --name activate-iri --subdomain activate-iri --keep --public -o text "$ACTIVATE_IRI_PORT" > logs/endpoint.out 2>&1 &
sleep 8
cat logs/endpoint.out
