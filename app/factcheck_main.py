"""공시 팩트체커 별도 진입점(설계 Outside Voice #6). 기본 앱(app/main.py)과 따로 띄운다.

    uvicorn app.factcheck_main:app --host 0.0.0.0 --port 8000

- 필요한 모듈만 import한다: Redis·Neo4j·Celery·원본 라우터를 끌어오지 않는다(1주차 공개는 익명만, 로그인 없음).
- 자체 lifespan은 PostgreSQL(마이그레이션 + 한도 표)과 Qdrant 연결만 한다.
- 공개 경로(허용 목록): 정적 화면(/ → factcheck.html, /js, /css), /api/health, /api/factcheck/*.
- Qdrant 클라이언트는 app.state.qdrant에 둔다. 문단 검색 저장소(T1 factcheck/store)·파이프라인(T2)이 이 연결을 쓰는 방식은
  T2 머지 때 맞춘다(연결 지점). Qdrant가 없어도 앱은 뜨고, 검수는 파이프라인이 실패를 알린다.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import postgres
from app.routes import factcheck, health
from app.services.factcheck.quota import FactcheckQuota, limits_from_env

ROOT = os.path.join(os.path.dirname(__file__), "..")
PUBLIC = os.path.join(ROOT, "public")


def _run_migrations() -> None:
    """PostgreSQL 스키마를 최신 Alembic revision으로 맞춘다(factcheck_quota 포함, 빈 DB면 0001부터)."""
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
    """시작: 마이그레이션 → PostgreSQL → 한도 연결 → Qdrant. 종료: 실행 중 검수 취소(정산) → 연결 닫기."""
    try:
        if settings.RUN_MIGRATIONS_ON_STARTUP:
            # alembic command.upgrade()는 내부에서 asyncio.run()을 열므로 별도 스레드에서 돌린다(app/main.py와 같은 이유)
            await asyncio.get_running_loop().run_in_executor(None, _run_migrations)
        await postgres.connect_postgres()
        factcheck.configure(quota=FactcheckQuota(postgres.get_session_factory(), limits_from_env()))
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] PostgreSQL 연결 실패 (검수 한도를 셀 수 없어 검수 비활성): {type(e).__name__}")
    app.state.qdrant = await _connect_qdrant()
    print("[factcheck] 서버 시작 완료")
    yield
    await factcheck.get_jobs().close()
    factcheck.configure(quota=None)
    if app.state.qdrant is not None:
        await app.state.qdrant.close()
    await postgres.close_postgres()


app = FastAPI(title="공시 팩트체커", description="분석글 문장을 DART 공시와 대조합니다(익명 데모).", version="0.1.0",
              lifespan=lifespan)
app.include_router(health.router)
app.include_router(factcheck.router)

if os.path.isdir(PUBLIC):
    app.mount("/js", StaticFiles(directory=os.path.join(PUBLIC, "js")), name="js")
    app.mount("/css", StaticFiles(directory=os.path.join(PUBLIC, "css")), name="css")

    @app.get("/", include_in_schema=False)
    async def index():
        """첫 화면 = 팩트체커."""
        return FileResponse(os.path.join(PUBLIC, "factcheck.html"))

    @app.get("/factcheck.html", include_in_schema=False)
    async def factcheck_page():
        """팩트체커 화면."""
        return FileResponse(os.path.join(PUBLIC, "factcheck.html"))

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        """탭 아이콘."""
        return FileResponse(os.path.join(PUBLIC, "favicon.ico"))
