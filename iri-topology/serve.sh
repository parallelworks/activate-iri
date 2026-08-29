#!/usr/bin/env bash
# Sweep the IRI endpoints every five minutes and publish the map through an ACTIVATE endpoint.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PY:-/home/mattshax/amsc-design/activate-iri/.venv/bin/python}
mkdir -p logs
pgrep -f "aggregate.py --every" >/dev/null || (nohup setsid "$PY" aggregate.py --every 300 > logs/aggregate.log 2>&1 &)
pgrep -f "endpoints serve --name iri-topology" >/dev/null || (nohup setsid /home/mattshax/pw/pw endpoints serve --name iri-topology --subdomain iri-topology --keep -o text web > logs/endpoint.log 2>&1 &)
sleep 6; tail -3 logs/aggregate.log; grep -m1 "https://" logs/endpoint.log || true
