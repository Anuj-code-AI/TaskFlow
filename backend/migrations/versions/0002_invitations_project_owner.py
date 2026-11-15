"""Add workspace invitations and explicit project ownership.

Revision ID: 0002_invitations_project_owner
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_invitations_project_owner"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("owner_id", sa.String(36), nullable=True))
    op.execute("""
        UPDATE projects AS p
        SET owner_id = (
            SELECT wm.user_id
            FROM workspace_members AS wm
            WHERE wm.workspace_id = p.workspace_id AND wm.role = 'OWNER'
            LIMIT 1
        )
    """)
    # Existing workspaces should always have an owner. Keep a safe fallback
    # for legacy data where the owner membership is missing.
    op.execute("UPDATE projects SET owner_id = created_by WHERE owner_id IS NULL")
    op.alter_column("projects", "owner_id", nullable=False)
    op.create_foreign_key(
        "fk_projects_owner_id_users", "projects", "users",
        ["owner_id"], ["id"]
    )
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])

    op.create_table(
        "project_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", postgresql.ENUM("OWNER", "ADMIN", "MEMBER", name="role", create_type=False), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )
    op.create_index("ix_project_members_project_id", "project_members", ["project_id"])
    op.create_index("ix_project_members_user_id", "project_members", ["user_id"])
    op.execute("""
        INSERT INTO project_members (id, project_id, user_id, role, joined_at)
        SELECT md5(random()::text || clock_timestamp()::text), p.id, p.owner_id, 'OWNER', p.created_at
        FROM projects p
    """)
    op.create_index(
        "uq_project_single_owner",
        "project_members",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("role = 'OWNER'"),
    )

    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("invited_by", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", postgresql.ENUM("OWNER", "ADMIN", "MEMBER", name="role", create_type=False), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("workspace_id", "email", name="uq_workspace_invitation_email"),
    )
    op.create_index("ix_workspace_invitations_workspace_id", "workspace_invitations", ["workspace_id"])
    op.create_index("ix_workspace_invitations_email", "workspace_invitations", ["email"])
    op.create_index("ix_workspace_invitations_status", "workspace_invitations", ["status"])

    # A workspace can have only one OWNER. This is enforced in PostgreSQL,
    # not merely by application checks.
    op.execute("""
        WITH ranked_owners AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY workspace_id
                       ORDER BY joined_at, id
                   ) AS owner_rank
            FROM workspace_members
            WHERE role = 'OWNER'
        )
        UPDATE workspace_members AS wm
        SET role = 'ADMIN'
        FROM ranked_owners AS ro
        WHERE wm.id = ro.id AND ro.owner_rank > 1
    """)
    op.create_index(
        "uq_workspace_single_owner",
        "workspace_members",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("role = 'OWNER'"),
    )


def downgrade():
    op.drop_index("uq_workspace_single_owner", table_name="workspace_members")
    op.drop_index("uq_project_single_owner", table_name="project_members")
    op.drop_index("ix_project_members_user_id", table_name="project_members")
    op.drop_index("ix_project_members_project_id", table_name="project_members")
    op.drop_table("project_members")
    op.drop_index("ix_workspace_invitations_status", table_name="workspace_invitations")
    op.drop_index("ix_workspace_invitations_email", table_name="workspace_invitations")
    op.drop_index("ix_workspace_invitations_workspace_id", table_name="workspace_invitations")
    op.drop_table("workspace_invitations")
    op.drop_index("ix_projects_owner_id", table_name="projects")
    op.drop_constraint("fk_projects_owner_id_users", "projects", type_="foreignkey")
    op.drop_column("projects", "owner_id")
