"""account domain: capabilities, projects, and allocations from ACTIVATE allocations.

ACTIVATE's accounting object is the allocation (a budget with a unit, a total, and rated
usage, optionally nested under a parent). The IRI Project is the top-level allocation a
caller can see; each Project has one ProjectAllocation per Capability the caller can reach,
and a UserAllocation that mirrors it (ACTIVATE meters per user through usage events, but does
not split the budget per user, so the user's ceiling is the project's ceiling).
"""
from __future__ import annotations

import logging

from app.routers.account import facility_adapter
from app.routers.account import models as account_models
from app.types.models import Capability
from app.types.scalars import AllocationUnit
from app.types.user import User

from .auth import ActivateAuthMixin
from .gateway import split_id
from .inventory import now, stable_id
from .runtime import get_runtime

logger = logging.getLogger(__name__)


def unit_urn(unit: str | None, cfg) -> str:
    u = (unit or "").strip().lower()
    if u in cfg.node_hour_units:
        return AllocationUnit.node_hours
    if u in ("bytes", "byte", "b"):
        return AllocationUnit.bytes
    if u in ("inodes", "inode"):
        return AllocationUnit.inodes
    return cfg.local_unit_urn


class AccountAdapter(ActivateAuthMixin, facility_adapter.FacilityAdapter):
    async def _allocations_for(self, user: User) -> list[dict]:
        rt = get_runtime()
        credential = user.api_key if user.api_key.startswith("pwt_") or user.api_key.count(".") == 2 and not user.api_key.startswith("ey") else None
        if credential and not rt.settings.fixtures_file:
            try:
                return await rt.client.allocations(None, credential=credential)
            except Exception as exc:  # noqa: BLE001  fall back to the service view
                logger.debug("user-scoped allocation read failed, using service account view: %s", exc)
        allocations = await rt.client.allocations(rt.settings.activate_organization)
        account = rt.identities.account_for(user)
        if account:
            allocations = [a for a in allocations if a.get("name") == account or a.get("parent") == account]
        return allocations

    async def get_capabilities(self, name=None, modified_since=None, offset: int = 0, limit: int = 1000) -> list[Capability]:
        inv = await get_runtime().inventory()
        items = Capability.find(list(inv.capabilities.values()), name=name, modified_since=modified_since)
        return items[offset:][:limit]

    async def get_projects(self, user: User) -> list[account_models.Project]:
        allocations = await self._allocations_for(user)
        projects = []
        rt = get_runtime()
        if rt.gateway:
            for fac in rt.gateway.upstreams:
                if rt.gateway.token_for(fac, caller_token=user.api_key):
                    try:
                        projects.extend(await rt.gateway.projects(fac, user.api_key))
                    except Exception as exc:  # noqa: BLE001  one facility's failure must not hide the others
                        logger.warning("gateway projects from %s failed: %s", fac, exc)
        for alloc in allocations:
            if alloc.get("parent"):
                continue
            projects.append(account_models.Project(
                id=stable_id("project", alloc["name"]), name=alloc["name"],
                description=alloc.get("description") or f"ACTIVATE allocation {alloc['name']} ({alloc.get('unit') or 'units'})",
                user_ids=[user.id], last_modified=now(), attributes={"activate_allocation": alloc["name"], "unit": alloc.get("unit")},
            ))
        return projects

    async def get_project_allocations(self, project: account_models.Project, user: User) -> list[account_models.ProjectAllocation]:
        rt = get_runtime()
        fac, uid = split_id(project.id)
        if fac and rt.gateway and fac in rt.gateway.upstreams:
            return await rt.gateway.project_allocations(fac, uid, user.api_key)
        inv = await rt.inventory()
        cfg = rt.config.allocation
        allocations = await self._allocations_for(user)
        alloc = next((a for a in allocations if a["name"] == (project.attributes or {}).get("activate_allocation") or a["name"] == project.name), None)
        if alloc is None:
            return []
        entry = account_models.AllocationEntry(allocation=float(alloc.get("total") or 0), usage=float(alloc.get("used") or 0), unit=unit_urn(alloc.get("unit"), cfg))
        out = []
        for cap_id in inv.capabilities:
            out.append(account_models.ProjectAllocation(
                id=stable_id("project-allocation", project.id, cap_id), project_id=project.id, capability_id=cap_id, entries=[entry],
                attributes={"activate_allocation": alloc["name"], "estimated_usage": alloc.get("estimatedUsed")},
            ))
        return out

    async def get_user_allocations(self, user: User, project_allocation: account_models.ProjectAllocation) -> list[account_models.UserAllocation]:
        return [account_models.UserAllocation(
            id=stable_id("user-allocation", project_allocation.id, user.id), project_id=project_allocation.project_id,
            project_allocation_id=project_allocation.id, user_id=user.id, entries=list(project_allocation.entries),
        )]
