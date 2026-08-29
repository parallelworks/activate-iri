#!/usr/bin/env python3
"""iri: call DOE IRI facilities from inside an ACTIVATE account (workspace terminal or workflow step).

Facilities and tokens come from a small config file (default ~/.iri/facilities.json) or from
ACTIVATE account variables named IRI_TOKEN_<FACILITY>. Every subcommand speaks IRI v1 or v2 as
the facility advertises. Examples:

    iri facilities                              # what is reachable, with status counts
    iri resources alcf                          # compute and storage resources at ALCF
    iri projects alcf                           # my projects and allocations (needs a token)
    iri submit alcf Polaris --exe /bin/hostname --queue debug --account myproj --dir /home/me
    iri status alcf Polaris 12345.polaris
    iri ls alcf Home /home/me                   # filesystem ops run through the task loop
    iri download pw labcluster /home/me/out.log

Config format (~/.iri/facilities.json):
    {"alcf": {"base_url": "https://api.alcf.anl.gov/api/v1", "token_env": "IRI_TOKEN_ALCF"},
     "pw":   {"base_url": "https://activate-iri.activate.pw/api/v2", "token_env": "PW_API_KEY"}}
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time

import httpx

DEFAULTS = {
    "alcf": {"base_url": "https://api.alcf.anl.gov/api/v1", "token_env": "IRI_TOKEN_ALCF"},
    "nersc": {"base_url": "https://api.iri.nersc.gov/api/v2", "token_env": "IRI_TOKEN_NERSC"},
    "esnet-east": {"base_url": "https://iri-dev.ppg.es.net/api/v1", "token_env": "IRI_TOKEN_ESNET"},
    "olcf-open": {"base_url": "https://amsc-open.s3m.olcf.ornl.gov/api/v1", "token_env": "IRI_TOKEN_OLCF"},
    "pw": {"base_url": "https://activate-iri.activate.pw/api/v2", "token_env": "PW_API_KEY"},
}


def load_config() -> dict:
    path = os.path.expanduser(os.environ.get("IRI_FACILITIES_FILE", "~/.iri/facilities.json"))
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    return cfg


class Facility:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.base = cfg["base_url"].rstrip("/")
        self.v2 = "/v2" in self.base
        self.token = os.environ.get(cfg.get("token_env", ""), "") or cfg.get("token", "")
        self.http = httpx.Client(timeout=30, follow_redirects=True)

    def _headers(self, auth: bool) -> dict:
        h = {"Accept": "application/json"}
        if auth:
            if not self.token:
                sys.exit(f"no token for {self.name}: set the token_env variable named in the config")
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, path: str, auth=False, **params):
        r = self.http.get(f"{self.base}{path}", headers=self._headers(auth), params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body=None, auth=True, **params):
        r = self.http.post(f"{self.base}{path}", headers={**self._headers(auth), "Content-Type": "application/json"}, json=body, params=params)
        r.raise_for_status()
        return r.json()

    def resource_id(self, name_or_id: str) -> str:
        for res in self.get("/status/resources", limit=500):
            if res.get("id") == name_or_id or (res.get("name") or "").lower() == name_or_id.lower():
                return res["id"]
        sys.exit(f"resource {name_or_id!r} not found at {self.name}")

    def fs(self, op: str, resource: str, body: dict, auth=True):
        """Filesystem op: v2 is POST with a JSON body; v1 uses GET/PUT/DELETE with query params."""
        rid = self.resource_id(resource)
        if self.v2:
            task = self.post(f"/filesystem/{op}/{rid}", body)
        else:
            method = {"mkdir": "POST", "symlink": "POST", "compress": "POST", "extract": "POST", "mv": "POST", "cp": "POST",
                      "chmod": "PUT", "chown": "PUT", "rm": "DELETE"}.get(op, "GET")
            kwargs = {"json": body} if method in ("POST", "PUT") else {"params": body}
            r = self.http.request(method, f"{self.base}/filesystem/{op}/{rid}", headers=self._headers(True), **kwargs)
            r.raise_for_status()
            task = r.json()
        return self.wait_task(task["task_uri"])

    def wait_task(self, task_uri: str, timeout: float = 180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self.http.get(task_uri, headers=self._headers(True)).json()
            if t.get("status") in ("completed", "failed", "canceled"):
                return t
            time.sleep(1.5)
        sys.exit("task timed out")


def cmd_facilities(args, cfg):
    for name, c in cfg.items():
        try:
            f = Facility(name, c)
            fac = f.get("/facility")
            res = f.get("/status/resources", limit=500)
            counts = {}
            for r in res:
                counts[r.get("current_status", "?")] = counts.get(r.get("current_status", "?"), 0) + 1
            print(f"{name:<12} {fac.get('short_name') or fac.get('name'):<28} {'v2' if f.v2 else 'v1'}  resources={len(res)} {counts}")
        except Exception as exc:
            print(f"{name:<12} unreachable: {str(exc)[:80]}")


def cmd_resources(args, cfg):
    f = Facility(args.facility, cfg[args.facility])
    for r in f.get("/status/resources", limit=500):
        rtype = (r.get("resource_type") or "").replace("urn:doe-iri:resource:", "")
        print(f"{r.get('current_status', '?'):<9} {rtype:<26} {r.get('name', ''):<32} {r.get('id')}")


def cmd_projects(args, cfg):
    f = Facility(args.facility, cfg[args.facility])
    for p in f.get("/account/projects", auth=True):
        print(f"{p['name']}  ({p['id']})")
        for pa in f.get(f"/account/projects/{p['id']}/project_allocations", auth=True):
            for e in pa.get("entries", []):
                print(f"    {e.get('unit')}: {e.get('usage')} / {e.get('allocation')}")


def cmd_submit(args, cfg):
    f = Facility(args.facility, cfg[args.facility])
    rid = f.resource_id(args.resource)
    spec = {"executable": args.exe, "arguments": args.args or [], "name": args.name, "directory": args.dir,
            "stdout_path": f"{args.dir}/{args.name}.out", "stderr_path": f"{args.dir}/{args.name}.err",
            "resources": {"node_count": args.nodes, "processes_per_node": args.ppn},
            "attributes": {"duration": args.walltime, "queue_name": args.queue, **({"account": args.account} if args.account else {})}}
    if args.gpus:
        spec["resources"]["gpu_cores_per_process"] = args.gpus
    if args.image:
        spec["container"] = {"image": args.image}
    job = f.post(f"/compute/job/{rid}", spec)
    print(json.dumps(job, indent=1))
    if args.wait:
        while True:
            s = f.get(f"/compute/status/{rid}/{job['id']}", auth=True, historical="true")
            state = s.get("status", {}).get("state")
            print(time.strftime("%H:%M:%S"), state)
            if state in ("completed", "failed", "canceled"):
                break
            time.sleep(args.poll)
        if args.fetch:
            t = f.fs("download", args.resource, {"path": spec["stdout_path"]})
            if t.get("status") == "completed":
                print(base64.b64decode(t["result"]["output"]).decode(errors="replace"))


def cmd_status(args, cfg):
    f = Facility(args.facility, cfg[args.facility])
    rid = f.resource_id(args.resource)
    print(json.dumps(f.get(f"/compute/status/{rid}/{args.job_id}", auth=True, historical="true"), indent=1))


def cmd_jobs(args, cfg):
    f = Facility(args.facility, cfg[args.facility])
    rid = f.resource_id(args.resource)
    for j in f.post(f"/compute/status/{rid}", {}, historical="true", limit=50):
        print(f"{j['id']:<16} {j.get('status', {}).get('state', '?')}")


def cmd_cancel(args, cfg):
    f = Facility(args.facility, cfg[args.facility])
    rid = f.resource_id(args.resource)
    f.http.delete(f"{f.base}/compute/cancel/{rid}/{args.job_id}", headers=f._headers(True)).raise_for_status()
    print("canceled")


def cmd_fs(args, cfg):
    f = Facility(args.facility, cfg[args.facility])
    body = {"path": args.path}
    if args.op == "mkdir":
        body["parent"] = True
    t = f.fs(args.op, args.resource, body)
    out = (t.get("result") or {}).get("output")
    if args.op == "ls" and isinstance(out, list):
        for e in out:
            print(f"{e.get('permissions')} {e.get('user'):<10} {e.get('size'):>10} {e.get('last_modified')} {e.get('name')}")
    elif args.op == "download" and isinstance(out, str):
        sys.stdout.write(base64.b64decode(out).decode(errors="replace"))
    else:
        print(json.dumps(t, indent=1))


def main(argv=None) -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="iri", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("facilities").set_defaults(func=cmd_facilities)
    p = sub.add_parser("resources"); p.add_argument("facility"); p.set_defaults(func=cmd_resources)
    p = sub.add_parser("projects"); p.add_argument("facility"); p.set_defaults(func=cmd_projects)
    p = sub.add_parser("submit"); p.add_argument("facility"); p.add_argument("resource")
    p.add_argument("--exe", required=True); p.add_argument("--args", nargs="*"); p.add_argument("--name", default="iri-job")
    p.add_argument("--dir", required=True); p.add_argument("--queue", default="debug"); p.add_argument("--account")
    p.add_argument("--nodes", type=int, default=1); p.add_argument("--ppn", type=int, default=1); p.add_argument("--gpus", type=int, default=0)
    p.add_argument("--walltime", type=int, default=600); p.add_argument("--image"); p.add_argument("--wait", action="store_true")
    p.add_argument("--fetch", action="store_true", help="after --wait, print stdout via the filesystem API"); p.add_argument("--poll", type=int, default=10)
    p.set_defaults(func=cmd_submit)
    p = sub.add_parser("status"); p.add_argument("facility"); p.add_argument("resource"); p.add_argument("job_id"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("jobs"); p.add_argument("facility"); p.add_argument("resource"); p.set_defaults(func=cmd_jobs)
    p = sub.add_parser("cancel"); p.add_argument("facility"); p.add_argument("resource"); p.add_argument("job_id"); p.set_defaults(func=cmd_cancel)
    for op in ("ls", "stat", "file", "checksum", "download", "mkdir", "rm", "head", "tail"):
        p = sub.add_parser(op); p.add_argument("facility"); p.add_argument("resource"); p.add_argument("path"); p.set_defaults(func=cmd_fs, op=op)
    args = ap.parse_args(argv)
    if getattr(args, "facility", None) and args.facility not in cfg:
        sys.exit(f"unknown facility {args.facility!r}; known: {', '.join(cfg)}")
    args.func(args, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
