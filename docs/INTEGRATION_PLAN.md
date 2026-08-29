# ACTIVATE in the DOE Integrated Research Infrastructure

Integration plan for Parallel Works ACTIVATE (commercial platform and the High Security Platform) with the American Science Cloud, the DOE IRI Facility API v2, and the Genesis Mission platform. Version 0.2, August 29, 2026.

## 1. Overview

The American Science Cloud (AmSC) federates DOE facilities behind one API, one identity, and one orchestrator, and its public onboarding recipe for a new compute resource is to register the site with the Resource Catalog, support the AmSC API endpoints, integrate with AmSC federated identity, and deploy AmSC-reachable services. Cloud and industry compute enter the same way. A facility that wants in must stand up an IRI Facility API endpoint, validate AmSC Keycards, and map AmSC projects to local accounts; a lab or company with a cluster and no software team has no short path to that.

Parallel Works closes both gaps with one adapter package that exposes ACTIVATE as an IRI v2 facility. In federation mode it publishes every cloud, NeoCloud, and connected on-premises cluster ACTIVATE operates as one facility with one Site per provider region or lab. In edge mode the same container runs on the login node of a single existing cluster, connected through the outbound-only PW agent and published through the platform's reverse tunnel, so a facility with nothing deployed gets a conformant endpoint in a day. For AmSC the effect is one Keycard-validating endpoint that reaches hyperscale, NeoCloud, and partner on-premises capacity, plus a repeatable onboarding kit for partner sites. For ACTIVATE users it is the reverse path: ALCF, NERSC, OLCF, and ESnet reachable from platform workflows.

The reference implementation in this repository serves all 47 operations of the IRI v2 specification through the DOE reference framework, passes the DOE spec-compliance check (47 present, 0 missing), passes an 18-test lifecycle suite, and runs as a live prototype against the ACTIVATE platform where a smoke test submits a Slurm job and exercises the asynchronous filesystem loop end to end.

## 2. Context

The IRI Facility API is the common interface the DOE ASCR facilities and AmSC use to reach compute, storage, and status programmatically. Version 1.0 is the accepted production contract (43 operations across facility, status, account, compute, filesystem, and task). Version 2.0 (draft, August 2026) has 47 operations: the same groups plus storage locations and access endpoints and a whoami call, every filesystem operation as an asynchronous POST returning a task, an Idempotency-Key on job submission, resource types and allocation units as registered URNs, an attributes object governed by Resource Definition Profiles, and hypermedia link relations. The Python reference implementation at HEAD (August 21, 2026) is v2 and includes AmSC Keycard validation, idempotency stores, gateway-aware URL generation, and OpenTelemetry. The ALCF facility API repository is a v1 snapshot from April 2026; NERSC serves both versions.

Live endpoints today: ALCF (v1; Polaris, Aurora, Sophia, Crux, and an inference service; PBS Pro job submission on Polaris and Crux), NERSC (v1 and v2), ESnet East and West (hosted on AWS: preemptible compute, EFS storage, a Globus DTN), and OLCF open and moderate enclaves (facility, status, and compute groups). AmSC's product catalog dates the IRI-API, federated identity, and MVP releases to October 1, 2026; its Model Access Gateway (LiteLLM, OpenAI-compatible) reached v1.0 on July 22, 2026. The Genesis Mission Request for Applications selected 278 projects on July 22, 2026, with Phase II awards expected in September 2026 and Phase II applications from FY26 Phase I awards due December 17, 2026. The AmSC orchestration toolkit (AmSCROT, published as doe-iri/iri-facility-api-toolkit) discovers facilities, submits PSI/J jobs, fetches output through the filesystem task loop, and ships Airflow operators, so any conformant endpoint is usable by AmSC orchestration without facility-specific client code.

## 3. Integration surfaces

Two modes carry the integration. Outbound, one PW endpoint exposes many ACTIVATE-connected systems and clusters that host no IRI software themselves, using the ACTIVATE API to reach them: the endpoint reads inventory from the platform and executes each scheduler or filesystem command on the target cluster as an ACTIVATE workflow run under the caller's own ACTIVATE credential (or through SSH, or locally on the login node in edge mode). Inbound, users call IRI facilities from inside their ACTIVATE account through the iri-job workflow and the `iri` command in the workspace. Surfaces 1 and 2 are the outbound mode; surfaces 3 and 4 are the inbound mode.

| Surface | Direction | What it is | Facility precondition | Code |
|---|---|---|---|---|
| 1. Federation endpoint | AmSC calls PW | One endpoint publishes every ACTIVATE cluster, storage system, and AI gateway provider selected for publication, grouped into Sites | None at the facility; ACTIVATE already reaches the cluster | activate-iri (federation mode) |
| 2. Edge endpoint | AmSC calls a partner site | The same container on one login node; the PW agent connects the cluster, pw endpoints publishes the API | A Linux login node with outbound TCP 443 | activate-iri (edge mode), deploy/edge |
| 3. IRI consumer | PW calls facilities | ACTIVATE workflow that submits PSI/J jobs to any IRI facility and returns output; cross-facility status board | Facility runs an IRI endpoint | activate-iri-connector |
| 4. Orchestration and AI | AmSC-IRO and MAG | AmSCROT profile for the PW endpoint (no code) and an optional native ACTIVATE service client; the ACTIVATE AI gateway published as an IRI inference service | AmSC registry entry | amscrot-activate |

```
                 AmSC control plane                                 Parallel Works
   +--------------------------------------------+      +------------------------------------------+
   |  API gateway -> orchestration (AmSC-IRO)    |      |  ACTIVATE control plane                  |
   |  federated identity (Keycard)               |      |   clusters, allocations, AI gateway,     |
   |  Resource Catalog / Model Access Gateway    |      |   workflows, agent heartbeat, SCIM       |
   +----------------+---------------------------+      +---------+------------------+-------------+
                    | Keycard (aud = endpoint URL)                |                  |
                    v                                             v                  v
   +------------------------------+   +-------------------------------+   +--------------------------+
   | Surface 1: federation        |   | Surface 2: edge endpoint      |   | Surface 3: consumer      |
   | activate-iri beside the      |   | activate-iri on login node    |   | iri-job workflow, board  |
   | control plane                |   | local executor (sudo -u)      |   | -> ALCF / NERSC / OLCF   |
   +------+-----------+----------+   +-------------+-----------------+   +--------------------------+
          |           |                            |
          v           v                            v
   cloud Slurm   NeoCloud / existing        partner Slurm or PBS cluster
   (AWS, Azure,  clusters (agent)           (agent + reverse tunnel, no inbound)
    GCP, OCI)
```

### 3.1 Federation endpoint

The endpoint runs beside the ACTIVATE control plane and reads the platform through a service account: the cluster list for inventory, managed-cluster detail for partitions and node-reported filesystems, buckets, Lustre, and NFS for storage, AI providers for inference, allocations for accounting, and platform alerts and maintenance mode for incidents. An operator selects what to publish by tag or name allowlist and assigns clusters to Sites by rule (CSP and region, cluster type, tag, or name). Elastic cloud clusters that are provisionable but idle are advertised as up with the configured node count set to the cluster maximum, because a job submission provisions them; a site that prefers the conservative reading reports them as unknown. Clusters the endpoint cannot execute on are published for discovery and status only.

For AmSC this is one facility whose Sites happen to be in different providers, the shape AmSC already uses for a multi-site laboratory. It complements per-CSP IRI translation layers: those expose a provider's native services; this endpoint exposes ACTIVATE-operated Slurm and PBS systems on those providers, together with the NeoCloud and on-premises clusters a per-CSP layer does not cover, under one identity and one allocation model.

### 3.2 Edge endpoint

The IRI reference framework needs a Python service with facility logic behind it; the DOE demo adapter is a starter kit. For a lab or industry partner with a cluster and no integration team, the edge kit supplies the whole stack. `deploy/edge/install-edge.sh`, run once on a login node, registers the node with ACTIVATE (`pw agent register --systemd`, outbound HTTPS and WebSocket on port 443), installs the activate-iri container in edge mode with the scheduler client binaries mounted in, installs a sudoers rule scoped to the service account, and publishes the API at `https://<name>.activate.pw` through `pw endpoints http --public --keep`. The facility, status, and capabilities groups are public by specification; the authenticated groups validate Keycards or ACTIVATE credentials themselves, so the public tunnel exposes nothing the specification does not already require to be public. The agent's heartbeat also carries user, group, SSH-key, and sudo state to the node, so the cluster gains the account provisioning that "run as the calling user" needs without facility-side identity work. Removal is a systemctl disable and a delete in ACTIVATE.

### 3.3 IRI consumer

The consumer half needs no platform change. In the workspace terminal, `iri facilities`, `iri resources <facility>`, `iri submit <facility> <resource> --exe ... --wait --fetch`, and `iri ls <facility> <resource> <path>` call any v1 or v2 facility with tokens held as account variables. The iri-job workflow takes a facility base URL, a compute resource, and a PSI/J form, submits with an Idempotency-Key tied to the workflow run, polls to a terminal state, and fetches stdout through the filesystem task loop. The facility bearer token is an account variable, granted once. Because it is an ordinary ACTIVATE workflow it composes: one DAG can stage data on a PW cloud cluster, run the large job on Polaris, and post-process on a NeoCloud GPU node. The cross-facility board lists resources, status, and open incidents across every reachable endpoint using the public groups only. A later increment adds an ACTIVATE resource type for IRI facilities so that Polaris or Perlmutter appears in the Compute list with live status and allocations.

### 3.4 Orchestration and the AI gateway

AmSC-IRO reaches the PW endpoint through an AmSCROT credential profile of type AMSC_IRI and the PW base URL. The optional `amscrot-activate` service client adds what the IRI contract does not carry: provider and region choice, cluster sizing for the job, and ACTIVATE allocation balances, through the same discover, plan, create, status, destroy lifecycle, so a Session can mix an ACTIVATE job with IRI jobs at lab facilities. The ACTIVATE AI gateway (OpenAI-compatible, allocation-bound keys with budget caps, per-user attribution) is published as `urn:doe-iri:resource:service:inference` with the OpenAI API URN and endpoint list, matching the v2 inference profile and the ALCF inference-service resource; the Model Access Gateway can route to it as one more provider.

### 3.5 Consolidated gateway

A third shape follows from the two modes: one endpoint that fronts every IRI facility a user can reach, the "proxy IRI server" of the IRI deployment models. In gateway mode the endpoint reads the public groups of each configured upstream facility (ALCF, NERSC, OLCF, ESnet, other PW endpoints), merges their Sites, resources, capabilities, and open incidents into its own facility under namespaced identifiers (`alcf:<upstream id>`), and forwards compute, filesystem, and account calls on a namespaced resource to the upstream with the caller's facility token. ACTIVATE's own clusters keep the normal path. An AmSC orchestrator, an Airflow DAG, or the `iri` command then needs one base URL and one credential map instead of one per facility. The prototype runs in this mode with ALCF, NERSC, ESnet East, and OLCF as upstreams for discovery; authenticated forwarding is exercised against a fake upstream in the test suite and turns on per facility when a token is configured.

Product direction: today a facility is registered through the endpoint's facility file. The platform equivalent of what `pw endpoints http --openai` does for model servers is an IRI endpoint type on connected systems: `pw endpoints http --iri` registers a local IRI facility endpoint with the platform, the platform keeps the registry, and the consolidated gateway builds its upstream list from it. The same registry backs the topology dashboard and the account-level view of facilities.

## 4. Resource model

| ACTIVATE object | IRI object | Type URN | Profile attributes populated |
|---|---|---|---|
| Organization plus facility file | Facility | | name, short name, organization, support URI |
| Provider region or lab (rule in facility file) | Site | | operating organization, locality, coordinates |
| Cluster of any type | Resource | `urn:doe-iri:resource:compute:system` | schema_version, system_capabilities, configured node, core, and memory counts, vendor, product, version |
| Managed-cluster partitions | Capability | node-hours | CPU and GPU partitions become separate capabilities |
| Node-reported filesystems; Lustre and NFS resources | Resource pair | `storage:filesystem` and `storage:mount` | scope, technology, protocol, tier, capacity_gib, mount_path; mount is mounted-on the cluster |
| Bucket | Resource plus AccessEndpoint | `storage:object` | object API s3, object technology; S3 endpoint |
| AI chat provider | Resource | `service:inference` | inference_apis openai, inference_endpoints, served_models |
| Allocation (top level and per capability) | Project, ProjectAllocation, UserAllocation | node-hours or `urn:doe-iri:allocation:ext:pw:credits` | total, used, estimated usage |
| Cluster status transition | Event; unplanned Incident on down or degraded, closed on recovery | | |
| Platform alert, maintenance mode | Planned Incident across published resources | | |

Identifiers are UUID5 values derived from ACTIVATE ids, stable across restarts and usable in AmSC Resource Cards. Two items need registry action with the IRI Interfaces subcommittee: an authority code (pw) so that facility-local URNs are valid extensions, and an allocation unit for cloud consumption (credits or currency; GPU-hours would serve NeoCloud clusters better than node-hours).

## 5. Identity

Three credential classes arrive at the same endpoint. AmSC Keycards are validated by the framework: JWKS signature against the AmSC issuer, exact audience match to the endpoint's own URL, expiry, and the presence of `sub` and `amsc_project_context`. The adapter maps the project context to an ACTIVATE user, a POSIX account, and the ACTIVATE allocation to charge, from the mapping file the framework already defines; an unmapped project is a 401. The account charged in ACTIVATE is the one named in the Keycard. ACTIVATE API keys and platform JWTs are the facility-specific credential the IRI milestones ask for, verified against the platform. Federated login for people runs the other way: ACTIVATE registers the AmSC identity provider as an organization OIDC auth method (issuer discovery, JIT user creation, claim mapping), so an AmSC user logs into ACTIVATE with the AmSC Passport.

POSIX identity is the ACTIVATE username by default, because the platform provisions the same accounts on managed clusters through the agent, with a per-user and per-cluster override file. In federation mode the endpoint reaches the cluster as that user over SSH with a certificate signed per request by an SSH certificate authority (five-minute validity, principal set to the mapped user), the FirecREST pattern; the managed-cluster access management distributes the CA public key. Sites that prefer not to run a CA use the workflow executor, which carries the script inside an audited ACTIVATE workflow run and needs no SSH plumbing. In edge mode the command runs through `sudo -n -u` under a rule scoped to the service account. No user secret is stored in the endpoint under any of the three.

## 6. Execution model

Every compute and filesystem operation is one short shell script executed as the mapped user; the executor decides where it runs (local, SSH, or workflow). Scheduler translation renders the PSI/J JobSpec into a Slurm or PBS Pro batch script (nodes, tasks per node, cores and GPUs per task, memory, walltime, partition or queue, account, reservation, custom directives, containers through Apptainer with GPU passthrough, environment, stdin and stdout paths, pre- and post-launch commands, launcher), submits it, reads status from the live queue, then the accounting database, then the controller's job memory, then an exit-code file the batch script records beside the submission, so status survives on clusters without a Slurm accounting database. A shell fallback covers SSH-only systems. Filesystem operations are the FirecREST set, queued through the task domain and executed by an in-process worker, with a Redis-backed queue for multi-replica deployments. Destructive operations refuse relative paths, parent references, and protected roots before anything reaches the shell.

Two loops close the integration on the ACTIVATE side. A status ledger turns cluster status changes into IRI events and incidents and platform alerts into planned incidents. An accounting hook posts node-hours for finished jobs as usage events against the caller's ACTIVATE allocation, so the AmSC allocation view and ACTIVATE budget enforcement agree. OpenTelemetry traces and metrics come from the framework and are forwarded to the AmSC operations collector by configuration.

## 7. Facility readiness matrix

| Starting point | Surface | What the site does | Time to first job |
|---|---|---|---|
| Facility already runs an IRI endpoint (ALCF, NERSC, OLCF, ESnet) | 3, and 1 if it also wants ACTIVATE sessions and workflows on the system | Nothing for 3; for 1, connect the cluster to ACTIVATE over SSH or install the agent | Same day |
| Cluster already connected to ACTIVATE (agent or SSH) | 1 | Add the cluster to the publication list and assign a Site | Same day |
| Cluster with nothing deployed (lab, university, industry partner) | 2 | Run `install-edge.sh` on a login node; supply the AmSC project mapping | One day |
| ACTIVATE cloud cluster on AWS, Azure, GCP, or OCI | 1 | Publish and assign; elastic provisioning on submit | Same day |
| NeoCloud GPU cluster operated by PW | 1 | Publish and assign as a NeoCloud Site | Same day |
| CUI or controlled workloads | 1 on the High Security Platform (section 8) | Separate endpoint host on the IL5 boundary | Later phase |

## 8. The High Security Platform as a secure-enclave facility

AmSC's first release is an open enclave; a secure enclave with separate ingress and identity is planned, and the IRI roadmap's stretch deliverable for late 2026 extends the API to secure-enclave resource discovery, allocations, and job submission. OLCF already runs separate open and moderate IRI hosts. The High Security Platform is an ACTIVATE deployment operated in GovCloud regions under a Department of Defense authorization to operate at NIST SP 800-53 Rev 5 High with a DoD IL5 provisional authorization; it runs the same code base as the commercial platform, so the same container applies with a second facility file and a second hostname on the IL5 boundary. The AmSC identity provider is FedRAMP High, so Keycard validation carries over unchanged.

The HSP endpoint is scheduled as a later-phase design item, for three reasons. The AmSC secure enclave and its identity stack are not yet built. Exposing an accredited boundary to a new external caller is a change the Authorizing Official governs and the sponsor must direct. The IRI v2 roadmap lists CUI capabilities for resources as an attribute still to be defined, and the most useful contribution now is to propose that vocabulary through the registry process, with the HSP as the first non-laboratory implementation when the secure enclave opens. NeoCloud providers cannot be published on the HSP endpoint because none offers an IL5 IaaS package today.

## 9. Conformance and verification status

Verified on August 29, 2026 against the DOE reference framework at HEAD:

1. All 47 operationIds in the official IRI v2 OpenAPI are served (plus whoami). The DOE `api-validator.py --checkspeccompliance` run reports 47 present, 0 missing, 1 extra.
2. 18 tests pass: inventory mapping with profile attributes and stable ids, elastic cloud status, storage and inference publication, publication allowlists, status transitions producing events and incidents, Slurm and PBS script generation and status parsing including the accounting-free fallbacks, POSIX parsers and path guards, and an HTTP lifecycle through the framework covering discovery, credential checks and AmSC project mapping, projects and allocations, an asynchronous filesystem sequence, a job submitted, polled to completion, and listed, and storage locations per user.
3. A live prototype (see `prototype/`) runs against the ACTIVATE platform and passes a 13-step smoke test that submits a Slurm job on a managed cluster, polls it queued to active to completed, retrieves its stdout through the filesystem task loop, and lists historical jobs.
4. The DOE Schemathesis behavioural run against the live prototype (dummy token, the practice of the doe-iri validation matrix) passes the public groups; findings on the authenticated groups are being worked through and recorded in `prototype/NOTES.md`.

Still to do: a live Keycard test once the endpoint audience is registered with AmSC, registration in the doe-iri validation matrix, and the performance targets (100 concurrent sessions, 1,000 queries per second, 90 percent of responses under 200 ms) measured on the deployed service.

## 10. Phased plan

Phase 0, engagement (September 2026). Share this design and the prototype endpoint with the AmSC partner-integration and IRI-integration teams and request the intake meeting their integration rubric prescribes, with an ordered list: federation endpoint (fills the NeoCloud and on-premises gap), edge kit (fills the partner-onboarding gap), AI gateway as inference service, secure-enclave design. Ask for Keycard audience registration for the endpoint hostname, the identity provider issuer and discovery URL, a test project context, and an orchestrator registry entry. Join the Genesis Mission Consortium HPC and Cloud Infrastructure working group.

Phase 1, federation endpoint in production on the commercial platform (September to October 2026). Deploy in federation mode on the platform's Kubernetes tier with the SSH-CA executor, Redis idempotency, and OTLP forwarding; publish an initial Site set (cloud regions, one NeoCloud cluster, the PW lab cluster); complete Keycard validation with a test token issued by AmSC; run the DOE Schemathesis suite to a clean pass; add the AmSC identity provider as an ACTIVATE OIDC auth method; ship the AmSCROT credential profile and the iri-job workflow; demonstrate the end-to-end loop from AmSCROT and from an Airflow DAG; submit the pw authority code and the cloud allocation unit to the IRI registry. About 0.5 FTE engineering for six weeks plus platform operations.

Phase 2, edge kit pilot and secure-enclave design (November to mid-December 2026). Pilot the edge kit at one partner site with no endpoint and measure time to first job; land the accounting loop and the cross-facility board in the ACTIVATE UI as an IRI facility resource type; draft the CUI resource attribute proposal for the IRI registry and the HSP secure-enclave endpoint design for sponsor review; join a Genesis Phase II team as the federation and access partner with the Phase 1 endpoint as the working demonstration.

Phase 3, orchestration depth (2027). Upstream the ACTIVATE AmSCROT service client for elastic placement, add Kubernetes services when the IRI container domain is specified, integrate ESnet L3VPN or cloud interconnects for the data plane where a Site warrants it, and publish an MCP server over the endpoint aligned with the IRI agentic discovery flow.

## 11. Risks and decisions

| Item | Assessment | Handling |
|---|---|---|
| v1 versus v2 | The validation matrix validates v1, the reference is v2, NERSC serves both | Serve v2; publish a v1 image from the ALCF snapshot if a consumer needs it |
| Keycard audience must equal the endpoint URL | Registration by the AmSC identity team is a prerequisite for any Tier 1 test | Request early; use the facility-credential path until then |
| URN authority and allocation units | Unregistered pw extensions validate but are not registered | Submit to the registry in Phase 1; publish node-hours where the unit permits |
| SSH CA versus sudo at partner sites | Site security policy decides | Both executors ship; the workflow executor needs neither |
| Per-CSP IRI layers built by the AmSC CSP integration team | Overlap perception | Position the endpoint as ACTIVATE-operated systems on those providers plus NeoCloud and on-premises, and route provider-native questions to that team |
| Long-lived credentials for the endpoint | Platform JWTs expire in 24 hours | Use a service API key from the ACTIVATE UI for any deployment beyond a demonstration |

## Appendix A. Code map

| Path | Purpose |
|---|---|
| `activate-iri/activate_iri/config.py` | Settings and the facility file (sites, publication rules, allocation units) |
| `activate-iri/activate_iri/activate_client.py` | ACTIVATE REST client (Basic for API keys, Bearer for JWTs) and a fixture-backed fake |
| `activate-iri/activate_iri/inventory.py` | ACTIVATE to IRI resource graph, capabilities, storage locations, status ledger |
| `activate-iri/activate_iri/auth.py` | Facility credential check, AmSC project mapping, POSIX identity resolution |
| `activate-iri/activate_iri/executor.py` | Local, SSH with per-request certificates, and workflow executors |
| `activate-iri/activate_iri/schedulers.py` | PSI/J to Slurm and PBS Pro scripts, status and cancel parsing with accounting-free fallbacks |
| `activate-iri/activate_iri/posix.py` | Filesystem operations as scripts, parsers, and path guards |
| `activate-iri/activate_iri/{facility,status,account,compute,filesystem,storage,task}.py` | The seven IRI v2 domain adapters |
| `activate-iri/activate_iri/asgi.py` | ASGI entry point with the root `/openapi.json` alias the DOE validator expects and route-aware 405 handling |
| `activate-iri/activate_iri/gateway.py` | Gateway mode: upstream IRI facilities merged under this endpoint with namespaced ids and token-forwarded calls |
| `activate-iri/activate_iri/executor.py` (`RoutingExecutor`) | Per-cluster routing between local, SSH, and ACTIVATE workflow execution; caller credential pass-through |
| `activate-iri/deploy/edge`, `deploy/federation` | Edge install script and systemd units; iri-exec workflow and Kubernetes manifest |
| `activate-iri/tests` | 18 tests, `make venv && make test` |
| `activate-iri-connector` | iri-job ACTIVATE workflow; cross-facility standboard |
| `amscrot-activate` | AmSCROT ServiceClient for ACTIVATE |
| `prototype` | Live prototype configuration, run and publish scripts, smoke test, conformance reports |
| `iri-topology` | Facilities topology dashboard |

## Appendix B. IRI v2 operation coverage

| Group | Operations | Status |
|---|---|---|
| facility | getFacility, getSites, getSite | Implemented from the facility file and Site rules |
| status | getResources, getResource, getIncidents, getIncident, getEventsByIncident, getEventByIncident | Implemented from ACTIVATE inventory and the status ledger |
| account | getCapabilities, getCapability, whoami, getProjects, getProject, project and user allocation reads | Implemented from ACTIVATE allocations |
| compute | getComputeResources, launchJob, updateJob, getJob, getJobs, cancelJob | Implemented for Slurm, PBS Pro, and shell; updateJob returns 501 |
| filesystem | 18 operations plus getFilesystemResources | Implemented through the task queue and executor |
| storage | getStorageLocations, getStorageAccessEndpoints | Implemented from cluster mounts and buckets |
| task | getTasks, getTask, deleteTask | Implemented |

Public sources: energy.gov Genesis Mission pages; amsc.energy.gov product catalog and documentation; the doe-iri GitHub organization (iri-facility-api-docs, iri-facility-api-python, iri-facility-api-demo-adapter, iri-facility-api-toolkit, iri-facility-api-examples); argonne-lcf/alcf-facility-api; the live facility OpenAPI documents; parallelworks.com documentation.
