"""PSI/J JobSpec to batch-scheduler translation for Slurm and PBS Pro.

The IRI compute domain uses the PSI/J JobSpec model. This module renders a submission script
from that model, and parses scheduler output back into IRI JobState values. Both ACTIVATE
cloud clusters and most existing lab clusters run Slurm; ALCF (Polaris, Aurora) runs PBS Pro,
so both are supported from one interface.
"""
from __future__ import annotations

import re
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from app.routers.compute import models as compute_models

JobState = compute_models.JobState


@dataclass
class JobRecord:
    job_id: str
    state: JobState
    name: str | None = None
    exit_code: int | None = None
    partition: str | None = None
    nodes: int | None = None
    reason: str | None = None
    raw: str | None = None


def _hms(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _mem_mb(memory_bytes: int) -> int:
    return max(1, int(memory_bytes / (1024 * 1024)))


def render_command(spec: compute_models.JobSpec, gpu: bool) -> str:
    """The command line that runs the user's work: bare executable, or apptainer-wrapped container."""
    args = " ".join(shlex.quote(a) for a in spec.arguments)
    if spec.container:
        mounts = " ".join(
            f"--bind {shlex.quote(v.source)}:{shlex.quote(v.target)}{':ro' if v.read_only else ''}" for v in spec.container.volume_mounts
        )
        nv = "--nv" if gpu else ""
        image = spec.container.image
        if "://" not in image:
            image = f"docker://{image}"
        entry = shlex.quote(spec.executable) if spec.executable else ""
        return f"apptainer exec {nv} {mounts} {shlex.quote(image)} {entry} {args}".strip()
    return f"{shlex.quote(spec.executable or '/bin/true')} {args}".strip()


class Scheduler(ABC):
    name = "generic"

    @abstractmethod
    def script(self, spec: compute_models.JobSpec, default_account: str | None = None) -> str: ...

    @abstractmethod
    def submit_command(self, script_path: str) -> str: ...

    @abstractmethod
    def parse_submit(self, stdout: str) -> str: ...

    @abstractmethod
    def status_command(self, job_id: str, historical: bool) -> str: ...

    @abstractmethod
    def parse_status(self, job_id: str, stdout: str) -> JobRecord | None: ...

    @abstractmethod
    def list_command(self, user: str, historical: bool) -> str: ...

    @abstractmethod
    def parse_list(self, stdout: str) -> list[JobRecord]: ...

    @abstractmethod
    def cancel_command(self, job_id: str) -> str: ...

    def preamble(self, spec: compute_models.JobSpec) -> list[str]:
        lines = ["set -o pipefail"]
        if spec.pre_launch:
            lines.append(spec.pre_launch)
        for key, value in (spec.environment or {}).items():
            lines.append(f"export {key}={shlex.quote(value)}")
        return lines

    submit_dir_var = "IRI_SUBMIT_DIR"

    def body(self, spec: compute_models.JobSpec, gpu: bool) -> list[str]:
        cmd = render_command(spec, gpu)
        if spec.launcher:
            cmd = f"{spec.launcher} {cmd}"
        if spec.stdin_path:
            cmd += f" < {shlex.quote(spec.stdin_path)}"
        lines = [cmd, "__iri_rc=$?"]
        if spec.post_launch:
            lines.append(spec.post_launch)
        # Record the exit code beside the submission so status survives schedulers without accounting.
        lines.append(f'[ -n "${{{self.submit_dir_var}:-}}" ] && echo $__iri_rc > "${self.submit_dir_var}/rc"')
        lines.append("exit $__iri_rc")
        return lines

    @staticmethod
    def rc_lookup(job_id: str) -> str:
        """Shell fragment: report a recorded exit code for job_id from the per-job directories."""
        q = shlex.quote(job_id)
        return (f'for d in "$HOME"/.iri/jobs/*/; do [ -f "$d/jobid" ] && [ "$(cat "$d/jobid")" = {q} ] && [ -f "$d/rc" ] '
                f'&& echo "{job_id}|RCFILE|$(cat "$d/rc")"; done; true')


class SlurmScheduler(Scheduler):
    name = "slurm"
    submit_dir_var = "SLURM_SUBMIT_DIR"
    _state_map: ClassVar[dict] = {
        "PENDING": JobState.QUEUED, "PD": JobState.QUEUED, "CONFIGURING": JobState.QUEUED, "CF": JobState.QUEUED,
        "RUNNING": JobState.ACTIVE, "R": JobState.ACTIVE, "COMPLETING": JobState.ACTIVE, "CG": JobState.ACTIVE,
        "STAGE_OUT": JobState.ACTIVE, "SIGNALING": JobState.ACTIVE,
        "COMPLETED": JobState.COMPLETED, "CD": JobState.COMPLETED,
        "FAILED": JobState.FAILED, "F": JobState.FAILED, "TIMEOUT": JobState.FAILED, "TO": JobState.FAILED,
        "NODE_FAIL": JobState.FAILED, "NF": JobState.FAILED, "OUT_OF_MEMORY": JobState.FAILED, "OOM": JobState.FAILED,
        "BOOT_FAIL": JobState.FAILED, "BF": JobState.FAILED, "DEADLINE": JobState.FAILED, "DL": JobState.FAILED,
        "PREEMPTED": JobState.FAILED, "PR": JobState.FAILED,
        "CANCELLED": JobState.CANCELED, "CA": JobState.CANCELED, "REVOKED": JobState.CANCELED,
        "SUSPENDED": JobState.HELD, "S": JobState.HELD, "REQUEUE_HOLD": JobState.HELD, "RH": JobState.HELD,
        "REQUEUED": JobState.QUEUED, "RQ": JobState.QUEUED, "RESIZING": JobState.ACTIVE, "RS": JobState.ACTIVE,
    }

    def script(self, spec, default_account=None):
        r, a = spec.resources, spec.attributes
        d = ["#!/bin/bash"]
        if spec.name:
            d.append(f"#SBATCH --job-name={shlex.quote(spec.name)}")
        if spec.directory:
            d.append(f"#SBATCH --chdir={shlex.quote(spec.directory)}")
        d.append(f"#SBATCH --output={shlex.quote(spec.stdout_path or 'iri-%j.out')}")
        if spec.stderr_path:
            d.append(f"#SBATCH --error={shlex.quote(spec.stderr_path)}")
        if r:
            if r.node_count:
                d.append(f"#SBATCH --nodes={r.node_count}")
            if r.process_count:
                d.append(f"#SBATCH --ntasks={r.process_count}")
            if r.processes_per_node:
                d.append(f"#SBATCH --ntasks-per-node={r.processes_per_node}")
            if r.cpu_cores_per_process:
                d.append(f"#SBATCH --cpus-per-task={r.cpu_cores_per_process}")
            if r.gpu_cores_per_process:
                d.append(f"#SBATCH --gpus-per-task={r.gpu_cores_per_process}")
            if r.memory:
                d.append(f"#SBATCH --mem={_mem_mb(r.memory)}M")
            if r.exclusive_node_use:
                d.append("#SBATCH --exclusive")
        if a:
            if a.duration:
                d.append(f"#SBATCH --time={_hms(a.duration)}")
            if a.queue_name:
                d.append(f"#SBATCH --partition={shlex.quote(a.queue_name)}")
            if a.account or default_account:
                d.append(f"#SBATCH --account={shlex.quote(a.account or default_account)}")
            if a.reservation_id:
                d.append(f"#SBATCH --reservation={shlex.quote(a.reservation_id)}")
            for key, value in (a.custom_attributes or {}).items():
                d.append(f"#SBATCH --{key}={shlex.quote(value)}" if value else f"#SBATCH --{key}")
        elif default_account:
            d.append(f"#SBATCH --account={shlex.quote(default_account)}")
        if not spec.inherit_environment:
            d.append("#SBATCH --export=NONE")
        gpu = bool(r and r.gpu_cores_per_process)
        return "\n".join(d + self.preamble(spec) + self.body(spec, gpu)) + "\n"

    def submit_command(self, script_path):
        return f"sbatch --parsable {shlex.quote(script_path)}"

    def parse_submit(self, stdout):
        first = stdout.strip().splitlines()[0] if stdout.strip() else ""
        return first.split(";")[0].strip()

    def status_command(self, job_id, historical):
        q = shlex.quote(job_id)
        live = f"squeue -h -j {q} -o '%i|%T|%j|%P|%D|%r' 2>/dev/null"
        if not historical:
            return live
        # Order of authority: live queue, accounting database, controller memory (MinJobAge), recorded rc file.
        return (f"({live}) || true; sacct -n -P -X -j {q} -o JobID,State,JobName,Partition,NNodes,ExitCode,ElapsedRaw 2>/dev/null; "
                f"scontrol -o show job {q} 2>/dev/null | sed 's/^/SCONTROL|/'; " + self.rc_lookup(job_id))

    def parse_status(self, job_id, stdout):
        records = self._parse(stdout)
        for rec in records:
            if rec.job_id == job_id or rec.job_id.split("_")[0] == job_id:
                return rec
        return records[0] if records else None

    @staticmethod
    def _parse_scontrol(line: str) -> JobRecord | None:
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        if "JobId" not in fields:
            return None
        state = SlurmScheduler._state_map.get(fields.get("JobState", "").upper(), JobState.FAILED)
        exit_code = None
        if re.match(r"^\d+:\d+$", fields.get("ExitCode", "")):
            exit_code = int(fields["ExitCode"].split(":")[0])
        nodes = int(fields["NumNodes"]) if fields.get("NumNodes", "").isdigit() else None
        return JobRecord(job_id=fields["JobId"], state=state, name=fields.get("JobName"), partition=fields.get("Partition"), nodes=nodes,
                         exit_code=exit_code, reason=fields.get("Reason") if fields.get("Reason") not in (None, "None") else None, raw=line)

    def list_command(self, user, historical):
        u = shlex.quote(user)
        live = f"squeue -h -u {u} -o '%i|%T|%j|%P|%D|%r' 2>/dev/null"
        if not historical:
            return live
        return f"({live}) || true; sacct -n -P -X -u {u} -S now-7days -o JobID,State,JobName,Partition,NNodes,ExitCode 2>/dev/null"

    def parse_list(self, stdout):
        seen: dict[str, JobRecord] = {}
        for rec in self._parse(stdout):
            seen.setdefault(rec.job_id, rec)
        return list(seen.values())

    def cancel_command(self, job_id):
        return f"scancel {shlex.quote(job_id)}"

    def _parse(self, stdout: str) -> list[JobRecord]:
        out = []
        for line in stdout.splitlines():
            if line.startswith("SCONTROL|"):
                rec = self._parse_scontrol(line[9:])
                if rec:
                    out.append(rec)
                continue
            parts = line.strip().split("|")
            if len(parts) < 2 or not parts[0]:
                continue
            if parts[1] == "RCFILE":
                rc = int(parts[2]) if len(parts) > 2 and parts[2].strip().lstrip("-").isdigit() else 1
                out.append(JobRecord(job_id=parts[0], state=JobState.COMPLETED if rc == 0 else JobState.FAILED, exit_code=rc, raw=line))
                continue
            state_word = parts[1].split()[0].upper() if parts[1] else "UNKNOWN"
            state_key = state_word.split("+")[0]
            if state_key.startswith("CANCELLED"):
                state_key = "CANCELLED"
            state = self._state_map.get(state_key, JobState.QUEUED if state_key.startswith("PEND") else JobState.FAILED)
            exit_code = None
            if len(parts) >= 6 and re.match(r"^\d+:\d+$", parts[5].strip()):
                exit_code = int(parts[5].split(":")[0])
            nodes = int(parts[4]) if len(parts) >= 5 and parts[4].strip().isdigit() else None
            out.append(JobRecord(job_id=parts[0].strip(), state=state, name=parts[2] if len(parts) > 2 else None,
                                 partition=parts[3] if len(parts) > 3 else None, nodes=nodes, exit_code=exit_code,
                                 reason=parts[5] if len(parts) > 5 and exit_code is None else None, raw=line))
        return out


class PBSScheduler(Scheduler):
    """PBS Pro (as used by ALCF Polaris/Aurora and many DoD/DOE sites)."""

    name = "pbs"
    submit_dir_var = "PBS_O_WORKDIR"
    _state_map: ClassVar[dict] = {"Q": JobState.QUEUED, "W": JobState.QUEUED, "T": JobState.QUEUED, "R": JobState.ACTIVE, "E": JobState.ACTIVE,
                  "B": JobState.ACTIVE, "H": JobState.HELD, "S": JobState.HELD, "U": JobState.HELD, "X": JobState.COMPLETED,
                  "F": JobState.COMPLETED}

    def script(self, spec, default_account=None):
        r, a = spec.resources, spec.attributes
        d = ["#!/bin/bash"]
        if spec.name:
            d.append(f"#PBS -N {shlex.quote(spec.name)}")
        d.append(f"#PBS -o {shlex.quote(spec.stdout_path or 'iri.out')}")
        d.append(f"#PBS -e {shlex.quote(spec.stderr_path or 'iri.err')}")
        select = []
        if r:
            nodes = r.node_count or 1
            chunk = [f"select={nodes}"]
            if r.processes_per_node:
                chunk.append(f"mpiprocs={r.processes_per_node}")
            if r.cpu_cores_per_process and r.processes_per_node:
                chunk.append(f"ncpus={r.cpu_cores_per_process * r.processes_per_node}")
            elif r.cpu_cores_per_process:
                chunk.append(f"ncpus={r.cpu_cores_per_process}")
            if r.gpu_cores_per_process:
                chunk.append(f"ngpus={r.gpu_cores_per_process * (r.processes_per_node or 1)}")
            if r.memory:
                chunk.append(f"mem={_mem_mb(r.memory)}mb")
            select.append(":".join(chunk))
            d.append(f"#PBS -l {select[0]}")
            if r.exclusive_node_use:
                d.append("#PBS -l place=scatter:excl")
        if a:
            if a.duration:
                d.append(f"#PBS -l walltime={_hms(a.duration)}")
            if a.queue_name:
                d.append(f"#PBS -q {shlex.quote(a.queue_name)}")
            if a.account or default_account:
                d.append(f"#PBS -A {shlex.quote(a.account or default_account)}")
            for key, value in (a.custom_attributes or {}).items():
                d.append(f"#PBS -{key} {value}".rstrip())
        elif default_account:
            d.append(f"#PBS -A {shlex.quote(default_account)}")
        if spec.inherit_environment:
            d.append("#PBS -V")
        body = self.preamble(spec)
        body.insert(1, f"cd {shlex.quote(spec.directory)}" if spec.directory else 'cd "$PBS_O_WORKDIR"')
        gpu = bool(r and r.gpu_cores_per_process)
        return "\n".join(d + body + self.body(spec, gpu)) + "\n"

    def submit_command(self, script_path):
        return f"qsub {shlex.quote(script_path)}"

    def parse_submit(self, stdout):
        return stdout.strip().splitlines()[0].strip() if stdout.strip() else ""

    def status_command(self, job_id, historical):
        flag = "-x" if historical else ""
        return f"qstat {flag} -f -F json {shlex.quote(job_id)} 2>/dev/null; " + self.rc_lookup(job_id)

    def parse_status(self, job_id, stdout):
        json_part = stdout.split("\n" + job_id + "|RCFILE|", 1)[0]
        records = self.parse_list(json_part)
        if records:
            return records[0]
        for line in stdout.splitlines():
            if line.startswith(job_id + "|RCFILE|"):
                rc = int(line.rsplit("|", 1)[1])
                return JobRecord(job_id=job_id, state=JobState.COMPLETED if rc == 0 else JobState.FAILED, exit_code=rc, raw=line)
        return None

    def list_command(self, user, historical):
        flag = "-x" if historical else ""
        return f"qstat {flag} -f -F json -u {shlex.quote(user)} 2>/dev/null"

    def parse_list(self, stdout):
        import json

        out = []
        try:
            data = json.loads(stdout) if stdout.strip() else {}
        except ValueError:
            return out
        for job_id, job in (data.get("Jobs") or {}).items():
            state = self._state_map.get(job.get("job_state", "Q"), JobState.QUEUED)
            exit_code = job.get("Exit_status")
            if state == JobState.COMPLETED and exit_code not in (None, 0):
                state = JobState.FAILED
            out.append(JobRecord(job_id=job_id, state=state, name=job.get("Job_Name"), partition=job.get("queue"),
                                 exit_code=exit_code, raw=json.dumps(job)[:500]))
        return out

    def cancel_command(self, job_id):
        return f"qdel {shlex.quote(job_id)}"


def scheduler_for(scheduler_type: str | None) -> Scheduler:
    if (scheduler_type or "").lower() == "pbs":
        return PBSScheduler()
    return SlurmScheduler()
