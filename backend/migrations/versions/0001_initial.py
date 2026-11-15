
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


# PostgreSQL ENUM definitions.
# create_type=False prevents SQLAlchemy from automatically
# creating these types again when tables are created.

role_enum = postgresql.ENUM(
    "OWNER",
    "ADMIN",
    "MEMBER",
    name="role",
    create_type=False,
)

status_enum = postgresql.ENUM(
    "BACKLOG",
    "TODO",
    "IN_PROGRESS",
    "IN_REVIEW",
    "DONE",
    name="taskstatus",
    create_type=False,
)

priority_enum = postgresql.ENUM(
    "LOW",
    "MEDIUM",
    "HIGH",
    "URGENT",
    name="priority",
    create_type=False,
)


def upgrade():
    bind = op.get_bind()

    # Create PostgreSQL ENUM types exactly once.
    role_enum.create(bind, checkfirst=True)
    status_enum.create(bind, checkfirst=True)
    priority_enum.create(bind, checkfirst=True)

    # --------------------------------------------------
    # USERS
    # --------------------------------------------------

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_users_email",
        "users",
        ["email"],
    )

    # --------------------------------------------------
    # REFRESH TOKENS
    # --------------------------------------------------

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "token_hash",
            sa.String(64),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_refresh_tokens_user_id",
        "refresh_tokens",
        ["user_id"],
    )

    op.create_index(
        "ix_refresh_tokens_token_hash",
        "refresh_tokens",
        ["token_hash"],
    )

    # --------------------------------------------------
    # WORKSPACES
    # --------------------------------------------------

    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "created_by",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
        ),
    )

    # --------------------------------------------------
    # WORKSPACE MEMBERS
    # --------------------------------------------------

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            role_enum,
            nullable=False,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            name="uq_workspace_member",
        ),
    )

    op.create_index(
        "ix_workspace_members_workspace_id",
        "workspace_members",
        ["workspace_id"],
    )

    op.create_index(
        "ix_workspace_members_user_id",
        "workspace_members",
        ["user_id"],
    )

    # --------------------------------------------------
    # PROJECTS
    # --------------------------------------------------

    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "created_by",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
        ),
    )

    op.create_index(
        "ix_projects_workspace_id",
        "projects",
        ["workspace_id"],
    )

    # --------------------------------------------------
    # TASKS
    # --------------------------------------------------

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "creator_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "assignee_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
        ),
        sa.Column(
            "priority",
            priority_enum,
            nullable=False,
        ),
        sa.Column(
            "status",
            status_enum,
            nullable=False,
        ),
        sa.Column(
            "due_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
        ),
    )

    op.create_index(
        "ix_tasks_workspace_id",
        "tasks",
        ["workspace_id"],
    )

    op.create_index(
        "ix_tasks_project_id",
        "tasks",
        ["project_id"],
    )

    op.create_index(
        "ix_tasks_assignee_id",
        "tasks",
        ["assignee_id"],
    )

    op.create_index(
        "ix_tasks_workspace_project_status",
        "tasks",
        ["workspace_id", "project_id", "status"],
    )

    # --------------------------------------------------
    # TASK COMMENTS
    # --------------------------------------------------

    op.create_table(
        "task_comments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(36),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_task_comments_workspace_id",
        "task_comments",
        ["workspace_id"],
    )

    op.create_index(
        "ix_task_comments_task_id",
        "task_comments",
        ["task_id"],
    )

    # --------------------------------------------------
    # AUDIT EVENTS
    # --------------------------------------------------

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            sa.String(36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("before_data", sa.JSON()),
        sa.Column("after_data", sa.JSON()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_audit_events_workspace_id",
        "audit_events",
        ["workspace_id"],
    )

    op.create_index(
        "ix_audit_events_entity_id",
        "audit_events",
        ["entity_id"],
    )

    # --------------------------------------------------
    # NOTIFICATIONS
    # --------------------------------------------------

    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "recipient_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_key", sa.String(180), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column(
            "read_at",
            sa.DateTime(timezone=True),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_notifications_recipient_id",
        "notifications",
        ["recipient_id"],
    )

    # Corrected spelling of the original index name.
    op.create_index(
        "ix_notifications_workspace_id",
        "notifications",
        ["workspace_id"],
    )

    op.create_index(
        "ix_notifications_event_key",
        "notifications",
        ["event_key"],
    )

    # --------------------------------------------------
    # OUTBOX EVENTS
    # --------------------------------------------------

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_outbox_events_aggregate_id",
        "outbox_events",
        ["aggregate_id"],
    )

    op.create_index(
        "ix_outbox_events_status",
        "outbox_events",
        ["status"],
    )


def downgrade():
    # Drop tables in reverse dependency order.
    op.drop_table("outbox_events")
    op.drop_table("notifications")
    op.drop_table("audit_events")
    op.drop_table("task_comments")
    op.drop_table("tasks")
    op.drop_table("projects")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_table("refresh_tokens")
    op.drop_table("users")

    # Drop ENUM types after all dependent tables are removed.
    bind = op.get_bind()

    priority_enum.drop(bind, checkfirst=True)
    status_enum.drop(bind, checkfirst=True)
    role_enum.drop(bind, checkfirst=True)
