"""Gateway mode: upstream IRI facilities consolidated under this endpoint, exercised against a
fake upstream served by httpx.MockTransport (v1 and v2 shapes)."""
import json

import httpx
import pytest

from activate_iri.gateway import Gateway, GatewayConfig, UpstreamFacility, ns, split_id

UP_FACILITY = {"id": "f1", "name": "Fake Lab Facility", "last_modified": "2026-08-01T00:00:00Z", "site_uris": ["https://up/api/v2/facility/sites/s1"]}
UP_SITES = [{"id": "s1", "name": "Fake Site", "short_name": "FAKE", "operating_organization": "Fake Lab", "last_modified": "2026-08-01T00:00:00Z", "self_uri": "https://up/api/v2/facility/sites/s1"}]
UP_RESOURCES = [{"id": "r-compute", "name": "Polaris-like", "resource_type": "urn:doe-iri:resource:compute:system", "current_status": "up", "last_modified": "2026-08-01T00:00:00Z",
                 "site_uri": "https://up/api/v2/facility/sites/s1", "supported_endpoints": ["compute", "filesystem"], "capability_uris": ["https://up/api/v2/account/capabilities/c1"]},
                {"id": "r-store", "name": "Eagle-like", "resource_type": "storage", "current_status": "degraded", "last_modified": "2026-08-01T00:00:00Z", "site_uri": "https://up/api/v2/facility/sites/s1"}]
UP_INCIDENTS = [{"id": "i1", "name": "Eagle degraded", "status": "degraded", "type": "unplanned", "resolution": "unresolved", "start": "2026-08-01T00:00:00Z", "last_modified": "2026-08-01T00:00:00Z",
                 "resource_uris": ["https://up/api/v2/status/resources/r-store"]}]
STATE = {"jobs": {}, "tasks": {}, "seen_auth": []}


def fake_upstream(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    STATE["seen_auth"].append(request.headers.get("authorization"))
    if path.endswith("/facility"):
        return httpx.Response(200, json=UP_FACILITY)
    if path.endswith("/facility/sites"):
        return httpx.Response(200, json=UP_SITES)
    if path.endswith("/status/resources"):
        return httpx.Response(200, json=UP_RESOURCES)
    if path.endswith("/status/incidents"):
        return httpx.Response(200, json=UP_INCIDENTS)
    if path.endswith("/account/projects"):
        if request.headers.get("authorization") != "Bearer up-token":
            return httpx.Response(401, json={"title": "Unauthorized"})
        return httpx.Response(200, json=[{"id": "p1", "name": "fakeproj", "description": "d", "user_ids": ["u"], "last_modified": "2026-08-01T00:00:00Z"}])
    if path.endswith("/account/projects/p1/project_allocations"):
        return httpx.Response(200, json=[{"id": "pa1", "project_uri": "https://up/api/v2/account/projects/p1", "capability_uri": "https://up/api/v2/account/capabilities/c1",
                                          "entries": [{"allocation": 100.0, "usage": 5.0, "unit": "node_hours"}]}])
    if path.endswith("/compute/job/r-compute") and request.method == "POST":
        body = json.loads(request.content)
        STATE["jobs"]["j1"] = body
        return httpx.Response(200, json={"id": "j1", "status": {"state": "queued", "time": 1.0}})
    if path.endswith("/compute/status/r-compute/j1"):
        return httpx.Response(200, json={"id": "j1", "status": {"state": "completed", "exit_code": 0}})
    if path.endswith("/compute/cancel/r-compute/j1"):
        return httpx.Response(204)
    if path.endswith("/filesystem/ls/r-compute"):
        STATE["tasks"]["t1"] = {"id": "t1", "status": "completed", "result": {"output": [{"name": "a.txt", "type": "file", "user": "u", "group": "g", "permissions": "rw-r--r--", "last_modified": "x", "size": "3"}]}}
        return httpx.Response(200, json={"task_id": "t1", "task_uri": "https://up/api/v2/task/t1"})
    if path.endswith("/task/t1"):
        return httpx.Response(200, json=STATE["tasks"]["t1"])
    return httpx.Response(404, json={"title": "not found", "detail": path})


@pytest.fixture()
def gw(monkeypatch):
    monkeypatch.setenv("IRI_TOKEN_FAKE", "up-token")
    g = Gateway(GatewayConfig(facilities=[UpstreamFacility(id="fake", name="Fake Lab", base_url="https://up/api/v2")], cache_ttl_seconds=0))
    g.http = httpx.AsyncClient(transport=httpx.MockTransport(fake_upstream))
    for up in g.upstreams.values():
        up.http = g.http
    return g


def test_namespacing():
    assert ns("alcf", "55c1") == "alcf:55c1"
    assert split_id("alcf:55c1") == ("alcf", "55c1")
    assert split_id("efc71bf3-41ab-562c-98a6-79d28b40b051") == (None, "efc71bf3-41ab-562c-98a6-79d28b40b051")
    assert split_id("urn:doe-iri:resource:compute")[0] is None


@pytest.mark.asyncio
async def test_merge_and_incidents(gw, runtime):
    inv = await runtime.inventory(refresh=True)
    before = len(inv.resources)
    await gw.refresh(force=True)
    gw.merge_into(inv)
    added = [r for r in inv.resources if r.id.startswith("fake:")]
    assert len(added) == 2 and len(inv.resources) == before + 2
    compute = next(r for r in added if r.id == "fake:r-compute")
    assert compute.site_id == "fake:s1" and [e.value for e in compute.supported_endpoints] == ["compute", "filesystem"]
    assert compute.capability_ids == ["fake:c1"] and "fake:c1" in inv.capabilities
    store = next(r for r in added if r.id == "fake:r-store")
    assert store.resource_type == "urn:doe-iri:resource:storage" and store.current_status.value == "degraded"
    assert any(s.id == "fake:s1" and "fake:r-compute" in s.resource_ids for s in inv.sites)
    inc = gw.incidents()
    assert inc[0].id == "fake:i1" and inc[0].resource_ids == ["fake:r-store"]


@pytest.mark.asyncio
async def test_forwarding_with_facility_token(gw):
    from app.routers.compute import models as cm
    job = await gw.submit_job("fake", "r-compute", "caller-token", cm.JobSpec(executable="/bin/true", attributes=cm.JobAttributes(queue_name="debug")))
    assert job.id == "j1" and job.status.state.value == "queued"
    assert STATE["jobs"]["j1"]["executable"] == "/bin/true" and "Bearer up-token" in STATE["seen_auth"]
    assert (await gw.get_job("fake", "r-compute", "j1", "caller-token", True)).status.state.value == "completed"
    assert await gw.cancel_job("fake", "r-compute", "j1", "caller-token")
    result = await gw.filesystem("fake", "r-compute", "ls", "caller-token", {"path": "/x"})
    assert result["output"][0]["name"] == "a.txt"
    projects = await gw.projects("fake", "caller-token")
    assert projects[0].id == "fake:p1"
    pas = await gw.project_allocations("fake", "p1", "caller-token")
    assert pas[0].capability_id == "fake:c1" and pas[0].entries[0].unit == "urn:doe-iri:allocation:compute:node-hours"


@pytest.mark.asyncio
async def test_missing_token_is_401(monkeypatch):
    from fastapi import HTTPException
    g = Gateway(GatewayConfig(facilities=[UpstreamFacility(id="nowhere", name="No token", base_url="https://up/api/v2")]))
    with pytest.raises(HTTPException) as exc:
        await g.forward_json("nowhere", "GET", "/account/projects", None)
    assert exc.value.status_code == 401
