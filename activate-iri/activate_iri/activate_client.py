"""Minimal async client for the ACTIVATE REST API (platform v6.x, see pw-sdk/openapi.json).

Only the endpoints the adapters need are wrapped. Two credential shapes are accepted, matching
parallelworks-client: an API key ``pwt_<b64(host)>.<secret>`` sent as HTTP Basic (key as the
username, empty password), or a platform JWT sent as a Bearer token.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx


def auth_header(credential: str) -> str:
    if credential.startswith("pwt_"):
        return "Basic " + base64.b64encode(f"{credential}:".encode()).decode()
    return f"Bearer {credential}"


class ActivateError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"ACTIVATE API {status}: {message}")
        self.status = status


class ActivateClient:
    """Service-scoped client. Per-user checks use :meth:`whoami` with the caller's credential."""

    def __init__(self, host: str, credential: str | None, timeout: float = 30.0):
        self.host = host.rstrip("/")
        self.credential = credential
        self._http = httpx.AsyncClient(base_url=self.host, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, credential: str | None = None, **kwargs) -> Any:
        cred = credential or self.credential
        headers = dict(kwargs.pop("headers", {}) or {})
        if cred:
            headers["Authorization"] = auth_header(cred)
        response = await self._http.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise ActivateError(response.status_code, message)
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        return response.json() if "json" in content_type else response.text

    async def get(self, path: str, **kw) -> Any:
        return await self._request("GET", path, **kw)

    async def post(self, path: str, **kw) -> Any:
        return await self._request("POST", path, **kw)

    async def delete(self, path: str, **kw) -> Any:
        return await self._request("DELETE", path, **kw)

    # -- identity -----------------------------------------------------------------------
    async def whoami(self, credential: str) -> str:
        value = await self.get("/api/auth/whoami", credential=credential)
        return value.strip().strip('"') if isinstance(value, str) else str(value)

    async def session(self, credential: str) -> dict:
        return await self.get("/api/auth/session", credential=credential)

    async def ssh_public_keys(self, username: str) -> str:
        value = await self.get(f"/api/users/{username}/ssh-public-keys")
        return value if isinstance(value, str) else ""

    # -- inventory ----------------------------------------------------------------------
    async def clusters(self) -> list[dict]:
        return await self.get("/api/clusters") or []

    async def managed_cluster(self, organization: str, name: str) -> dict:
        return await self.get(f"/api/organizations/{organization}/managed-clusters/{name}")

    async def scheduler_jobs(self, cluster: str) -> list[dict]:
        return await self.get("/api/scheduler-jobs", params={"cluster": cluster}) or []

    async def buckets(self) -> list[dict]:
        return await self.get("/api/buckets") or []

    async def lustre(self) -> list[dict]:
        return await self.get("/api/lustre") or []

    async def nfs(self) -> list[dict]:
        return await self.get("/api/nfs") or []

    async def aichat_providers(self, organization: str, user: str) -> list[dict]:
        return await self.get(f"/api/organizations/{organization}/users/{user}/aichat-providers") or []

    async def settings(self) -> dict:
        return await self.get("/api/settings") or {}

    async def alerts(self) -> list[dict]:
        try:
            return await self.get("/api/admin/alerts") or []
        except ActivateError as exc:  # platform-admin only; a service key without it just sees no alerts
            if exc.status in (401, 403):
                return []
            raise

    # -- accounting ---------------------------------------------------------------------
    async def allocations(self, organization: str | None, credential: str | None = None) -> list[dict]:
        if organization and not credential:
            try:
                return await self.get(f"/api/organizations/{organization}/allocations") or []
            except ActivateError as exc:
                if exc.status not in (401, 403):
                    raise
        return await self.get("/api/allocations", credential=credential) or []

    async def groups(self, organization: str) -> list[dict]:
        return await self.get(f"/api/organizations/{organization}/groups") or []

    async def post_usage(self, organization: str, allocation: str, sku: str, quantity: float,
                         started_at: str, ended_at: str, user: str | None = None, metadata: dict | None = None) -> dict:
        body = {"sku": sku, "quantity": quantity, "startedAt": started_at, "endedAt": ended_at}
        if user:
            body["user"] = user
        if metadata:
            body["metadata"] = metadata
        return await self.post(f"/api/organizations/{organization}/allocations/{allocation}/usage", json=body)

    # -- workflows ----------------------------------------------------------------------
    async def start_run(self, workflow: str, inputs: dict, cluster_id: str | None = None) -> dict:
        body = {"inputs": inputs, "appInfo": {"clusterId": cluster_id, "sessionNames": {}}}
        return await self.post(f"/api/workflows/{workflow}/runs", json=body)

    async def delete_run(self, workflow: str, number: int) -> None:
        await self.delete(f"/api/workflows/{workflow}/runs/{number}")


class FakeActivateClient(ActivateClient):
    """Fixture-backed client for tests and offline demos (no platform account required).

    The fixture is a JSON object with optional keys: clusters, managed_clusters (name -> detail),
    scheduler_jobs (cluster -> list), buckets, lustre, nfs, aichat_providers, settings, alerts,
    allocations, groups, users (credential -> username).
    """

    def __init__(self, fixtures: dict | str):
        self.host = "https://fixtures.invalid"
        self.credential = None
        if isinstance(fixtures, str):
            with open(fixtures, encoding="utf-8") as fh:
                fixtures = json.load(fh)
        self.f = fixtures
        self.usage_events: list[dict] = []
        self.runs: list[dict] = []

    async def aclose(self) -> None:
        return None

    async def whoami(self, credential: str) -> str:
        users = self.f.get("users", {})
        if credential in users:
            return users[credential]
        raise ActivateError(401, "invalid credential")

    async def session(self, credential: str) -> dict:
        name = await self.whoami(credential)
        return {"username": name, "name": name, "organization": self.f.get("organization", "demo-org")}

    async def ssh_public_keys(self, username: str) -> str:
        return ""

    async def clusters(self) -> list[dict]:
        return list(self.f.get("clusters", []))

    async def managed_cluster(self, organization: str, name: str) -> dict:
        detail = self.f.get("managed_clusters", {}).get(name)
        if detail is None:
            raise ActivateError(404, f"managed cluster {name} not found")
        return detail

    async def scheduler_jobs(self, cluster: str) -> list[dict]:
        return list(self.f.get("scheduler_jobs", {}).get(cluster, []))

    async def buckets(self) -> list[dict]:
        return list(self.f.get("buckets", []))

    async def lustre(self) -> list[dict]:
        return list(self.f.get("lustre", []))

    async def nfs(self) -> list[dict]:
        return list(self.f.get("nfs", []))

    async def aichat_providers(self, organization: str, user: str) -> list[dict]:
        return list(self.f.get("aichat_providers", []))

    async def settings(self) -> dict:
        return dict(self.f.get("settings", {"version": "6.12.0", "maintenanceMode": False}))

    async def alerts(self) -> list[dict]:
        return list(self.f.get("alerts", []))

    async def allocations(self, organization: str | None, credential: str | None = None) -> list[dict]:
        return list(self.f.get("allocations", []))

    async def groups(self, organization: str) -> list[dict]:
        return list(self.f.get("groups", []))

    async def post_usage(self, organization, allocation, sku, quantity, started_at, ended_at, user=None, metadata=None) -> dict:
        event = {"allocation": allocation, "sku": sku, "quantity": quantity, "startedAt": started_at,
                 "endedAt": ended_at, "user": user, "metadata": metadata, "createdAt": time.time()}
        self.usage_events.append(event)
        return event

    async def start_run(self, workflow: str, inputs: dict, cluster_id: str | None = None) -> dict:
        run = {"id": f"run-{len(self.runs) + 1}", "number": len(self.runs) + 1, "status": "running",
               "workflowName": workflow, "inputs": inputs}
        self.runs.append(run)
        return {"run": run, "redirect": ""}

    async def delete_run(self, workflow: str, number: int) -> None:
        return None
