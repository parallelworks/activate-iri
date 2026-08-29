"""One class implementing all seven domains, for single-variable wiring."""
from .account import AccountAdapter
from .compute import ComputeAdapter
from .facility import FacilityAdapter
from .filesystem import FilesystemAdapter
from .status import StatusAdapter
from .storage import StorageAdapter
from .task import TaskAdapter


class ActivateAdapter(FacilityAdapter, StatusAdapter, AccountAdapter, ComputeAdapter, FilesystemAdapter, StorageAdapter, TaskAdapter):
    """Wire every IRI_API_ADAPTER_<domain> to activate_iri.combined.ActivateAdapter."""
