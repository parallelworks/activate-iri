#!/usr/bin/env bash
# Keepalive: every minute, restart the endpoint or the tunnel if either is down. Run detached:
#   nohup setsid ./watch.sh > logs/watch.log 2>&1 &
cd "$(dirname "$0")"
source ./env.sh
mkdir -p logs; echo $$ > logs/watch.pid
while true; do
  if ! curl -fsS -m 10 "http://127.0.0.1:$ACTIVATE_IRI_PORT/api/v2/facility" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) endpoint down, restarting"; ./stop.sh >/dev/null; ./run.sh
  fi
  if ! pgrep -f "^/home/mattshax/pw/pw endpoints http --name activate-iri" >/dev/null; then
    echo "$(date -u +%FT%TZ) tunnel down, republishing"; ./publish.sh | tail -2
  fi
  sleep 60
done
