import os
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


@pytest.fixture(scope="session", autouse=True)
def _env(tmp_path_factory):
    """Fixture-backed runtime: no ACTIVATE account, filesystem ops run locally as this user."""
    os.environ["ACTIVATE_IRI_FIXTURES"] = str(ROOT / "examples" / "fixtures.json")
    os.environ["ACTIVATE_IRI_FACILITY_FILE"] = str(ROOT / "examples" / "facility.federation.yaml")
    os.environ["ACTIVATE_IRI_MODE"] = "federation"
    os.environ["ACTIVATE_ORGANIZATION"] = "demo-org"
    os.environ["ACTIVATE_IRI_EXECUTOR"] = "local"
    os.environ["ACTIVATE_IRI_LOCAL_RUN_AS"] = "direct"
    os.environ["AMSC_PROJECT_MAPPING_FILE"] = str(ROOT / "examples" / "amsc_project_mapping.json")
    os.environ["API_URL_ROOT"] = "http://testserver"
    os.environ["API_URL"] = "api/v2"
    os.environ["IRI_IDEMPOTENCY_STORE"] = "activate_iri.idempotency.InMemoryIdempotencyStore"
    from activate_iri.cli import ADAPTERS
    for domain, dotted in ADAPTERS.items():
        os.environ[f"IRI_API_ADAPTER_{domain}"] = dotted
    yield


@pytest.fixture()
def runtime():
    from activate_iri.runtime import reset_runtime
    return reset_runtime()


@pytest.fixture()
def client():
    from app.main import APP
    from fastapi.testclient import TestClient
    with TestClient(APP) as tc:
        yield tc
