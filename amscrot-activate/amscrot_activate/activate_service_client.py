"""AmSCROT ServiceClient for Parallel Works ACTIVATE.

AmSCROT (the AmSC Infrastructure Services Resource Orchestration Toolkit, published as
doe-iri/iri-facility-api-toolkit) drives facilities through ServiceClient implementations:
IriServiceClient for any IRI facility, KubeServiceClient for Kueue, AmscIroServiceClient for the
SENSE-O based orchestrator. This module adds an ACTIVATE-native client for the case where the
orchestrator wants what the IRI contract does not yet carry: elastic provisioning of a cluster
sized for the job, provider choice (hyperscaler, NeoCloud, on-premises), and ACTIVATE's own
allocation accounting. It talks to the ACTIVATE REST API and runs jobs as workflow runs.

Registration (until upstreamed):

    from amscrot.util.constants import Constants
    Constants.SERVICE_CLIENT_CLASSES["activate"] = "amscrot_activate.activate_service_client.ActivateServiceClient"

credentials.yml profile:

    pw-activate:
      client_type: ACTIVATE
      api_key: pwt_...
      api_endpoint: https://activate.parallel.works
      workflow: iri-job-runner          # ACTIVATE workflow that runs a PSI/J spec on a cluster

For the common case (AmSC-IRO treating PW as one more IRI facility) no code is needed: point an
AMSC_IRI profile at https://iri.activate.pw/api/v2 and IriServiceClient does the rest.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

import httpx

try:  # AmSCROT is optional at import time so the module can be unit tested standalone.
    from amscrot.serviceclient import CreateError, DestroyError, PlanError, ServiceClient
    from amscrot.model.discovery import DiscoveredResource, DiscoveryResult
    from amscrot.client.job import JobStatus, JobState
except Exception:  # pragma: no cover
    class ServiceClient:  # minimal stand-in with the same method names
        def __init__(self, name: str, type: str, endpoint_uri: str | None = None, **kw):
            self.name, self.type, self.endpoint_uri = name, type, endpoint_uri
            self.credential, self.profile = kw.get("credential"), kw.get("profile")
            import logging
            self.logger = logging.getLogger("amscrot_activate")

    class PlanError(Exception):
        def __init__(self, errors, warnings=None):
            super().__init__("; ".join(errors)); self.errors, self.warnings = errors, warnings or []

    CreateError = DestroyError = PlanError

    class DiscoveredResource:
        def __init__(self, type, data): self.type, self.data = type, data

    class DiscoveryResult:
        def __init__(self): self.all = []
        def add(self, item): self.all.append(item)
        def by_type(self, t): return [i for i in self.all if i.type == t]

    class JobState:
        INIT, PLANNED, PENDING, NEW, QUEUED, ACTIVE, COMPLETED, FAILED, CANCELED, UNKNOWN = ("INIT", "PLANNED", "PENDING", "NEW", "QUEUED", "ACTIVE", "COMPLETED", "FAILED", "CANCELED", "UNKNOWN")

    class JobStatus:
        def __init__(self, state="UNKNOWN", message=None, exit_code=None, job_id=None, resource_id=None, provider_status=None):
            self.state, self.message, self.exit_code, self.job_id, self.resource_id, self.provider_status = state, message, exit_code, job_id, resource_id, provider_status


def _auth(credential: str) -> str:
    if credential.startswith("pwt_"):
        return "Basic " + base64.b64encode(f"{credential}:".encode()).decode()
    return f"Bearer {credential}"


_RUN_STATE = {"running": JobState.ACTIVE, "pending": JobState.QUEUED, "queued": JobState.QUEUED, "completed": JobState.COMPLETED,
              "error": JobState.FAILED, "failed": JobState.FAILED, "canceled": JobState.CANCELED, "cancelled": JobState.CANCELED}


class ActivateServiceClient(ServiceClient):
    """ServiceClient over the ACTIVATE REST API and pw CLI.

    discover(): every cluster the credential can see becomes a compute item (with provider,
    scheduler, status, and node counts), plus buckets, Lustre, NFS as storage items.
    plan(): validates the JobSpec against the cluster (scheduler present, node count within
    maxNodes) and returns the rendered inputs.
    create(): starts the configured workflow with the PSI/J spec; the run slug is the job id.
    status(): maps the run status; destroy(): cancels the run.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("type", "activate")
        super().__init__(**kwargs)
        cred = kwargs.get("credential") or {}
        if hasattr(cred, "to_dict"):
            cred = cred.to_dict()
        self.api_key = cred.get("api_key") or os.environ.get("PW_API_KEY")
        self.api_endpoint = (cred.get("api_endpoint") or self.endpoint_uri or "https://activate.parallel.works").rstrip("/")
        self.workflow = cred.get("workflow") or "iri-job-runner"
        self.pw_bin = cred.get("pw_bin") or "pw"
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._http = httpx.Client(base_url=self.api_endpoint, timeout=30, headers={"Authorization": _auth(self.api_key)} if self.api_key else {})

    # -- discovery ----------------------------------------------------------------------
    def discover(self, native: bool = True):
        result = DiscoveryResult()
        clusters = self._http.get("/api/clusters").json()
        for c in clusters:
            result.add(DiscoveredResource(type="compute", data={
                "id": c.get("id"), "name": c.get("name"), "display_name": c.get("displayName"), "provider": c.get("csp") or ("on-premises" if c.get("type") in ("managed-cluster", "existing") else c.get("type")),
                "cluster_type": c.get("type"), "scheduler": c.get("schedulerType"), "status": c.get("status"), "region": c.get("region"),
                "active_nodes": c.get("activeNodes"), "max_nodes": c.get("maxNodes"), "elastic": c.get("type", "").endswith("-slurm"),
                "tags": c.get("tags") or [],
            }))
        for kind, path in (("bucket", "/api/buckets"), ("lustre", "/api/lustre"), ("nfs", "/api/nfs")):
            try:
                for s in self._http.get(path).json() or []:
                    result.add(DiscoveredResource(type="storage", data={"id": s.get("id"), "name": s.get("name"), "kind": kind, "csp": s.get("csp"), "region": s.get("region"), "size_gb": s.get("sizeGb")}))
            except Exception as exc:
                self.logger.warning(f"[{self.name}] storage discovery {path} failed: {exc}")
        try:
            for a in self._http.get("/api/allocations").json() or []:
                result.add(DiscoveredResource(type="allocation", data={"id": a.get("name"), "name": a.get("name"), "total": a.get("total"), "used": a.get("used"), "unit": a.get("unit"), "parent": a.get("parent")}))
        except Exception:
            pass
        return result if native else self.normalize_discovery(result)

    # -- lifecycle ----------------------------------------------------------------------
    def _cluster(self, resource_id: str) -> Dict[str, Any]:
        for c in self._http.get("/api/clusters").json():
            if c.get("id") == resource_id or c.get("name") == resource_id:
                return c
        raise PlanError([f"cluster {resource_id} not found in ACTIVATE"])

    def plan(self, job, skip_checks: bool = False) -> Dict:
        errors, warnings = [], []
        cluster = self._cluster(job.resource_id)
        spec = job.job_spec.to_dict() if hasattr(job.job_spec, "to_dict") else dict(job.job_spec)
        nodes = (spec.get("resources") or {}).get("node_count") or 1
        if cluster.get("maxNodes") and nodes > int(cluster["maxNodes"]):
            errors.append(f"{nodes} nodes requested; {cluster['name']} allows {cluster['maxNodes']}")
        if not cluster.get("schedulerType") and nodes > 1:
            errors.append(f"{cluster['name']} has no batch scheduler; multi-node jobs need Slurm or PBS")
        if cluster.get("status") not in ("active", "on") and not cluster.get("type", "").endswith("-slurm"):
            warnings.append(f"{cluster['name']} is {cluster.get('status')}; the job will queue until the agent reports it online")
        if errors and not skip_checks:
            raise PlanError(errors, warnings)
        inputs = {"resource": cluster["name"], "job_spec_json": json.dumps(spec), "job_name": job.name}
        self._jobs.setdefault(job.name, {})["plan"] = {"cluster": cluster, "inputs": inputs, "warnings": warnings}
        return {"inputs": inputs, "warnings": warnings, "cluster": cluster["name"]}

    def create(self, job):
        planned = self._jobs.get(job.name, {}).get("plan") or self.plan(job)
        try:
            out = subprocess.run([self.pw_bin, "workflows", "run", "-o", "json", "--name", job.name, "-i", json.dumps(planned["inputs"]), self.workflow],
                                 capture_output=True, text=True, check=True, timeout=120)
            run = json.loads(out.stdout)
        except Exception as exc:
            raise CreateError([f"workflow start failed: {exc}"])
        slug = run.get("slug") or run.get("run", {}).get("slug")
        self._jobs[job.name]["run"] = {"slug": slug, "started": time.time()}
        job.job_id = slug
        return slug

    def status(self, job) -> "JobStatus":
        slug = self._jobs.get(job.name, {}).get("run", {}).get("slug") or getattr(job, "job_id", None)
        if not slug:
            return JobStatus(state=JobState.INIT)
        try:
            view = subprocess.run([self.pw_bin, "workflows", "runs", "view", "-o", "json", slug], capture_output=True, text=True, check=True, timeout=60)
            data = json.loads(view.stdout)
        except Exception as exc:
            return JobStatus(state=JobState.UNKNOWN, message=str(exc), job_id=slug)
        state = _RUN_STATE.get(str(data.get("status", "")).lower(), JobState.UNKNOWN)
        return JobStatus(state=state, job_id=slug, resource_id=job.resource_id, provider_status=data)

    def destroy(self, job):
        slug = self._jobs.get(job.name, {}).get("run", {}).get("slug") or getattr(job, "job_id", None)
        if not slug:
            return
        try:
            subprocess.run([self.pw_bin, "workflows", "runs", "cancel", slug], capture_output=True, text=True, check=True, timeout=60)
        except Exception as exc:
            raise DestroyError([f"cancel failed: {exc}"])

    def normalize_discovery(self, native_result):
        return native_result
