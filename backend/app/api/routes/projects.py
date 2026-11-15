from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.redis import redis_client
from app.core.errors import not_found, forbidden, bad_request
from app.api.dependencies import current_user, workspace_member
from app.models import User, Project, Role, WorkspaceMember, ProjectMember
from app.schemas import ProjectCreate, ProjectUpdate, ProjectOut
from app.services.common import add_audit
from datetime import datetime, timezone
import json

router = APIRouter(prefix="/workspaces/{workspace_id}/projects", tags=["Projects"])

@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(workspace_id: str, payload: ProjectCreate, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    member = await workspace_member(workspace_id, user, db)
    if member.role == Role.MEMBER:
        raise forbidden("Only OWNER or ADMIN can manage projects")
    workspace_owner = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == Role.OWNER,
        ).with_for_update()
    )
    if not workspace_owner:
        raise not_found("Workspace owner not found")

    if not payload.name.strip():
        raise bad_request("Project name cannot be empty")
    project = Project(
        workspace_id=workspace_id,
        name=payload.name.strip(),
        description=payload.description,
        created_by=user.id,
        owner_id=workspace_owner.user_id,
    )
    db.add(project)
    await db.flush()
    db.add(ProjectMember(
        project_id=project.id,
        user_id=workspace_owner.user_id,
        role=Role.OWNER,
    ))
    add_audit(db, workspace_id, user.id, "PROJECT_CREATED", "PROJECT", project.id, after={"name": project.name})
    await db.commit()
    await invalidate_project_cache(workspace_id)
    await db.refresh(project)
    return project

@router.get("", response_model=list[ProjectOut])
async def list_projects(workspace_id: str, limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await workspace_member(workspace_id, user, db)
    cache_key = f"taskflow:workspace:{workspace_id}:projects:v1"
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            cached_rows = json.loads(cached)
            return cached_rows[max(offset, 0):max(offset, 0) + min(max(limit, 1), 100)]
    except Exception:
        pass  # Redis is an optimization, not a dependency for reads.
    rows = await db.scalars(
        select(Project).where(
            Project.workspace_id == workspace_id,
            Project.archived_at.is_(None),
        ).order_by(Project.created_at.desc(), Project.id)
    )
    result = [ProjectOut.model_validate(item).model_dump(mode="json") for item in rows.all()]
    try:
        await redis_client.set(cache_key, json.dumps(result), ex=60)
    except Exception:
        pass
    return result[max(offset, 0):max(offset, 0) + min(max(limit, 1), 100)]


async def invalidate_project_cache(workspace_id: str):
    try:
        await redis_client.delete(f"taskflow:workspace:{workspace_id}:projects:v1")
    except Exception:
        pass

@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(workspace_id: str, project_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await workspace_member(workspace_id, user, db)
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id, Project.archived_at.is_(None)))
    if not project:
        raise not_found("Project not found")
    return project

@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(workspace_id: str, project_id: str, payload: ProjectUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    member = await workspace_member(workspace_id, user, db)
    if member.role == Role.MEMBER:
        raise forbidden()
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id, Project.archived_at.is_(None)))
    if not project:
        raise not_found("Project not found")
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise bad_request("Project name cannot be null")
    before = {"name": project.name, "description": project.description}
    if "name" in changes and isinstance(changes["name"], str) and not changes["name"].strip():
        raise bad_request("Project name cannot be empty")
    for key, value in changes.items():
        setattr(project, key, value.strip() if key == "name" and isinstance(value, str) else value)
    add_audit(db, workspace_id, user.id, "PROJECT_UPDATED", "PROJECT", project.id, before, {"name": project.name, "description": project.description})
    await db.commit()
    await invalidate_project_cache(workspace_id)
    await db.refresh(project)
    return project

@router.post("/{project_id}/archive", response_model=ProjectOut)
async def archive_project(workspace_id: str, project_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    member = await workspace_member(workspace_id, user, db)
    if member.role == Role.MEMBER:
        raise forbidden()
    project = await db.scalar(select(Project).where(Project.id == project_id, Project.workspace_id == workspace_id, Project.archived_at.is_(None)))
    if not project:
        raise not_found("Project not found")
    project.archived_at = datetime.now(timezone.utc)
    add_audit(db, workspace_id, user.id, "PROJECT_ARCHIVED", "PROJECT", project.id, before={"archived_at": None}, after={"archived_at": project.archived_at.isoformat()})
    await db.commit()
    await invalidate_project_cache(workspace_id)
    await db.refresh(project)
    return project
