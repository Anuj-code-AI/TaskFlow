"""Add task dependencies, idempotency records, and reliable outbox metadata."""
from alembic import op
import sqlalchemy as sa

revision = "0003_reliability_dependencies"
down_revision = "0002_invitations_project_owner"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_dependencies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("depends_on_task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency"),
        sa.CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dependency_not_self"),
    )
    op.create_index("ix_task_dependencies_workspace_id", "task_dependencies", ["workspace_id"])
    op.create_index("ix_task_dependencies_task_id", "task_dependencies", ["task_id"])
    op.create_index("ix_task_dependencies_depends_on_task_id", "task_dependencies", ["depends_on_task_id"])

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scope", sa.String(180), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_body", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )
    op.create_index("ix_idempotency_records_scope", "idempotency_records", ["scope"])

    op.add_column("outbox_events", sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.execute("UPDATE outbox_events SET available_at = created_at WHERE available_at IS NULL")
    # Earlier MVP relay marked events PUBLISHED without a consumer; requeue them
    # for the new database-backed worker.
    op.execute("UPDATE outbox_events SET status = 'PENDING' WHERE status = 'PUBLISHED'")
    op.alter_column("outbox_events", "available_at", nullable=False)
    op.create_index("ix_outbox_events_available_at", "outbox_events", ["available_at"])
    op.create_index(
        "uq_outbox_task_overdue", "outbox_events", ["aggregate_id"],
        unique=True, postgresql_where=sa.text("event_type = 'task.overdue'"),
    )
    op.create_index("ix_audit_events_workspace_created", "audit_events", ["workspace_id", "created_at"])
    op.create_index("ix_notifications_recipient_created", "notifications", ["recipient_id", "created_at"])

    # Existing MVP rows may contain duplicate events; retain the oldest row.
    op.execute("""
        DELETE FROM notifications
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY recipient_id, event_key
                    ORDER BY created_at, id
                ) AS rn
                FROM notifications
            ) ranked
            WHERE rn > 1
        )
    """)
    op.create_unique_constraint(
        "uq_notification_recipient_event", "notifications", ["recipient_id", "event_key"]
    )


def downgrade():
    op.drop_constraint("uq_notification_recipient_event", "notifications", type_="unique")
    op.drop_index("uq_outbox_task_overdue", table_name="outbox_events")
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
    op.drop_index("ix_audit_events_workspace_created", table_name="audit_events")
    op.drop_index("ix_outbox_events_available_at", table_name="outbox_events")
    op.drop_column("outbox_events", "last_error")
    op.drop_column("outbox_events", "processed_at")
    op.drop_column("outbox_events", "available_at")
    op.drop_index("ix_idempotency_records_scope", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    op.drop_index("ix_task_dependencies_depends_on_task_id", table_name="task_dependencies")
    op.drop_index("ix_task_dependencies_task_id", table_name="task_dependencies")
    op.drop_index("ix_task_dependencies_workspace_id", table_name="task_dependencies")
    op.drop_table("task_dependencies")
