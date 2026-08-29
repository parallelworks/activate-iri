#!/usr/bin/env bash
# Edge mode bootstrap for a facility that has nothing deployed yet.
#
# Run on the cluster login node as root. In about ten minutes the cluster is (1) connected to
# ACTIVATE through the outbound-only PW agent, (2) fronted by a DOE IRI Facility API v2 endpoint,
# and (3) reachable from AmSC at https://<name>.activate.pw without any inbound firewall change.
#
# Prerequisites: an ACTIVATE org admin has created the managed cluster and minted a node token
# (pw CLI: POST /api/organizations/<org>/managed-clusters/<cluster>/node-token), and has an API
# key for the service account the endpoint will use for inventory reads.
set -euo pipefail

: "${PW_NODE_TOKEN:?registration token from the ACTIVATE org admin}"
: "${PW_SERVICE_API_KEY:?API key of the ACTIVATE service account used for inventory reads}"
: "${IRI_NAME:?public endpoint name, e.g. exlab-iri}"
: "${IRI_CLUSTER:?ACTIVATE cluster name this login node belongs to}"
FACILITY_YAML=${FACILITY_YAML:-/etc/activate-iri/facility.yaml}
IMAGE=${IMAGE:-ghcr.io/parallelworks/activate-iri:latest}

echo "[1/5] pw CLI"
command -v pw >/dev/null || curl -fsSL https://activate.parallel.works/cli/install.sh | sh

echo "[2/5] connect this node to ACTIVATE (outbound only, systemd unit pw-agent)"
pw agent register --token "$PW_NODE_TOKEN" --systemd

echo "[3/5] service account for the endpoint"
id -u iri >/dev/null 2>&1 || useradd --system --home /var/lib/activate-iri --create-home --shell /usr/sbin/nologin iri
install -d -m 0750 -o iri /etc/activate-iri
[ -f "$FACILITY_YAML" ] || install -m 0640 -o iri "$(dirname "$0")/../../examples/facility.edge.yaml" "$FACILITY_YAML"
# The endpoint runs scheduler and filesystem commands as the calling user through sudo.
cat > /etc/sudoers.d/activate-iri <<'SUDO'
Defaults:iri !requiretty
iri ALL=(ALL:ALL) NOPASSWD: /bin/bash -l -s
SUDO
chmod 0440 /etc/sudoers.d/activate-iri

echo "[4/5] IRI endpoint container (host network so sbatch/squeue and the shared filesystems are visible)"
install -m 0640 -o iri /dev/stdin /etc/activate-iri/env <<ENV
ACTIVATE_IRI_MODE=edge
ACTIVATE_IRI_EDGE_CLUSTER=$IRI_CLUSTER
ACTIVATE_IRI_EXECUTOR=local
ACTIVATE_IRI_LOCAL_RUN_AS=sudo
ACTIVATE_SERVICE_API_KEY=$PW_SERVICE_API_KEY
ACTIVATE_HOST=${ACTIVATE_HOST:-https://activate.parallel.works}
ACTIVATE_ORGANIZATION=${ACTIVATE_ORGANIZATION:-}
API_URL_ROOT=https://$IRI_NAME.activate.pw
API_URL=api/v2
AMSC_TOKEN_ENABLED=${AMSC_TOKEN_ENABLED:-false}
AMSC_TOKEN_ISSUER=${AMSC_TOKEN_ISSUER:-}
AMSC_TOKEN_AUDIENCE=https://$IRI_NAME.activate.pw/
AMSC_OIDC_DISCOVERY_URL=${AMSC_OIDC_DISCOVERY_URL:-}
AMSC_PROJECT_MAPPING_FILE=/etc/activate-iri/amsc_project_mapping.json
OPENTELEMETRY_ENABLED=${OPENTELEMETRY_ENABLED:-false}
OTLP_ENDPOINT=${OTLP_ENDPOINT:-}
ENV
install -m 0644 "$(dirname "$0")/activate-iri.service" /etc/systemd/system/activate-iri.service
install -m 0644 "$(dirname "$0")/activate-iri-endpoint.service" /etc/systemd/system/activate-iri-endpoint.service
sed -i "s#__IMAGE__#$IMAGE#; s#__NAME__#$IRI_NAME#" /etc/systemd/system/activate-iri.service /etc/systemd/system/activate-iri-endpoint.service
systemctl daemon-reload
systemctl enable --now activate-iri.service

echo "[5/5] publish through the ACTIVATE reverse tunnel"
systemctl enable --now activate-iri-endpoint.service
echo "IRI endpoint: https://$IRI_NAME.activate.pw/api/v2  (OpenAPI at /api/v2/openapi.json)"
