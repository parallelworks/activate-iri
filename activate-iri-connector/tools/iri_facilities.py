#!/usr/bin/env python3
"""Cross-facility standboard: one table of resources and status across every IRI endpoint the
user can reach, ACTIVATE's own facility endpoint included. Unauthenticated groups only (facility,
status), so no tokens are needed; pass --token to add the caller's projects and allocations.

    python iri_facilities.py                      # defaults: ALCF, NERSC, ESnet, OLCF open, PW
    python iri_facilities.py --endpoint https://iri.activate.pw/api/v2 --token $IRI_TOKEN
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

DEFAULT_ENDPOINTS = {
    "ALCF": "https://api.alcf.anl.gov/api/v1",
    "NERSC": "https://api.iri.nersc.gov/api/v2",
    "ESnet East": "https://iri-dev.ppg.es.net/api/v1",
    "OLCF (open)": "https://amsc-open.s3m.olcf.ornl.gov/api/v1",
    "PW ACTIVATE": "https://iri.activate.pw/api/v2",
}


def fetch(client: httpx.Client, base: str, path: str, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.get(f"{base}{path}", headers=headers)
    r.raise_for_status()
    return r.json()


def short_type(urn_or_enum: str) -> str:
    return urn_or_enum.replace("urn:doe-iri:resource:", "") if urn_or_enum.startswith("urn:") else urn_or_enum


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", action="append", help="extra base URL (repeatable)")
    ap.add_argument("--only", help="comma-separated facility names to include")
    ap.add_argument("--token", help="bearer token for the authenticated groups")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    endpoints = dict(DEFAULT_ENDPOINTS)
    for extra in args.endpoint or []:
        endpoints[extra] = extra
    if args.only:
        keep = {n.strip() for n in args.only.split(",")}
        endpoints = {k: v for k, v in endpoints.items() if k in keep}
    rows, out = [], {}
    with httpx.Client(timeout=20) as client:
        for name, base in endpoints.items():
            try:
                facility = fetch(client, base, "/facility")
                resources = fetch(client, base, "/status/resources?limit=200")
                incidents = fetch(client, base, "/status/incidents?resolution=unresolved&limit=50") if True else []
            except Exception as exc:
                rows.append((name, "-", "-", f"unreachable: {str(exc)[:60]}", "-"))
                continue
            out[name] = {"facility": facility, "resources": resources, "incidents": incidents}
            if args.token:
                try:
                    out[name]["projects"] = fetch(client, base, "/account/projects", args.token)
                except Exception as exc:
                    out[name]["projects_error"] = str(exc)[:120]
            for r in resources:
                rows.append((name, r.get("name", "?"), short_type(r.get("resource_type", "?")), r.get("current_status", "?"), r.get("group") or ""))
    if args.json:
        json.dump(out, sys.stdout, indent=2)
        return 0
    width = max(len(r[1]) for r in rows) if rows else 10
    print(f"{'facility':<14} {'resource':<{width}} {'type':<20} {'status':<10} group")
    for row in rows:
        print(f"{row[0]:<14} {row[1]:<{width}} {row[2]:<20} {row[3]:<10} {row[4]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
