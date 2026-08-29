"""Process-wide runtime shared by every adapter instance.

The reference framework instantiates adapter classes with no arguments, several times per
process. Shared state (ACTIVATE client, inventory cache, executor, task queue, identity map)
therefore lives here, created lazily on first use.
"""
from __future__ import annotations

import asyncio
import time

from .activate_client import ActivateClient, FakeActivateClient
from .config import Settings, load_facility_config
from .executor import Executor, LocalExecutor, SSHExecutor, WorkflowExecutor
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

    def _make_executor(self) -> Executor:
        kind = self.settings.resolved_executor()
        if kind == "local":
            return LocalExecutor(run_as=self.settings.local_run_as)
        if kind == "workflow":
            return WorkflowExecutor(self.settings.pw_bin, self.settings.exec_workflow)
        return SSHExecutor(self.settings.ssh_key, self.settings.ssh_ca_key, self.settings.ssh_jump, self.settings.ssh_known_hosts)

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
            self._inventory = await self.builder.build(self.client, self.settings.activate_organization, self.service_user or None, only_cluster=only)
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
