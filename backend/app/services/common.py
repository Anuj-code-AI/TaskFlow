from datetime import datetime, timezone

def ensure_utc(value: datetime) -> datetime:
    """Normalize datetimes returned by drivers that drop timezone metadata."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

from app.models import AuditEvent, OutboxEvent

def add_audit(db, workspace_id, actor_id, action, entity_type, entity_id, before=None, after=None):
    db.add(AuditEvent(
        workspace_id=workspace_id, actor_id=actor_id, action=action,
        entity_type=entity_type, entity_id=entity_id,
        before_data=before, after_data=after
    ))

def add_outbox(db, event_type, aggregate_id, payload):
    db.add(OutboxEvent(event_type=event_type, aggregate_id=aggregate_id, payload=payload))
