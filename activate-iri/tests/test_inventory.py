"""The ACTIVATE -> IRI mapping produces valid v2 objects with registered URNs and profiles."""
import pytest
from app.routers.status import models as status_models


@pytest.mark.asyncio
async def test_inventory_builds_resource_graph(runtime):
    inv = await runtime.inventory(refresh=True)
    names = {r.name for r in inv.resources}
    assert "Lab cluster" in names and "Public Cloud (AWS)" in names and "NeoCloud H100 pool" in names
    assert "Not published" not in names, "clusters without the iri tag stay private"
    lab = next(r for r in inv.resources if r.name == "Lab cluster")
    assert lab.resource_type == "urn:doe-iri:resource:compute:system"
    assert lab.current_status == status_models.Status.up
    assert set(lab.supported_endpoints) == {status_models.Endpoint.compute, status_models.Endpoint.filesystem}
    assert lab.attributes["schema_version"] == "1.0.0"
    assert "urn:doe-iri:compute:system-capability:batch-scheduling" in lab.attributes["system_capabilities"]
    assert "urn:doe-iri:compute:system-capability:accelerator-support" in lab.attributes["system_capabilities"]
    assert lab.attributes["configured_cpu_core_count"] == 64
    # managed-cluster partitions become CPU and GPU capabilities
    caps = [inv.capabilities[c].name for c in lab.capability_ids]
    assert any("GPU" in c for c in caps) and any("CPU" in c for c in caps)
    # node filesystems become filesystem + mount resources related to the cluster
    mounts = [r for r in inv.resources if r.resource_type == "urn:doe-iri:resource:storage:mount"]
    assert any(r.attributes["mount_path"] == "/scratch" for r in mounts)
    scratch_mount = next(r for r in mounts if r.attributes["mount_path"] == "/scratch")
    assert scratch_mount.get_extra("related_resource_ids")["mounted-on"] == [lab.id]
    assert inv.cluster_for(scratch_mount.id)["name"] == "labcluster"


@pytest.mark.asyncio
async def test_elastic_cloud_cluster_and_storage_and_inference(runtime):
    inv = await runtime.inventory(refresh=True)
    aws = next(r for r in inv.resources if r.name == "Public Cloud (AWS)")
    assert aws.current_status == status_models.Status.up, "an off-but-provisionable cloud cluster is advertised as available"
    assert aws.attributes["configured_node_count"] == 11
    lustre = next(r for r in inv.resources if r.name == "FSx Lustre scratch")
    assert lustre.attributes["filesystem_technology"] == "urn:doe-iri:storage:filesystem-technology:lustre"
    assert lustre.attributes["capacity_gib"] == 12000
    bucket = next(r for r in inv.resources if r.resource_type == "urn:doe-iri:resource:storage:object")
    assert inv.access_endpoints[bucket.id][0].protocol.value == "s3"
    inference = next(r for r in inv.resources if r.resource_type == "urn:doe-iri:resource:service:inference")
    assert inference.attributes["inference_apis"] == ["urn:doe-iri:service:inference-api:openai"]


@pytest.mark.asyncio
async def test_sites_and_ids_are_stable(runtime):
    inv1 = await runtime.inventory(refresh=True)
    inv2 = await runtime.inventory(refresh=True)
    assert [r.id for r in inv1.resources] == [r.id for r in inv2.resources]
    lab_site = next(s for s in inv1.sites if s.short_name == "PW-LAB")
    lab = next(r for r in inv1.resources if r.name == "Lab cluster")
    assert lab.id in lab_site.resource_ids
    assert lab.site_uri.endswith(f"/facility/sites/{lab_site.id}")


@pytest.mark.asyncio
async def test_status_transitions_create_events_and_incidents(runtime):
    inv = await runtime.inventory(refresh=True)
    lab = next(r for r in inv.resources if r.name == "Lab cluster")
    runtime.client.f["clusters"][0]["status"] = "failed"
    await runtime.inventory(refresh=True)
    incidents = [i for i in runtime.ledger.incidents if lab.id in i.resource_ids]
    assert incidents and incidents[-1].resolution.value == "unresolved" and incidents[-1].type.value == "unplanned"
    runtime.client.f["clusters"][0]["status"] = "active"
    await runtime.inventory(refresh=True)
    assert incidents[-1].resolution.value == "completed" and incidents[-1].end is not None
    assert len([e for e in runtime.ledger.events if e.resource_id == lab.id]) >= 3


@pytest.mark.asyncio
async def test_execution_clusters_restrict_endpoints(runtime):
    runtime.config.inventory.execution_clusters = ["labcluster"]
    inv = await runtime.inventory(refresh=True)
    lab = next(r for r in inv.resources if r.name == "Lab cluster")
    aws = next(r for r in inv.resources if r.name == "Public Cloud (AWS)")
    assert lab.supported_endpoints and aws.supported_endpoints == []
    runtime.config.inventory.execution_clusters = []


def test_routing_executor_picks_by_cluster():
    from activate_iri.executor import ExecIdentity, RoutingExecutor

    class Tag:
        def __init__(self, name): self.name = name
        async def run(self, identity, script, cwd=None, stdin=None, timeout=300): return self.name

    r = RoutingExecutor(default=Tag("workflow"), local=Tag("local"), local_clusters=["labcluster"], ssh=Tag("ssh"), ssh_clusters=["neo-h100"])
    assert r.pick(ExecIdentity(posix_user="u", cluster_name="labcluster")).name == "local"
    assert r.pick(ExecIdentity(posix_user="u", cluster_name="neo-h100")).name == "ssh"
    assert r.pick(ExecIdentity(posix_user="u", cluster_name="awssmall")).name == "workflow"


def test_caller_credential_travels_with_identity(runtime):
    from app.types.user import User

    from activate_iri.auth import is_activate_credential
    assert is_activate_credential("pwt_ZGVtbw.not-a-credential") and not is_activate_credential("12345")
    ident = runtime.identities.resolve(User(id="jane.doe", name="jane.doe", api_key="pwt_ZGVtbw.not-a-credential"), {"name": "awssmall", "id": "x"})
    assert ident.credential == "pwt_ZGVtbw.not-a-credential" and ident.cluster_name == "awssmall"
    ident = runtime.identities.resolve(User(id="gtorok", name="gtorok", api_key="12345"), {"name": "awssmall", "id": "x"})
    assert ident.credential is None
