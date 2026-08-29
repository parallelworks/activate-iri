"""activate-iri: DOE IRI Facility API v2 adapters backed by Parallel Works ACTIVATE.

One adapter package, two deployment shapes:

* federation mode: the endpoint runs next to (or inside) the ACTIVATE control plane and
  publishes every cluster, storage system, and inference gateway the platform federates
  (cloud, NeoCloud, and existing on-premises clusters) as one IRI facility with one Site
  per provider region or lab.
* edge mode: the same container runs on the login node of a single existing cluster that
  has nothing else deployed. The PW agent connects the cluster to ACTIVATE outbound-only,
  and `pw endpoints` publishes the IRI endpoint through the platform's reverse tunnel.

Every domain adapter is wired through the reference framework's IRI_API_ADAPTER_<domain>
environment variables, so a facility can mix these adapters with its own per domain.
"""

__version__ = "0.1.0"
