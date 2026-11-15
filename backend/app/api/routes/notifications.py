from datetime import datetime, timezone
import asyncio
import json
import secrets
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.responses import StreamingResponse
from app.core.database import get_db
from app.core.redis import redis_client
from app.api.dependencies import current_user
from app.models import User, Notification
from app.schemas import NotificationOut

router = APIRouter(prefix="/notifications", tags=["Notifications"])



@router.post("/stream-ticket")
async def create_stream_ticket(user: User = Depends(current_user)):
    # Native EventSource cannot set Authorization headers. Issue a one-time,
    # short-lived ticket rather than putting the long-lived access JWT in a URL.
    ticket = secrets.token_urlsafe(32)
    # Reusable for EventSource reconnects, but short-lived and scoped to one user.
    await redis_client.set(f"taskflow:sse-ticket:{ticket}", user.id, ex=300)
    return {"ticket": ticket, "expires_in": 300}


@router.get("/stream")
async def stream_events(ticket: str = Query(..., min_length=20)):
    user_id = await redis_client.get(f"taskflow:sse-ticket:{ticket}")
    if not user_id:
        from app.core.errors import unauthorized
        raise unauthorized("Invalid or expired event-stream ticket")

    async def event_generator():
        pubsub = redis_client.pubsub()
        channel = f"taskflow:user:{user_id}:events"
        await pubsub.subscribe(channel)
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=15.0,
                )
                if message and message.get("data"):
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    yield f"event: message\ndata: {data}\n\n"
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.1)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("", response_model=list[NotificationOut])
async def list_notifications(limit: int = 50, offset: int = 0, unread_only: bool = False, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    query = select(Notification).where(Notification.recipient_id == user.id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    rows = await db.scalars(query.order_by(Notification.created_at.desc(), Notification.id).limit(min(max(limit, 1), 100)).offset(max(offset, 0)))
    return list(rows.all())

@router.get("/unread-count")
async def unread_count(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    count = await db.scalar(select(func.count()).select_from(Notification).where(Notification.recipient_id == user.id, Notification.read_at.is_(None)))
    return {"unread_count": count or 0}

@router.patch("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(notification_id: str, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    item = await db.scalar(select(Notification).where(Notification.id == notification_id, Notification.recipient_id == user.id))
    if not item:
        from app.core.errors import not_found
        raise not_found("Notification not found")
    if not item.read_at:
        item.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(item)
    return item

@router.patch("/read-all")
async def mark_all_read(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    await db.execute(update(Notification).where(Notification.recipient_id == user.id, Notification.read_at.is_(None)).values(read_at=datetime.now(timezone.utc)))
    await db.commit()
    return {"updated": True}
