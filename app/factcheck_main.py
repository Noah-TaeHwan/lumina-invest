"""공시 팩트체커 별도 진입점(설계 Outside Voice #6). 기본 앱(app/main.py)과 따로 띄운다.

    uvicorn app.factcheck_main:app --host 0.0.0.0 --port 8000

- 필요한 모듈만 import한다: Redis·Neo4j·Celery·원본 라우터를 끌어오지 않는다(1주차 공개는 익명만, 로그인 없음).
- 자체 lifespan은 PostgreSQL(마이그레이션 + 한도·예약·솔트 표)과 Qdrant 연결만 한다. 시작할 때 오래된(30분) 미정산 예약을
  예약량으로 정산하고, job 저장소 sweeper(만료 job 정리 + 오래된 예약 정산)를 띄운다. 종료할 때 실행 중 검수를 취소하고
  정산이 끝난 뒤에 PostgreSQL을 닫는다.
- 공개 경로(허용 목록): /, /factcheck.html, /js/factcheck.js, /favicon.ico, /api/health, /api/factcheck/*.
  원본 화면 스크립트(/js 전체)·/css·/docs·/redoc·/openapi.json은 열지 않는다.
- 파이프라인(build_pipeline): T1 저장소(Qdrant factcheck_passages + Ollama 임베딩)·XBRL 계약 행(xbrl_facts.json)·
  상장사명 사전(corp_names.json, FACTCHECK_DATA_DIR)으로 T2 FactcheckPipeline을 만들고, jev에는
  metering.MeteredJev(ServiceJevClient)를 넣는다 — 유료 호출은 검수 작업의 원장 안에서만 나간다. ServiceJevClient의
  Redis 캐시·근거 모드 한도는 쓰지 않는다(빈 캐시·빈 기록기, 한도는 factcheck_quota가 한다).
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.config import settings
from app.database import postgres
from app.routes import factcheck, health
from app.services.factcheck import metering
from app.services.factcheck import settings as fc_settings
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


class _NoCache:
    """ServiceJevClient 캐시 자리: 늘 비어 있다(팩트체커 진입점은 Redis를 쓰지 않는다)."""

    async def get(self, key):
        return None

    async def set(self, key, value, ex=None):
        return True


class _NoQuota:
    """ServiceJevClient 한도 기록 자리: 아무것도 하지 않는다(사용량은 metering 원장 → factcheck_quota로 센다)."""

    async def record(self, user_id, tokens):
        return None


def _json_rows(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def build_pipeline(data_dir: Path | str | None = None) -> Any:
    """T2 파이프라인을 조립한다(jev = MeteredJev(ServiceJevClient)). 데이터 파일이 없으면 XBRL 대조 없이 문단 판정만 한다."""
    from app.lib.jev_service import ServiceJevClient
    from app.services.factcheck.pipeline import FactcheckPipeline
    from app.services.factcheck.store import default_store

    d = Path(data_dir or fc_settings.load().FACTCHECK_DATA_DIR)
    facts = _json_rows(d / "xbrl_facts.json")
    names: dict[str, list[str]] = {c["corp_code"]: [c["corp_name"]] for c in factcheck.COMPANIES}
    for e in _json_rows(d / "corp_names.json"):
        if e.get("corp_code") and e.get("corp_name"):
            names.setdefault(e["corp_code"], []).append(e["corp_name"])
    jev = metering.MeteredJev(ServiceJevClient(_NoCache(), _NoQuota()))
    return FactcheckPipeline(store=default_store(), jev=jev, names=names, facts=facts)


async def close_pipeline(p: Any) -> None:
    """파이프라인의 저장소·JEV 연결을 닫는다."""
    if p is None:
        return
    for closer in (getattr(p.store, "aclose", None), getattr(p.jev.inner, "aclose", None)):
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """시작: 마이그레이션 → PostgreSQL → 한도·익명 키 → 오래된 예약 정산 → 파이프라인(Qdrant) → sweeper.
    종료: sweeper 정지·실행 중 검수 취소·정산 대기 → 연결 닫기."""
    jobs = factcheck.get_jobs()
    quota = keyer = pipeline = None
    try:
        if settings.RUN_MIGRATIONS_ON_STARTUP:
            # alembic command.upgrade()는 내부에서 asyncio.run()을 열므로 별도 스레드에서 돌린다(app/main.py와 같은 이유)
            await asyncio.get_running_loop().run_in_executor(None, _run_migrations)
        await postgres.connect_postgres()
        factory = postgres.get_session_factory()
        quota, keyer = FactcheckQuota(factory, limits_from_env()), AnonKeyer(factory)
        settled = await quota.settle_stale(STALE_S)
        if settled:
            print(f"[factcheck] 오래된 미정산 예약 {settled}건을 예약량으로 정산")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] PostgreSQL 연결 실패 (검수 한도를 셀 수 없어 검수 비활성): {type(e).__name__}")
    try:
        pipeline = build_pipeline()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 파이프라인 조립 실패 (검수 불가): {type(e).__name__}")
    if pipeline is not None:
        try:  # Qdrant는 나중에 떠도 된다: 확인만 하고 파이프라인은 그대로 둔다(검색 실패는 검수 실패로 보인다)
            if not await pipeline.store.exists():
                print("[WARN] Qdrant factcheck_passages 컬렉션이 없다 (적재 전에는 검수가 저장소 오류로 실패)")
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] Qdrant 확인 실패: {type(e).__name__}")
    factcheck.configure(quota=quota, keyer=keyer, pipeline=pipeline)
    jobs.start_sweeper(also=(lambda: quota.settle_stale(STALE_S)) if quota else None)
    print("[factcheck] 서버 시작 완료")
    yield
    await jobs.close()
    factcheck.configure(quota=None)
    await close_pipeline(pipeline)
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
