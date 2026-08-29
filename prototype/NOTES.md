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

Routing and gateway (2026-08-29, evening).

The endpoint now runs `ACTIVATE_IRI_EXECUTOR=auto`: the lab cluster is served locally, and the second cluster (`a30gpuserver`, an existing-cluster record) is served through `iri-exec` workflow runs under the service account. Verified through the public contract: a filesystem ls on the second cluster returned the listing, and a job submitted to it (Slurm job 22) went queued to completed with its stdout on disk. Two fixes came out of this: the workflow executor now waits for the END marker because step logs can lag the run status by a few seconds, and missing markers are reported as a failure instead of an empty success. Per the direction to serve everything under one account for now, runs use the service credential; the caller-credential pass-through is in place for the later per-user extension.

Gateway mode is on with ALCF, NERSC, ESnet East, and OLCF open as upstreams: the consolidated `/status/resources` carries their resources under namespaced ids, `/status/incidents` merges their open incidents, and `/compute/resources` lists their compute systems alongside ACTIVATE's. Forwarded compute and filesystem calls need a facility token in `IRI_TOKEN_<FACILITY>`; none is configured yet, so those return 401 for upstream resources until a token is added. The gateway test suite covers forwarding against a fake upstream.

Operations.

    ./run.sh          start the endpoint (idempotent)
    ./publish.sh      publish through pw endpoints (idempotent)
    ./smoke.sh        live end-to-end check, exits non-zero on failure
    ./stop.sh         stop both

Topology dashboard: `../iri-topology/serve.sh` publishes the facilities map at https://iri-topology.activate.pw/ with this endpoint as one of seven facilities.
