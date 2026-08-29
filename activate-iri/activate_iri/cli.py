"""activate-iri command line: inspect the inventory, check configuration, run the endpoint."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

ADAPTERS = {
    "facility": "activate_iri.facility.FacilityAdapter",
    "status": "activate_iri.status.StatusAdapter",
    "account": "activate_iri.account.AccountAdapter",
    "compute": "activate_iri.compute.ComputeAdapter",
    "filesystem": "activate_iri.filesystem.FilesystemAdapter",
    "storage": "activate_iri.storage.StorageAdapter",
    "task": "activate_iri.task.TaskAdapter",
}


def wire_environment() -> None:
    for domain, dotted in ADAPTERS.items():
        os.environ.setdefault(f"IRI_API_ADAPTER_{domain}", dotted)
    os.environ.setdefault("API_URL", "api/v2")
    os.environ.setdefault("IRI_IDEMPOTENCY_STORE", "activate_iri.idempotency.InMemoryIdempotencyStore")
    os.environ.setdefault("IRI_API_PARAMS", json.dumps({
        "title": "Parallel Works ACTIVATE implementation of the IRI Facility API",
        "description": "Federated cloud, NeoCloud, and on-premises resources exposed as one DOE IRI facility.",
        "contact": {"name": "Parallel Works", "url": "https://parallelworks.com"},
        "terms_of_service": "https://parallelworks.com/terms",
    }))


def cmd_inventory(args) -> int:
    from .runtime import get_runtime

    async def go():
        rt = get_runtime()
        inv = await rt.inventory(refresh=True)
        doc = {
            "facility": inv.facility.model_dump(mode="json"),
            "sites": [s.model_dump(mode="json") for s in inv.sites],
            "resources": [r.model_dump(mode="json") for r in inv.resources],
            "capabilities": [c.model_dump(mode="json") for c in inv.capabilities.values()],
            "incidents": [i.model_dump(mode="json") for i in rt.ledger.incidents],
        }
        print(json.dumps(doc, indent=2, default=str))
        return 0

    return asyncio.run(go())


def cmd_check(args) -> int:
    from .config import Settings, load_facility_config

    settings = Settings()
    cfg = load_facility_config(settings.facility_file)
    print(f"mode={settings.mode} executor={settings.resolved_executor()} facility={cfg.facility.name} sites={len(cfg.sites)}")
    if settings.mode == "edge" and not settings.edge_cluster:
        print("edge mode requires ACTIVATE_IRI_EDGE_CLUSTER", file=sys.stderr)
        return 2
    if not settings.fixtures_file and not settings.activate_api_key:
        print("ACTIVATE_SERVICE_API_KEY is not set (inventory reads will fail)", file=sys.stderr)
        return 2
    print("ok")
    return 0


def cmd_serve(args) -> int:
    wire_environment()
    import uvicorn

    uvicorn.run("activate_iri.asgi:APP", host=args.host, port=args.port, workers=args.workers)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="activate-iri")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory", help="print the IRI resource graph this endpoint publishes").set_defaults(func=cmd_inventory)
    sub.add_parser("check", help="validate settings and the facility file").set_defaults(func=cmd_check)
    serve = sub.add_parser("serve", help="run the IRI endpoint")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--workers", type=int, default=1)
    serve.set_defaults(func=cmd_serve)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
