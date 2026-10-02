"""대화 스레드 확보 — 채팅(/api/chat)과 근거 모드(/api/evidence/chat)가 같이 쓴다."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.lib.user_state import get_active_conversation, set_active_conversation
from app.models import Conversation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_or_create_conversation(db: AsyncSession, user_id: str, cid: Optional[str]) -> str:
    """conversation_id 가 주어지면 검증, 없으면 Redis 활성 스레드 또는 신규 생성."""
    uid = uuid.UUID(user_id)
    if cid:
        try:
            result = await db.execute(
                select(Conversation).where(Conversation.id == uuid.UUID(cid), Conversation.user_id == uid)
            )
            conv = result.scalar_one_or_none()
        except Exception:
            conv = None
        if not conv:
            raise HTTPException(404, "대화 스레드를 찾을 수 없습니다.")
        return cid

    # Redis 에서 활성 스레드 확인
    active = await get_active_conversation(user_id)
    if active:
        result = await db.execute(
            select(Conversation).where(Conversation.id == uuid.UUID(active), Conversation.user_id == uid)
        )
        if result.scalar_one_or_none():
            return active

    # 새 스레드 생성
    conv = Conversation(user_id=uid, title=f"대화 {_now()[:10]}", message_count=0)
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    new_cid = str(conv.id)
    await set_active_conversation(user_id, new_cid)
    return new_cid
