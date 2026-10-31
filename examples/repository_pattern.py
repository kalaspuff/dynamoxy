"""The repository pattern: domain repositories, a service layer and a unit of work.

Services depend on repositories with intention-revealing methods instead of raw DynamoDB calls,
and multi-item business operations run as one transaction.

Run: uv run python examples/repository_pattern.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import dynamoxy
from dynamoxy import Attr, GlobalIndex, Model, field, partition_key, sort_key


class Project(Model, table="pm-projects"):
    project_id: Attr[str] = partition_key()
    name: Attr[str]
    open_tasks: Attr[int] = field(default=0, ge=0)
    version: Attr[str] = dynamoxy.version()


class Task(Model, table="pm-tasks"):
    project_id: Attr[str] = partition_key()
    task_id: Attr[str] = sort_key()
    title: Attr[str] = field(min_length=1, max_length=200)
    assignee: Attr[str | None] = field(default=None)
    done_at: Attr[datetime | None] = field(default=None)

    by_assignee = GlobalIndex(partition_key=assignee, sort_key=task_id)


class ProjectRepository(dynamoxy.Repository[Project]):
    async def require(self, project_id: str) -> Project:
        return await self.get_or_raise(project_id)


class TaskRepository(dynamoxy.Repository[Task]):
    async def open_tasks_of(self, assignee: str) -> list[Task]:
        return await self.query(index=Task.by_assignee, assignee=assignee).filter(Task.done_at.not_exists()).all()


class TaskService:
    """Business operations; every multi-item change is one transaction (a unit of work)."""

    def __init__(self, client: dynamoxy.Client) -> None:
        self.client = client
        self.projects = ProjectRepository(client)
        self.tasks = TaskRepository(client)

    async def add_task(self, project_id: str, task_id: str, title: str, assignee: str) -> Task:
        project = await self.projects.require(project_id)
        task = Task(project_id=project_id, task_id=task_id, title=title, assignee=assignee)
        async with self.client.transaction() as tx:
            tx.create(task)
            tx.update(project, Project.open_tasks.add(1))  # also checks the project's version
        return task

    async def complete(self, project_id: str, task_id: str) -> None:
        async with self.client.transaction() as tx:
            tx.update(
                Task.key(project_id, task_id),
                Task.done_at.set(datetime.now(UTC)),
                condition=Task.done_at.not_exists(),  # completing twice fails the transaction
            )
            tx.update(Project.key(project_id), Project.open_tasks.add(-1), model=Project)


async def main(client: dynamoxy.Client | None = None) -> int:
    """Runs on the in-memory DynamoDB unless a client is given."""
    async with client or dynamoxy.testing.memory_client() as db:
        await db.ensure_tables(Project, Task)
        service = TaskService(db)
        await service.projects.create(Project(project_id="p1", name="Launch"))

        await service.add_task("p1", "t1", "Write docs", assignee="ada")
        await service.add_task("p1", "t2", "Ship it", assignee="ada")
        await service.complete("p1", "t1")
        try:
            await service.complete("p1", "t1")
        except dynamoxy.TransactionCanceledError:
            print("t1 was already done")

        open_tasks = await service.tasks.open_tasks_of("ada")
        project = await service.projects.require("p1")
        print("ada's open tasks:", [task.title for task in open_tasks], "| project counter:", project.open_tasks)
        return project.open_tasks


if __name__ == "__main__":
    asyncio.run(main())
