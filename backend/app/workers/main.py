"""Transactional-outbox worker.

The database is the durable queue. Rows are claimed with SKIP LOCKED, processed
idempotently, retried with exponential backoff, and eventually dead-lettered.
Redis Pub/Sub is best-effort fan-out; persisted notifications remain authoritative.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import SessionLocal
from app.core.redis import redis_client
from app.models import OutboxEvent, Notification, Task, TaskComment, TaskStatus, WorkspaceMember, Role, AuditEvent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("taskflow.worker")
MAX_ATTEMPTS = 8


async def _notification(db, recipient_id, workspace_id, event_key, message):
    if not recipient_id:
        return
    existing = await db.scalar(select(Notification.id).where(
        Notification.recipient_id == recipient_id,
        Notification.event_key == event_key,
    ))
    if not existing:
        db.add(Notification(
            recipient_id=recipient_id, workspace_id=workspace_id,
            event_key=event_key, message=message,
        ))


async def process_event(db, event):
    payload = event.payload or {}
    etype = event.event_type
    if etype in ("task.assigned", "task.created"):
        event_key = (
            f"task-assigned:{event.aggregate_id}"
            if etype == "task.created"
            else f"task-assigned:{event.aggregate_id}:{payload.get('version', 1)}"
        )
        await _notification(
            db, payload.get("assignee_id"), payload["workspace_id"],
            event_key,
            f"You were assigned: {payload.get('title', 'a task')}",
        )
    elif etype == "comment.created":
        task = await db.get(Task, event.aggregate_id)
        comment = await db.get(TaskComment, payload.get("comment_id"))
        if task and comment:
            recipients = {task.creator_id, task.assignee_id}
            for recipient in recipients - {comment.author_id}:
                await _notification(
                    db, recipient, task.workspace_id, f"comment:{comment.id}",
                    f"New comment on: {task.title}",
                )
    elif etype == "task.status_changed" and payload.get("status") == TaskStatus.IN_REVIEW.value:
        admins = await db.scalars(select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == payload["workspace_id"],
            WorkspaceMember.role.in_([Role.OWNER, Role.ADMIN]),
        ))
        for recipient_id in admins.all():
            await _notification(
                db, recipient_id, payload["workspace_id"],
                f"task-review:{event.aggregate_id}:{payload.get('version', 1)}",
                f"Task submitted for review: {payload.get('title', 'Task')}",
            )
    elif etype == "task.overdue":
        await _notification(
            db, payload.get("assignee_id"), payload["workspace_id"],
            f"task-overdue:{event.aggregate_id}",
            f"Task is overdue: {payload.get('title', 'Untitled task')}",
        )

    # Mark processed in the same transaction as notification persistence.
    event.status = "PROCESSED"
    event.processed_at = datetime.now(timezone.utc)
    event.last_error = None


async def enqueue_overdue_events():
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        tasks = await db.scalars(
            select(Task).where(
                Task.due_at.is_not(None),
                Task.due_at < now,
                Task.status != TaskStatus.DONE,
                Task.status != TaskStatus.BACKLOG,
                Task.archived_at.is_(None),
            )
            .order_by(Task.due_at, Task.id)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
        for task in tasks:
            previous_status = task.status.value
            task.status = TaskStatus.BACKLOG
            task.version += 1
            task.updated_at = now
            db.add(AuditEvent(
                workspace_id=task.workspace_id, actor_id=task.creator_id,
                action="TASK_AUTO_BACKLOGGED", entity_type="TASK", entity_id=task.id,
                before_data={"status": previous_status, "version": task.version - 1},
                after_data={"status": TaskStatus.BACKLOG.value, "version": task.version, "reason": "Due date passed; automated by worker"},
            ))
            db.add(OutboxEvent(
                event_type="task.status_changed", aggregate_id=task.id,
                payload={"workspace_id": task.workspace_id, "task_id": task.id,
                         "status": TaskStatus.BACKLOG.value, "version": task.version, "title": task.title},
            ))
            if task.assignee_id:
                already_notified = await db.scalar(select(OutboxEvent.id).where(
                    OutboxEvent.event_type == "task.overdue",
                    OutboxEvent.aggregate_id == task.id,
                ).limit(1))
                if not already_notified:
                    db.add(OutboxEvent(
                        event_type="task.overdue", aggregate_id=task.id,
                        payload={"workspace_id": task.workspace_id, "assignee_id": task.assignee_id, "title": task.title},
                    ))
        await db.commit()


async def process_batch():
    processed = []
    async with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        rows = await db.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.status.in_(["PENDING", "RETRY"]),
                OutboxEvent.available_at <= now,
            )
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(50)
            .with_for_update(skip_locked=True)
        )
        batch = list(rows.all())
        for event in batch:
            try:
                await process_event(db, event)
                processed.append((event.id, event.event_type, event.aggregate_id, event.payload))
            except Exception as exc:
                event.attempts += 1
                event.last_error = str(exc)[:1000]
                if event.attempts >= MAX_ATTEMPTS:
                    event.status = "DEAD"
                else:
                    event.status = "RETRY"
                    event.available_at = now + timedelta(seconds=min(3600, 2 ** event.attempts * 5))
        await db.commit()

    # Live notifications are an optimization; persisted inbox entries are durable.
    for event_id, event_type, aggregate_id, payload in processed:
        try:
            recipients = set()
            if event_type in ("task.created", "task.assigned") and payload.get("assignee_id"):
                recipients.add(payload["assignee_id"])
            elif event_type == "comment.created":
                async with SessionLocal() as db:
                    task = await db.get(Task, aggregate_id)
                    comment = await db.get(TaskComment, payload.get("comment_id"))
                    if task:
                        recipients.update(x for x in (task.creator_id, task.assignee_id) if x)
                    if comment:
                        recipients.discard(comment.author_id)
            elif event_type == "task.overdue" and payload.get("assignee_id"):
                recipients.add(payload["assignee_id"])
            elif event_type == "task.status_changed" and payload.get("status") == TaskStatus.IN_REVIEW.value:
                async with SessionLocal() as db:
                    admins = await db.scalars(select(WorkspaceMember.user_id).where(
                        WorkspaceMember.workspace_id == payload["workspace_id"],
                        WorkspaceMember.role.in_([Role.OWNER, Role.ADMIN]),
                    ))
                    recipients.update(admins.all())
            event_data = json.dumps({"type": event_type, "aggregate_id": aggregate_id, "event_id": event_id})
            for recipient in recipients:
                await redis_client.publish(f"taskflow:user:{recipient}:events", event_data)
        except Exception:
            logger.exception("Realtime fan-out failed for outbox event %s", event_id)


async def run_worker():
    last_overdue_scan = datetime.min.replace(tzinfo=timezone.utc)
    while True:
        try:
            if datetime.now(timezone.utc) - last_overdue_scan >= timedelta(minutes=5):
                await enqueue_overdue_events()
                last_overdue_scan = datetime.now(timezone.utc)
            await process_batch()
        except Exception:
            logger.exception("Outbox worker iteration failed")
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run_worker())
