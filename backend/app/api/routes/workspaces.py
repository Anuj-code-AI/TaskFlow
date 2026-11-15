from datetime import datetime, timedelta, timezone
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select, or_, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.errors import not_found, conflict, forbidden, bad_request
from app.core.redis import redis_client
from app.api.dependencies import current_user, workspace_member, require_admin
from app.models import (
    User, Workspace, WorkspaceMember, WorkspaceInvitation,
    Project, ProjectMember, Task, TaskComment, AuditEvent, Role
)
from app.schemas import (
    WorkspaceCreate, WorkspaceUpdate, WorkspaceOut,
    MemberRoleUpdate, MemberOut, InvitationCreate, InvitationOut,
    AuditLogOut, OwnershipTransfer
)
from app.services.common import add_audit, ensure_utc

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])


@router.post("", response_model=WorkspaceOut, status_code=201)
async def create_workspace(
    payload: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    if not payload.name.strip():
        raise bad_request("Workspace name cannot be empty")
    workspace = Workspace(
        name=payload.name.strip(),
        description=payload.description,
        created_by=user.id,
    )
    db.add(workspace)
    await db.flush()
    db.add(WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=Role.OWNER,
    ))
    add_audit(
        db, workspace.id, user.id, "WORKSPACE_CREATED",
        "WORKSPACE", workspace.id, after={"name": workspace.name},
    )
    await db.commit()
    await db.refresh(workspace)
    return workspace


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    limit: int = 50, offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    result = await db.scalars(
        select(Workspace)
        .join(WorkspaceMember)
        .where(
            WorkspaceMember.user_id == user.id,
            Workspace.archived_at.is_(None),
        )
        .order_by(Workspace.created_at.desc(), Workspace.id)
        .limit(min(max(limit, 1), 100)).offset(max(offset, 0))
    )
    return list(result.all())


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await workspace_member(workspace_id, user, db)
    workspace = await db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.archived_at.is_(None),
        )
    )
    if not workspace:
        raise not_found("Workspace not found")
    return workspace


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    member = await workspace_member(workspace_id, user, db)
    if member.role != Role.OWNER:
        raise forbidden("Only the workspace owner can update workspace settings")

    workspace = await db.get(Workspace, workspace_id)
    if not workspace or workspace.archived_at:
        raise not_found("Workspace not found")

    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise bad_request("Workspace name cannot be null")
    before = {"name": workspace.name, "description": workspace.description}
    if "name" in changes and isinstance(changes["name"], str) and not changes["name"].strip():
        raise bad_request("Workspace name cannot be empty")
    for key, value in changes.items():
        setattr(workspace, key, value.strip() if key == "name" and isinstance(value, str) else value)

    add_audit(
        db, workspace_id, user.id, "WORKSPACE_UPDATED",
        "WORKSPACE", workspace.id, before,
        {"name": workspace.name, "description": workspace.description},
    )
    await db.commit()
    await db.refresh(workspace)
    return workspace


# --------------------------------------------------
# INVITATIONS
# --------------------------------------------------

@router.post(
    "/{workspace_id}/invitations",
    response_model=InvitationOut,
    status_code=201,
)
async def invite_member(
    workspace_id: str,
    payload: InvitationCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    actor = await workspace_member(workspace_id, user, db)
    require_admin(actor)

    if payload.role == Role.OWNER:
        raise forbidden("Workspace ownership can only be changed through ownership transfer")

    email = str(payload.email).lower()
    target = await db.scalar(
        select(User).where(User.email == email, User.is_active.is_(True))
    )
    if not target:
        raise not_found("No registered active user exists with this email. The invitee must create an account first.")

    if target:
        existing_member = await db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == target.id,
            )
        )
        if existing_member:
            raise conflict("This user is already a workspace member")

    existing_invitation = await db.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.email == email,
        )
    )
    now = datetime.now(timezone.utc)
    if existing_invitation and existing_invitation.status == "PENDING" and ensure_utc(existing_invitation.expires_at) > now:
        raise conflict("A pending invitation already exists for this email")

    if existing_invitation:
        existing_invitation.invited_by = user.id
        existing_invitation.role = payload.role
        existing_invitation.status = "PENDING"
        existing_invitation.expires_at = now + timedelta(days=7)
        existing_invitation.created_at = now
        existing_invitation.accepted_at = None
        invitation = existing_invitation
    else:
        invitation = WorkspaceInvitation(
            workspace_id=workspace_id,
            email=email,
            invited_by=user.id,
            role=payload.role,
            status="PENDING",
            expires_at=now + timedelta(days=7),
        )
        db.add(invitation)

    await db.flush()
    workspace = await db.get(Workspace, workspace_id)
    add_audit(
        db, workspace_id, user.id, "MEMBER_INVITED",
        "WORKSPACE_INVITATION", invitation.id,
        after={"email": email, "role": payload.role.value},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise conflict("An invitation for this workspace and email already exists")
    await db.refresh(invitation)

    # If the invitee already has an account, push the event immediately.
    # The invitation remains persisted for users who are offline.
    if target:
        event = {
            "type": "workspace_invitation",
            "invitation_id": invitation.id,
            "workspace_id": workspace_id,
            "workspace_name": workspace.name if workspace else "Workspace",
            "email": email,
            "role": payload.role.value,
        }
        try:
            await redis_client.publish(
                f"taskflow:user:{target.id}:events",
                json.dumps(event),
            )
        except Exception:
            # The database invitation is durable even if Redis is temporarily down.
            pass

    return invitation


@router.get("/{workspace_id}/invitations", response_model=list[InvitationOut])
async def list_workspace_invitations(
    workspace_id: str, limit: int = 50, offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    actor = await workspace_member(workspace_id, user, db)
    require_admin(actor)
    rows = await db.scalars(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.workspace_id == workspace_id)
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id)
        .limit(min(max(limit, 1), 100)).offset(max(offset, 0))
    )
    return list(rows.all())


@router.get("/invitations/mine", response_model=list[InvitationOut])
async def my_invitations(
    limit: int = 50, offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    now = datetime.now(timezone.utc)
    rows = await db.scalars(
        select(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.email == user.email,
            WorkspaceInvitation.status == "PENDING",
            WorkspaceInvitation.expires_at > now,
        )
        .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id)
        .limit(min(max(limit, 1), 100)).offset(max(offset, 0))
    )
    return list(rows.all())


@router.post("/invitations/{invitation_id}/accept", response_model=MemberOut)
async def accept_invitation(
    invitation_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    invitation = await db.scalar(
        select(WorkspaceInvitation)
        .where(WorkspaceInvitation.id == invitation_id)
        .with_for_update()
    )
    if not invitation:
        raise not_found("Invitation not found")
    if invitation.email.lower() != user.email.lower():
        raise forbidden("This invitation belongs to a different email address")
    if invitation.status != "PENDING":
        raise conflict("This invitation is no longer pending")
    now = datetime.now(timezone.utc)
    if ensure_utc(invitation.expires_at) <= now:
        invitation.status = "EXPIRED"
        await db.commit()
        raise conflict("This invitation has expired")

    existing = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == invitation.workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if existing:
        invitation.status = "ACCEPTED"
        invitation.accepted_at = now
        await db.commit()
        return MemberOut(
            user_id=user.id, email=user.email, name=user.name, role=existing.role
        )

    member = WorkspaceMember(
        workspace_id=invitation.workspace_id,
        user_id=user.id,
        role=invitation.role,
    )
    db.add(member)
    await db.flush()

    invitation.status = "ACCEPTED"
    invitation.accepted_at = now
    add_audit(
        db, invitation.workspace_id, user.id, "INVITATION_ACCEPTED",
        "WORKSPACE_MEMBER", member.id,
        after={"user_id": user.id, "role": member.role.value},
    )
    await db.commit()
    return MemberOut(
        user_id=user.id, email=user.email, name=user.name, role=member.role
    )


@router.delete("/{workspace_id}/invitations/{invitation_id}", status_code=204)
async def revoke_invitation(
    workspace_id: str,
    invitation_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    actor = await workspace_member(workspace_id, user, db)
    require_admin(actor)
    invitation = await db.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.id == invitation_id,
            WorkspaceInvitation.workspace_id == workspace_id,
        )
    )
    if not invitation:
        raise not_found("Invitation not found")
    if invitation.status != "PENDING":
        raise conflict("Only pending invitations can be revoked")
    invitation.status = "REVOKED"
    add_audit(
        db, workspace_id, user.id, "INVITATION_REVOKED",
        "WORKSPACE_INVITATION", invitation.id,
        after={"email": invitation.email},
    )
    await db.commit()


@router.post("/{workspace_id}/transfer-ownership", response_model=MemberOut)
async def transfer_ownership(
    workspace_id: str,
    payload: OwnershipTransfer,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    current = await db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user.id,
    ).with_for_update())
    if not current or current.role != Role.OWNER:
        raise forbidden("Only the current workspace owner can transfer ownership")
    target = await db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == payload.new_owner_user_id,
    ).with_for_update())
    if not target:
        raise not_found("Target must already be a workspace member")
    if target.user_id == user.id:
        raise bad_request("The selected user is already the workspace owner")
    old_owner_id = current.user_id
    current.role = Role.ADMIN
    await db.flush()
    target.role = Role.OWNER
    projects = await db.scalars(select(Project).where(Project.workspace_id == workspace_id))
    for project in projects.all():
        project.owner_id = target.user_id
        old_project_owner = await db.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.role == Role.OWNER,
        ).with_for_update())
        if old_project_owner:
            old_project_owner.role = Role.ADMIN
            await db.flush()
        new_project_owner = await db.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == target.user_id,
        ).with_for_update())
        if new_project_owner:
            new_project_owner.role = Role.OWNER
        else:
            db.add(ProjectMember(project_id=project.id, user_id=target.user_id, role=Role.OWNER))
    add_audit(
        db, workspace_id, user.id, "OWNERSHIP_TRANSFERRED",
        "WORKSPACE", workspace_id,
        before={"owner_id": old_owner_id},
        after={"owner_id": target.user_id},
    )
    target_user = await db.get(User, target.user_id)
    await db.commit()
    try:
        await redis_client.delete(f"taskflow:workspace:{workspace_id}:projects:v1")
    except Exception:
        pass
    return MemberOut(
        user_id=target_user.id, email=target_user.email,
        name=target_user.name, role=Role.OWNER,
    )


# --------------------------------------------------
# MEMBERS
# --------------------------------------------------

@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    workspace_id: str, limit: int = 50, offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await workspace_member(workspace_id, user, db)
    rows = await db.execute(
        select(User, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(User.name, User.id)
        .limit(min(max(limit, 1), 100)).offset(max(offset, 0))
    )
    return [
        MemberOut(user_id=u.id, email=u.email, name=u.name, role=m.role)
        for u, m in rows.all()
    ]


@router.patch("/{workspace_id}/members/{member_user_id}", response_model=MemberOut)
async def change_member_role(
    workspace_id: str,
    member_user_id: str,
    payload: MemberRoleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    actor = await workspace_member(workspace_id, user, db)
    require_admin(actor)
    target = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == member_user_id,
        )
    )
    if not target:
        raise not_found("Member not found")
    if target.role == Role.OWNER or payload.role == Role.OWNER:
        raise forbidden("Ownership changes require a dedicated transfer operation")
    before = target.role.value
    target.role = payload.role
    add_audit(
        db, workspace_id, user.id, "MEMBER_ROLE_CHANGED",
        "WORKSPACE_MEMBER", target.id,
        {"role": before}, {"role": payload.role.value},
    )
    target_user = await db.get(User, member_user_id)
    await db.commit()
    return MemberOut(
        user_id=target_user.id,
        email=target_user.email,
        name=target_user.name,
        role=target.role,
    )


@router.delete("/{workspace_id}/members/{member_user_id}", status_code=204)
async def remove_member(
    workspace_id: str,
    member_user_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    actor = await workspace_member(workspace_id, user, db)
    require_admin(actor)
    target = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == member_user_id,
        )
    )
    if not target:
        raise not_found("Member not found")
    if target.role == Role.OWNER:
        raise forbidden("Workspace ownership cannot be removed")
    add_audit(
        db, workspace_id, user.id, "MEMBER_REMOVED",
        "WORKSPACE_MEMBER", target.id,
        before={"user_id": member_user_id, "role": target.role.value},
    )
    project_ids = select(Project.id).where(Project.workspace_id == workspace_id)
    await db.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id.in_(project_ids),
            ProjectMember.user_id == member_user_id,
        )
    )
    await db.delete(target)
    await db.commit()


# --------------------------------------------------
# WORKSPACE AUDIT LOGS
# --------------------------------------------------

@router.get("/{workspace_id}/audit-logs", response_model=list[AuditLogOut])
async def workspace_audit_logs(
    workspace_id: str,
    project_id: str | None = None,
    entity_type: str | None = None,
    actor_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(current_user),
):
    await workspace_member(workspace_id, user, db)

    query = (
        select(AuditEvent, User.name)
        .join(User, User.id == AuditEvent.actor_id)
        .where(AuditEvent.workspace_id == workspace_id)
    )

    if entity_type:
        query = query.where(AuditEvent.entity_type == entity_type.upper())
    if actor_id:
        query = query.where(AuditEvent.actor_id == actor_id)
    if since:
        query = query.where(AuditEvent.created_at >= since)
    if until:
        query = query.where(AuditEvent.created_at <= until)

    if project_id:
        project = await db.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == workspace_id,
            )
        )
        if not project:
            raise not_found("Project not found")

        task_ids = select(Task.id).where(
            Task.workspace_id == workspace_id,
            Task.project_id == project_id,
        )
        comment_ids = select(TaskComment.id).where(
            TaskComment.workspace_id == workspace_id,
            TaskComment.task_id.in_(task_ids),
        )
        query = query.where(
            or_(
                (AuditEvent.entity_type == "PROJECT") & (AuditEvent.entity_id == project_id),
                (AuditEvent.entity_type == "TASK") & AuditEvent.entity_id.in_(task_ids),
                (AuditEvent.entity_type == "TASK_COMMENT") & AuditEvent.entity_id.in_(comment_ids),
            )
        )

    rows = await db.execute(
        query.order_by(AuditEvent.created_at.desc(), AuditEvent.id)
        .limit(min(max(limit, 1), 200))
        .offset(max(offset, 0))
    )
    output = []
    for event, actor_name in rows.all():
        project_name = None
        if event.entity_type == "PROJECT":
            related_project = await db.get(Project, event.entity_id)
            project_name = related_project.name if related_project else None
        elif event.entity_type == "TASK":
            related_task = await db.get(Task, event.entity_id)
            if related_task:
                related_project = await db.get(Project, related_task.project_id)
                project_name = related_project.name if related_project else None
        elif event.entity_type == "TASK_COMMENT":
            related_comment = await db.get(TaskComment, event.entity_id)
            if related_comment:
                related_task = await db.get(Task, related_comment.task_id)
                if related_task:
                    related_project = await db.get(Project, related_task.project_id)
                    project_name = related_project.name if related_project else None

        output.append(AuditLogOut(
            id=event.id,
            workspace_id=event.workspace_id,
            actor_id=event.actor_id,
            actor_name=actor_name,
            action=event.action,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            project_name=project_name,
            before_data=event.before_data,
            after_data=event.after_data,
            created_at=event.created_at,
        ))
    return output
