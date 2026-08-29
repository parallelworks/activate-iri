"""facility domain: the PW facility and its Sites (one Site per provider region or lab)."""
from __future__ import annotations

from app.routers.facility import facility_adapter
from app.routers.facility import models as facility_models
from fastapi import HTTPException

from .auth import ActivateAuthMixin
from .runtime import get_runtime


class FacilityAdapter(ActivateAuthMixin, facility_adapter.FacilityAdapter):
    async def get_facility(self, modified_since=None) -> facility_models.Facility | None:
        inv = await get_runtime().inventory()
        return inv.facility

    async def list_sites(self, modified_since=None, name=None, offset=None, limit=None, short_name=None) -> list[facility_models.Site]:
        inv = await get_runtime().inventory()
        items = facility_models.Site.find(inv.sites, name=name, modified_since=modified_since, short_name=short_name)
        return items[offset or 0:][: (limit if limit is not None else 100)]

    async def get_site(self, site_id: str, modified_since=None) -> facility_models.Site | None:
        inv = await get_runtime().inventory()
        site = facility_models.Site.find_by_id(inv.sites, site_id)
        if site is None:
            raise HTTPException(status_code=404, detail="Site not found")
        return site
