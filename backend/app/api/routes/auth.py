from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Response, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.config import settings
from app.core.security import hash_password, verify_password, create_access_token, new_refresh_token, hash_token
from app.core.errors import conflict, unauthorized
from app.models import User, RefreshToken
from app.schemas import UserCreate, LoginIn, TokenOut, UserOut
from app.api.dependencies import current_user
from app.services.common import ensure_utc

router = APIRouter(prefix="/auth", tags=["Authentication"])

def set_refresh_cookie(response: Response, token: str):
    response.set_cookie(
        key=settings.refresh_cookie_name, value=token,
        httponly=True, secure=settings.cookie_secure,
        samesite=settings.cookie_samesite.lower(),
        max_age=settings.refresh_token_ttl_days * 86400,
        path="/api/v1/auth"
    )

@router.post("/register", response_model=UserOut, status_code=201)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    email = payload.email.lower()
    exists = await db.scalar(select(User).where(User.email == email))
    if exists:
        raise conflict("An account with this email already exists")
    if not payload.name.strip():
        from app.core.errors import bad_request
        raise bad_request("Name cannot be empty")
    user = User(name=payload.name.strip(), email=email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise conflict("An account with this email already exists")
    await db.refresh(user)
    return user

@router.post("/login", response_model=TokenOut)
async def login(payload: LoginIn, response: Response, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.email == payload.email.lower(), User.is_active.is_(True)))
    if not user or not verify_password(payload.password, user.password_hash):
        raise unauthorized("Invalid email or password")
    raw = new_refresh_token()
    db.add(RefreshToken(
        user_id=user.id, token_hash=hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days)
    ))
    await db.commit()
    set_refresh_cookie(response, raw)
    return TokenOut(access_token=create_access_token(user.id), user=user)

@router.post("/refresh", response_model=TokenOut)
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise unauthorized("Refresh token missing")
    stored = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)).with_for_update())
    now = datetime.now(timezone.utc)
    if not stored:
        raise unauthorized("Refresh token is invalid or expired")
    if stored.revoked_at:
        # Reuse of a rotated token indicates theft or replay. Revoke the user's
        # remaining active refresh sessions rather than silently accepting it.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == stored.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.commit()
        raise unauthorized("Refresh token reuse detected; active sessions were revoked")
    if ensure_utc(stored.expires_at) <= now:
        stored.revoked_at = now
        await db.commit()
        raise unauthorized("Refresh token is invalid or expired")
    user = await db.get(User, stored.user_id)
    if not user or not user.is_active:
        raise unauthorized()
    stored.revoked_at = now
    replacement = new_refresh_token()
    db.add(RefreshToken(
        user_id=user.id, token_hash=hash_token(replacement),
        expires_at=now + timedelta(days=settings.refresh_token_ttl_days)
    ))
    await db.commit()
    set_refresh_cookie(response, replacement)
    return TokenOut(access_token=create_access_token(user.id), user=user)

@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(settings.refresh_cookie_name)
    if raw:
        stored = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
        if stored and not stored.revoked_at:
            stored.revoked_at = datetime.now(timezone.utc)
            await db.commit()
    response.delete_cookie(settings.refresh_cookie_name, path="/api/v1/auth")
    response.status_code = 204
    return response

@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)):
    return user
