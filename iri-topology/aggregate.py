#!/usr/bin/env python3
"""Aggregate DOE IRI facility endpoints into the HPC Status Monitor topology format.

Reads the public groups (facility, sites, status resources, incidents) of every configured IRI
endpoint and writes web/api/topology as the graph the status-monitor topology viewer draws:
one monitor node, one site node per IRI Site (pinned at the facility's coordinates), and one
system node per compute or inference resource with status, capacity, and open incidents.

"Connected" means the facility API answered this sweep; latency is the HTTP round trip to its
facility endpoint. No token is needed for the public groups. Run once, or loop with --every.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time

import httpx
import yaml

HERE = pathlib.Path(__file__).resolve().parent
STATUS_MAP = {"up": "UP", "degraded": "DEGRADED", "down": "DOWN", "unknown": "UNKNOWN"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "unnamed"


def short_type(urn: str) -> str:
    return urn.replace("urn:doe-iri:resource:", "") if urn.startswith("urn:") else urn


class Facility:
    def __init__(self, cfg: dict):
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.base = cfg["base_url"].rstrip("/")
        self.scheduler = cfg.get("scheduler", "")
        self.site_overrides = cfg.get("sites", {})
        self.default_site = cfg.get("default_site", {})
        self.include_types = set(cfg.get("include_types", ["compute", "service:inference"]))

    def fetch(self, client: httpx.Client) -> dict:
        t0 = time.monotonic()
        facility = client.get(f"{self.base}/facility").json()
        latency_ms = int((time.monotonic() - t0) * 1000)
        sites = client.get(f"{self.base}/facility/sites").json()
        resources = client.get(f"{self.base}/status/resources", params={"limit": 200}).json()
        try:
            incidents = client.get(f"{self.base}/status/incidents", params={"resolution": "unresolved", "limit": 100}).json()
            if not isinstance(incidents, list):
                incidents = []
        except Exception:
            incidents = []
        try:
            openapi = client.get(f"{self.base}/openapi.json").json()
            groups = sorted({p.split("/")[3] for p in openapi.get("paths", {}) if p.count("/") >= 3})
        except Exception:
            groups = []
        return {"facility": facility, "sites": sites, "resources": resources, "incidents": incidents, "latency_ms": latency_ms, "groups": groups}


def site_record(fac: Facility, site: dict) -> dict:
    key = site.get("short_name") or site.get("name") or site.get("id")
    override = fac.site_overrides.get(key) or fac.site_overrides.get(site.get("id"), {}) or {}
    site_id = f"{fac.id}-{slugify(override.get('slug') or key)}"
    lat = site.get("latitude") or override.get("lat") or fac.default_site.get("lat")
    lon = site.get("longitude") or override.get("lon") or fac.default_site.get("lon")
    loc = ", ".join(x for x in [site.get("locality_name"), site.get("state_or_province_name")] if x) or override.get("location") or fac.default_site.get("location", "")
    return {
        "id": site_id, "name": override.get("name") or f"{fac.name}: {site.get('name') or key}", "organization": site.get("operating_organization") or fac.name,
        "location": loc, "lat": lat, "lon": lon, "cloud": bool(override.get("cloud", fac.default_site.get("cloud", False))),
        "systems": 0, "connected": 0, "status": "UNKNOWN", "capacity": {"cores_total": 0, "cores_running": 0}, "members": [], "node_id": f"site:{site_id}",
        "iri_site_uri": site.get("self_uri"), "facility": fac.id,
    }


def system_node(fac: Facility, res: dict, site: dict, incidents: list[dict], connected: bool, latency_ms: int, groups: list[str], observed: str) -> dict:
    attrs = res.get("attributes") or {}
    rtype = short_type(res.get("resource_type", ""))
    status = STATUS_MAP.get(str(res.get("current_status", "unknown")).lower(), "UNKNOWN")
    slug = f"{fac.id}-{slugify(res.get('name') or res.get('id'))}"
    caps = attrs.get("system_capabilities") or []
    scheduler = fac.scheduler or ("SLURM" if any("batch-scheduling" in c for c in caps) else "")
    if rtype.startswith("service:inference"):
        scheduler = "INFERENCE"
    related = [i for i in incidents if res.get("self_uri") in (i.get("resource_uris") or []) or res.get("id") in (i.get("resource_uris") or [])]
    insights = [{"type": "warning" if i.get("type") == "planned" else "critical", "message": f"{i.get('name')}: {i.get('description') or ''}"[:200], "priority": 3, "metric": "incident"} for i in related]
    alert = "critical" if any(x["type"] == "critical" for x in insights) else ("warning" if insights else None)
    capacity = {}
    if attrs.get("configured_cpu_core_count"):
        capacity["cores_total"] = int(attrs["configured_cpu_core_count"])
    if attrs.get("configured_node_count"):
        capacity["nodes_total"] = int(attrs["configured_node_count"])
    if attrs.get("configured_gpu_count"):
        capacity["gpus_total"] = int(attrs["configured_gpu_count"])
    return {
        "id": f"sys:{slug}", "kind": "system", "label": res.get("name") or slug, "slug": slug, "site": site["id"], "site_label": site["name"],
        "status": status, "reported_status": res.get("current_status"), "status_source": "status page", "scheduler": scheduler,
        "login": fac.base.split("//", 1)[-1].split("/")[0], "address": None, "origin": "fleet", "resource_type": rtype,
        "endpoints": res.get("supported_endpoints") or [], "iri_uri": res.get("self_uri"), "description": res.get("description"),
        "connected": connected, "connection": {
            "source": "iri", "uri": fac.base, "capabilities": groups, "connected_since": observed, "connected_for_seconds": 0,
            "uptime_ratio": 1.0 if connected else 0.0, "uptime_window_hours": 24, "transitions": 0, "latency_ms": latency_ms,
            "window_start": observed, "window_end": observed, "spans": [{"status": status, "from": observed, "to": observed, "seconds": 0}],
        } if connected else None,
        "capacity": capacity, "queues": None, "allocation": None, "alert": alert, "insights": insights, "links": {},
    }


def build(config: dict, timeout: float = 20.0) -> dict:
    observed = now_iso()
    sites: dict[str, dict] = {}
    nodes: list[dict] = []
    edges: list[dict] = []
    facilities_meta = []
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"Accept": "application/json"}) as client:
        for cfg in config["facilities"]:
            fac = Facility(cfg)
            try:
                data = fac.fetch(client)
                connected = True
                error = None
            except Exception as exc:
                connected, error, data = False, str(exc)[:160], {"facility": {}, "sites": [], "resources": [], "incidents": [], "latency_ms": None, "groups": []}
            facilities_meta.append({"id": fac.id, "name": fac.name, "base_url": fac.base, "connected": connected, "error": error, "latency_ms": data["latency_ms"],
                                    "groups": data["groups"], "facility_name": data["facility"].get("name"), "resources": len(data["resources"])})
            site_by_uri: dict[str, dict] = {}
            for s in data["sites"] or [dict(id=fac.id, name=fac.name)]:
                rec = site_record(fac, s)
                sites.setdefault(rec["id"], rec)
                site_by_uri[s.get("self_uri") or s.get("id") or fac.id] = sites[rec["id"]]
            if not data["sites"]:
                site_by_uri[fac.id] = sites[site_record(fac, dict(id=fac.id, name=fac.name))["id"]]
            for res in data["resources"]:
                rtype = short_type(res.get("resource_type", ""))
                if not any(rtype == t or rtype.startswith(t + ":") for t in fac.include_types):
                    continue
                site = site_by_uri.get(res.get("site_uri")) or next(iter(site_by_uri.values()))
                node = system_node(fac, res, site, data["incidents"], connected, data["latency_ms"] or 0, data["groups"], observed)
                nodes.append(node)
                site["members"].append(node["slug"])
                site["systems"] += 1
                site["connected"] += 1 if connected else 0
                site["capacity"]["cores_total"] += node["capacity"].get("cores_total", 0)
            edges.append({"id": f"monitor->{fac.id}", "source": "monitor", "target": f"facility:{fac.id}", "kind": "facility", "connected": connected})
    for site in sites.values():
        statuses = [n["status"] for n in nodes if n["site"] == site["id"]]
        site["status"] = "DOWN" if statuses and all(s == "DOWN" for s in statuses) else ("DEGRADED" if any(s in ("DOWN", "DEGRADED") for s in statuses) else ("UP" if statuses else "UNKNOWN"))
        edges.append({"id": f"monitor->{site['node_id']}", "source": "monitor", "target": site["node_id"], "kind": "site", "connected": site["connected"] > 0})
    for link in config.get("links", []):  # illustrative inter-site links (ESnet paths, cloud interconnects); not measured
        edges.append({"id": f"{link['source']}<->{link['target']}", "source": f"site:{link['source']}", "target": f"site:{link['target']}", "kind": "network",
                      "label": link.get("label", ""), "mock": True, "connected": True})
    status_counts: dict[str, int] = {}
    for n in nodes:
        status_counts[n["status"]] = status_counts.get(n["status"], 0) + 1
    sched_counts: dict[str, int] = {}
    for n in nodes:
        if n["scheduler"]:
            sched_counts[n["scheduler"]] = sched_counts.get(n["scheduler"], 0) + 1
    monitor = {"id": "monitor", "kind": "monitor", "label": config.get("monitor_label", "ACTIVATE IRI federation view"), "status": "UP"}
    up = status_counts.get("UP", 0)
    return {
        "meta": {"generated_at": observed, "platform": "iri", "site_label": "Facility", "fleet_observed_at": observed, "telemetry_clusters": sum(1 for f in facilities_meta if f["connected"]),
                 "ready": True, "collection_progress": None, "facilities": facilities_meta, "links_are_illustrative": True},
        "summary": {"sites": len(sites), "systems": len(nodes), "connected": sum(1 for n in nodes if n["connected"]), "alerts": sum(1 for n in nodes if n["alert"]),
                    "up": up, "uptime_ratio": (up / len(nodes)) if nodes else 0.0, "status_counts": status_counts, "scheduler_counts": sched_counts, "queues": 0,
                    "capacity": {"cores_total": sum(n["capacity"].get("cores_total", 0) for n in nodes), "cores_running": 0, "cores_free": 0,
                                 "nodes_total": sum(n["capacity"].get("nodes_total", 0) for n in nodes), "gpus_total": sum(n["capacity"].get("gpus_total", 0) for n in nodes), "utilization_percent": 0}},
        "sites": list(sites.values()), "nodes": [monitor] + nodes, "edges": edges,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "facilities.yaml"))
    ap.add_argument("--out", default=str(HERE / "web" / "api"))
    ap.add_argument("--every", type=int, default=0, help="seconds between sweeps (0 = once)")
    args = ap.parse_args(argv)
    config = yaml.safe_load(open(args.config, encoding="utf-8"))
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    while True:
        graph = build(config)
        (out / "topology").write_text(json.dumps(graph, indent=1))
        frame = {"observed_at": graph["meta"]["generated_at"], "nodes": [{"id": n["id"], "status": n.get("status"), "capacity": n.get("capacity", {})} for n in graph["nodes"]]}
        history.append(frame)
        history = history[-288:]  # 24 hours at 5-minute sweeps
        (out / "history").write_text(json.dumps({"window_hours": 24, "frames": history}))
        print(f"{graph['meta']['generated_at']} facilities={len(config['facilities'])} connected={graph['meta']['telemetry_clusters']} sites={graph['summary']['sites']} systems={graph['summary']['systems']} up={graph['summary']['up']}", flush=True)
        if not args.every:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
