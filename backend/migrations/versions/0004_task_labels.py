"""Add task labels."""
from alembic import op
import sqlalchemy as sa

revision = "0004_task_labels"
down_revision = "0003_reliability_dependencies"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tasks",
        sa.Column("labels", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.alter_column("tasks", "labels", server_default=None)


def downgrade():
    op.drop_column("tasks", "labels")
