# Extends the DOE reference image; adds the ACTIVATE adapters and the pw CLI.
FROM ghcr.io/doe-iri/iri-facility-api-python:main

# pw CLI: used by the workflow executor and by edge-mode helpers (agent, endpoints).
RUN curl -fsSL https://activate.parallel.works/cli/install.sh | sh || true

COPY pyproject.toml README.md /opt/activate-iri/
COPY activate_iri /opt/activate-iri/activate_iri
RUN pip install --no-cache-dir /opt/activate-iri

ENV IRI_API_ADAPTER_facility="activate_iri.facility.FacilityAdapter" \
    IRI_API_ADAPTER_status="activate_iri.status.StatusAdapter" \
    IRI_API_ADAPTER_account="activate_iri.account.AccountAdapter" \
    IRI_API_ADAPTER_compute="activate_iri.compute.ComputeAdapter" \
    IRI_API_ADAPTER_filesystem="activate_iri.filesystem.FilesystemAdapter" \
    IRI_API_ADAPTER_storage="activate_iri.storage.StorageAdapter" \
    IRI_API_ADAPTER_task="activate_iri.task.TaskAdapter" \
    API_URL="api/v2" \
    IRI_IDEMPOTENCY_STORE="activate_iri.idempotency.InMemoryIdempotencyStore" \
    ACTIVATE_IRI_FACILITY_FILE="/etc/activate-iri/facility.yaml" \
    IRI_API_PARAMS='{"title": "Parallel Works ACTIVATE implementation of the IRI Facility API", "contact": {"name": "Parallel Works", "url": "https://parallelworks.com"}, "terms_of_service": "https://parallelworks.com/terms", "docs_url": "/"}'

EXPOSE 8000
CMD ["fastapi", "run", "app/main.py", "--port", "8000"]
