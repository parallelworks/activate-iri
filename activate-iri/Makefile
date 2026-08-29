.PHONY: venv test demo lint
PY ?= 3.13
venv:
	uv venv --python $(PY) .venv && uv pip install -p .venv/bin/python -e ".[dev]"
test:
	.venv/bin/python -m pytest -q
demo:  ## run against fixtures (no ACTIVATE account needed); filesystem ops run locally as the current user
	ACTIVATE_IRI_FIXTURES=examples/fixtures.json ACTIVATE_IRI_FACILITY_FILE=examples/facility.federation.yaml \
	ACTIVATE_ORGANIZATION=demo-org ACTIVATE_IRI_MODE=edge ACTIVATE_IRI_EDGE_CLUSTER=labcluster ACTIVATE_IRI_LOCAL_RUN_AS=direct \
	API_URL_ROOT=http://127.0.0.1:8000 .venv/bin/activate-iri serve --port 8000
lint:
	.venv/bin/ruff check activate_iri tests
