"""Runtime settings (environment) and the facility description file (YAML).

The facility file is the only place where PW staff describe *what* the endpoint publishes:
which ACTIVATE clusters become IRI compute resources, how they group into Sites, and how
ACTIVATE allocations are presented. Everything else is discovered from the platform.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml
from pydantic import BaseModel, Field

ALL_CLUSTER_TYPES = ["aws-slurm", "azure-slurm", "google-slurm", "openstack-slurm", "managed-cluster", "existing"]


class SiteMatch(BaseModel):
    """Rules that assign an ACTIVATE cluster to a Site. Any non-empty list is an OR within the
    field; different fields are ANDed. An empty rule set never matches (use default_site)."""

    csp: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)

    def matches(self, cluster: dict) -> bool:
        checks = []
        if self.csp:
            checks.append((cluster.get("csp") or "") in self.csp)
        if self.types:
            checks.append(cluster.get("type") in self.types)
        if self.names:
            checks.append(cluster.get("name") in self.names)
        if self.tags:
            checks.append(bool(set(cluster.get("tags") or []) & set(self.tags)))
        if self.regions:
            checks.append((cluster.get("region") or "") in self.regions)
        return bool(checks) and all(checks)


class SiteConfig(BaseModel):
    id: str
    name: str
    short_name: str | None = None
    description: str | None = None
    operating_organization: str | None = None
    country_name: str | None = "United States"
    locality_name: str | None = None
    state_or_province_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    match: SiteMatch = Field(default_factory=SiteMatch)


class FacilityInfo(BaseModel):
    id: str
    name: str
    short_name: str | None = None
    description: str | None = None
    organization_name: str | None = "Parallel Works, Inc."
    support_uri: str | None = None


class InventoryConfig(BaseModel):
    include_cluster_types: list[str] = Field(default_factory=lambda: list(ALL_CLUSTER_TYPES))
    exclude_names: list[str] = Field(default_factory=list)
    # Allowlist of cluster names to publish (empty = all that pass the other filters).
    include_names: list[str] = Field(default_factory=list)
    # Allowlist of storage resource names (buckets, lustre, nfs) to publish (empty = all).
    storage_names: list[str] = Field(default_factory=list)
    require_tag: str | None = None
    # Clusters this endpoint can run commands on through its executor. Empty means all published clusters.
    # Others are published for discovery and status only (no compute or filesystem endpoints).
    execution_clusters: list[str] = Field(default_factory=list)
    # How a cloud cluster that is provisionable but currently "off" is reported. ACTIVATE cloud
    # clusters are elastic: "off" means no nodes are billed right now, not that the system is
    # unavailable. "up" advertises on-demand availability; "unknown" is the conservative choice.
    elastic_status: str = "up"
    publish_storage: bool = True
    publish_inference: bool = True
    gpu_partition_patterns: list[str] = Field(default_factory=lambda: ["gpu", "a100", "h100", "h200", "b200", "l40", "mi300"])
    cache_ttl_seconds: int = 30


class AllocationConfig(BaseModel):
    # ACTIVATE allocation units are free text. Units whose name matches one of these are
    # published as urn:doe-iri:allocation:compute:node-hours; anything else is published under a
    # facility-local URN that still validates (domain "allocation") and is flagged for registry submission.
    node_hour_units: list[str] = Field(default_factory=lambda: ["node-hour", "node-hours", "node_hours", "nodehour", "nodehours"])
    local_unit_urn: str = "urn:doe-iri:allocation:ext:pw:credits"
    usage_sku: str | None = "SLURM_NODE_HOUR"


class FacilityConfig(BaseModel):
    facility: FacilityInfo
    sites: list[SiteConfig]
    default_site: str
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)
    allocation: AllocationConfig = Field(default_factory=AllocationConfig)
    # Gateway mode: upstream IRI facilities consolidated under this endpoint (see gateway.py).
    gateway: dict | None = None

    def site_for(self, cluster: dict) -> SiteConfig:
        for site in self.sites:
            if site.match.matches(cluster):
                return site
        for site in self.sites:
            if site.id == self.default_site:
                return site
        raise ValueError(f"default_site {self.default_site!r} is not defined in sites")


def load_facility_config(path: str) -> FacilityConfig:
    with open(path, encoding="utf-8") as fh:
        return FacilityConfig.model_validate(yaml.safe_load(fh))


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    """Process-level settings. Read once at import by runtime.get_runtime()."""

    mode: str = field(default_factory=lambda: _env("ACTIVATE_IRI_MODE", "federation"))  # federation | edge
    activate_host: str = field(default_factory=lambda: _env("ACTIVATE_HOST", "https://activate.parallel.works"))
    activate_api_key: str | None = field(default_factory=lambda: _env("ACTIVATE_SERVICE_API_KEY"))
    activate_organization: str | None = field(default_factory=lambda: _env("ACTIVATE_ORGANIZATION"))
    facility_file: str = field(default_factory=lambda: _env("ACTIVATE_IRI_FACILITY_FILE", "facility.yaml"))
    fixtures_file: str | None = field(default_factory=lambda: _env("ACTIVATE_IRI_FIXTURES"))  # offline demo/test mode
    # edge mode: the single cluster this endpoint fronts (must match the ACTIVATE cluster name)
    edge_cluster: str | None = field(default_factory=lambda: _env("ACTIVATE_IRI_EDGE_CLUSTER"))
    # executor selection: local (edge), ssh (federation default), workflow (federation fallback)
    executor: str = field(default_factory=lambda: _env("ACTIVATE_IRI_EXECUTOR", ""))  # local | ssh | workflow | auto
    # auto mode: clusters served by the local executor (this host) and by SSH; the rest use workflow runs
    local_clusters: list[str] = field(default_factory=lambda: [c for c in (_env("ACTIVATE_IRI_LOCAL_CLUSTERS", "") or "").split(",") if c])
    ssh_clusters: list[str] = field(default_factory=lambda: [c for c in (_env("ACTIVATE_IRI_SSH_CLUSTERS", "") or "").split(",") if c])
    local_run_as: str = field(default_factory=lambda: _env("ACTIVATE_IRI_LOCAL_RUN_AS", "sudo"))  # sudo | direct
    ssh_key: str | None = field(default_factory=lambda: _env("ACTIVATE_IRI_SSH_KEY"))
    ssh_ca_key: str | None = field(default_factory=lambda: _env("ACTIVATE_IRI_SSH_CA_KEY"))
    ssh_jump: str | None = field(default_factory=lambda: _env("ACTIVATE_IRI_SSH_JUMP"))
    ssh_known_hosts: str | None = field(default_factory=lambda: _env("ACTIVATE_IRI_SSH_KNOWN_HOSTS"))
    pw_bin: str = field(default_factory=lambda: _env("ACTIVATE_IRI_PW_BIN", "pw"))
    exec_workflow: str = field(default_factory=lambda: _env("ACTIVATE_IRI_EXEC_WORKFLOW", "iri-exec"))
    user_map_file: str | None = field(default_factory=lambda: _env("ACTIVATE_IRI_USER_MAP_FILE"))
    amsc_mapping_file: str | None = field(default_factory=lambda: _env("AMSC_PROJECT_MAPPING_FILE"))
    task_ttl_seconds: int = field(default_factory=lambda: int(_env("ACTIVATE_IRI_TASK_TTL", "3600")))
    command_timeout: int = field(default_factory=lambda: int(_env("ACTIVATE_IRI_COMMAND_TIMEOUT", "300")))

    def resolved_executor(self) -> str:
        if self.executor:
            return self.executor
        return "local" if self.mode == "edge" else "ssh"
