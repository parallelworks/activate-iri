#!/usr/bin/env bash
cd "$(dirname "$0")"
pkill -f "endpoints http --name activate-iri" || true
[ -f logs/serve.pid ] && kill "$(cat logs/serve.pid)" 2>/dev/null || true
pkill -f "activate-iri serve" || true
echo stopped
