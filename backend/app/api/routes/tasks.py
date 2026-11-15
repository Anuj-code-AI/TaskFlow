from datetime import datetime, timezone
import hashlib, json
from fastapi import APIRouter, Depends, Header
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.errors import not_found, forbidden, conflict, bad_request
from app.api.dependencies import current_user, workspace_member, require_admin
from app.models import User, Task, Project, WorkspaceMember, Role, TaskStatus, Priority, TaskComment, Notification, TaskDependency, IdempotencyRecord, Workspace
from app.schemas import TaskCreate, TaskUpdate, TaskAssign, TaskTransition, TaskOut, CommentCreate, CommentOut, DependencyCreate, DependencyOut, TaskArchive
from app.services.common import add_audit, add_outbox

router = APIRouter(prefix="/workspaces/{workspace_id}/tasks", tags=["Tasks"])
from app.services.workflow import ALLOWED_TRANSITIONS, would_create_cycle, dependencies_must_be_complete


async def get_task(db, workspace_id, task_id, for_update: bool = False):
    query = select(Task).where(
        Task.id == task_id,
        Task.workspace_id == workspace_id,
        Task.archived_at.is_(None),
    )
    if for_update:
        query = query.with_for_update()
    task = await db.scalar(query)
    if not task:
        raise not_found("Task not found")
    return task

def task_snapshot(task):
    return {
        "title": task.title, "status": task.status.value,
        "priority": task.priority.value, "assignee_id": task.assignee_id,
        "labels": task.labels, "version": task.version,
    }

@router.post("", response_model=TaskOut, status_code=201)
async def create_task(
    workspace_id: str,
    payload: TaskCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    member = await workspace_member(workspace_id, user, db)
    if member.role == Role.MEMBER and payload.assignee_id != user.id:
        raise forbidden("Members may create tasks only when assigning them to themselves")
    if member.role == Role.MEMBER and payload.depends_on_task_ids:
        raise forbidden("Only workspace OWNER or ADMIN can manage task dependencies")
    if not idempotency_key or not idempotency_key.strip():
        raise bad_request("Idempotency-Key header is required")
    if len(idempotency_key) > 128:
        raise bad_request("Idempotency-Key must be at most 128 characters")
    request_data = payload.model_dump(mode="json")
    request_hash = hashlib.sha256(json.dumps(request_data, sort_keys=True).encode()).hexdigest()
    scope = f"task:create:{user.id}:{workspace_id}"
    previous = await db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.scope == scope, IdempotencyRecord.key == idempotency_key
    ))
    if previous:
        if previous.request_hash != request_hash:
            raise conflict("Idempotency-Key was already used with a different request payload")
        return TaskOut.model_validate(previous.response_body)

    # Serialize initial dependency creation with all other dependency-graph mutations.
    if payload.depends_on_task_ids:
        await db.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
        if len(set(payload.depends_on_task_ids)) != len(payload.depends_on_task_ids):
            raise bad_request("A task dependency was selected more than once")
    project = await db.scalar(select(Project).where(
        Project.id == payload.project_id,
        Project.workspace_id == workspace_id,
        Project.archived_at.is_(None),
    ))
    if not project:
        raise not_found("Project not found")
    assignee = await db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == payload.assignee_id,
    ).with_for_update())
    if not assignee:
        raise bad_request("Assignee must be a member of this workspace")

    if not payload.title.strip():
        raise bad_request("Task title cannot be empty")
    now = datetime.now(timezone.utc)
    due_at = payload.due_at
    if due_at is not None and due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    # New tasks start in TODO. BACKLOG is reserved for tasks whose due date
    # has actually passed (including tasks created with a past due date).
    initial_status = (
        TaskStatus.BACKLOG
        if due_at is not None and due_at < now
        else TaskStatus.TODO
    )
    task = Task(
        workspace_id=workspace_id, project_id=payload.project_id,
        title=payload.title.strip(), description=payload.description,
        creator_id=user.id, assignee_id=payload.assignee_id,
        priority=payload.priority, due_at=due_at, status=initial_status,
        labels=payload.labels,
    )
    db.add(task)
    await db.flush()

    # Validate and persist initial blockers in the same transaction as task creation.
    if payload.depends_on_task_ids:
        blocker_rows = await db.scalars(
            select(Task)
            .where(
                Task.id.in_(payload.depends_on_task_ids),
                Task.workspace_id == workspace_id,
                Task.archived_at.is_(None),
            )
            .order_by(Task.id)
            .with_for_update()
        )
        blockers = {row.id: row for row in blocker_rows.all()}
        if len(blockers) != len(payload.depends_on_task_ids):
            raise bad_request("Every dependency must be an active task in this workspace")
        edges = (await db.execute(select(
            TaskDependency.task_id, TaskDependency.depends_on_task_id
        ).where(TaskDependency.workspace_id == workspace_id))).all()
        graph = {}
        for source, target in edges:
            graph.setdefault(source, set()).add(target)
        for blocker_id in payload.depends_on_task_ids:
            if blocker_id == task.id or would_create_cycle(graph, task.id, blocker_id):
                raise bad_request("Dependency would create a circular task dependency")
            graph.setdefault(task.id, set()).add(blocker_id)
            db.add(TaskDependency(
                workspace_id=workspace_id, task_id=task.id,
                depends_on_task_id=blocker_id, created_by=user.id,
            ))

    add_audit(db, workspace_id, user.id, "TASK_CREATED", "TASK", task.id,
              after={**task_snapshot(task), "depends_on_task_ids": payload.depends_on_task_ids})
    add_outbox(db, "task.created", task.id, {
        "workspace_id": workspace_id, "actor_id": user.id, "task_id": task.id,
        "assignee_id": payload.assignee_id, "title": task.title, "version": task.version,
    })
    response_body = TaskOut.model_validate(task).model_dump(mode="json")
    db.add(IdempotencyRecord(
        scope=scope, key=idempotency_key, request_hash=request_hash,
        response_body=response_body,
    ))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        winner = await db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope, IdempotencyRecord.key == idempotency_key
        ))
        if winner:
            if winner.request_hash != request_hash:
                raise conflict("Idempotency-Key was already used with a different request payload")
            return TaskOut.model_validate(winner.response_body)
        raise
    await db.refresh(task)
    return task

@router.get("", response_model=list[TaskOut])
async def list_tasks(workspace_id: str, project_id: str | None = None, status: TaskStatus | None = None, limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await workspace_member(workspace_id, user, db)
    limit = min(max(limit, 1), 100)
    query = select(Task).where(Task.workspace_id == workspace_id, Task.archived_at.is_(None))
    if project_id: query = query.where(Task.project_id == project_id)
    if status: query = query.where(Task.status == status)
    rows = await db.scalars(query.order_by(Task.created_at.desc(), Task.id).limit(limit).offset(max(offset, 0)))
    return list(rows.all())

@router.get("/{task_id}", response_model=TaskOut)
async def get_task_route(workspace_id: str, task_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await workspace_member(workspace_id, user, db)
    return await get_task(db, workspace_id, task_id)

@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(workspace_id: str, task_id: str, payload: TaskUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    member = await workspace_member(workspace_id, user, db)
    task = await get_task(db, workspace_id, task_id)
    if member.role == Role.MEMBER:
        if task.assignee_id != user.id:
            raise forbidden("You can only access tasks assigned to you")
        raise forbidden("Members cannot edit task details. You may add comments or move your assigned task forward one step.")
    before = task_snapshot(task)
    values = payload.model_dump(exclude={"expected_version"}, exclude_unset=True)
    if "title" in values and isinstance(values["title"], str) and not values["title"].strip():
        raise bad_request("Task title cannot be empty")
    if "title" in values and values["title"] is None:
        raise bad_request("Task title cannot be null")
    if "labels" in values and values["labels"] is None:
        raise bad_request("Task labels cannot be null; use an empty list to clear them")
    if "priority" in values and member.role == Role.MEMBER:
        raise forbidden("Only workspace OWNER or ADMIN can change task priority")
    result = await db.execute(update(Task).where(
        Task.id == task_id, Task.workspace_id == workspace_id, Task.version == payload.expected_version,
        Task.archived_at.is_(None)
    ).values(**values, version=Task.version + 1, updated_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        await db.rollback()
        raise conflict("Task was modified by another request. Reload it and retry with the latest version.")
    await db.flush()
    task = await get_task(db, workspace_id, task_id)
    add_audit(db, workspace_id, user.id, "TASK_UPDATED", "TASK", task.id, before, task_snapshot(task))
    add_outbox(db, "task.updated", task.id, {"workspace_id": workspace_id, "task_id": task.id})
    await db.commit()
    await db.refresh(task)
    return task

@router.post("/{task_id}/assign", response_model=TaskOut)
async def assign_task(workspace_id: str, task_id: str, payload: TaskAssign, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    member = await workspace_member(workspace_id, user, db)
    if member.role == Role.MEMBER:
        raise forbidden("Only OWNER or ADMIN can assign tasks")
    task = await get_task(db, workspace_id, task_id)
    before = task_snapshot(task)
    if payload.assignee_id:
        assignee = await db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == payload.assignee_id).with_for_update())
        if not assignee:
            raise bad_request("Assignee must be a workspace member")
    result = await db.execute(update(Task).where(Task.id == task_id, Task.version == payload.expected_version).values(
        assignee_id=payload.assignee_id, version=Task.version + 1, updated_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        await db.rollback()
        raise conflict("Task version is stale")
    await db.flush()
    task = await get_task(db, workspace_id, task_id)
    add_audit(db, workspace_id, user.id, "TASK_ASSIGNED", "TASK", task.id, before, task_snapshot(task))
    add_outbox(db, "task.assigned", task.id, {"workspace_id": workspace_id, "task_id": task.id, "assignee_id": payload.assignee_id, "version": task.version})
    await db.commit()
    await db.refresh(task)
    return task

@router.post("/{task_id}/transition", response_model=TaskOut)
async def transition_task(workspace_id: str, task_id: str, payload: TaskTransition, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    member = await workspace_member(workspace_id, user, db)
    # Workspace row is the serialization point shared with dependency mutations.
    await db.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    task = await get_task(db, workspace_id, task_id, for_update=True)
    if member.role == Role.MEMBER:
        if task.assignee_id != user.id:
            raise forbidden("You can only update tasks assigned to you")
        if payload.status == TaskStatus.DONE:
            raise forbidden("Only workspace OWNER or ADMIN can mark a task complete")
        if task.status in (TaskStatus.IN_REVIEW, TaskStatus.DONE):
            raise forbidden("A submitted or completed task can only be changed by an admin")
        member_transitions = {
            TaskStatus.BACKLOG: {TaskStatus.TODO},
            TaskStatus.TODO: {TaskStatus.IN_PROGRESS},
            TaskStatus.IN_PROGRESS: {TaskStatus.IN_REVIEW},
        }
        if payload.status not in member_transitions.get(task.status, set()):
            raise forbidden("Members can only move assigned tasks forward up to review")
    if payload.status == TaskStatus.DONE and member.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden("Only workspace OWNER or ADMIN can mark a task complete")
    # Members follow the strict one-step workflow enforced above. OWNER/ADMIN
    # may move a task to any workflow state to unblock or correct operations.
    if member.role == Role.MEMBER and payload.status not in ALLOWED_TRANSITIONS[task.status]:
        raise bad_request(f"Transition from {task.status.value} to {payload.status.value} is not allowed")
    # A dependency is a workflow gate, not merely a DONE-time validation.
    # A task cannot advance into execution/review/completion until every direct
    # prerequisite is DONE. This applies equally to OWNER, ADMIN and MEMBER.
    if dependencies_must_be_complete(payload.status):
        incomplete_blocker = await db.scalar(
            select(Task.id)
            .join(TaskDependency, TaskDependency.depends_on_task_id == Task.id)
            .where(
                TaskDependency.task_id == task.id,
                Task.status != TaskStatus.DONE,
            )
            .limit(1)
        )
        if incomplete_blocker:
            raise conflict("This task is blocked. Complete all prerequisite tasks before moving it to In Progress, In Review, or Done.")
    before = task_snapshot(task)
    result = await db.execute(update(Task).where(Task.id == task_id, Task.version == payload.expected_version).values(
        status=payload.status, version=Task.version + 1, updated_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        await db.rollback()
        raise conflict("Task version is stale")
    await db.flush()
    task = await get_task(db, workspace_id, task_id)
    add_audit(db, workspace_id, user.id, "TASK_STATUS_CHANGED", "TASK", task.id, before, task_snapshot(task))
    add_outbox(db, "task.status_changed", task.id, {
        "workspace_id": workspace_id, "task_id": task.id,
        "status": task.status.value, "version": task.version, "title": task.title,
    })
    await db.commit()
    await db.refresh(task)
    return task

@router.post("/{task_id}/archive", response_model=TaskOut)
async def archive_task(
    workspace_id: str, task_id: str, payload: TaskArchive,
    db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
):
    member = await workspace_member(workspace_id, user, db)
    if member.role == Role.MEMBER:
        raise forbidden()
    task = await get_task(db, workspace_id, task_id)
    before = task_snapshot(task)
    now = datetime.now(timezone.utc)
    result = await db.execute(update(Task).where(
        Task.id == task_id, Task.workspace_id == workspace_id,
        Task.version == payload.expected_version, Task.archived_at.is_(None),
    ).values(
        archived_at=now, version=Task.version + 1, updated_at=now,
    ))
    if result.rowcount != 1:
        await db.rollback()
        raise conflict("Task version is stale")
    await db.flush()
    task = await db.get(Task, task_id)
    add_audit(db, workspace_id, user.id, "TASK_ARCHIVED", "TASK", task.id, before, {"archived": True})
    await db.commit()
    await db.refresh(task)
    return task

@router.get("/{task_id}/dependencies", response_model=list[DependencyOut])
async def list_dependencies(
    workspace_id: str, task_id: str, limit: int = 50, offset: int = 0,
    db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
):
    await workspace_member(workspace_id, user, db)
    await get_task(db, workspace_id, task_id)
    rows = await db.scalars(select(TaskDependency).where(
        TaskDependency.workspace_id == workspace_id,
        TaskDependency.task_id == task_id,
    ).order_by(TaskDependency.created_at, TaskDependency.id)
     .limit(min(max(limit, 1), 100)).offset(max(offset, 0)))
    return list(rows.all())


@router.post("/{task_id}/dependencies", response_model=DependencyOut, status_code=201)
async def add_dependency(
    workspace_id: str, task_id: str, payload: DependencyCreate,
    db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
):
    member = await workspace_member(workspace_id, user, db)
    require_admin(member)
    if task_id == payload.depends_on_task_id:
        raise bad_request("A task cannot depend on itself")
    # Serialize dependency graph changes and DONE transitions per workspace.
    await db.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    # Lock both endpoint rows in deterministic order.
    locked = await db.scalars(
        select(Task)
        .where(
            Task.id.in_(sorted([task_id, payload.depends_on_task_id])),
            Task.workspace_id == workspace_id,
            Task.archived_at.is_(None),
        )
        .order_by(Task.id)
        .with_for_update()
    )
    locked_tasks = {item.id: item for item in locked.all()}
    task = locked_tasks.get(task_id)
    blocker = locked_tasks.get(payload.depends_on_task_id)
    if not task or not blocker:
        raise not_found("Task not found")
    if task.status == TaskStatus.DONE:
        raise conflict("Dependencies cannot be added to a completed task")
    existing = await db.scalar(select(TaskDependency).where(
        TaskDependency.task_id == task.id,
        TaskDependency.depends_on_task_id == blocker.id,
    ))
    if existing:
        raise conflict("This dependency already exists")
    edges = (await db.execute(select(TaskDependency.task_id, TaskDependency.depends_on_task_id).where(
        TaskDependency.workspace_id == workspace_id
    ))).all()
    graph = {}
    for source, target in edges:
        graph.setdefault(source, set()).add(target)
    # Adding task -> blocker is cyclic exactly when blocker already reaches task.
    if would_create_cycle(graph, task.id, blocker.id):
        raise bad_request("Dependency would create a circular task dependency")
    before = task_snapshot(task)
    dep = TaskDependency(
        workspace_id=workspace_id, task_id=task.id,
        depends_on_task_id=blocker.id, created_by=user.id,
    )
    db.add(dep)
    task.version += 1
    task.updated_at = datetime.now(timezone.utc)
    add_audit(db, workspace_id, user.id, "TASK_DEPENDENCY_ADDED", "TASK", task.id,
              before=before, after={**task_snapshot(task), "depends_on_task_id": blocker.id})
    await db.commit()
    await db.refresh(dep)
    return dep


@router.delete("/{task_id}/dependencies/{dependency_id}", status_code=204)
async def remove_dependency(
    workspace_id: str, task_id: str, dependency_id: str,
    db: AsyncSession = Depends(get_db), user: User = Depends(current_user),
):
    member = await workspace_member(workspace_id, user, db)
    require_admin(member)
    await db.scalar(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    task = await get_task(db, workspace_id, task_id, for_update=True)
    dep = await db.scalar(select(TaskDependency).where(
        TaskDependency.id == dependency_id,
        TaskDependency.workspace_id == workspace_id,
        TaskDependency.task_id == task_id,
    ))
    if not dep:
        raise not_found("Dependency not found")
    before = task_snapshot(task)
    blocker_id = dep.depends_on_task_id
    await db.delete(dep)
    task.version += 1
    task.updated_at = datetime.now(timezone.utc)
    add_audit(db, workspace_id, user.id, "TASK_DEPENDENCY_REMOVED", "TASK", task_id,
              before=before, after={**task_snapshot(task), "removed_dependency_id": blocker_id})
    await db.commit()


@router.post("/{task_id}/comments", response_model=CommentOut, status_code=201)
async def add_comment(workspace_id: str, task_id: str, payload: CommentCreate, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await workspace_member(workspace_id, user, db)
    task = await get_task(db, workspace_id, task_id)
    if not payload.body.strip():
        raise bad_request("Comment body cannot be empty")
    comment = TaskComment(workspace_id=workspace_id, task_id=task.id, author_id=user.id, body=payload.body.strip())
    db.add(comment)
    await db.flush()
    add_audit(db, workspace_id, user.id, "COMMENT_CREATED", "TASK_COMMENT", comment.id, after={"task_id": task.id, "body": comment.body})
    add_outbox(db, "comment.created", task.id, {
        "workspace_id": workspace_id, "task_id": task.id,
        "comment_id": comment.id, "actor_id": user.id,
    })
    await db.commit()
    await db.refresh(comment)
    return comment

@router.get("/{task_id}/comments", response_model=list[CommentOut])
async def list_comments(workspace_id: str, task_id: str, limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await workspace_member(workspace_id, user, db)
    await get_task(db, workspace_id, task_id)
    rows = await db.scalars(select(TaskComment).where(TaskComment.workspace_id == workspace_id, TaskComment.task_id == task_id).order_by(TaskComment.created_at, TaskComment.id).limit(min(max(limit, 1), 100)).offset(max(offset, 0)))
    return list(rows.all())
