"""Full HTTP lifecycle through the DOE framework with the ACTIVATE adapters wired in:
auth (facility-specific API key and AmSC Keycard mapping), discovery, allocations, a job on the
shell scheduler, and the async filesystem task loop, all against fixtures and the local executor."""
import base64
import os
import time

import pytest

AUTH = {"Authorization": "Bearer 12345"}   # fixtures map this credential to user gtorok


def wait_task(client, task_uri, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(task_uri, headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("completed", "failed", "canceled"):
            return body
        time.sleep(0.2)
    raise AssertionError("task did not finish")


def test_unauthenticated_discovery(client, runtime):
    fac = client.get("/api/v2/facility").json()
    assert fac["short_name"] == "PW-ACTIVATE" and len(fac["site_uris"]) == 4
    sites = client.get("/api/v2/facility/sites").json()
    assert {s["short_name"] for s in sites} >= {"PW-LAB", "NEO", "CLOUD-USE"}
    res = client.get("/api/v2/status/resources", params={"resource_type": "urn:doe-iri:resource:compute"}).json()
    assert {r["name"] for r in res} == {"Lab cluster", "Public Cloud (AWS)", "NeoCloud H100 pool"}
    assert all(r["resource_type"] == "urn:doe-iri:resource:compute:system" for r in res)
    compute = client.get("/api/v2/compute/resources").json()
    assert len(compute) == 3
    caps = client.get("/api/v2/account/capabilities").json()
    assert any(c["units"] == ["urn:doe-iri:allocation:compute:node-hours"] for c in caps)


def test_authentication_paths(client, runtime):
    assert client.get("/api/v2/account/whoami").status_code == 401  # missing bearer -> framework 401/403
    assert client.get("/api/v2/account/whoami", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/v2/account/whoami", headers=AUTH).json()["username"] == "gtorok"
    # AmSC project mapping (the framework validates the Keycard; the mapping is ours)
    from activate_iri.auth import resolve_amsc_mapping
    mapped = resolve_amsc_mapping("prj-open-science-materials-2026", os.environ["AMSC_PROJECT_MAPPING_FILE"])
    assert mapped.user == "amsc-materials-svc" and mapped.posix_user == "amscmat" and mapped.account == "amsc-materials"
    with pytest.raises(ValueError):
        resolve_amsc_mapping("prj-unknown", os.environ["AMSC_PROJECT_MAPPING_FILE"])


def test_projects_and_allocations(client, runtime):
    projects = client.get("/api/v2/account/projects", headers=AUTH).json()
    names = {p["name"] for p in projects}
    assert names == {"amsc-materials", "research-credits"}, "child allocations fold into their parent project"
    proj = next(p for p in projects if p["name"] == "amsc-materials")
    pas = client.get(f"/api/v2/account/projects/{proj['id']}/project_allocations", headers=AUTH).json()
    assert pas and pas[0]["entries"][0] == {"allocation": 5000.0, "usage": 812.5, "unit": "urn:doe-iri:allocation:compute:node-hours"}
    uas = client.get(f"/api/v2/account/projects/{proj['id']}/project_allocations/{pas[0]['id']}/user_allocations", headers=AUTH).json()
    assert uas[0]["user_id"] == "gtorok" and uas[0]["entries"] == pas[0]["entries"]
    credits = next(p for p in projects if p["name"] == "research-credits")
    cpas = client.get(f"/api/v2/account/projects/{credits['id']}/project_allocations", headers=AUTH).json()
    assert cpas[0]["entries"][0]["unit"] == "urn:doe-iri:allocation:ext:pw:credits"


def test_filesystem_task_loop_and_shell_job(client, runtime, tmp_path):
    # point the lab cluster at a scratch dir and strip its scheduler so the shell fallback runs locally
    runtime.client.f["clusters"][0]["schedulerType"] = None
    runtime.client.f["managed_clusters"]["labcluster"]["partitions"] = []
    inv = client.get("/api/v2/status/resources", params={"name": "Lab cluster"}).json()
    rid = inv[0]["id"]
    work = tmp_path / "iri-work"

    r = client.post(f"/api/v2/filesystem/mkdir/{rid}", headers=AUTH, json={"path": str(work), "parent": True})
    assert r.status_code == 201, r.text
    task = wait_task(client, r.json()["task_uri"])
    assert task["status"] == "completed" and task["result"]["output"]["type"] == "directory"

    r = client.post(f"/api/v2/filesystem/upload/{rid}", headers=AUTH, params={"path": str(work / "hello.txt")},
                    files={"file": ("hello.txt", b"hello iri\n")})
    assert r.status_code == 200, r.text
    assert wait_task(client, r.json()["task_uri"])["status"] == "completed"

    r = client.post(f"/api/v2/filesystem/ls/{rid}", headers=AUTH, json={"path": str(work)})
    listing = wait_task(client, r.json()["task_uri"])["result"]["output"]
    assert [f["name"] for f in listing] == ["hello.txt"] and listing[0]["size"] == "10"

    r = client.post(f"/api/v2/filesystem/checksum/{rid}", headers=AUTH, json={"path": str(work / "hello.txt")})
    import hashlib
    assert wait_task(client, r.json()["task_uri"])["result"]["output"]["checksum"] == hashlib.sha256(b"hello iri\n").hexdigest()

    r = client.post(f"/api/v2/filesystem/download/{rid}", headers=AUTH, json={"path": str(work / "hello.txt")})
    assert base64.b64decode(wait_task(client, r.json()["task_uri"])["result"]["output"]) == b"hello iri\n"

    # compute: submit, poll to completion, list
    os.environ["HOME"] = str(tmp_path)  # shell scheduler keeps jobs under $HOME/.iri/jobs
    job = {"executable": "/bin/sh", "arguments": ["-c", "echo done > result.txt; sleep 0.2"], "name": "smoke",
           "directory": str(work), "attributes": {"account": "amsc-materials"}}
    r = client.post(f"/api/v2/compute/job/{rid}", headers=AUTH, json=job)
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]
    assert r.json()["status"]["state"] == "queued" and r.json()["status"]["meta_data"]["scheduler"] == "shell"
    state = None
    for _ in range(50):
        s = client.get(f"/api/v2/compute/status/{rid}/{job_id}", headers=AUTH).json()
        state = s["status"]["state"]
        if state in ("completed", "failed"):
            break
        time.sleep(0.2)
    assert state == "completed", s
    assert (work / "result.txt").read_text().strip() == "done"
    jobs = client.post(f"/api/v2/compute/status/{rid}", headers=AUTH).json()
    assert any(j["id"] == job_id for j in jobs)
    assert client.get(f"/api/v2/compute/status/{rid}/does-not-exist", headers=AUTH).status_code == 404

    # storage locations resolve templates for this user
    locs = client.get(f"/api/v2/storage/locations/{rid}", headers=AUTH).json()
    assert any(l["logical_name"] == "home" and l["path"] == "/home/gtorok" for l in locs)
