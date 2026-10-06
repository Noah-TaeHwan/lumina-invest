"""공시 팩트체커 별도 진입점(설계 Outside Voice #6). 기본 앱(app/main.py)과 따로 띄운다.

    uvicorn app.factcheck_main:app --host 0.0.0.0 --port 8000

- 필요한 모듈만 import한다: Redis·Neo4j·Celery·원본 라우터를 끌어오지 않는다(1주차 공개는 익명만, 로그인 없음).
- 시작(prepare, 단계마다 실패를 따로 알린다): 마이그레이션 → PostgreSQL 연결 → 오래된 미정산 예약 복구(실행 마감 + 여유) →
  판정 API 키 확인 → 데이터·파이프라인 조립(T2 pipeline.build_pipeline, 파일이 없으면 예외) → sweeper.
  어느 단계든 실패하면 검수 엔드포인트는 503(startup_failed / no_api_key / data_unavailable)이고 /api/health가 그 이유와
  각 단계 결과를 보인다(503). 복구가 실패하면 한도·익명 키를 공개하지 않는다. XBRL 행·상장사명·문단이 하나라도 0이면
  data_unavailable(FACTCHECK_ALLOW_NO_DATA로만 허용) — 데이터 누락이 조용히 ❔만 내는 잘못된 판정이 되지 않게.
  준비 상태는 시작 때 한 번 정한다(Qdrant·데이터를 나중에 올렸으면 재시작한다).
- 종료(shutdown): 실행 중 검수 취소 → 정산 대기 → 파이프라인·PostgreSQL 연결 닫기.
- 공개 경로(허용 목록): /, /factcheck.html, /js/factcheck.js, /favicon.ico, /api/health, /api/factcheck/*.
  원본 화면 스크립트(/js 전체)·/css·/docs·/redoc·/openapi.json은 열지 않는다.
- 파이프라인: T1 저장소(Qdrant factcheck_passages + Ollama 임베딩)·XBRL 계약 행·상장사명 사전(FACTCHECK_DATA_DIR, 상대 경로는
  저장소 루트 기준)으로 T2 FactcheckPipeline을 만들고, jev에는 metering.service_client()(MeteredJev(ServiceJevClient))를 넣는다.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.database import postgres
from app.lib.jev import load_api_key
from app.routes import factcheck
from app.services.factcheck import metering
from app.services.factcheck import settings as fc_settings
from app.services.factcheck.quota import STALE_S, AnonKeyer, FactcheckQuota, limits_from_env

ROOT_PATH = Path(__file__).resolve().parent.parent
ROOT = str(ROOT_PATH)
PUBLIC = os.path.join(ROOT, "public")
STATIC = {  # 공개 경로 → public/ 안 파일(이것만 연다)
    "/": "factcheck.html",
    "/factcheck.html": "factcheck.html",
    "/js/factcheck.js": os.path.join("js", "factcheck.js"),
    "/favicon.ico": "favicon.ico",
}
DEMO_NAMES = [{"corp_code": c["corp_code"], "corp_name": c["corp_name"]} for c in factcheck.COMPANIES]


def make_store() -> Any:
    """T1 저장소(앱 설정의 Qdrant·Ollama 임베딩). 테스트에서 바꾼다."""
    from app.services.factcheck.store import default_store

    return default_store()


def make_jev() -> metering.MeteredJev:
    """계량 래퍼로 감싼 실제 ServiceJevClient. 테스트에서 바꾼다(외부 호출 없음)."""
    return metering.service_client(api_key=lambda: load_api_key())


def data_dir_path(raw: str | None = None) -> Path:
    """FACTCHECK_DATA_DIR를 절대 경로로(상대 경로는 실행 폴더가 아니라 저장소 루트 기준)."""
    p = Path(raw or fc_settings.load().FACTCHECK_DATA_DIR)
    return p if p.is_absolute() else (ROOT_PATH / p).resolve()


class DataUnavailable(RuntimeError):
    """검수에 필요한 데이터(XBRL 행·상장사명·문단)가 없거나 비었다."""

    def __init__(self, counts: dict, reason: str):
        super().__init__(reason)
        self.counts, self.reason = counts, reason


@dataclass
class Readiness:
    """시작 때 정한 준비 상태. code: None(준비됨) / startup_failed / no_api_key / data_unavailable / starting."""

    ready: bool = False
    code: str | None = "starting"
    checks: dict = field(default_factory=dict)


_readiness = Readiness()
_pipeline: Any = None


async def _passages(store: Any) -> int:
    """적재된 문단 수(컬렉션이 없거나 Qdrant 오류면 0)."""
    try:
        if not await store.exists():
            return 0
        return int((await store.client.count(store.collection, exact=True)).count)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Qdrant 문단 수 확인 실패: {type(e).__name__}")
        return 0


async def close_pipeline(p: Any) -> None:
    """파이프라인의 저장소·JEV 연결을 닫는다."""
    if p is None:
        return
    for closer in (getattr(p.store, "aclose", None), getattr(getattr(p.jev, "inner", None), "aclose", None)):
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass


async def assemble(data_dir: Path, *, allow_no_data: bool = False) -> tuple[Any, dict]:
    """T2 pipeline.build_pipeline으로 조립하고 (파이프라인, {facts, names, passages})를 돌려준다. 파일이 없거나 하나라도 0이면
    DataUnavailable(allow_no_data면 빈 데이터로 조립하고, 상장사명이 없으면 데모 두 종목 이름을 쓴다)."""
    from app.services.factcheck import pipeline as fp
    from app.services.factcheck.scope import CompanyIndex

    store, jev = make_store(), make_jev()
    zero = {"facts": 0, "names": 0, "passages": 0}
    try:
        p = fp.build_pipeline(store=store, jev=jev, corp_names_path=str(data_dir / "corp_names.json"),
                              facts_path=str(data_dir / "xbrl_facts.json"))
    except (OSError, ValueError) as e:
        if not allow_no_data:
            await close_pipeline(type("P", (), {"store": store, "jev": jev})())
            raise DataUnavailable(zero, f"{type(e).__name__}")
        p = fp.build_pipeline(store=store, jev=jev, corp_entries=[], facts=[])
    if allow_no_data and not p.names.names:
        p.names = CompanyIndex.from_entries(DEMO_NAMES)
    counts = {"facts": len(p.facts), "names": len(p.names.names), "passages": await _passages(store)}
    print(f"[factcheck] data dir={data_dir} facts={counts['facts']} names={counts['names']} "
          f"passages={counts['passages']}")
    if not allow_no_data and min(counts.values()) == 0:
        await close_pipeline(p)
        raise DataUnavailable(counts, "empty")
    return p, counts


async def prepare(*, data_dir: Path | str | None = None, allow_no_data: bool | None = None) -> Readiness:
    """시작 단계를 차례로 돌리고 라우트에 한도·익명 키·파이프라인·준비 안 된 이유를 넣는다(lifespan·테스트 공용)."""
    global _readiness, _pipeline
    cfg = fc_settings.load()
    allow = cfg.FACTCHECK_ALLOW_NO_DATA if allow_no_data is None else allow_no_data
    checks: dict = {"migrations": None, "db": False, "recovery": False, "api_key": False,
                    "data": {"facts": 0, "names": 0, "passages": 0}, "allow_no_data": allow}
    code: str | None = None
    quota = keyer = pipeline = None
    try:
        if settings.RUN_MIGRATIONS_ON_STARTUP:
            # alembic command.upgrade()는 내부에서 asyncio.run()을 열므로 별도 스레드에서 돌린다(app/main.py와 같은 이유)
            await asyncio.get_running_loop().run_in_executor(None, _run_migrations)
            checks["migrations"] = True
    except Exception as e:  # noqa: BLE001
        checks["migrations"], code = False, "startup_failed"
        print(f"[WARN] 시작 실패 — 마이그레이션: {type(e).__name__}")
    if code is None:
        try:
            await postgres.connect_postgres()
            checks["db"] = True
        except Exception as e:  # noqa: BLE001
            code = "startup_failed"
            print(f"[WARN] 시작 실패 — PostgreSQL 연결: {type(e).__name__}")
    if code is None:
        factory = postgres.get_session_factory()
        q = FactcheckQuota(factory, limits_from_env())
        try:
            settled = await q.settle_stale(STALE_S)
            checks["recovery"] = True
            quota, keyer = q, AnonKeyer(factory)  # 복구가 끝나야 한도를 공개한다
            if settled:
                print(f"[factcheck] 오래된 미정산 예약 {settled}건을 예약량으로 정산")
        except Exception as e:  # noqa: BLE001
            code = "startup_failed"
            print(f"[WARN] 시작 실패 — 미정산 예약 복구(한도 비공개): {type(e).__name__}")
    try:
        key = load_api_key()
        if not isinstance(key, str) or not key.strip():
            raise ValueError("empty api key")  # 빈 키 파일도 키 없음과 같다(예외 없이 ""를 돌려준다)
        checks["api_key"] = True
    except Exception as e:  # noqa: BLE001
        code = code or "no_api_key"
        print(f"[WARN] 시작 실패 — 판정 API 키 없음(검수 비활성): {type(e).__name__}")
    if checks["api_key"]:
        try:
            pipeline, checks["data"] = await assemble(data_dir_path(str(data_dir) if data_dir else None),
                                                      allow_no_data=allow)
        except DataUnavailable as e:
            checks["data"] = e.counts
            code = code or "data_unavailable"
            print(f"[WARN] 시작 실패 — 데이터 없음(검수 비활성, FACTCHECK_ALLOW_NO_DATA로만 허용): {e.reason}")
        except Exception as e:  # noqa: BLE001
            code = code or "data_unavailable"
            print(f"[WARN] 시작 실패 — 파이프라인 조립: {type(e).__name__}")
    _pipeline = pipeline
    factcheck.configure(quota=quota, keyer=keyer, pipeline=pipeline, not_ready=code)
    if quota is not None:
        factcheck.get_jobs().start_sweeper(also=lambda: quota.settle_stale(STALE_S))
    _readiness = Readiness(code is None, code, checks)
    print("[factcheck] 서버 시작 완료" + ("" if code is None else f" (검수 비활성: {code})"))
    return _readiness


async def shutdown() -> None:
    """실행 중 검수를 취소하고 정산을 기다린 뒤 연결을 닫는다."""
    global _readiness, _pipeline
    await factcheck.get_jobs().close()
    factcheck.configure(quota=None)
    await close_pipeline(_pipeline)
    _pipeline = None
    _readiness = Readiness()
    await postgres.close_postgres()


def _run_migrations() -> None:
    """PostgreSQL 스키마를 최신 Alembic revision으로 맞춘다(팩트체커 표 포함, 빈 DB면 0001부터)."""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    alembic_command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """prepare → (서비스) → shutdown."""
    await prepare()
    yield
    await shutdown()


app = FastAPI(title="공시 팩트체커", description="분석글 문장을 DART 공시와 대조합니다(익명 데모).", version="0.1.0",
              lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(factcheck.router)


@app.get("/api/health", include_in_schema=False)
async def health():
    """준비 상태: 준비됐으면 200, 아니면 503과 이유(code)·단계별 결과(checks). 키 값·경로는 내지 않는다."""
    r = _readiness
    return JSONResponse({"status": "ok" if r.ready else "unavailable", "service": "factcheck", "code": r.code,
                         "checks": r.checks}, status_code=200 if r.ready else 503)


def _static_route(path: str, rel: str) -> None:
    async def serve():
        return FileResponse(os.path.join(PUBLIC, rel))

    serve.__doc__ = f"정적 파일 {rel}."
    app.add_api_route(path, serve, methods=["GET"], include_in_schema=False)


for _path, _rel in STATIC.items():
    _static_route(_path, _rel)
