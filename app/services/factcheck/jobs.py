# app/services/factcheck/jobs.py
"""팩트체커 익명 검수 job 저장소: 프로세스 메모리, TTL 15분(설계 D7·Outside Voice #9, 검수 결과 남용 8·9).

- job_id는 secrets.token_urlsafe(32)(256비트). 소유자는 익명 쿠키 값의 sha256으로만 기억하고(쿠키 원값 미보관),
  조회할 때 hmac.compare_digest로 비교한다. job이 없거나, 만료됐거나, 소유자가 다르면 똑같이 None(라우트는 같은 404).
- 결과는 문장별로 끝나는 대로 쌓는다(폴링). 붙여 넣은 원문 전체는 보관하지 않고, 결과에 담긴 문장만 메모리에 둔다.
  DB·로그에는 어느 것도 쓰지 않는다.
- 상한: job 개수(max_jobs, 만료 전 끝난 job 포함), 동시 실행(max_running), 보관 글자 수 추정(max_chars). 넘으면 StoreBusy.
  자리는 예약(await) 전에 동기로 잡고(admit), 실패하면 돌려준다(release) — 경쟁 요청이 상한을 넘지 못한다.
- 만료: 조회는 즉시 막고, 백그라운드 sweeper가 주기적으로 지우며 그 job의 작업을 취소하고 끝날 때까지 기다린다
  (원문이 담긴 작업 프레임이 15분을 넘겨 남지 않는다).
- 정산 작업(track)도 여기서 들고 있다가 종료 때 기다린다(DB를 닫기 전에 정산이 끝나야 한다).
# ponytail: 단일 인스턴스 메모리 저장, 다중 인스턴스면 Redis로
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

TTL_S = 15 * 60
STATUSES = ("supported", "contradicted", "no_evidence", "unjudged", "skipped")
MAX_JOBS = 300
MAX_RUNNING = 8
MAX_CHARS = 3_000_000  # 보관 글자 수 추정 합(입력 + 문장당 근거 문단 여유), 대략 수십 MB 이하
EVIDENCE_CHARS_PER_SENTENCE = 2_000
SWEEP_S = 30.0


def owner_hash(cookie: str) -> str:
    """익명 쿠키 값의 sha256(16진수)."""
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()


def size_estimate(text_chars: int, sentences: int) -> int:
    """job 하나가 메모리에 둘 글자 수 추정: 문장 원문 + 문장마다 근거 문단 여유."""
    return text_chars + sentences * EVIDENCE_CHARS_PER_SENTENCE


class StoreBusy(Exception):
    """job 저장소 상한(개수·동시 실행·메모리)에 닿았다."""


@dataclass
class Job:
    """검수 한 번. results는 idx → 계약 필드만 남긴 dict."""

    id: str
    owner: str
    corp_code: str
    source: str
    total: int
    size: int
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

    def __init__(self, ttl_s: float = TTL_S, now: Callable[[], float] = time.monotonic, *, max_jobs: int = MAX_JOBS,
                 max_running: int = MAX_RUNNING, max_chars: int = MAX_CHARS):
        self.ttl_s = ttl_s
        self._now = now
        self.max_jobs, self.max_running, self.max_chars = max_jobs, max_running, max_chars
        self._jobs: dict[str, Job] = {}
        self._pending_jobs = self._pending_running = self._pending_chars = 0
        self._settling: set[asyncio.Future] = set()
        self._sweeper: asyncio.Task | None = None

    # ── 상한 ──
    def _running(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status == "running")

    def _live(self) -> list[Job]:
        cut = self._now() - self.ttl_s
        return [j for j in self._jobs.values() if j.created >= cut]

    def admit(self, size: int) -> None:
        """새 job 자리(개수·동시 실행·글자 수)를 동기로 잡는다. 없으면 StoreBusy."""
        live = self._live()
        if (len(live) + self._pending_jobs >= self.max_jobs
                or self._running() + self._pending_running >= self.max_running
                or sum(j.size for j in live) + self._pending_chars + size > self.max_chars):
            raise StoreBusy()
        self._pending_jobs += 1
        self._pending_running += 1
        self._pending_chars += size

    def release(self, size: int) -> None:
        """admit으로 잡은 자리를 돌려준다(예약 실패 등)."""
        self._pending_jobs -= 1
        self._pending_running -= 1
        self._pending_chars -= size

    def admit_running(self) -> None:
        """이미 있는 job을 다시 돌릴 자리(동시 실행)만 잡는다(수동 검수). 없으면 StoreBusy."""
        if self._running() + self._pending_running >= self.max_running:
            raise StoreBusy()
        self._pending_running += 1

    def release_running(self) -> None:
        """admit_running을 돌려준다."""
        self._pending_running -= 1

    # ── job ──
    def create(self, owner: str, corp_code: str, source: str, total: int, size: int) -> Job:
        """admit으로 잡은 자리를 job으로 바꾼다(job_id 256비트 토큰)."""
        self.release(size)
        job = Job(secrets.token_urlsafe(32), owner, corp_code, source, total, size, self._now())
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str, owner: str) -> Job | None:
        """job_id와 소유자 해시가 둘 다 맞고 만료 전일 때만 돌려준다."""
        job = self._jobs.get(job_id)
        if job is None or job.created < self._now() - self.ttl_s or not hmac.compare_digest(job.owner, owner):
            return None
        return job

    def expires_in(self, job: Job) -> int:
        """만료까지 남은 초."""
        return max(0, int(job.created + self.ttl_s - self._now()))

    def spawn(self, job: Job, coro: Awaitable) -> asyncio.Task:
        """job에 딸린 작업을 띄운다."""
        task = asyncio.ensure_future(coro)
        job.tasks.add(task)
        task.add_done_callback(job.tasks.discard)
        return task

    def track(self, fut: asyncio.Future) -> asyncio.Future:
        """정산 같은 뒷정리 작업을 종료 때까지 붙잡아 둔다."""
        self._settling.add(fut)
        fut.add_done_callback(self._settling.discard)
        return fut

    # ── 만료·종료 ──
    async def sweep(self) -> int:
        """만료된 job을 지우고, 그 작업을 취소해 끝날 때까지 기다린다. 지운 수를 돌려준다."""
        cut = self._now() - self.ttl_s
        gone = [self._jobs.pop(j) for j in [j for j, job in self._jobs.items() if job.created < cut]]
        tasks = [t for job in gone for t in job.tasks]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(gone)

    def start_sweeper(self, interval_s: float = SWEEP_S, also: Callable[[], Awaitable] | None = None) -> None:
        """interval_s마다 sweep(과 also, 예: 오래된 예약 정산)을 돌리는 백그라운드 작업을 띄운다."""
        async def loop():
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.sweep()
                    if also is not None:
                        await also()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — 한 번 실패해도 다음 주기에 다시 한다
                    pass
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.ensure_future(loop())

    async def stop_sweeper(self) -> None:
        """sweeper를 멈춘다."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None

    async def drain(self) -> None:
        """실행 중 작업과 정산 작업이 모두 끝날 때까지 기다린다(테스트·종료용)."""
        while True:
            tasks = [t for job in self._jobs.values() for t in job.tasks] + list(self._settling)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        """sweeper를 멈추고, 실행 중 작업을 취소하고, 정산까지 끝날 때까지 기다린 뒤 비운다(앱 종료)."""
        await self.stop_sweeper()
        for t in [t for job in self._jobs.values() for t in job.tasks]:
            t.cancel()
        await self.drain()
        self._jobs.clear()
