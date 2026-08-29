"""Build the IRI resource graph (Facility, Sites, Resources, Capabilities, Incidents, Events)
from what ACTIVATE knows about its federated clusters, storage, and AI gateway.

Mapping summary (details in the integration plan, section "Resource model"):

  ACTIVATE cluster (any type)          -> urn:doe-iri:resource:compute:system   (+ compute/system profile attributes)
  managed-cluster partition            -> Capability (node-hours), GPU partitions get their own capability
  managed-cluster node filesystems     -> urn:doe-iri:resource:storage:filesystem + storage:mount (mounted-on the cluster)
  ACTIVATE lustre / nfs                -> urn:doe-iri:resource:storage:filesystem (attached clusters -> mounts)
  ACTIVATE bucket                      -> urn:doe-iri:resource:storage:object (S3 access endpoint)
  ACTIVATE AI provider (OpenAI-compat) -> urn:doe-iri:resource:service:inference
  cluster status transitions           -> Event; down/failed -> unplanned Incident; platform alerts -> planned Incident

Identifiers are stable UUID5 values derived from ACTIVATE ids so that AmSC Resource Cards and
IRO caches survive endpoint restarts.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from app.routers.facility import models as facility_models
from app.routers.status import models as status_models
from app.routers.storage import models as storage_models
from app.types.models import Capability
from app.types.scalars import AllocationUnit, ResourceType

from .config import FacilityConfig

logger = logging.getLogger(__name__)

NS = uuid.UUID("6f1c3e2a-9b8d-4c1e-a5f0-1d2e3f4a5b6c")  # activate-iri namespace


def stable_id(kind: str, *parts: str) -> str:
    return str(uuid.uuid5(NS, f"{kind}:" + ":".join(parts)))


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


UP, DOWN, DEGRADED, UNKNOWN = status_models.Status.up, status_models.Status.down, status_models.Status.degraded, status_models.Status.unknown
CLOUD_TYPES = {"aws-slurm", "azure-slurm", "google-slurm", "openstack-slurm"}


def cluster_status(cluster: dict, elastic_status: str) -> status_models.Status:
    """Translate an ACTIVATE cluster status into the IRI four-state model."""
    s = (cluster.get("status") or "").lower()
    if cluster.get("updateFailed") or s == "failed":
        return DOWN if s == "failed" else DEGRADED
    if s in ("active", "on"):
        return UP
    if s in ("provisioning", "resuming", "updating", "connecting"):
        return DEGRADED
    if s in ("stopping", "stopped", "deleted"):
        return DOWN
    if s == "off":
        if cluster.get("type") in CLOUD_TYPES:
            return status_models.Status(elastic_status)
        return DOWN  # an existing or managed cluster with no agent heartbeat
    return UNKNOWN


def is_gpu_partition(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(p in lowered for p in patterns)


@dataclass
class Inventory:
    facility: facility_models.Facility
    sites: list[facility_models.Site]
    resources: list[status_models.Resource]
    capabilities: dict[str, Capability]
    clusters_by_resource: dict[str, dict]           # resource id -> ACTIVATE cluster dict (with detail merged)
    capability_resource: dict[str, str]             # capability id -> compute resource id
    storage_locations: dict[str, list[storage_models.StorageInstance]] = field(default_factory=dict)
    access_endpoints: dict[str, list[storage_models.AccessEndpoint]] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)

    def resource(self, resource_id: str) -> status_models.Resource | None:
        return next((r for r in self.resources if r.id == resource_id), None)

    def cluster_for(self, resource_id: str) -> dict | None:
        cluster = self.clusters_by_resource.get(resource_id)
        if cluster:
            return cluster
        # storage mounts and filesystems resolve to the cluster they are mounted on
        res = self.resource(resource_id)
        if res is None:
            return None
        related = getattr(res, "related_resource_ids", None) or res.get_extra("related_resource_ids", {}) or {}
        for target in related.get("mounted-on", []):
            if target in self.clusters_by_resource:
                return self.clusters_by_resource[target]
        return None


class StatusLedger:
    """Remembers the last observed status per resource and emits Events and Incidents on change.

    In-process by design for the testbed. A production deployment persists this (Redis or the
    platform's own alert store) so that history survives restarts and replicas agree.
    """

    def __init__(self):
        self.last: dict[str, status_models.Status] = {}
        self.events: list[status_models.Event] = []
        self.incidents: list[status_models.Incident] = []

    def observe(self, resource: status_models.Resource) -> None:
        prev = self.last.get(resource.id)
        cur = resource.current_status or UNKNOWN
        if prev == cur:
            return
        self.last[resource.id] = cur
        stamp = now()
        event = status_models.Event(
            id=stable_id("event", resource.id, stamp.isoformat()), name=f"{resource.name} is {cur.value}",
            description=f"Observed status change {prev.value if prev else 'none'} -> {cur.value} via ACTIVATE",
            last_modified=stamp, occurred_at=stamp, status=cur, resource_id=resource.id,
        )
        open_incident = next((i for i in self.incidents if resource.id in i.resource_ids and i.resolution == status_models.Resolution.unresolved), None)
        if cur in (DOWN, DEGRADED) and prev is not None and open_incident is None:
            incident = status_models.Incident(
                id=stable_id("incident", resource.id, stamp.isoformat()), name=f"{resource.name} {cur.value}",
                description="Unplanned status change reported by the ACTIVATE control plane", last_modified=stamp,
                status=cur, resource_ids=[resource.id], event_ids=[event.id], start=stamp, type=status_models.IncidentType.unplanned,
                resolution=status_models.Resolution.unresolved,
            )
            self.incidents.append(incident)
            event.incident_id = incident.id
        elif cur == UP and open_incident is not None:
            open_incident.end = stamp
            open_incident.resolution = status_models.Resolution.completed
            open_incident.status = UP
            open_incident.last_modified = stamp
            open_incident.event_ids.append(event.id)
            event.incident_id = open_incident.id
        self.events.append(event)

    def planned_from_alerts(self, alerts: list[dict], resource_ids: list[str]) -> None:
        for alert in alerts:
            iid = stable_id("alert", str(alert.get("id")))
            if any(i.id == iid for i in self.incidents):
                continue
            created = alert.get("createdAt") or now().isoformat()
            self.incidents.append(status_models.Incident(
                id=iid, name=alert.get("title") or "Platform notice", description=alert.get("message"),
                last_modified=alert.get("updatedAt") or created, status=DEGRADED, resource_ids=list(resource_ids),
                start=created, type=status_models.IncidentType.planned, resolution=status_models.Resolution.pending,
            ))


class InventoryBuilder:
    def __init__(self, config: FacilityConfig, ledger: StatusLedger | None = None):
        self.config = config
        self.ledger = ledger or StatusLedger()

    async def build(self, client, organization: str | None, service_user: str | None = None, only_cluster: str | None = None) -> Inventory:
        cfg, inv = self.config, self.config.inventory
        sites: dict[str, facility_models.Site] = {}
        for s in cfg.sites:
            sites[s.id] = facility_models.Site(
                id=stable_id("site", s.id), name=s.name, description=s.description, last_modified=now(), short_name=s.short_name,
                operating_organization=s.operating_organization, country_name=s.country_name, locality_name=s.locality_name,
                state_or_province_name=s.state_or_province_name, latitude=s.latitude, longitude=s.longitude, resource_ids=[],
            )
        resources: list[status_models.Resource] = []
        capabilities: dict[str, Capability] = {}
        clusters_by_resource: dict[str, dict] = {}
        capability_resource: dict[str, str] = {}
        locations: dict[str, list[storage_models.StorageInstance]] = {}
        endpoints: dict[str, list[storage_models.AccessEndpoint]] = {}

        clusters = await client.clusters()
        for cluster in clusters:
            if only_cluster and cluster.get("name") != only_cluster:
                continue
            if cluster.get("type") not in inv.include_cluster_types or cluster.get("name") in inv.exclude_names:
                continue
            if inv.include_names and cluster.get("name") not in inv.include_names:
                continue
            if inv.require_tag and inv.require_tag not in (cluster.get("tags") or []):
                continue
            detail = {}
            if cluster.get("type") == "managed-cluster" and organization:
                try:
                    detail = await client.managed_cluster(organization, cluster["name"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("managed cluster detail unavailable for %s: %s", cluster.get("name"), exc)
                    detail = {}
            merged = {**cluster, **{k: v for k, v in detail.items() if k not in cluster or not cluster.get(k)}}
            merged["_detail"] = detail
            site_cfg = cfg.site_for(cluster)
            site = sites[site_cfg.id]
            rid = stable_id("cluster", cluster.get("id") or cluster["name"])
            caps = self._capabilities(rid, merged, capabilities)
            for cap_id in caps:
                capability_resource[cap_id] = rid
            resource = status_models.Resource(
                id=rid, site_id=site.id, name=cluster.get("displayName") or cluster["name"],
                description=self._describe(merged), last_modified=now(), group=self._group(merged),
                current_status=cluster_status(merged, inv.elastic_status), resource_type=ResourceType.compute_system,
                supported_endpoints=self._endpoints_for(cluster), capability_ids=list(caps), attributes=self._compute_attributes(merged),
            )
            resources.append(resource)
            site.resource_ids.append(rid)
            clusters_by_resource[rid] = merged
            for fs_res in self._node_filesystems(rid, merged, site.id):
                resources.append(fs_res)
                site.resource_ids.append(fs_res.id)
            locations[rid] = self._locations(merged)

        if inv.publish_storage:
            for kind, items in (("lustre", await client.lustre()), ("nfs", await client.nfs())):
                for item in items:
                    if inv.storage_names and item.get("name") not in inv.storage_names:
                        continue
                    res, mounts = self._shared_filesystem(kind, item, clusters_by_resource, sites, cfg)
                    resources.append(res)
                    sites[res.site_id and self._site_key(sites, res.site_id)].resource_ids.append(res.id)
                    resources.extend(mounts)
            for bucket in await client.buckets():
                if inv.storage_names and bucket.get("name") not in inv.storage_names:
                    continue
                res, ep = self._bucket(bucket, sites, cfg)
                resources.append(res)
                sites[self._site_key(sites, res.site_id)].resource_ids.append(res.id)
                endpoints[res.id] = [ep]

        if inv.publish_inference and organization and service_user:
            try:
                providers = await client.aichat_providers(organization, service_user)
            except Exception as exc:  # noqa: BLE001
                logger.warning("AI provider inventory unavailable: %s", exc)
                providers = []
            for provider in providers:
                res = self._inference(provider, sites, cfg, client.host)
                resources.append(res)
                sites[self._site_key(sites, res.site_id)].resource_ids.append(res.id)

        for res in resources:
            self.ledger.observe(res)
        try:
            self.ledger.planned_from_alerts(await client.alerts(), [r.id for r in resources])
            settings = await client.settings()
            if settings.get("maintenanceMode"):
                self.ledger.planned_from_alerts([{"id": "maintenance", "title": "Platform maintenance",
                                                  "message": settings.get("maintenanceMessage") or "Maintenance mode is on"}],
                                                [r.id for r in resources])
        except Exception as exc:  # noqa: BLE001
            logger.warning("platform alerts or settings unavailable: %s", exc)

        facility = facility_models.Facility(
            id=stable_id("facility", cfg.facility.id), name=cfg.facility.name, short_name=cfg.facility.short_name,
            description=cfg.facility.description, last_modified=now(), organization_name=cfg.facility.organization_name,
            support_uri=cfg.facility.support_uri, site_ids=[s.id for s in sites.values()],
        )
        return Inventory(facility=facility, sites=list(sites.values()), resources=resources, capabilities=capabilities,
                         clusters_by_resource=clusters_by_resource, capability_resource=capability_resource,
                         storage_locations=locations, access_endpoints=endpoints)

    # -- helpers ------------------------------------------------------------------------
    def _endpoints_for(self, cluster: dict) -> list:
        allowed = self.config.inventory.execution_clusters
        if allowed and cluster.get("name") not in allowed:
            return []
        return [status_models.Endpoint.compute, status_models.Endpoint.filesystem]

    @staticmethod
    def _site_key(sites: dict, site_id: str) -> str:
        for key, site in sites.items():
            if site.id == site_id:
                return key
        return next(iter(sites))

    @staticmethod
    def _group(cluster: dict) -> str:
        t = cluster.get("type") or ""
        if t in CLOUD_TYPES:
            return f"cloud-{cluster.get('csp') or t.split('-')[0]}"
        return "on-premises"

    @staticmethod
    def _describe(cluster: dict) -> str:
        t = cluster.get("type") or "cluster"
        sched = cluster.get("schedulerType") or "no scheduler"
        where = f"{cluster.get('csp')} {cluster.get('region') or ''}".strip() if cluster.get("csp") else "existing on-premises system connected through the ACTIVATE agent"
        elastic = " Elastic: nodes are provisioned on demand up to maxNodes." if t in CLOUD_TYPES else ""
        return (cluster.get("description") or f"ACTIVATE {t} ({sched}) on {where}.") + elastic

    def _compute_attributes(self, cluster: dict) -> dict:
        detail = cluster.get("_detail") or {}
        caps = ["urn:doe-iri:compute:system-capability:container-execution", "urn:doe-iri:compute:system-capability:interactive-access"]
        if cluster.get("schedulerType"):
            caps.insert(0, "urn:doe-iri:compute:system-capability:batch-scheduling")
        partitions = detail.get("partitions") or []
        if any(is_gpu_partition(p.get("name", ""), self.config.inventory.gpu_partition_patterns) for p in partitions) or \
           any(t.lower() in ("gpu", "a100", "h100", "h200", "b200") for t in (cluster.get("tags") or [])):
            caps.append("urn:doe-iri:compute:system-capability:accelerator-support")
        attrs: dict = {"schema_version": "1.0.0", "system_capabilities": caps}
        nodes = detail.get("nodes") or []
        if cluster.get("maxNodes"):
            attrs["configured_node_count"] = int(cluster["maxNodes"])
        cores = sum(int((n.get("systemInfo") or {}).get("cpuCores") or 0) for n in nodes)
        mem = sum(int((n.get("systemInfo") or {}).get("memoryTotal") or 0) for n in nodes)
        if cores:
            attrs["configured_cpu_core_count"] = cores
        if mem:
            attrs["configured_memory_gib"] = int(mem // (1024 ** 3))
        attrs["vendor"] = {"aws": "Amazon Web Services", "google": "Google Cloud", "azure": "Microsoft Azure", "openstack": "OpenStack"}.get(cluster.get("csp") or "", "Parallel Works")
        attrs["product"] = f"ACTIVATE {cluster.get('type')}"
        if cluster.get("agentVersion"):
            attrs["version"] = str(cluster["agentVersion"])
        return attrs

    def _capabilities(self, rid: str, cluster: dict, capabilities: dict[str, Capability]) -> list[str]:
        detail = cluster.get("_detail") or {}
        partitions = [p.get("name") for p in (detail.get("partitions") or []) if p.get("name")]
        ids = []
        if not partitions:
            cap_id = stable_id("capability", rid, "nodes")
            capabilities[cap_id] = Capability(id=cap_id, name=f"{cluster.get('displayName') or cluster['name']} nodes",
                                              description="Node-hours on this system", last_modified=now(), units=[AllocationUnit.node_hours])
            return [cap_id]
        gpu, cpu = [], []
        for p in partitions:
            (gpu if is_gpu_partition(p, self.config.inventory.gpu_partition_patterns) else cpu).append(p)
        for label, parts in (("CPU", cpu), ("GPU", gpu)):
            if not parts:
                continue
            cap_id = stable_id("capability", rid, label)
            capabilities[cap_id] = Capability(id=cap_id, name=f"{cluster.get('displayName') or cluster['name']} {label} nodes",
                                              description=f"Partitions: {', '.join(parts)}", last_modified=now(), units=[AllocationUnit.node_hours],
                                              attributes={"partitions": parts})
            ids.append(cap_id)
        return ids

    def _node_filesystems(self, rid: str, cluster: dict, site_id: str) -> list[status_models.Resource]:
        """Managed-cluster agents report every mounted filesystem; publish the shared ones."""
        detail = cluster.get("_detail") or {}
        seen: dict[str, dict] = {}
        for node in detail.get("nodes") or []:
            for fs in node.get("filesystems") or []:
                mp = fs.get("mountpoint") or ""
                if not mp or mp in ("/", "/boot", "/boot/efi") or mp.startswith(("/run", "/sys", "/proc", "/dev", "/snap", "/var/lib")):
                    continue
                seen.setdefault(mp, fs)
        out = []
        for mp, fs in seen.items():
            tech = (fs.get("fstype") or "").lower()
            fs_id = stable_id("filesystem", rid, mp)
            mount_id = stable_id("mount", rid, mp)
            attrs = {"schema_version": "1.0.0", "filesystem_scope": "urn:doe-iri:storage:filesystem-scope:network" if tech in ("lustre", "nfs", "nfs4", "gpfs", "beegfs", "cephfs") else "urn:doe-iri:storage:filesystem-scope:local"}
            if tech in ("lustre", "beegfs", "cephfs"):
                attrs["filesystem_technology"] = f"urn:doe-iri:storage:filesystem-technology:{tech}"
            if tech in ("nfs", "nfs4"):
                attrs["filesystem_protocols"] = ["urn:doe-iri:storage:filesystem-protocol:nfs"]
            if fs.get("total"):
                attrs["capacity_gib"] = int(int(fs["total"]) // (1024 ** 3))
            if any(k in mp for k in ("scratch", "tmp")):
                attrs["tier"] = "urn:doe-iri:storage:tier:scratch"
            elif "home" in mp:
                attrs["tier"] = "urn:doe-iri:storage:tier:home"
            elif any(k in mp for k in ("project", "work", "data", "shared")):
                attrs["tier"] = "urn:doe-iri:storage:tier:project"
            out.append(status_models.Resource(
                id=fs_id, site_id=site_id, name=f"{cluster['name']} {mp}", description=f"{tech or 'filesystem'} at {mp} reported by the cluster agent",
                last_modified=now(), group=self._group(cluster), current_status=UP, resource_type=ResourceType.storage_filesystem,
                supported_endpoints=[status_models.Endpoint.filesystem], attributes=attrs, related_resource_ids={"has-mount": [mount_id]},
            ))
            out.append(status_models.Resource(
                id=mount_id, site_id=site_id, name=f"{cluster['name']} mount {mp}", description=f"Exposure of {mp} on {cluster['name']}",
                last_modified=now(), group=self._group(cluster), current_status=UP, resource_type=ResourceType.storage_mount,
                attributes={"schema_version": "1.0.0", "mount_path": mp, "access_mode": "urn:doe-iri:storage:mount-access-mode:read-write"},
                related_resource_ids={"mounted-on": [rid]},
            ))
        return out

    def _shared_filesystem(self, kind: str, item: dict, clusters_by_resource: dict, sites: dict, cfg: FacilityConfig):
        site = sites[cfg.site_for(item).id]
        fs_id = stable_id(kind, item.get("id") or item["name"])
        attrs = {"schema_version": "1.0.0", "filesystem_scope": "urn:doe-iri:storage:filesystem-scope:network"}
        if kind == "lustre":
            attrs["filesystem_technology"] = "urn:doe-iri:storage:filesystem-technology:lustre"
            attrs["filesystem_capabilities"] = ["urn:doe-iri:storage:filesystem-capability:parallel-io"]
        else:
            attrs["filesystem_protocols"] = ["urn:doe-iri:storage:filesystem-protocol:nfs"]
        if item.get("sizeGb"):
            attrs["capacity_gib"] = int(item["sizeGb"])
        attached = [a for a in (item.get("attachedTo") or []) if a.get("type") == "cluster"]
        mounts = []
        for att in attached:
            target_rid = next((rid for rid, c in clusters_by_resource.items() if c.get("name") == att.get("name") or c.get("id") == att.get("id")), None)
            if not target_rid:
                continue
            mount_id = stable_id("mount", fs_id, target_rid)
            mounts.append(status_models.Resource(
                id=mount_id, site_id=site.id, name=f"{item['name']} on {att.get('name')}", description=f"{kind} {item['name']} mounted on {att.get('name')}",
                last_modified=now(), group="storage", current_status=UP if item.get("provisioned", True) else DOWN, resource_type=ResourceType.storage_mount,
                attributes={"schema_version": "1.0.0", "mount_path": item.get("mountPoint") or f"/{item['name']}", "access_mode": "urn:doe-iri:storage:mount-access-mode:read-write"},
                related_resource_ids={"mounted-on": [target_rid]},
            ))
        res = status_models.Resource(
            id=fs_id, site_id=site.id, name=item.get("displayName") or item["name"], description=item.get("description") or f"ACTIVATE {kind} filesystem",
            last_modified=now(), group="storage", current_status=UP if item.get("provisioned", True) and not item.get("provisioningError") else DOWN,
            resource_type=ResourceType.storage_filesystem, attributes=attrs, related_resource_ids={"has-mount": [m.id for m in mounts]},
        )
        return res, mounts

    def _bucket(self, bucket: dict, sites: dict, cfg: FacilityConfig):
        site = sites[cfg.site_for(bucket).id]
        rid = stable_id("bucket", bucket.get("id") or bucket["name"])
        csp = (bucket.get("csp") or "").lower()
        tech = {"aws": "amazon-s3"}.get(csp)
        attrs = {"schema_version": "1.0.0", "object_apis": ["urn:doe-iri:storage:object-api:s3"]}
        if tech:
            attrs["object_technology"] = f"urn:doe-iri:storage:object-technology:{tech}"
        res = status_models.Resource(
            id=rid, site_id=site.id, name=bucket.get("displayName") or bucket["name"], description=f"Object storage bucket {bucket.get('bucketName') or bucket['name']} ({csp})",
            last_modified=now(), group="storage", current_status=UP if (bucket.get("status") or "provisioned") in ("provisioned", "active", "on") else DEGRADED,
            resource_type="urn:doe-iri:resource:storage:object", attributes=attrs,
        )
        endpoint = storage_models.AccessEndpoint(
            id=stable_id("endpoint", rid), resource_id=rid, protocol=storage_models.AccessProtocol.s3, display_name=f"S3 access to {bucket['name']}",
            auth_type="activate-presigned-url", capabilities=[storage_models.AccessCapability.list, storage_models.AccessCapability.read, storage_models.AccessCapability.write],
            bucket=bucket.get("bucketName") or bucket["name"], region=bucket.get("region"),
        )
        return res, endpoint

    def _inference(self, provider: dict, sites: dict, cfg: FacilityConfig, host: str) -> status_models.Resource:
        site = sites[cfg.default_site]
        rid = stable_id("inference", provider.get("id") or provider["name"])
        tech = "vllm" if (provider.get("csp") or "") == "custom" else None
        attrs = {"schema_version": "1.0.0", "inference_apis": ["urn:doe-iri:service:inference-api:openai"],
                 "inference_endpoints": [{"url": f"{host}/api/ai/v1", "api": "urn:doe-iri:service:inference-api:openai", "name": provider["name"]}]}
        if tech:
            attrs["inference_technology"] = f"urn:doe-iri:service:inference-technology:{tech}"
        if provider.get("model"):
            attrs["served_models"] = [{"id": provider["model"], "name": provider["model"]}]
        return status_models.Resource(
            id=rid, site_id=site.id, name=provider.get("displayName") or provider["name"],
            description="ACTIVATE AI gateway provider (OpenAI-compatible, token-metered against ACTIVATE allocations)",
            last_modified=now(), group="ai-gateway", current_status=UP if provider.get("status") in (None, "provisioned", "active") else DEGRADED,
            resource_type="urn:doe-iri:resource:service:inference", attributes=attrs,
        )

    @staticmethod
    def _locations(cluster: dict) -> list[storage_models.StorageInstance]:
        """Logical storage tiers a user sees on this cluster. Paths are templates resolved per user."""
        rw = storage_models.AccessPermissions(read=True, write=True, execute=True)
        out = [storage_models.StorageInstance(logical_name=storage_models.LogicalName.home, path="/home/{user}", filesystem="home", performance_tier="medium", shared=False, access=rw)]
        detail = cluster.get("_detail") or {}
        mounts = {fs.get("mountpoint") for n in (detail.get("nodes") or []) for fs in (n.get("filesystems") or []) if fs.get("mountpoint")}
        for mp in sorted(mounts):
            if "scratch" in mp:
                out.append(storage_models.StorageInstance(logical_name=storage_models.LogicalName.scratch, path=f"{mp}/{{user}}", filesystem=mp, performance_tier="high", purge_policy_days=30, shared=False, access=rw))
            elif any(k in mp for k in ("project", "work", "shared", "data")):
                out.append(storage_models.StorageInstance(logical_name=storage_models.LogicalName.project, path=f"{mp}/{{project}}", filesystem=mp, performance_tier="high", shared=True, access=rw))
        if not any(i.logical_name == storage_models.LogicalName.scratch for i in out):
            out.append(storage_models.StorageInstance(logical_name=storage_models.LogicalName.scratch, path="/tmp/{user}", filesystem="local", performance_tier="low", purge_policy_days=7, shared=False, access=rw))
        return out


_TIME_RE = re.compile(r"^(\d+)-(\d+):(\d+):(\d+)$|^(\d+):(\d+):(\d+)$|^(\d+):(\d+)$|^(\d+)$")


def slurm_time_to_seconds(value: str | None) -> int | None:
    if not value or value in ("UNLIMITED", "infinite"):
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        return None
    g = m.groups()
    if g[0] is not None:
        return int(g[0]) * 86400 + int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3])
    if g[4] is not None:
        return int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6])
    if g[7] is not None:
        return int(g[7]) * 60 + int(g[8])
    return int(g[9]) * 60
