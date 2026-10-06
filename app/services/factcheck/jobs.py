# app/services/factcheck/jobs.py
"""팩트체커 익명 검수 job 저장소: 프로세스 메모리, TTL 15분(설계 D7·Outside Voice #9).

- job_id는 secrets.token_urlsafe(32)(256비트). 소유자는 익명 쿠키 값의 sha256으로만 기억하고(쿠키 원값 미보관),
  조회할 때 hmac.compare_digest로 비교한다. job이 없거나 소유자가 다르면 똑같이 None(라우트는 같은 404).
- 결과는 문장별로 끝나는 대로 쌓는다(폴링). 붙여 넣은 원문 전체는 보관하지 않고, 결과에 담긴 문장만 메모리에 둔다.
  DB·로그에는 어느 것도 쓰지 않는다.
- 만료 job은 접근할 때마다 지운다. 실행 중 작업은 앱 종료 시 close()가 취소하고 기다린다(취소돼도 한도 정산은 작업의 finally가 한다).
# ponytail: 단일 인스턴스 메모리 저장, 다중 인스턴스면 Redis로
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

TTL_S = 15 * 60
STATUSES = ("supported", "contradicted", "no_evidence", "unjudged", "skipped")


def owner_hash(cookie: str) -> str:
    """익명 쿠키 값의 sha256(16진수)."""
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()


@dataclass
class Job:
    """검수 한 번. results는 idx → 계약 필드만 남긴 dict."""

    id: str
    owner: str
    corp_code: str
    source: str
    total: int
    targets: int
    created: float
    status: str = "running"  # running / done / failed
    error: dict | None = None
    results: dict[int, dict] = field(default_factory=dict)
    rechecked: set[int] = field(default_factory=set)
    tasks: set[asyncio.Task] = field(default_factory=set)

    def counts(self) -> dict[str, int]:
        """상태별 문장 수(코드가 센다, LLM 없음)."""
        out = {s: 0 for s in STATUSES}
        for r in self.results.values():
            if r.get("status") in out:
                out[r["status"]] += 1
        return out


class JobStore:
    """메모리 job 저장소. now는 테스트에서 바꾼다(단조 시계, 초)."""

    def __init__(self, ttl_s: float = TTL_S, now: Callable[[], float] = time.monotonic):
        self.ttl_s = ttl_s
        self._now = now
        self._jobs: dict[str, Job] = {}

    def purge(self) -> None:
        """만료된 job을 지운다(실행 중이면 작업도 취소한다)."""
        cut = self._now() - self.ttl_s
        for jid in [j for j, job in self._jobs.items() if job.created < cut]:
            for t in self._jobs.pop(jid).tasks:
                t.cancel()

    def create(self, owner: str, corp_code: str, source: str, total: int, targets: int) -> Job:
        """새 job을 만든다(job_id 256비트 토큰)."""
        self.purge()
        job = Job(secrets.token_urlsafe(32), owner, corp_code, source, total, targets, self._now())
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str, owner: str) -> Job | None:
        """job_id와 소유자 해시가 둘 다 맞을 때만 돌려준다."""
        self.purge()
        job = self._jobs.get(job_id)
        if job is None or not hmac.compare_digest(job.owner, owner):
            return None
        return job

    def expires_in(self, job: Job) -> int:
        """만료까지 남은 초."""
        return max(0, int(job.created + self.ttl_s - self._now()))

    def spawn(self, job: Job, coro: Any) -> asyncio.Task:
        """job에 딸린 작업을 띄운다."""
        task = asyncio.create_task(coro)
        job.tasks.add(task)
        task.add_done_callback(job.tasks.discard)
        return task

    async def drain(self) -> None:
        """실행 중 작업이 모두 끝날 때까지 기다린다(테스트·종료용)."""
        while True:
            tasks = [t for job in self._jobs.values() for t in job.tasks]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        """실행 중 작업을 취소하고 끝날 때까지 기다린 뒤 비운다(앱 종료)."""
        tasks = [t for job in self._jobs.values() for t in job.tasks]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
