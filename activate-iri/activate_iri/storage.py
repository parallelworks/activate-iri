"""storage domain (v2): logical storage locations per user and data access endpoints."""
from __future__ import annotations

from app.routers.status import models as status_models
from app.routers.storage import facility_adapter
from app.routers.storage import models as storage_models
from app.types.user import User

from .auth import ActivateAuthMixin
from .runtime import get_runtime


class StorageAdapter(ActivateAuthMixin, facility_adapter.FacilityAdapter):
    async def get_locations(self, resource: status_models.Resource, user: User, logicalpath, project, allocation, intent) -> list[storage_models.StorageInstance]:
        rt = get_runtime()
        inv = await rt.inventory()
        cluster = inv.cluster_for(resource.id)
        templates = inv.storage_locations.get(inv.clusters_by_resource and next((rid for rid, c in inv.clusters_by_resource.items() if c is cluster), resource.id), [])
        posix_user = rt.identities.resolve(user, cluster).posix_user
        effective_project = project or allocation or rt.identities.account_for(user) or ""
        out = []
        for tmpl in templates:
            if logicalpath and tmpl.logical_name != logicalpath:
                continue
            if intent == storage_models.StorageIntent.long_term_storage and tmpl.logical_name != storage_models.LogicalName.archive:
                continue
            if intent == storage_models.StorageIntent.staging and tmpl.logical_name == storage_models.LogicalName.archive:
                continue
            if intent == storage_models.StorageIntent.write and not tmpl.access.write:
                continue
            path = tmpl.path.replace("{user}", posix_user).replace("{first}", posix_user[:1]).replace("{project}", effective_project)
            if "{project}" in tmpl.path and not effective_project:
                continue
            out.append(tmpl.model_copy(update={"path": path}))
        return out

    async def get_access_endpoints(self, resource: status_models.Resource, user: User, protocol, endpoint_id) -> list[storage_models.AccessEndpoint]:
        inv = await get_runtime().inventory()
        items = inv.access_endpoints.get(resource.id, [])
        if protocol:
            items = [e for e in items if e.protocol == protocol]
        if endpoint_id:
            items = [e for e in items if e.id == endpoint_id]
        return items
