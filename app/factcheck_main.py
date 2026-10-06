"""공시 팩트체커 별도 진입점(설계 Outside Voice #6). 기본 앱(app/main.py)과 따로 띄운다.

    uvicorn app.factcheck_main:app --host 0.0.0.0 --port 8000

- 필요한 모듈만 import한다: Redis·Neo4j·Celery·원본 라우터를 끌어오지 않는다(1주차 공개는 익명만, 로그인 없음).
- 자체 lifespan은 PostgreSQL(마이그레이션 + 한도·예약·솔트 표)과 Qdrant 연결만 한다. 시작할 때 오래된(30분) 미정산 예약을
  예약량으로 정산하고, job 저장소 sweeper(만료 job 정리 + 오래된 예약 정산)를 띄운다. 종료할 때 실행 중 검수를 취소하고
  정산이 끝난 뒤에 PostgreSQL을 닫는다.
- 공개 경로(허용 목록): /, /factcheck.html, /js/factcheck.js, /favicon.ico, /api/health, /api/factcheck/*.
  원본 화면 스크립트(/js 전체)·/css·/docs·/redoc·/openapi.json은 열지 않는다.
- 파이프라인 연결(T2 연결 지점): T1 저장소(Qdrant factcheck_passages)·XBRL 행·상장사명 사전으로 FactcheckPipeline을 만들고
  jev에 metering.MeteredJev(ServiceJevClient)를 넣어 factcheck.configure(pipeline=…)로 넘긴다. T1·T2가 main에 들어온 뒤
  연결한다 — 그 전에는 pipeline=None이라 검수 요청은 503(pipeline_unavailable)이다.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.config import settings
from app.database import postgres
from app.routes import factcheck, health
from app.services.factcheck.quota import STALE_S, AnonKeyer, FactcheckQuota, limits_from_env

ROOT = os.path.join(os.path.dirname(__file__), "..")
PUBLIC = os.path.join(ROOT, "public")
STATIC = {  # 공개 경로 → public/ 안 파일(이것만 연다)
    "/": "factcheck.html",
    "/factcheck.html": "factcheck.html",
    "/js/factcheck.js": os.path.join("js", "factcheck.js"),
    "/favicon.ico": "favicon.ico",
}


def _run_migrations() -> None:
    """PostgreSQL 스키마를 최신 Alembic revision으로 맞춘다(팩트체커 표 포함, 빈 DB면 0001부터)."""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    alembic_command.upgrade(cfg, "head")


async def _connect_qdrant():
    """Qdrant 비동기 클라이언트를 만들고 한 번 확인한다. 실패해도 앱은 뜬다(None)."""
    try:
        from qdrant_client import AsyncQdrantClient

        client = AsyncQdrantClient(url=settings.QDRANT_URL)
        await client.get_collections()
        return client
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Qdrant 연결 실패 (검수 불가): {type(e).__name__}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """시작: 마이그레이션 → PostgreSQL → 한도·익명 키 → 오래된 예약 정산 → Qdrant → sweeper.
    종료: sweeper 정지·실행 중 검수 취소·정산 대기 → 연결 닫기."""
    jobs = factcheck.get_jobs()
    quota = None
    try:
        if settings.RUN_MIGRATIONS_ON_STARTUP:
            # alembic command.upgrade()는 내부에서 asyncio.run()을 열므로 별도 스레드에서 돌린다(app/main.py와 같은 이유)
            await asyncio.get_running_loop().run_in_executor(None, _run_migrations)
        await postgres.connect_postgres()
        factory = postgres.get_session_factory()
        quota = FactcheckQuota(factory, limits_from_env())
        settled = await quota.settle_stale(STALE_S)
        if settled:
            print(f"[factcheck] 오래된 미정산 예약 {settled}건을 예약량으로 정산")
        factcheck.configure(quota=quota, keyer=AnonKeyer(factory), pipeline=None)  # T2 머지 뒤 파이프라인 연결
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] PostgreSQL 연결 실패 (검수 한도를 셀 수 없어 검수 비활성): {type(e).__name__}")
    app.state.qdrant = await _connect_qdrant()
    jobs.start_sweeper(also=(lambda: quota.settle_stale(STALE_S)) if quota else None)
    print("[factcheck] 서버 시작 완료")
    yield
    await jobs.close()
    factcheck.configure(quota=None)
    if app.state.qdrant is not None:
        await app.state.qdrant.close()
    await postgres.close_postgres()


app = FastAPI(title="공시 팩트체커", description="분석글 문장을 DART 공시와 대조합니다(익명 데모).", version="0.1.0",
              lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(health.router)
app.include_router(factcheck.router)


def _static_route(path: str, rel: str) -> None:
    async def serve():
        return FileResponse(os.path.join(PUBLIC, rel))

    serve.__doc__ = f"정적 파일 {rel}."
    app.add_api_route(path, serve, methods=["GET"], include_in_schema=False)


for _path, _rel in STATIC.items():
    _static_route(_path, _rel)
