# activate-iri

DOE IRI Facility API v2 adapters backed by Parallel Works ACTIVATE. The package plugs into the
DOE reference framework (`iri-facility-api-python`) through its `IRI_API_ADAPTER_<domain>`
mechanism and implements all seven domains: facility, status, account, compute, filesystem,
storage, task. AmSC Keycard validation, idempotent job submission, OpenTelemetry, and the
API-gateway forwarding headers come from the framework unchanged.

## Two deployment shapes, one code path

| | Federation mode | Edge mode |
|---|---|---|
| Where it runs | Beside the ACTIVATE control plane (Kubernetes) | On the login node of one existing cluster |
| What it publishes | Every tagged cluster, storage system, and AI gateway provider ACTIVATE federates, grouped into Sites | That one cluster |
| Reaches the cluster via | SSH with per-request CA-signed certificates (`SSHExecutor`), or an audited ACTIVATE workflow run (`WorkflowExecutor`) | `sudo -n -u <user>` on the node (`LocalExecutor`) |
| Network requirements at the facility | None (ACTIVATE already reaches the cluster) | None: the PW agent dials out; `pw endpoints` publishes the API through a reverse tunnel |
| Typical user | Parallel Works as an AmSC Infrastructure Partner | A lab or industry partner with a Slurm or PBS cluster and no IRI endpoint yet |

The compute and filesystem adapters render every operation as a short shell script executed as
the mapped POSIX user; the executor decides where that script runs. Scheduler translation covers
Slurm, PBS Pro, and a shell fallback for SSH-only systems.

## Resource model

| ACTIVATE object | IRI resource | Profile attributes |
|---|---|---|
| Cluster (cloud, NeoCloud, managed, existing) | `urn:doe-iri:resource:compute:system` | system capabilities, configured node/core/memory counts, vendor, product |
| Managed-cluster partitions | Capability (node-hours), GPU partitions separately | partition list |
| Node-reported filesystems, ACTIVATE Lustre and NFS | `storage:filesystem` plus `storage:mount` (`mounted-on` the cluster) | technology, protocol, tier, capacity |
| Bucket | `storage:object` with an S3 access endpoint | object API and technology |
| AI gateway provider | `service:inference` | OpenAI-compatible API, endpoint, served models |
| Allocation | Project, ProjectAllocation, UserAllocation | node-hours or `urn:doe-iri:allocation:ext:pw:credits` |
| Cluster status transitions, platform alerts, maintenance mode | Events and Incidents | |

Elastic cloud clusters that are provisionable but idle are advertised as `up` (configurable), with
`configured_node_count` set to the cluster's maximum; submitting a job triggers provisioning.

## Identity

1. AmSC Keycard: validated by the framework (`AMSC_TOKEN_*`), then `amsc_project_context` is
   mapped to an ACTIVATE user, POSIX account, and allocation via `AMSC_PROJECT_MAPPING_FILE`.
2. ACTIVATE API key or platform JWT: the facility-specific credential; verified with `whoami`.
3. POSIX identity defaults to the ACTIVATE username (the platform provisions the same accounts
   on managed clusters); `ACTIVATE_IRI_USER_MAP_FILE` overrides per user or per cluster.

## Quick start (no ACTIVATE account needed)

    make venv && make test        # 15 tests: mapping, schedulers, POSIX parsers, full HTTP lifecycle
    make demo                     # serves http://127.0.0.1:8000/api/v2 from examples/fixtures.json
    curl http://127.0.0.1:8000/api/v2/status/resources | jq '.[].name'
    curl -H 'Authorization: Bearer 12345' http://127.0.0.1:8000/api/v2/account/projects

Against a real platform, set `ACTIVATE_HOST`, `ACTIVATE_SERVICE_API_KEY`, `ACTIVATE_ORGANIZATION`,
tag the clusters to publish with `iri`, and run `activate-iri inventory` to review the graph before
serving. `deploy/edge/install-edge.sh` and `deploy/federation/` carry the two deployment recipes.

## Environment variables

| Variable | Purpose |
|---|---|
| `ACTIVATE_IRI_MODE` | `federation` (default) or `edge` |
| `ACTIVATE_IRI_EXECUTOR` | `ssh`, `local`, or `workflow` (defaults: ssh in federation, local in edge) |
| `ACTIVATE_IRI_EDGE_CLUSTER` | edge mode: the ACTIVATE cluster name this node belongs to |
| `ACTIVATE_HOST`, `ACTIVATE_SERVICE_API_KEY`, `ACTIVATE_ORGANIZATION` | platform access for inventory and accounting |
| `ACTIVATE_IRI_FACILITY_FILE` | facility, sites, and publication rules (see `examples/`) |
| `ACTIVATE_IRI_SSH_CA_KEY`, `ACTIVATE_IRI_SSH_KEY`, `ACTIVATE_IRI_SSH_JUMP` | SSH executor credentials |
| `ACTIVATE_IRI_LOCAL_RUN_AS` | edge mode: `sudo` (default) or `direct` |
| `ACTIVATE_IRI_USER_MAP_FILE`, `AMSC_PROJECT_MAPPING_FILE` | identity mapping files |
| `ACTIVATE_IRI_FIXTURES` | offline mode backed by a JSON fixture |
| `IRI_IDEMPOTENCY_STORE` | `activate_iri.idempotency.InMemoryIdempotencyStore` for one replica; Redis store for many |

Framework variables (`API_URL_ROOT`, `AMSC_*`, `OPENTELEMETRY_ENABLED`, `OTLP_ENDPOINT`) are
documented in the upstream README.
