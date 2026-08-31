# Prototype notes

Living record of the running prototype. Update on every change.

## 2026-08-29: first deployment

Where it runs. The federation endpoint runs on the Parallel Works lab cluster controller as a detached process on 127.0.0.1:8100 (`run.sh`), reads the live ACTIVATE API with the pw CLI's current credential, and is published through the platform reverse tunnel at https://activate-iri.activate.pw/api/v2 (`publish.sh`, `--public` so the specification's unauthenticated groups are reachable by validators). Execution is enabled on the lab cluster only (`execution_clusters: [labcluster]`); three elastic cloud Slurm clusters and three storage systems are published for discovery and status. The local executor runs commands as the service user without sudo, which is acceptable for a single-operator prototype and not for a shared deployment (switch `ACTIVATE_IRI_LOCAL_RUN_AS=sudo` with the sudoers rule from `deploy/edge`, or the SSH-CA executor).

Verification.

| Check | Result |
|---|---|
| DOE spec compliance (`api-validator.py --checkspeccompliance` against `all_spec_v2.yaml`) | 47 present, 0 missing, 1 extra (whoami) |
| DOE Schemathesis behavioural run (dummy token, matrix practice, 10 examples per operation) | 48 passed, 1 skipped, 0 failed, after adding the route-aware `Allow` header on 405 (`activate_iri/asgi.py`) |
| Live smoke test (`smoke.sh`, platform credential) | 13 of 13: whoami, discovery, projects and allocations, storage locations, mkdir, upload, ls, checksum, Slurm job on the debug partition (queued, active, completed), stdout via filesystem download, historical job list, rm |
| Unit and lifecycle tests (`activate-iri`, `make test`) | 18 passed |

Reports are in `reports/` (JUnit XML and HTML from the validator; not committed).

Findings during bring-up.

1. The lab cluster has no Slurm accounting database, so `sacct` fails after a job leaves the queue. Status now falls back from `squeue` to `sacct` to `scontrol show job` to an exit-code file the batch script writes beside its submission. The same fallback covers PBS.
2. The reference framework answers every 405 with `Allow: GET, HEAD`; the validator flags the mismatch on POST-only routes. The ASGI entry point now computes `Allow` from the route table.
3. The validator fetches `<base>/openapi.json`; the v2 framework serves it at `/api/v2/openapi.json`. The entry point serves both.
4. The platform credential in use is a 24-hour token from the pw CLI context. Create a service API key in the ACTIVATE UI before leaving the endpoint running unattended; `run.sh` picks up an `apikey` entry automatically.
5. AmSC Keycard validation stays off (`AMSC_TOKEN_ENABLED=false`) until AmSC registers the endpoint audience (`https://activate-iri.activate.pw/`) and supplies the issuer and a test project context. The mapping file carries a placeholder project.

Workflows on the platform (2026-08-29, later the same day).

| Workflow | Purpose | Status |
|---|---|---|
| `iri-exec` (from `activate-iri/deploy/federation/iri-exec.workflow.yaml`) | Transport for the `WorkflowExecutor`: runs a base64 script on a cluster login node and prints the result between markers | Created with `pw workflows create --yaml`. Verified from Python: `WorkflowExecutor.run()` against the `a30gpuserver` record returned rc 0 with stdout and the stdin payload. On the managed-cluster record (`labcluster`) the run fails at SSH because the platform user does not exist on the node; enable user population and SSH-key sync in the managed cluster's access management, or use an existing-cluster record with an explicit SSH user, before pointing the executor at it. |
| `iri-job` (from `activate-iri-connector/workflow/iri-job/workflow.yaml`) | Consumer side: submit a PSI/J job to any IRI facility, poll, fetch stdout | Created and validated against the platform workflow schema (0 errors). Round trip verified: run `iri-job-00001` submitted through the public prototype endpoint, Slurm job 18 ran on the lab cluster, stdout came back through the filesystem task loop. |

Both YAML files validate against https://activate.parallel.works/workflow.schema.json. The CLI's `runs logs -o json` returns a list of `{job, step, stepIndex, status, duration, logs}`; the executor reads the `logs` key.
Routing and gateway (2026-08-29, evening).

The endpoint now runs `ACTIVATE_IRI_EXECUTOR=auto`: the lab cluster is served locally, and the second cluster (`a30gpuserver`, an existing-cluster record) is served through `iri-exec` workflow runs under the service account. Verified through the public contract: a filesystem ls on the second cluster returned the listing, and a job submitted to it (Slurm job 22) went queued to completed with its stdout on disk. Two fixes came out of this: the workflow executor now waits for the END marker because step logs can lag the run status by a few seconds, and missing markers are reported as a failure instead of an empty success. Per the direction to serve everything under one account for now, runs use the service credential; the caller-credential pass-through is in place for the later per-user extension.

Gateway mode is on with ALCF, NERSC, ESnet East, and OLCF open as upstreams: the consolidated `/status/resources` carries their resources under namespaced ids, `/status/incidents` merges their open incidents, and `/compute/resources` lists their compute systems alongside ACTIVATE's. Forwarded compute and filesystem calls need a facility token in `IRI_TOKEN_<FACILITY>`; none is configured yet, so those return 401 for upstream resources until a token is added. The gateway test suite covers forwarding against a fake upstream. DOE Schemathesis behavioural run after the merge: 48 passed, 1 skipped, 0 failed.

Tunnel outage (2026-08-30). The public session showed as stopped because `stop.sh` killed the reverse tunnel along with the endpoint during a restart and nothing republished it. `stop.sh` now leaves the tunnel running unless called with `--all`, `run.sh` republishes if needed, process checks are anchored to the binary path so a shell wrapper cannot be mistaken for the process, and `watch.sh` (detached, pid in `logs/watch.pid`) restarts the endpoint or the tunnel within a minute if either is down.

Console and demo (2026-08-30). The endpoint now serves `/console/`, a same-origin page that shows the facilities behind it, resources, and open incidents from the public groups, and with an ACTIVATE credential pasted in the browser (session storage only) runs a job through the IRI API and follows it: whoami, mkdir through the task loop, submit, queued, active, completed, stdout through the filesystem download. Presets: GPU inventory (`nvidia-smi`), a CUDA container through Apptainer `--nv`, and a hostname check. The lab A30 host has an NVIDIA A30 and no Slurm gres configuration, so GPU passthrough for containers uses the `apptainer-nv` custom attribute instead of a `--gpus` directive. `demo_a30.sh` is the terminal version and prints every call with status and timing.

Credential expiry and degraded mode (2026-08-31). The pw CLI platform token expired (24 hour lifetime), so the endpoint's ACTIVATE reads returned 401 and the facility answered 500. The endpoint now degrades instead of failing: while the control plane is unreachable it serves the previous inventory, and with no cache it publishes the gateway upstreams only and marks the inventory degraded. Verified live: the public facility answers and serves the 49 upstream resources while ACTIVATE's own clusters are absent. Restoring full service needs a fresh credential: run `pw auth` and paste a service API key created in the ACTIVATE UI (Account settings, API keys), then `./run.sh`; an API key has no 24 hour expiry, which is why the runbook calls for one.

Dashboard tabs (2026-08-31). The topology page's leftover status-monitor navigation (Fleet status, Queue health, Quota, Storage, Insights, API) pointed at pages that do not exist here. The map now has real tabs: Topology, Facilities (per-endpoint reachability, spec version, latency, groups served, resource and incident counts), Resources (filterable table across the federation), Incidents (merged open incidents), and a link to the endpoint console. The aggregator emits the incidents list and facility metadata the tabs read.

Operations.

    ./run.sh          start the endpoint (idempotent)
    ./publish.sh      publish through pw endpoints (idempotent)
    ./smoke.sh        live end-to-end check, exits non-zero on failure
    ./stop.sh         stop the endpoint (add --all to stop the tunnel too)
    ./demo_a30.sh     GPU inventory demo through the public endpoint (PRESET=container|hello)
    ./watch.sh        keepalive loop; run detached: nohup setsid ./watch.sh > logs/watch.log 2>&1 &

Topology dashboard: `../iri-topology/serve.sh` publishes the facilities map at https://iri-topology.activate.pw/ with this endpoint as one of seven facilities.
