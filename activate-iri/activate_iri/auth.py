"""Authentication and identity mapping.

Three credential classes reach the same endpoint:

1. AmSC Keycard (PingAM JWT). The framework validates it (JWKS, iss, aud, exp, sub,
   amsc_project_context) and calls ``get_current_user_amsc``; this module maps the project
   context to an ACTIVATE username through the mapping file that the reference framework
   already defines (AMSC_PROJECT_MAPPING_FILE). Values may be a bare username or an object
   with ``user``, ``posix_user``, and ``account`` (the scheduler account to charge).
2. ACTIVATE API key or platform JWT (facility-specific authentication, IRI Q2). Verified by
   asking the platform who the caller is; the ACTIVATE username is the IRI user id.
3. Registered service credentials for machine callers (AmSC-IRO consumers) are ACTIVATE API
   keys for project service accounts; nothing extra is needed.

POSIX identity: on ACTIVATE managed clusters the platform provisions the same usernames it
authenticates (the agent's heartbeat carries uid/gid/groups down to every node), so the default
is posix_user == ACTIVATE username. ACTIVATE_IRI_USER_MAP_FILE overrides per user or per cluster.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from app.types.user import User
from fastapi import HTTPException

from .executor import ExecIdentity
from .runtime import get_runtime


@dataclass
class MappedIdentity:
    user: str
    posix_user: str | None = None
    account: str | None = None


def is_activate_credential(value: str | None) -> bool:
    """ACTIVATE API keys start with pwt_; platform tokens are JWTs. AmSC Keycards are also JWTs, so a
    Keycard caller is recognized by the framework before this is consulted (see get_current_user_amsc)."""
    if not value:
        return False
    if value.startswith("pwt_"):
        return True
    parts = value.split(".")
    return len(parts) == 3 and value.startswith("eyJ") and value not in _keycards_seen


_keycards_seen: set[str] = set()


class _Cache:
    def __init__(self, ttl: float = 300.0):
        self.ttl, self.data = ttl, {}

    def get(self, key):
        item = self.data.get(key)
        if item and item[0] > time.time():
            return item[1]
        return None

    def put(self, key, value):
        self.data[key] = (time.time() + self.ttl, value)


_whoami_cache = _Cache(ttl=120.0)


def _load_json(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh) or {}


def resolve_amsc_mapping(project_context: str, mapping_file: str | None) -> MappedIdentity:
    data = _load_json(mapping_file).get("project_mapping") or {}
    entry = data.get(project_context)
    if entry is None:
        raise ValueError(f"No local mapping for AmSC project '{project_context}'")
    if isinstance(entry, str):
        return MappedIdentity(user=entry)
    return MappedIdentity(user=entry["user"], posix_user=entry.get("posix_user"), account=entry.get("account"))


class ActivateAuthMixin:
    """Mix in first: ``class X(ActivateAuthMixin, facility_adapter.FacilityAdapter)``."""

    async def get_current_user(self, api_key: str, client_ip: str | None) -> str:
        cached = _whoami_cache.get(api_key)
        if cached:
            return cached
        rt = get_runtime()
        try:
            username = await rt.client.whoami(api_key)
        except Exception as exc:  # any platform rejection is a 401 to the caller
            raise HTTPException(status_code=401, detail=f"ACTIVATE rejected the credential: {exc}") from exc
        _whoami_cache.put(api_key, username)
        return username

    async def get_current_user_amsc(self, api_key: str, client_ip: str | None, amsc_claims: dict) -> str:
        rt = get_runtime()
        mapped = resolve_amsc_mapping(amsc_claims["amsc_project_context"], rt.settings.amsc_mapping_file)
        rt.identities.remember(mapped)
        _keycards_seen.add(api_key)   # a Keycard is not an ACTIVATE credential; jobs run under the endpoint's account
        return mapped.user

    async def get_user(self, user_id: str, api_key: str, client_ip: str | None) -> User:
        return User(id=user_id, name=user_id, api_key=api_key, client_ip=client_ip)


class IdentityResolver:
    """Turns an IRI User plus a target resource into the account the executor runs as."""

    def __init__(self, user_map_file: str | None):
        self.user_map_file = user_map_file
        self._amsc: dict[str, MappedIdentity] = {}

    def remember(self, mapped: MappedIdentity) -> None:
        self._amsc[mapped.user] = mapped

    def account_for(self, user: User) -> str | None:
        mapped = self._amsc.get(user.id)
        return mapped.account if mapped else None

    def resolve(self, user: User, cluster: dict | None) -> ExecIdentity:
        overrides = _load_json(self.user_map_file)
        posix = user.id
        mapped = self._amsc.get(user.id)
        if mapped and mapped.posix_user:
            posix = mapped.posix_user
        per_user = overrides.get("users", {}).get(user.id)
        if isinstance(per_user, str):
            posix = per_user
        elif isinstance(per_user, dict):
            posix = per_user.get("posix_user", posix)
            if cluster and cluster.get("name") in per_user.get("clusters", {}):
                posix = per_user["clusters"][cluster["name"]]
        host = None
        if cluster:
            host = cluster.get("loginNode") if cluster.get("loginNode") not in (None, "", "user-workspace") else None
            host = host or cluster.get("ipAddress") or None
            conn = cluster.get("connectionString") or ""
            if not host and "@" in conn:
                host = conn.split("@", 1)[1]
        credential = user.api_key if is_activate_credential(user.api_key) else None
        return ExecIdentity(posix_user=posix, host=host, platform_user=user.id, credential=credential,
                            cluster_id=cluster.get("id") if cluster else None, cluster_name=cluster.get("name") if cluster else None)
