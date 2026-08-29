"""compute domain: PSI/J job submission, status, and cancellation on any ACTIVATE cluster.

The batch script is rendered from the JobSpec by the scheduler module, written into a per-job
directory under the user's home (or the requested working directory), and submitted with the
scheduler's native command through the executor. Status comes from the scheduler (live queue,
then accounting history), so the IRI state is authoritative rather than inferred.
"""
from __future__ import annotations

import datetime as dt
import re
import shlex
import time
import uuid

from app.routers.compute import facility_adapter
from app.routers.compute import models as compute_models
from app.routers.status import models as status_models
from app.types.user import User
from fastapi import HTTPException

from .auth import ActivateAuthMixin
from .gateway import split_id
from .runtime import get_runtime
from .schedulers import JobRecord, Scheduler, scheduler_for

JobState = compute_models.JobState
TERMINAL = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELED}


class ShellScheduler(Scheduler):
    """Fallback for clusters with no batch scheduler (ACTIVATE 'existing' SSH-only systems):
    the job runs as a detached process in its job directory, which doubles as the job id."""

    name = "shell"

    def script(self, spec, default_account=None):
        gpu = bool(spec.resources and spec.resources.gpu_cores_per_process)
        lines = ["#!/bin/bash"] + self.preamble(spec) + (["cd " + shlex.quote(spec.directory)] if spec.directory else []) + self.body(spec, gpu)
        return "\n".join(lines) + "\n"

    def submit_command(self, script_path):
        # The adapter has already changed into the job directory; the directory name is the job id.
        return (f"(nohup setsid bash {shlex.quote(script_path)} > stdout 2> stderr; echo $? > rc) >/dev/null 2>&1 & "
                f"echo $! > pid; basename \"$PWD\"")

    def parse_submit(self, stdout):
        return stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""

    def status_command(self, job_id, historical):
        d = f"$HOME/.iri/jobs/{_safe(job_id)}"
        return (f"if [ -f {d}/rc ]; then echo \"{job_id}|$(cat {d}/rc)\"; elif [ -f {d}/pid ] && kill -0 $(cat {d}/pid) 2>/dev/null; "
                f"then echo '{job_id}|running'; elif [ -d {d} ]; then echo '{job_id}|lost'; fi")

    def parse_status(self, job_id, stdout):
        line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        if "|" not in line:
            return None
        jid, state = line.split("|", 1)
        if state == "running":
            return JobRecord(job_id=jid, state=JobState.ACTIVE)
        if state == "lost":
            return JobRecord(job_id=jid, state=JobState.FAILED, reason="process disappeared without an exit code")
        rc = int(state)
        return JobRecord(job_id=jid, state=JobState.COMPLETED if rc == 0 else JobState.FAILED, exit_code=rc)

    def list_command(self, user, historical):
        return ('for d in "$HOME"/.iri/jobs/*/; do [ -d "$d" ] || continue; j=$(basename "$d"); '
                'if [ -f "$d/rc" ]; then echo "$j|$(cat "$d/rc")"; '
                'elif [ -f "$d/pid" ] && kill -0 $(cat "$d/pid") 2>/dev/null; then echo "$j|running"; '
                'else echo "$j|lost"; fi; done')

    def parse_list(self, stdout):
        return [rec for rec in (self.parse_status("", line) for line in stdout.splitlines()) if rec]

    def cancel_command(self, job_id):
        d = f"$HOME/.iri/jobs/{_safe(job_id)}"
        return f"kill -TERM $(cat {d}/pid) 2>/dev/null; echo 143 > {d}/rc"


_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe(job_id: str) -> str:
    if not _SAFE.match(job_id or ""):
        raise HTTPException(status_code=400, detail="invalid job id")
    return job_id


def pick_scheduler(cluster: dict | None) -> Scheduler:
    kind = (cluster or {}).get("schedulerType")
    if not kind:
        return ShellScheduler()
    return scheduler_for(kind)


def to_job(rec: JobRecord, cluster_name: str | None, scheduler: str, extra: dict | None = None, spec=None) -> compute_models.Job:
    meta = {"cluster": cluster_name, "scheduler": scheduler}
    if rec.partition:
        meta["partition"] = rec.partition
    if rec.nodes is not None:
        meta["nodes"] = rec.nodes
    if rec.reason:
        meta["reason"] = rec.reason
    meta.update(extra or {})
    return compute_models.Job(
        id=rec.job_id,
        status=compute_models.JobStatus(state=rec.state, time=time.time(), message=rec.name or rec.reason, exit_code=rec.exit_code, meta_data=meta),
        job_spec=spec,
    )


class ComputeAdapter(ActivateAuthMixin, facility_adapter.FacilityAdapter):
    async def _context(self, resource: status_models.Resource | None, user: User):
        rt = get_runtime()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        inv = await rt.inventory()
        cluster = inv.cluster_for(resource.id)
        if cluster is None:
            raise HTTPException(status_code=400, detail="Resource is not a compute system")
        return rt, cluster, pick_scheduler(cluster), rt.identities.resolve(user, cluster)

    @staticmethod
    def _upstream(resource):
        rt = get_runtime()
        fac, uid = split_id(resource.id) if resource is not None else (None, "")
        if fac and rt.gateway and fac in rt.gateway.upstreams:
            return rt.gateway, fac, uid
        return None, None, None

    async def submit_job(self, resource, user, job_spec: compute_models.JobSpec) -> compute_models.Job:
        gw, fac, uid = self._upstream(resource)
        if gw:
            return await gw.submit_job(fac, uid, user.api_key, job_spec)
        rt, cluster, scheduler, identity = await self._context(resource, user)
        account = (job_spec.attributes.account if job_spec.attributes else None) or rt.identities.account_for(user)
        job_key = str(uuid.uuid4())
        jobdir = f"$HOME/.iri/jobs/{job_key}"
        script = scheduler.script(job_spec, default_account=account)
        submit = "\n".join([
            f"mkdir -p {jobdir} && cd {jobdir}",
            f"cat > job.sh <<'__IRI_JOB__'\n{script}__IRI_JOB__",
            "chmod 700 job.sh",
            scheduler.submit_command("job.sh"),
        ])
        result = await rt.executor.run(identity, submit, timeout=rt.settings.command_timeout)
        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=f"submission failed: {(result.stderr or result.stdout).strip()[-500:]}")
        job_id = scheduler.parse_submit(result.stdout)
        if not job_id:
            raise HTTPException(status_code=500, detail="scheduler returned no job id")
        if scheduler.name != "shell":
            await rt.executor.run(identity, f"echo {shlex.quote(job_id)} > {jobdir}/jobid", timeout=30)
        rec = JobRecord(job_id=job_id, state=JobState.QUEUED, name=job_spec.name)
        return to_job(rec, cluster.get("name"), scheduler.name, {"job_dir": jobdir.replace("$HOME", "~"), "account": account, "iri_job_key": job_key})

    async def update_job(self, resource, user, job_spec, job_id: str) -> compute_models.Job:
        raise HTTPException(status_code=501, detail="Job modification after submission is not supported on this facility")

    async def get_job(self, resource, user, job_id: str, historical: bool = True, include_spec: bool = False) -> compute_models.Job:
        gw, fac, uid = self._upstream(resource)
        if gw:
            return await gw.get_job(fac, uid, job_id, user.api_key, historical)
        rt, cluster, scheduler, identity = await self._context(resource, user)
        result = await rt.executor.run(identity, scheduler.status_command(job_id, historical), timeout=rt.settings.command_timeout)
        rec = scheduler.parse_status(job_id, result.stdout)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        if rec.state in TERMINAL:
            await self._record_usage(rt, user, cluster, rec, result.stdout)
        return to_job(rec, cluster.get("name"), scheduler.name)

    async def get_jobs(self, resource, user, offset: int, limit: int, filters=None, historical: bool = False, include_spec: bool = False) -> list[compute_models.Job]:
        gw, fac, uid = self._upstream(resource)
        if gw:
            return await gw.get_jobs(fac, uid, user.api_key, offset, limit, historical)
        rt, cluster, scheduler, identity = await self._context(resource, user)
        result = await rt.executor.run(identity, scheduler.list_command(identity.posix_user, historical), timeout=rt.settings.command_timeout)
        records = scheduler.parse_list(result.stdout)
        if filters and filters.get("state"):
            records = [r for r in records if r.state.value == str(filters["state"]).lower()]
        return [to_job(r, cluster.get("name"), scheduler.name) for r in records[offset:][:limit]]

    async def cancel_job(self, resource, user, job_id: str) -> bool:
        gw, fac, uid = self._upstream(resource)
        if gw:
            return await gw.cancel_job(fac, uid, job_id, user.api_key)
        rt, _cluster, scheduler, identity = await self._context(resource, user)
        result = await rt.executor.run(identity, scheduler.cancel_command(job_id), timeout=rt.settings.command_timeout)
        if result.returncode != 0 and "already" not in (result.stderr + result.stdout).lower():
            raise HTTPException(status_code=400, detail=f"cancel failed: {(result.stderr or result.stdout).strip()[-300:]}")
        return True

    async def _record_usage(self, rt, user: User, cluster: dict, rec: JobRecord, raw: str) -> None:
        """Close the accounting loop: post node-hours for finished jobs to the user's ACTIVATE allocation.

        Only Slurm's sacct output carries the elapsed time here; PBS and shell jobs are skipped.
        Idempotent per job id within the process lifetime; a persistent store replaces the set below.
        """
        sku = rt.config.allocation.usage_sku
        account = rt.identities.account_for(user)
        org = rt.settings.activate_organization
        if not (sku and account and org) or rec.nodes is None:
            return
        seen = getattr(rt, "_usage_seen", None)
        if seen is None:
            seen = rt._usage_seen = set()
        if rec.job_id in seen:
            return
        elapsed = _elapsed_seconds(raw, rec.job_id)
        if elapsed is None:
            return
        seen.add(rec.job_id)
        ended = dt.datetime.now(dt.UTC)
        started = ended - dt.timedelta(seconds=elapsed)
        try:
            await rt.client.post_usage(org, account, sku, rec.nodes * elapsed / 3600.0, started.isoformat(), ended.isoformat(),
                                       user=user.id, metadata={"job_id": rec.job_id, "cluster": cluster.get("name"), "source": "activate-iri"})
        except Exception:  # noqa: BLE001
            seen.discard(rec.job_id)


def _elapsed_seconds(raw: str, job_id: str) -> float | None:
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) >= 7 and parts[0].strip() == job_id and parts[6].strip().isdigit():
            return float(parts[6])
    return None
