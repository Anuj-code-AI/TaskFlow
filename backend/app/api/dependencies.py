from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.security import decode_access_token
from app.core.errors import unauthorized, forbidden, not_found
from app.models import User, WorkspaceMember, Role

async def current_user(authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized()
    token = authorization.split(" ", 1)[1]
    try:
        user_id = decode_access_token(token)
    except ValueError:
        raise unauthorized("Invalid or expired access token")
    user = await db.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if not user:
        raise unauthorized()
    return user

async def workspace_member(workspace_id: str, user: User, db: AsyncSession) -> WorkspaceMember:
    member = await db.scalar(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == user.id
    ))
    if not member:
        raise not_found("Workspace not found")
    return member

def require_admin(member: WorkspaceMember):
    if member.role not in (Role.OWNER, Role.ADMIN):
        raise forbidden()
