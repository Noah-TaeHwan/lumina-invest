"""판정 API가 쓰는 일지 조회: 판정 실행마다 내 기록 id(serialize_run의 journal_entry_id, spec 결정 5-6).

호출자는 JOURNAL_ENABLED일 때만 부른다(0010 미적용 환경에서 판정 API가 깨지지 않게).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JudgmentEntry


async def journal_entry_ids(db: AsyncSession, user_id: str, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not run_ids:
        return {}
    rows = await db.execute(select(JudgmentEntry.run_id, JudgmentEntry.id).where(
        JudgmentEntry.user_id == uuid.UUID(user_id), JudgmentEntry.run_id.in_(run_ids)))
    return {run_id: str(entry_id) for run_id, entry_id in rows}
