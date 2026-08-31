"""Process-wide runtime shared by every adapter instance.

The reference framework instantiates adapter classes with no arguments, several times per
process. Shared state (ACTIVATE client, inventory cache, executor, task queue, identity map)
therefore lives here, created lazily on first use.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .activate_client import ActivateClient, FakeActivateClient
from .config import Settings, load_facility_config
from .executor import Executor, LocalExecutor, RoutingExecutor, SSHExecutor, WorkflowExecutor
from .gateway import Gateway, GatewayConfig
from .inventory import Inventory, InventoryBuilder, StatusLedger


class Runtime:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.config = load_facility_config(self.settings.facility_file)
        if self.settings.fixtures_file:
            self.client = FakeActivateClient(self.settings.fixtures_file)
        else:
            self.client = ActivateClient(self.settings.activate_host, self.settings.activate_api_key)
        self.ledger = StatusLedger()
        self.builder = InventoryBuilder(self.config, self.ledger)
        self._inventory: Inventory | None = None
        self._lock = asyncio.Lock()
        self.executor: Executor = self._make_executor()
        from .auth import IdentityResolver  # local import: auth imports runtime
        from .tasks import TaskQueue

        self.identities = IdentityResolver(self.settings.user_map_file)
        self.tasks = TaskQueue(ttl_seconds=self.settings.task_ttl_seconds)
        self.service_user: str | None = None
        self.gateway: Gateway | None = Gateway(GatewayConfig.model_validate(self.config.gateway)) if self.config.gateway else None

    def _make_executor(self) -> Executor:
        s = self.settings
        kind = s.resolved_executor()
        local = LocalExecutor(run_as=s.local_run_as)
        workflow = WorkflowExecutor(s.pw_bin, s.exec_workflow, service_credential=s.activate_api_key)
        ssh = SSHExecutor(s.ssh_key, s.ssh_ca_key, s.ssh_jump, s.ssh_known_hosts)
        if kind == "local":
            return local
        if kind == "workflow":
            return workflow
        if kind == "auto":
            return RoutingExecutor(default=workflow, local=local, local_clusters=s.local_clusters, ssh=ssh, ssh_clusters=s.ssh_clusters)
        return ssh

    async def inventory(self, refresh: bool = False) -> Inventory:
        ttl = self.config.inventory.cache_ttl_seconds
        if not refresh and self._inventory and time.time() - self._inventory.built_at < ttl:
            return self._inventory
        async with self._lock:
            if not refresh and self._inventory and time.time() - self._inventory.built_at < ttl:
                return self._inventory
            if self.settings.fixtures_file and self.service_user is None:
                self.service_user = "fixtures"
            if self.service_user is None and self.settings.activate_api_key and not self.settings.fixtures_file:
                try:
                    self.service_user = await self.client.whoami(self.settings.activate_api_key)
                except Exception:  # noqa: BLE001
                    self.service_user = ""
            only = self.settings.edge_cluster if self.settings.mode == "edge" else None
            try:
                inventory = await self.builder.build(self.client, self.settings.activate_organization, self.service_user or None, only_cluster=only)
            except Exception as exc:  # noqa: BLE001  the control plane being unreachable must not take the facility down
                logging.getLogger(__name__).error("inventory refresh failed (%s); %s", exc,
                                                  "serving the previous inventory" if self._inventory else "publishing gateway upstreams only")
                if self._inventory:
                    self._inventory.built_at = time.time() - self.config.inventory.cache_ttl_seconds + 30
                    return self._inventory
                inventory = await self.builder.build(FakeActivateClient({}), None, None, only_cluster=only)
                inventory.degraded = str(exc)[:200]
            if self.gateway:
                await self.gateway.refresh()
                self.gateway.merge_into(inventory)
            self._inventory = inventory
            return self._inventory


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


def reset_runtime(settings: Settings | None = None) -> Runtime:
    """Used by tests and the CLI to rebuild the runtime with explicit settings."""
    global _runtime
    _runtime = Runtime(settings)
    return _runtime
