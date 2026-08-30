# Architecture

How activate-iri exposes Parallel Works ACTIVATE as a DOE IRI Facility API v2 facility, routes work to connected systems, consolidates other facilities under one endpoint, and lets ACTIVATE users call IRI facilities. Diagrams are Mermaid and render on GitHub; the plan in `INTEGRATION_PLAN.md` explains why these shapes were chosen.

## 1. The two modes and the gateway

```mermaid
flowchart TB
    AMSC["AmSC orchestrator<br/>AmSC-IRO, AmSCROT, Airflow"]
    USER["ACTIVATE user<br/>iri-job workflow, iri command"]
    EP["activate-iri endpoint<br/>one IRI Facility API v2 for everything ACTIVATE reaches,<br/>with other facilities consolidated behind it"]
    SYS["Systems ACTIVATE reaches, no IRI software on them<br/>lab clusters, existing and NeoCloud clusters,<br/>elastic cloud Slurm, partner sites via the edge kit"]
    LABS["Facilities with their own IRI endpoints<br/>ALCF, NERSC, OLCF, ESnet"]

    AMSC -- "mode 1: AmSC calls in<br/>Keycard or ACTIVATE credential" --> EP
    USER -- "mode 2: users call any facility<br/>through one URL" --> EP
    USER -. "or directly" .-> LABS
    EP -- "jobs and file operations run through the ACTIVATE API<br/>workflow runs, SSH, or local" --> SYS
    EP -- "gateway: forwarded with the caller's facility token" --> LABS
```

Mode 1 (outbound): AmSC and its orchestrator reach many ACTIVATE-connected systems through one endpoint, using the ACTIVATE API rather than facility-side software. Mode 2 (inbound): ACTIVATE users reach IRI facilities from their own account. Gateway mode: the same endpoint also fronts the lab facilities, so a client needs one base URL.

## 2. Request path inside the endpoint

```mermaid
flowchart TB
    REQ["HTTP request<br/>/api/v2/domain/..."]
    FW["DOE reference framework<br/>routers, models, error handling,<br/>AmSC Keycard validation, idempotency, OTel"]
    AUTH["ActivateAuthMixin<br/>get_current_user / get_current_user_amsc"]
    ADP["Domain adapters<br/>facility, status, account,<br/>compute, filesystem, storage, task"]
    RT["Runtime (process singleton)<br/>settings, ACTIVATE client, inventory cache,<br/>status ledger, executor, task queue, gateway"]
    INV["Inventory builder<br/>ACTIVATE objects to IRI resource graph"]
    GW["Gateway<br/>upstream facilities merged and forwarded"]
    EX["RoutingExecutor<br/>local | ssh | workflow"]
    TQ["Task queue<br/>async filesystem operations"]
    ACT["ACTIVATE REST API<br/>/api/clusters, allocations,<br/>managed-clusters, buckets, ..."]

    REQ --> FW --> AUTH --> ADP
    ADP --> RT
    RT --> INV --> ACT
    RT --> GW
    RT --> EX
    RT --> TQ --> ADP
```

The framework owns the contract; the adapters own the mapping to ACTIVATE. Every domain adapter is loaded through `IRI_API_ADAPTER_<domain>`, so a facility can replace one domain at a time.

## 3. Identity

```mermaid
flowchart LR
    C1["AmSC Keycard<br/>(PingAM JWT, amsc_project_context)"]
    C2["ACTIVATE API key or platform JWT"]
    C3["Facility token header<br/>X-IRI-Facility-Token-facility<br/>(gateway forwarding only)"]

    V1["Framework validates:<br/>JWKS signature, iss, exact aud, exp,<br/>sub and amsc_project_context"]
    V2["Endpoint verifies with<br/>GET /api/auth/whoami"]

    M1["Mapping file<br/>project context to ACTIVATE user,<br/>POSIX account, allocation to charge"]
    ID["ExecIdentity<br/>posix_user, host, platform_user,<br/>cluster, credential"]

    C1 --> V1 --> M1 --> ID
    C2 --> V2 --> ID
    C3 -. "token map or env var<br/>IRI_TOKEN_FACILITY" .-> GWF["Gateway forward"]
    ID --> EXEC["Executor runs the command<br/>as posix_user on the cluster"]
```

An unmapped AmSC project is a 401, as the framework requires. In the single-account configuration every run uses the endpoint's service credential; a caller who presents an ACTIVATE credential carries it in `ExecIdentity.credential`, and the workflow executor sets `PW_API_KEY` from it so the run lands in that caller's account. That is the per-user extension, already wired.

## 4. Job submission and status

```mermaid
sequenceDiagram
    autonumber
    participant O as AmSC-IRO / client
    participant E as activate-iri endpoint
    participant R as RoutingExecutor
    participant P as ACTIVATE platform
    participant L as Cluster login node
    participant S as Slurm / PBS

    O->>E: POST /compute/job/{resource_id} (PSI/J JobSpec, Idempotency-Key)
    E->>E: resolve cluster, scheduler, ExecIdentity
    E->>E: render batch script from JobSpec
    E->>R: run(submit script)
    alt cluster is local to this host
        R->>L: sudo -u user bash -l (local)
    else cluster reached over SSH
        R->>L: ssh with per-request CA certificate
    else any other cluster the account can see
        R->>P: pw workflows run iri-exec (PW_API_KEY)
        P->>L: workflow step on the login node
    end
    L->>S: sbatch --parsable / qsub
    S-->>L: job id
    L-->>R: __IRI_BEGIN__ job id __IRI_END__ rc=0
    R-->>E: CommandResult
    E-->>O: Job{id, status: queued, meta_data}

    O->>E: GET /compute/status/{resource_id}/{job_id}?historical=true
    E->>R: run(status script)
    R->>L: squeue, then sacct, then scontrol show job, then rc file
    L-->>E: first authoritative answer
    E-->>O: Job{status: queued | active | completed | failed}
```

Status falls back through four sources because many clusters have no Slurm accounting database: the live queue, `sacct`, the controller's job memory (`scontrol`), and an exit-code file the batch script writes beside its submission. On a terminal state with elapsed time available, node-hours are posted to the caller's ACTIVATE allocation as a usage event.

## 5. Filesystem task loop

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant E as Endpoint (filesystem router)
    participant Q as Task queue
    participant F as FilesystemAdapter
    participant X as Executor
    participant L as Cluster

    C->>E: POST /filesystem/ls/{resource_id} {path}
    E->>Q: enqueue TaskCommand(router=filesystem, command=ls, args)
    E-->>C: 200 {task_id, task_uri}
    Q->>F: on_task -> ls(resource, user, path, ...)
    F->>X: run("ls -l --time-style=... -- path")
    X->>L: as the mapped user (local, ssh, or iri-exec)
    L-->>X: listing
    X-->>F: stdout
    F-->>Q: GetDirectoryLsResponse
    C->>E: GET /task/{task_id}
    E-->>C: {status: completed, result: {output: [File...]}}
```

Every FirecREST-style operation is one shell script with a parser; destructive operations refuse relative paths, parent references, and protected roots before anything reaches the shell.

## 6. Routing decision

```mermaid
flowchart TD
    A["Command for cluster X"] --> B{"X in<br/>ACTIVATE_IRI_LOCAL_CLUSTERS?"}
    B -- yes --> L["LocalExecutor<br/>sudo -n -u user, or direct"]
    B -- no --> C{"X in<br/>ACTIVATE_IRI_SSH_CLUSTERS?"}
    C -- yes --> S["SSHExecutor<br/>ssh-keygen -s CA, 5 minute certificate<br/>principal = mapped user"]
    C -- no --> W["WorkflowExecutor<br/>pw workflows run iri-exec<br/>base64 script + stdin<br/>wait for __IRI_END__ marker"]
    L & S & W --> R["CommandResult<br/>rc, stdout, stderr"]
```

`ACTIVATE_IRI_EXECUTOR=auto` builds this router; `local`, `ssh`, or `workflow` selects one executor for everything.

## 7. Gateway mode

```mermaid
flowchart LR
    subgraph EP["activate-iri endpoint (gateway on)"]
        INV["Own inventory<br/>ACTIVATE clusters, storage, AI gateway"]
        MERGE["merge_into<br/>Sites, resources, capabilities, incidents<br/>ids namespaced facility:upstream_id"]
        SPLIT["split_id on every call<br/>alcf:55c1... -> (alcf, 55c1...)"]
        FWD["forward_json / filesystem<br/>Bearer = facility token"]
    end
    U1["ALCF v1"]:::up
    U2["NERSC v2"]:::up
    U3["OLCF open"]:::up
    U4["ESnet East"]:::up
    T["Token resolution<br/>1 request header X-IRI-Facility-Token-f<br/>2 token file (facility -> token or env name)<br/>3 IRI_TOKEN_FACILITY"]

    U1 & U2 & U3 & U4 -- "public groups, cached" --> MERGE
    INV --> MERGE
    SPLIT --> FWD
    T --> FWD
    FWD -- "compute, filesystem (task loop), account" --> U1 & U2 & U3 & U4
    classDef up fill:#eef,stroke:#88a
```

Discovery of upstream facilities needs no token. Forwarded calls need the caller's facility token; a missing token is a 401 naming the facility. Resources without a namespace are ACTIVATE's own and never leave the endpoint.

## 8. Mode 2: calling facilities from an ACTIVATE account

```mermaid
sequenceDiagram
    autonumber
    participant U as ACTIVATE user
    participant W as iri-job workflow / iri command
    participant F as IRI facility (ALCF, NERSC, PW endpoint)

    U->>W: run with facility URL, resource, PSI/J form, token variable
    W->>F: GET /compute/resources (resolve resource by name)
    W->>F: POST /compute/job/{rid} (Idempotency-Key = run id)
    loop until terminal
        W->>F: GET /compute/status/{rid}/{job}
    end
    W->>F: POST /filesystem/download/{rid} {stdout path}
    W->>F: GET /task/{id} until completed
    W-->>U: stdout in the run log; job id in XCom-style outputs
```

The same steps work for v1 facilities: the `iri` command switches filesystem verbs (v1 GET/PUT/DELETE with query parameters, v2 POST bodies) based on the base URL.

## 9. Edge kit: a facility with nothing deployed

```mermaid
flowchart LR
    subgraph Site["Partner site (no inbound ports)"]
        LN["Login node"]
        AG["pw agent<br/>(systemd, outbound 443)"]
        CT["activate-iri container<br/>edge mode, host network,<br/>scheduler binaries mounted"]
        TUN["pw endpoints http --public --keep<br/>(reverse tunnel)"]
        SCH["Slurm or PBS"]
        LN --- AG & CT & TUN
        CT --> SCH
    end
    subgraph PW["ACTIVATE"]
        CP["Control plane<br/>partitions, jobs, node metrics,<br/>user/group/ssh-key/sudo sync"]
        URL["https://name.activate.pw/api/v2"]
    end
    AmSC["AmSC / validators"]
    AG -- heartbeat --> CP
    CP -- access management --> AG
    TUN -- registers --> URL
    AmSC -- IRI v2 --> URL --> TUN --> CT
```

`deploy/edge/install-edge.sh` does all of it on one login node. The unauthenticated groups (facility, status, capabilities) are public by specification; the authenticated groups validate their own bearer tokens.

## 10. Resource model

```mermaid
classDiagram
    class Facility { id; name; short_name; organization_name; support_uri; site_uris }
    class Site { id; name; operating_organization; locality; lat; lon; resource_uris }
    class Resource { id; name; resource_type URN; current_status; supported_endpoints; capability_uris; attributes }
    class Capability { id; name; units }
    class Project { id; name; user_ids }
    class ProjectAllocation { id; entries }
    class UserAllocation { id; user_id; entries }
    class Incident { id; status; type; resolution; start; end; resource_uris }
    class Event { id; occurred_at; status; resource_uri; incident_uri }
    Facility "1" --> "*" Site
    Site "1" --> "*" Resource
    Resource "*" --> "*" Capability
    Project "1" --> "*" ProjectAllocation
    ProjectAllocation "1" --> "1" Capability
    ProjectAllocation "1" --> "*" UserAllocation
    Incident "1" --> "*" Event
    Event "*" --> "1" Resource
```

| ACTIVATE object | IRI object | Type URN |
|---|---|---|
| Cluster (any type) | Resource | `urn:doe-iri:resource:compute:system` |
| Managed-cluster partition | Capability | node-hours; GPU partitions separately |
| Node-reported filesystem, Lustre, NFS | Resource pair | `storage:filesystem` and `storage:mount` |
| Bucket | Resource plus AccessEndpoint | `storage:object` |
| AI chat provider | Resource | `service:inference` (OpenAI API) |
| Allocation | Project, ProjectAllocation, UserAllocation | node-hours or `allocation:ext:pw:credits` |
| Cluster status change, platform alert | Event, Incident | |

## 11. Status ledger

```mermaid
stateDiagram-v2
    [*] --> Unobserved
    Unobserved --> up: first observation
    Unobserved --> down: first observation
    up --> degraded: provisioning, updating, updateFailed
    up --> down: stopped, failed, agent offline
    degraded --> up: recovered
    down --> up: recovered
    degraded --> down
    note right of down
        Transition from up creates an Event and opens
        an unplanned Incident on the resource.
        Return to up closes it with resolution completed.
    end note
```

Platform alerts and maintenance mode add planned incidents across every published resource. The ledger is in-process for the prototype; a multi-replica deployment persists it.

## 12. Facilities topology dashboard

```mermaid
flowchart LR
    A["aggregate.py<br/>every 5 minutes"] -- "facility, sites, resources,<br/>incidents, openapi" --> F1["ALCF"] & F2["NERSC"] & F3["ESnet E/W"] & F4["OLCF open/moderate"] & F5["PW endpoint"]
    A --> J["web/api/topology<br/>(status-monitor graph format)<br/>monitor, site, system nodes<br/>edges: monitor->site, illustrative links"]
    J --> V["topology viewer<br/>(HPC Status Monitor, unchanged)<br/>geo, hierarchy, radial, force, lanes, load"]
    V --> S["pw endpoints serve<br/>https://iri-topology.activate.pw/"]
```

A facility counts as connected when its API answered the sweep; link speed is the HTTP round trip. Inter-site links are illustrative until a measured source exists.
