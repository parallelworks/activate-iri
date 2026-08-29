"""status domain: resources, events, incidents derived from ACTIVATE state."""
from __future__ import annotations

import datetime

from app.routers.status import facility_adapter
from app.routers.status import models as status_models
from app.types.models import Capability

from .runtime import get_runtime


def _page(items, offset, limit):
    return items[offset or 0:][: (limit if limit is not None else 100)]


class StatusAdapter(facility_adapter.FacilityAdapter):
    async def get_resources(self, offset: int, limit: int, name=None, description=None, group=None,
                            modified_since: datetime.datetime | None = None, resource_type=None, current_status=None,
                            capability: Capability | None = None, site_id=None) -> list[status_models.Resource]:
        inv = await get_runtime().inventory()
        items = status_models.Resource.find(inv.resources, name=name, description=description, modified_since=modified_since, group=group,
                                            resource_type=resource_type, current_status=current_status, capability=capability, site_id=site_id)
        return _page(items, offset, limit)

    async def get_resources_for_endpoint(self, endpoint: status_models.Endpoint) -> list[status_models.Resource]:
        inv = await get_runtime().inventory()
        return [r for r in inv.resources if endpoint in (r.supported_endpoints or [])]

    async def get_resource(self, id_: str) -> status_models.Resource:
        inv = await get_runtime().inventory()
        return inv.resource(id_)

    async def get_events(self, offset: int, limit: int, incident_id=None, resource_id=None, name=None, description=None, status=None,
                         from_=None, to=None, time_=None, modified_since=None) -> list[status_models.Event]:
        await get_runtime().inventory()
        items = status_models.Event.find(get_runtime().ledger.events, incident_id=incident_id, name=name, description=description,
                                         modified_since=modified_since, resource_id=resource_id, status=status, from_=from_, to=to, time_=time_)
        return _page(sorted(items, key=lambda e: e.occurred_at, reverse=True), offset, limit)

    async def get_event(self, id_: str) -> status_models.Event:
        await get_runtime().inventory()
        return status_models.Event.find_by_id(get_runtime().ledger.events, id_)

    async def get_incidents(self, offset: int, limit: int, name=None, description=None, status=None, type_=None, from_=None, to=None,
                            time_=None, modified_since=None, resource_id=None, resolution=None) -> list[status_models.Incident]:
        await get_runtime().inventory()
        items = status_models.Incident.find(get_runtime().ledger.incidents, name=name, description=description, modified_since=modified_since,
                                            status=status, type_=type_, from_=from_, to=to, time_=time_, resource_id=resource_id, resolution=resolution)
        return _page(sorted(items, key=lambda i: i.start, reverse=True), offset, limit)

    async def get_incident(self, id_: str) -> status_models.Incident:
        await get_runtime().inventory()
        return status_models.Incident.find_by_id(get_runtime().ledger.incidents, id_)
