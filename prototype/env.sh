# Sourced by run.sh. No secrets here: the ACTIVATE credential is read from the pw CLI context at start.
export ACTIVATE_IRI_MODE=federation
export ACTIVATE_IRI_EXECUTOR=local          # this host is the lab cluster controller
export ACTIVATE_IRI_LOCAL_RUN_AS=direct     # prototype: commands run as the service user (mattshax)
export ACTIVATE_HOST=https://activate.parallel.works
export ACTIVATE_ORGANIZATION=parallelworks
export ACTIVATE_IRI_FACILITY_FILE=/home/mattshax/amsc-design/prototype/facility.yaml
export ACTIVATE_IRI_USER_MAP_FILE=/home/mattshax/amsc-design/prototype/user_map.json
export AMSC_PROJECT_MAPPING_FILE=/home/mattshax/amsc-design/prototype/amsc_project_mapping.json
export AMSC_TOKEN_ENABLED=false
export API_URL_ROOT=https://activate-iri.activate.pw
export API_URL=api/v2
export IRI_IDEMPOTENCY_STORE=activate_iri.idempotency.InMemoryIdempotencyStore
export OPENTELEMETRY_ENABLED=false
export LOG_LEVEL=INFO
export IRI_LOG_FILE=/home/mattshax/amsc-design/prototype/logs/api.log
export ACTIVATE_IRI_PORT=8100
