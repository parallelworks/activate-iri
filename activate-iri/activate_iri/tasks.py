"""Asynchronous task queue for filesystem operations (IRI task domain).

The framework turns every filesystem call into a TaskCommand and expects the task adapter to
run it later and expose its state. This queue executes commands on an asyncio worker inside
the API process, keeps results for a TTL, and scopes visibility to the submitting user. It is
sufficient for a single replica; a multi-replica deployment swaps in a Redis-backed queue with
the same interface (submit/get/list/cancel).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from app.routers.status import models as status_models
from app.routers.task import facility_adapter as task_adapter
from app.routers.task import models as task_models
from app.types.user import User
from pydantic import BaseModel


@dataclass
class QueuedTask:
    id: str
    user_id: str
    resource: status_models.Resource | None
    user: User
    command: task_models.TaskCommand
    status: task_models.TaskStatus = task_models.TaskStatus.pending
    result: dict | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def to_model(self) -> task_models.Task:
        return task_models.Task(id=self.id, status=self.status, result=self.result, command=self.command)


class TaskQueue:
    def __init__(self, ttl_seconds: int = 3600, concurrency: int = 8):
        self.ttl = ttl_seconds
        self.tasks: dict[str, QueuedTask] = {}
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task] = []
        self.concurrency = concurrency

    def _ensure_workers(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            self._workers = [loop.create_task(self._worker()) for _ in range(self.concurrency)]

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            task_id = await self._queue.get()
            task = self.tasks.get(task_id)
            if task is None or task.status == task_models.TaskStatus.canceled:
                self._queue.task_done()
                continue
            task.status = task_models.TaskStatus.active
            try:
                result, status = await task_adapter.FacilityAdapter.on_task(task.resource, task.user, task.command)
                if isinstance(result, BaseModel):
                    task.result = result.model_dump()
                elif isinstance(result, dict):
                    task.result = result
                else:
                    task.result = {"output": result}
                task.status = status
            except Exception as exc:  # noqa: BLE001  on_task already catches; this is belt and braces
                task.result = {"output": f"Error: {exc}"}
                task.status = task_models.TaskStatus.failed
            task.finished = time.time()
            self._queue.task_done()

    def _expire(self) -> None:
        cutoff = time.time() - self.ttl
        for task_id in [t.id for t in self.tasks.values() if t.finished and t.finished < cutoff]:
            self.tasks.pop(task_id, None)

    async def submit(self, user: User, resource: status_models.Resource | None, command: task_models.TaskCommand) -> task_models.TaskSubmitResponse:
        self._ensure_workers()
        self._expire()
        task = QueuedTask(id=str(uuid.uuid4()), user_id=user.id, resource=resource, user=user, command=command)
        self.tasks[task.id] = task
        await self._queue.put(task.id)
        return task_models.TaskSubmitResponse(task_id=task.id)

    def get(self, user: User, task_id: str) -> task_models.Task | None:
        task = self.tasks.get(task_id)
        if task is None or task.user_id != user.id:
            return None
        return task.to_model()

    def list(self, user: User) -> list[task_models.Task]:
        self._expire()
        return [t.to_model() for t in self.tasks.values() if t.user_id == user.id]

    def cancel(self, user: User, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if task and task.user_id == user.id and task.status in (task_models.TaskStatus.pending, task_models.TaskStatus.active):
            task.status = task_models.TaskStatus.canceled
            task.result = None
            task.finished = time.time()
