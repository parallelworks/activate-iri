"""task domain: asynchronous execution of filesystem commands (see tasks.py)."""
from __future__ import annotations

from app.routers.status import models as status_models
from app.routers.task import facility_adapter
from app.routers.task import models as task_models
from app.types.user import User

from .auth import ActivateAuthMixin
from .runtime import get_runtime


class TaskAdapter(ActivateAuthMixin, facility_adapter.FacilityAdapter):
    async def get_task(self, user: User, task_id: str) -> task_models.Task | None:
        return get_runtime().tasks.get(user, task_id)

    async def get_tasks(self, user: User) -> list[task_models.Task]:
        return get_runtime().tasks.list(user)

    async def put_task(self, user: User, resource: status_models.Resource | None, task: task_models.TaskCommand) -> task_models.TaskSubmitResponse:
        return await get_runtime().tasks.submit(user, resource, task)

    async def delete_task(self, user: User, task_id: str) -> None:
        get_runtime().tasks.cancel(user, task_id)
