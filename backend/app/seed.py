"""Create an optional first owner/workspace/project from environment variables.

Run after migrations with: python -m app.seed
No demo password or production credential is committed to the repository.
"""
import asyncio
import os

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import User, Workspace, WorkspaceMember, Project, ProjectMember, Role


async def seed():
    email = os.getenv("SEED_ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("SEED_ADMIN_PASSWORD", "")
    name = os.getenv("SEED_ADMIN_NAME", "TaskFlow Owner").strip()
    workspace_name = os.getenv("SEED_WORKSPACE_NAME", "TaskFlow Workspace").strip()
    project_name = os.getenv("SEED_PROJECT_NAME", "Getting Started").strip()
    if not email or len(password) < 12:
        raise SystemExit("Set SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD (at least 12 characters)")

    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if not user:
            user = User(name=name or "TaskFlow Owner", email=email, password_hash=hash_password(password))
            db.add(user)
            await db.flush()

        workspace = await db.scalar(select(Workspace).where(
            Workspace.name == workspace_name,
            Workspace.created_by == user.id,
        ))
        if not workspace:
            workspace = Workspace(name=workspace_name, created_by=user.id)
            db.add(workspace)
            await db.flush()
            db.add(WorkspaceMember(
                workspace_id=workspace.id, user_id=user.id, role=Role.OWNER
            ))
        else:
            membership = await db.scalar(select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == user.id,
            ))
            if not membership or membership.role != Role.OWNER:
                raise SystemExit(
                    "The configured seed user already exists, but is not the owner "
                    "of the matching workspace; choose a new workspace name or owner account."
                )

        await db.flush()
        project = await db.scalar(select(Project).where(
            Project.workspace_id == workspace.id,
            Project.name == project_name,
        ))
        if not project:
            project = Project(
                workspace_id=workspace.id, name=project_name,
                created_by=user.id, owner_id=user.id,
            )
            db.add(project)
            await db.flush()
            db.add(ProjectMember(
                project_id=project.id, user_id=user.id, role=Role.OWNER
            ))
        await db.commit()
        print(f"Seeded owner {email}, workspace '{workspace_name}', project '{project_name}'")


if __name__ == "__main__":
    asyncio.run(seed())
