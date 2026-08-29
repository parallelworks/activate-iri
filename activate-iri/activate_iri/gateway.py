"""Gateway mode: one IRI endpoint that consolidates many IRI facilities.

The IRI deployment models include a "proxy IRI server" that fronts several facility servers
behind one contract. This module implements that shape on top of the ACTIVATE adapters:
upstream facilities (ALCF, NERSC, OLCF, ESnet, other PW endpoints) are configured with a base
URL and a token source; their Sites, resources, capabilities, incidents, and projects are
merged into this facility with namespaced identifiers (``<facility>:<upstream id>``), and
compute, filesystem, and account calls on a namespaced resource are forwarded to the upstream
with the caller's facility token. Resources without a namespace are ACTIVATE's own and take
the normal path.

Token resolution, in order: a caller-supplied header ``X-IRI-Facility-Token-<FACILITY>``, the
token file (``gateway.token_file``, a JSON map facility -> token or env variable name), then an
environment variable ``IRI_TOKEN_<FACILITY>``. The single-account prototype uses the file; the
per-user extension reads the same names from ACTIVATE account variables.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx
from app.routers.account import models as account_models
from app.routers.compute import models as compute_models
from app.routers.facility import models as facility_models
from app.routers.status import models as status_models
from app.types.models import Capability
from app.types.scalars import AllocationUnit
from fastapi import HTTPException
from pydantic import BaseModel, Field

from .inventory import Inventory, now

logger = logging.getLogger(__name__)
SEP = ":"


class UpstreamFacility(BaseModel):
    id: str
    name: str
    base_url: str
    token_env: str | None = None
    scheduler: str | None = None
    site_overrides: dict[str, dict] = Field(default_factory=dict)


class GatewayConfig(BaseModel):
    facilities: list[UpstreamFacility] = Field(default_factory=list)
    token_file: str | None = None
    timeout_seconds: float = 20.0
    cache_ttl_seconds: int = 60


def split_id(resource_id: str) -> tuple[str | None, str]:
    """('alcf', '55c1...') for a namespaced id, (None, id) for a local one."""
    if SEP in resource_id:
        prefix, rest = resource_id.split(SEP, 1)
        if prefix and rest and not prefix.startswith("urn"):
            return prefix, rest
    return None, resource_id


def ns(facility_id: str, upstream_id: str) -> str:
    return f"{facility_id}{SEP}{upstream_id}"


@dataclass
class Upstream:
    cfg: UpstreamFacility
    v2: bool
    http: httpx.AsyncClient
    facility: dict = field(default_factory=dict)
    sites: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    incidents: list[dict] = field(default_factory=list)
    fetched_at: float = 0.0
    error: str | None = None

    @property
    def base(self) -> str:
        return self.cfg.base_url.rstrip("/")

    async def get(self, path: str, token: str | None = None, **params):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = await self.http.get(f"{self.base}{path}", headers=headers, params=params or None)
        return r

    async def request(self, method: str, path: str, token: str, json_body=None, params=None, files=None):
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        r = await self.http.request(method, f"{self.base}{path}", headers=headers, json=json_body, params=params, files=files)
        return r


class Gateway:
    def __init__(self, config: GatewayConfig):
        self.config = config
        self.http = httpx.AsyncClient(timeout=config.timeout_seconds, follow_redirects=True)
        self.upstreams: dict[str, Upstream] = {f.id: Upstream(cfg=f, v2="/v2" in f.base_url, http=self.http) for f in config.facilities}
        self._tokens_cache: dict | None = None

    # -- tokens -------------------------------------------------------------------------
    def token_for(self, facility_id: str, caller_headers: dict | None = None, caller_token: str | None = None) -> str | None:
        key = facility_id.upper().replace("-", "_")
        if caller_headers:
            for h, v in caller_headers.items():
                if h.lower() == f"x-iri-facility-token-{facility_id.lower()}":
                    return v
        if self.config.token_file and os.path.exists(self.config.token_file):
            if self._tokens_cache is None:
                with open(self.config.token_file, encoding="utf-8") as fh:
                    self._tokens_cache = json.load(fh).get("tokens", {})
            value = self._tokens_cache.get(facility_id) or self._tokens_cache.get(key)
            if value:
                return os.environ.get(value, value) if value.isupper() and value.replace("_", "").isalnum() else value
        env_name = self.upstreams[facility_id].cfg.token_env or f"IRI_TOKEN_{key}"
        if os.environ.get(env_name):
            return os.environ[env_name]
        if facility_id == "pw" and caller_token:
            return caller_token   # a PW upstream accepts the same ACTIVATE credential the caller used here
        return None

    def _require_token(self, facility_id: str, caller_token: str | None) -> str:
        token = self.token_for(facility_id, caller_token=caller_token)
        if not token:
            raise HTTPException(status_code=401, detail=f"no credential for facility {facility_id}; supply X-IRI-Facility-Token-{facility_id} or configure the token map")
        return token

    # -- discovery (public groups, cached) ---------------------------------------------
    async def refresh(self, force: bool = False) -> None:
        for up in self.upstreams.values():
            if not force and time.time() - up.fetched_at < self.config.cache_ttl_seconds:
                continue
            try:
                fac = await up.get("/facility")
                fac.raise_for_status()
                sites = await up.get("/facility/sites")
                res = await up.get("/status/resources", limit=500)
                inc = await up.get("/status/incidents", resolution="unresolved", limit=100)
                up.facility, up.sites, up.resources = fac.json(), sites.json() if sites.status_code == 200 else [], res.json() if res.status_code == 200 else []
                up.incidents = inc.json() if inc.status_code == 200 and isinstance(inc.json(), list) else []
                up.error = None
            except Exception as exc:  # noqa: BLE001  an unreachable facility is reported, not fatal
                up.error = str(exc)[:200]
                logger.warning("gateway: %s unreachable: %s", up.cfg.id, up.error)
            up.fetched_at = time.time()

    def merge_into(self, inv: Inventory) -> None:
        """Append namespaced upstream sites and resources to a built ACTIVATE inventory."""
        for up in self.upstreams.values():
            site_ids: dict[str, str] = {}
            for s in up.sites or [{"id": up.cfg.id, "name": up.cfg.name, "self_uri": up.cfg.id}]:
                sid = ns(up.cfg.id, s.get("id") or up.cfg.id)
                site_ids[s.get("self_uri") or s.get("id") or up.cfg.id] = sid
                ov = up.cfg.site_overrides.get(s.get("short_name") or s.get("id") or "", {})
                inv.sites.append(facility_models.Site(
                    id=sid, name=f"{up.cfg.name}: {s.get('name') or s.get('id') or up.cfg.id}", description=s.get("description"),
                    last_modified=s.get("last_modified") or now(), short_name=s.get("short_name"), operating_organization=s.get("operating_organization") or up.cfg.name,
                    country_name=s.get("country_name"), locality_name=s.get("locality_name") or ov.get("location"), state_or_province_name=s.get("state_or_province_name"),
                    latitude=s.get("latitude") or ov.get("lat"), longitude=s.get("longitude") or ov.get("lon"), resource_ids=[],
                ))
            inv.facility.site_ids.extend(site_ids.values())
            for r in up.resources:
                rid = ns(up.cfg.id, r["id"])
                sid = site_ids.get(r.get("site_uri")) or next(iter(site_ids.values()))
                rtype = r.get("resource_type") or "urn:doe-iri:resource:unknown"
                if not str(rtype).startswith("urn:"):
                    rtype = {"compute": "urn:doe-iri:resource:compute", "storage": "urn:doe-iri:resource:storage", "service": "urn:doe-iri:service:generic",
                             "website": "urn:doe-iri:service:website", "network": "urn:doe-iri:resource:network", "system": "urn:doe-iri:resource:system"}.get(rtype, "urn:doe-iri:resource:unknown")
                endpoints = [status_models.Endpoint(e) for e in (r.get("supported_endpoints") or []) if e in ("compute", "filesystem")]
                if not endpoints and "compute" in str(rtype):
                    endpoints = [status_models.Endpoint.compute, status_models.Endpoint.filesystem]
                status = status_models.Status(r.get("current_status")) if r.get("current_status") in ("up", "down", "degraded", "unknown") else status_models.Status.unknown
                cap_ids = [ns(up.cfg.id, u.rstrip("/").rsplit("/", 1)[-1]) for u in (r.get("capability_uris") or [])]
                for cid in cap_ids:
                    inv.capabilities.setdefault(cid, Capability(id=cid, name=f"{up.cfg.name} capability {cid.split(SEP, 1)[1][:8]}", last_modified=now(), units=[AllocationUnit.node_hours]))
                    inv.capability_resource[cid] = rid
                inv.resources.append(status_models.Resource(
                    id=rid, site_id=sid, name=r.get("name"), description=r.get("description"), last_modified=r.get("last_modified") or now(),
                    group=r.get("group"), current_status=status, resource_type=rtype, supported_endpoints=endpoints, capability_ids=cap_ids,
                    attributes={**(r.get("attributes") or {}), "iri_upstream": up.base, "iri_upstream_facility": up.cfg.id, "iri_upstream_uri": r.get("self_uri")},
                ))
                for site in inv.sites:
                    if site.id == sid:
                        site.resource_ids.append(rid)

    def incidents(self) -> list[status_models.Incident]:
        out = []
        for up in self.upstreams.values():
            for i in up.incidents:
                try:
                    out.append(status_models.Incident(
                        id=ns(up.cfg.id, i["id"]), name=i.get("name"), description=i.get("description"), last_modified=i.get("last_modified") or now(),
                        status=status_models.Status(i.get("status", "unknown")), resource_ids=[ns(up.cfg.id, u.rstrip("/").rsplit("/", 1)[-1]) for u in (i.get("resource_uris") or [])],
                        event_ids=[], start=i.get("start") or now(), end=i.get("end"), type=status_models.IncidentType(i.get("type", "unplanned")),
                        resolution=status_models.Resolution(i.get("resolution", "unresolved")),
                    ))
                except Exception as exc:  # noqa: BLE001  skip malformed upstream incidents
                    logger.debug("gateway: skipping malformed incident from %s: %s", up.cfg.id, exc)
                    continue
        return out

    # -- forwarding ---------------------------------------------------------------------
    async def forward_json(self, facility_id: str, method: str, path: str, caller_token: str | None, json_body=None, params=None):
        up = self.upstreams[facility_id]
        token = self._require_token(facility_id, caller_token)
        r = await up.request(method, path, token, json_body=json_body, params=params)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail") or r.json().get("title") or r.text
            except ValueError:
                detail = r.text
            raise HTTPException(status_code=r.status_code if r.status_code in (400, 401, 403, 404, 409, 422, 501) else 502, detail=f"{facility_id}: {detail}"[:400])
        return r.json() if r.content else None

    async def projects(self, facility_id: str, caller_token: str | None) -> list[account_models.Project]:
        items = await self.forward_json(facility_id, "GET", "/account/projects", caller_token) or []
        return [account_models.Project(id=ns(facility_id, p["id"]), name=p.get("name") or p["id"], description=p.get("description") or "", user_ids=p.get("user_ids") or [],
                                       last_modified=p.get("last_modified") or now(), attributes={"iri_upstream_facility": facility_id}) for p in items]

    async def project_allocations(self, facility_id: str, project_uid: str, caller_token: str | None) -> list[account_models.ProjectAllocation]:
        items = await self.forward_json(facility_id, "GET", f"/account/projects/{project_uid}/project_allocations", caller_token) or []
        out = []
        for pa in items:
            cap = (pa.get("capability_uri") or "").rstrip("/").rsplit("/", 1)[-1]
            entries = [account_models.AllocationEntry(allocation=e["allocation"], usage=e["usage"], unit=_unit_urn(e.get("unit"))) for e in pa.get("entries", [])]
            out.append(account_models.ProjectAllocation(id=ns(facility_id, pa["id"]), project_id=ns(facility_id, project_uid), capability_id=ns(facility_id, cap), entries=entries))
        return out

    async def submit_job(self, facility_id: str, upstream_rid: str, caller_token: str | None, spec: compute_models.JobSpec) -> compute_models.Job:
        body = spec.model_dump(exclude_none=True)
        data = await self.forward_json(facility_id, "POST", f"/compute/job/{upstream_rid}", caller_token, json_body=body)
        return _job(data)

    async def get_job(self, facility_id: str, upstream_rid: str, job_id: str, caller_token: str | None, historical: bool) -> compute_models.Job:
        data = await self.forward_json(facility_id, "GET", f"/compute/status/{upstream_rid}/{job_id}", caller_token, params={"historical": "true" if historical else "false"})
        return _job(data)

    async def get_jobs(self, facility_id: str, upstream_rid: str, caller_token: str | None, offset: int, limit: int, historical: bool) -> list[compute_models.Job]:
        data = await self.forward_json(facility_id, "POST", f"/compute/status/{upstream_rid}", caller_token, json_body={}, params={"offset": offset, "limit": limit, "historical": "true" if historical else "false"}) or []
        return [_job(j) for j in data]

    async def cancel_job(self, facility_id: str, upstream_rid: str, job_id: str, caller_token: str | None) -> bool:
        await self.forward_json(facility_id, "DELETE", f"/compute/cancel/{upstream_rid}/{job_id}", caller_token)
        return True

    async def filesystem(self, facility_id: str, upstream_rid: str, op: str, caller_token: str | None, body: dict) -> dict:
        """Forward a filesystem operation and wait for the upstream task; returns the upstream result dict."""
        up = self.upstreams[facility_id]
        token = self._require_token(facility_id, caller_token)
        if up.v2:
            r = await up.request("POST", f"/filesystem/{op}/{upstream_rid}", token, json_body=body)
        else:
            method = {"mkdir": "POST", "symlink": "POST", "compress": "POST", "extract": "POST", "mv": "POST", "cp": "POST", "chmod": "PUT", "chown": "PUT", "rm": "DELETE"}.get(op, "GET")
            r = await up.request(method, f"/filesystem/{op}/{upstream_rid}", token, json_body=body if method in ("POST", "PUT") else None, params=None if method in ("POST", "PUT") else body)
        if r.status_code >= 400:
            raise HTTPException(status_code=r.status_code if r.status_code < 500 else 502, detail=f"{facility_id}: {r.text[:300]}")
        task_uri = r.json().get("task_uri")
        deadline = time.time() + 180
        while time.time() < deadline:
            t = await self.http.get(task_uri, headers={"Authorization": f"Bearer {token}"})
            data = t.json()
            if data.get("status") in ("completed", "failed", "canceled"):
                if data.get("status") != "completed":
                    raise HTTPException(status_code=400, detail=f"{facility_id}: upstream task {data.get('status')}: {str(data.get('result'))[:200]}")
                return data.get("result") or {}
            await asyncio_sleep(1.5)
        raise HTTPException(status_code=504, detail=f"{facility_id}: upstream task timed out")


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _unit_urn(unit) -> str:
    if isinstance(unit, str) and unit.startswith("urn:"):
        return unit
    return {"node_hours": AllocationUnit.node_hours, "bytes": AllocationUnit.bytes, "inodes": AllocationUnit.inodes}.get(str(unit), AllocationUnit.node_hours)


def _job(data: dict | None) -> compute_models.Job:
    data = data or {}
    st = data.get("status") or {}
    state = str(st.get("state", "new")).lower()
    return compute_models.Job(id=str(data.get("id")), status=compute_models.JobStatus(state=compute_models.JobState(state), time=st.get("time") or time.time(),
                                                                                   message=st.get("message"), exit_code=st.get("exit_code"), meta_data=st.get("meta_data")))


def load_gateway_config(path: str | None) -> GatewayConfig | None:
    if not path or not os.path.exists(path):
        return None
    import yaml

    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return GatewayConfig.model_validate(data.get("gateway", data))
