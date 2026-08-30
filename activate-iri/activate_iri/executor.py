"""Executors: run a shell script as a mapped POSIX user on a cluster login node.

Every operation the IRI compute and filesystem domains perform (sbatch, squeue, ls, tar, ...)
is a short shell script executed under the caller's identity. The executor decides *where*
that script runs; the adapters never care.

* LocalExecutor    edge mode. The container runs on the login node itself; the script runs
                   through ``sudo -n -u <user>`` (a sudoers rule scoped to the service account)
                   or directly when the service already runs as the target user.
* SSHExecutor      federation mode. The endpoint runs beside the ACTIVATE control plane and
                   reaches the cluster over SSH. With ``ACTIVATE_IRI_SSH_CA_KEY`` set, a
                   short-lived user certificate (5 minutes, principal = mapped user) is minted
                   per call, the FirecREST pattern; nodes trust the CA via TrustedUserCAKeys,
                   which the PW agent's access-management already provisions for managed
                   clusters. Without a CA, a static key is used.
* WorkflowExecutor federation fallback that needs no inbound network path at all: the script is
                   carried by an ACTIVATE workflow run (``pw workflows run``) that executes on the
                   target cluster and streams the result back through the platform. Slower
                   (seconds), fully audited in ACTIVATE, no SSH plumbing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

BEGIN = "__IRI_BEGIN__"
END = "__IRI_END__"


@dataclass
class ExecIdentity:
    posix_user: str          # account the script runs as on the cluster
    host: str | None = None  # login node (ignored by LocalExecutor)
    platform_user: str | None = None
    cluster_id: str | None = None
    cluster_name: str | None = None


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    def check(self, what: str = "command") -> CommandResult:
        if self.returncode != 0:
            raise CommandError(what, self.returncode, self.stdout, self.stderr)
        return self


class CommandError(RuntimeError):
    def __init__(self, cmd: str, returncode: int, stdout: str = "", stderr: str = ""):
        detail = (stderr or stdout or "").strip().splitlines()
        tail = detail[-1] if detail else ""
        super().__init__(f"{cmd} failed (rc={returncode}): {tail}")
        self.cmd, self.returncode, self.stdout, self.stderr = cmd, returncode, stdout, stderr


class Executor(ABC):
    @abstractmethod
    async def run(self, identity: ExecIdentity, script: str, cwd: str | None = None,
                  stdin: bytes | None = None, timeout: int = 300) -> CommandResult: ...


def _wrap(script: str, cwd: str | None) -> str:
    """Bash -l wrapper so module/lmod environments and the user's PATH are present."""
    lines = ["set -o pipefail"]
    if cwd:
        lines.append(f"cd {shlex.quote(cwd)} || exit 97")
    lines.append(script)
    return "\n".join(lines) + "\n"


async def _spawn(argv: list[str], stdin: bytes | None, timeout: int, env: dict | None = None) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return CommandResult(124, "", f"timed out after {timeout}s")
    return CommandResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


class LocalExecutor(Executor):
    def __init__(self, run_as: str = "sudo"):
        self.run_as = run_as

    async def run(self, identity, script, cwd=None, stdin=None, timeout=300):
        body = _wrap(script, cwd).encode()
        if self.run_as == "sudo":
            argv = ["sudo", "-n", "-u", identity.posix_user, "-H", "--", "/bin/bash", "-l", "-s"]
        else:
            argv = ["/bin/bash", "-l", "-s"]
        # The script arrives on stdin; a payload for the script (upload) follows a marker line.
        payload = body if stdin is None else body + b"\n" + stdin
        return await _spawn(argv, payload, timeout)


class SSHExecutor(Executor):
    def __init__(self, key_path: str | None, ca_key_path: str | None = None, jump: str | None = None,
                 known_hosts: str | None = None, cert_ttl: str = "+5m"):
        self.key_path, self.ca_key_path, self.jump, self.known_hosts, self.cert_ttl = key_path, ca_key_path, jump, known_hosts, cert_ttl

    async def _mint_certificate(self, principal: str) -> tuple[str, str]:
        """Return (private key path, certificate path) for a short-lived user certificate."""
        tmpdir = tempfile.mkdtemp(prefix="iri-ssh-")
        key = os.path.join(tmpdir, "id")
        gen = await _spawn(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], None, 30)
        gen.check("ssh-keygen")
        ident = f"iri-{principal}-{int(time.time())}"
        sign = await _spawn(["ssh-keygen", "-q", "-s", self.ca_key_path, "-I", ident, "-n", principal,
                             "-V", self.cert_ttl, key + ".pub"], None, 30)
        sign.check("ssh-keygen -s")
        return key, key + "-cert.pub"

    async def run(self, identity, script, cwd=None, stdin=None, timeout=300):
        if not identity.host:
            return CommandResult(98, "", "no login host for resource")
        opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        if self.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        if self.jump:
            opts += ["-J", self.jump]
        cleanup: list[str] = []
        if self.ca_key_path:
            key, cert = await self._mint_certificate(identity.posix_user)
            opts += ["-i", key, "-o", f"CertificateFile={cert}", "-o", "IdentitiesOnly=yes"]
            cleanup += [key, key + ".pub", cert]
        elif self.key_path:
            opts += ["-i", self.key_path, "-o", "IdentitiesOnly=yes"]
        argv = ["ssh", *opts, f"{identity.posix_user}@{identity.host}", "/bin/bash", "-l", "-s"]
        body = _wrap(script, cwd).encode()
        payload = body if stdin is None else body + b"\n" + stdin
        try:
            return await _spawn(argv, payload, timeout)
        finally:
            for path in cleanup:
                try:
                    os.unlink(path)
                except OSError:
                    pass


class WorkflowExecutor(Executor):
    """Carry the script inside an ACTIVATE workflow run (see deploy/federation/iri-exec.workflow.yaml).

    The workflow runs ``bash -l -s`` on the chosen cluster's login node as the platform user and
    prints the script output between BEGIN/END markers followed by ``rc=<n>``. This executor
    invokes the pw CLI so it works against any ACTIVATE version the CLI supports.
    """

    def __init__(self, pw_bin: str = "pw", workflow: str = "iri-exec", poll: float = 3.0):
        self.pw_bin, self.workflow, self.poll = pw_bin, workflow, poll

    async def run(self, identity, script, cwd=None, stdin=None, timeout=300):
        if not identity.cluster_name:
            return CommandResult(98, "", "no cluster name for resource")
        inputs = {
            "resource": identity.cluster_name,
            "script_b64": base64.b64encode(_wrap(script, cwd).encode()).decode(),
            "stdin_b64": base64.b64encode(stdin or b"").decode(),
        }
        start = await _spawn([self.pw_bin, "workflows", "run", "-o", "json", "-i", json.dumps(inputs), self.workflow], None, 60)
        start.check("pw workflows run")
        run = json.loads(start.stdout)
        slug = run.get("slug") or run.get("run", {}).get("slug")
        deadline = time.time() + timeout
        status = "running"
        while time.time() < deadline:
            view = await _spawn([self.pw_bin, "workflows", "runs", "view", "-o", "json", slug], None, 60)
            if view.returncode == 0:
                status = json.loads(view.stdout).get("status", status)
                if status in ("completed", "error", "canceled"):
                    break
            await asyncio.sleep(self.poll)
        logs = await _spawn([self.pw_bin, "workflows", "runs", "logs", "-o", "json", slug], None, 60)
        steps = json.loads(logs.stdout or "[]")
        if isinstance(steps, dict):
            steps = steps.get("steps") or steps.get("logs") or []
        text = "\n".join((step.get("logs") or step.get("log") or "") if isinstance(step, dict) else str(step) for step in steps)
        return parse_marked_output(text, default_rc=0 if status == "completed" else 1)


def parse_marked_output(text: str, default_rc: int = 1) -> CommandResult:
    """Extract the payload the iri-exec workflow prints between BEGIN and END markers."""
    if BEGIN in text and END in text:
        payload = text.split(BEGIN, 1)[1].split(END, 1)[0]
        trailer = text.split(END, 1)[1]
        rc = default_rc
        for line in trailer.splitlines():
            if line.startswith("rc="):
                rc = int(line[3:].strip() or default_rc)
        return CommandResult(rc, payload.lstrip("\n"), "")
    return CommandResult(default_rc, text, "")
