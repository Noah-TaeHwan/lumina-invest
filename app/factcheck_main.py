"""공시 팩트체커 별도 진입점(설계 Outside Voice #6). 기본 앱(app/main.py)과 따로 띄운다.

    uvicorn app.factcheck_main:app --host 0.0.0.0 --port 8000

- 필요한 모듈만 import한다: Redis·Neo4j·Celery·원본 라우터를 끌어오지 않는다(1주차 공개는 익명만, 로그인 없음).
- 시작(prepare, 단계마다 실패를 따로 알린다): 마이그레이션 → PostgreSQL 연결 → 오래된 미정산 예약 복구(실행 마감 + 여유) →
  판정 API 키 확인 → 데이터·파이프라인 조립(T2 pipeline.build_pipeline, 파일이 없으면 예외) → sweeper.
  어느 단계든 실패하면 검수 엔드포인트는 503(startup_failed / no_api_key / data_unavailable)이고 /api/health가 그 이유와
  각 단계 결과를 보인다(503). 복구가 실패하면 한도·익명 키를 공개하지 않는다. 상장사명이 없거나 Qdrant가 안 되거나 데모
  회사 중 하나라도 XBRL 행·문단이 0이면 data_unavailable(FACTCHECK_ALLOW_NO_DATA로만 허용) — 데이터 누락이 조용히 ❔만 내는
  잘못된 판정이 되지 않게.
- 준비가 안 됐으면 RECHECK_S(30초)마다 안 된 단계만 다시 확인해, 회복하면 재시작 없이 연다. 이미 준비된 앱은 데이터를 다시
  읽지 않는다(분기 갱신은 앱 재시작).
- 판정 키 차단기: 401이 이어지면(metering) 검수를 no_api_key로 닫고, AUTH_PROBE_S(10분)마다 한도 예약·정산 안에서 시험
  호출 1회로 다시 확인해 받아들여지면 연다.
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
from typing import Any, Callable

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


def make_jev(on_auth_block: Callable[[], None] | None = None,
             on_auth_unblock: Callable[[], None] | None = None) -> metering.MeteredJev:
    """계량 래퍼로 감싼 실제 ServiceJevClient. 401이 이어지면 on_auth_block, 시험 호출이 성공하면 on_auth_unblock을 부른다.
    테스트에서 바꾼다(외부 호출 없음)."""
    return metering.service_client(api_key=lambda: load_api_key(), on_auth_block=on_auth_block,
                                   on_auth_unblock=on_auth_unblock)


def data_dir_path(raw: str | None = None) -> Path:
    """FACTCHECK_DATA_DIR를 절대 경로로(상대 경로는 실행 폴더가 아니라 저장소 루트 기준)."""
    p = Path(raw or fc_settings.load().FACTCHECK_DATA_DIR)
    return p if p.is_absolute() else (ROOT_PATH / p).resolve()


class DataUnavailable(RuntimeError):
    """검수에 필요한 데이터(XBRL 행·상장사명·문단)가 없거나, 데모 회사 중 하나라도 비었다."""

    def __init__(self, counts: dict, reason: str):
        super().__init__(reason)
        self.counts, self.reason = counts, reason


@dataclass
class Readiness:
    """준비 상태. code: None(준비됨) / startup_failed / no_api_key / data_unavailable / starting."""

    ready: bool = False
    code: str | None = "starting"
    checks: dict = field(default_factory=dict)


RECHECK_S = 30.0  # 준비가 안 됐을 때 다시 확인하는 간격(일시 장애에서 재시작 없이 회복)
AUTH_PROBE_S = 600.0  # 판정 키가 막힌 뒤 시험 호출 간격(한도 예약·정산 안에서 1회)
AUTH_PROBE_KEY = "auth-probe"  # 시험 호출 예약의 한도 키(익명 키와 섞이지 않는다)
CORPS = [c["corp_code"] for c in factcheck.COMPANIES]

_readiness = Readiness()
_pipeline: Any = None
_state: dict = {}
_recheck_task: asyncio.Task | None = None
_auth_probe_task: asyncio.Task | None = None


def _empty_data() -> dict:
    return {"facts": 0, "names": 0, "passages": 0, "qdrant": "unknown",
            "by_corp": {c: {"facts": 0, "passages": 0} for c in CORPS}, "missing": list(CORPS)}


async def _passages(store: Any) -> tuple[dict[str, int], str]:
    """회사별 적재 문단 수와 Qdrant 상태(ok / no_collection / unreachable)."""
    try:
        if not await store.exists():
            return {c: 0 for c in CORPS}, "no_collection"
        from qdrant_client.http import models as qm

        out = {}
        for c in CORPS:
            flt = qm.Filter(must=[qm.FieldCondition(key="corp_code", match=qm.MatchValue(value=c))])
            out[c] = int((await store.client.count(store.collection, count_filter=flt, exact=True)).count)
        return out, "ok"
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Qdrant 확인 실패(연결): {type(e).__name__}")
        return {c: 0 for c in CORPS}, "unreachable"


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


async def assemble(data_dir: Path, *, allow_no_data: bool = False,
                   on_auth_block: Callable[[], None] | None = None) -> tuple[Any, dict]:
    """T2 pipeline.build_pipeline으로 조립하고 (파이프라인, 데이터 검사 결과)를 돌려준다. 검사는 데모 회사마다 XBRL 행·문단
    수를 센다. 파일이 없거나, 상장사명이 없거나, Qdrant가 안 되거나, 어느 한 회사라도 행·문단이 0이면 DataUnavailable
    (allow_no_data면 빈 데이터로 조립하고, 상장사명이 없으면 데모 두 종목 이름을 쓴다). 실패하면 만든 연결을 닫는다."""
    from app.services.factcheck import pipeline as fp
    from app.services.factcheck.scope import CompanyIndex

    store, jev = make_store(), make_jev(on_auth_block, _on_auth_unblock)
    shell = type("P", (), {"store": store, "jev": jev})()
    try:
        try:
            p = fp.build_pipeline(store=store, jev=jev, corp_names_path=str(data_dir / "corp_names.json"),
                                  facts_path=str(data_dir / "xbrl_facts.json"))
        except (OSError, ValueError) as e:
            if not allow_no_data:
                raise DataUnavailable(_empty_data(), f"{type(e).__name__}")
            p = fp.build_pipeline(store=store, jev=jev, corp_entries=[], facts=[])
        if allow_no_data and not p.names.names:
            p.names = CompanyIndex.from_entries(DEMO_NAMES)
        per_passages, qdrant = await _passages(store)
        by_corp = {c: {"facts": sum(1 for r in p.facts if r.get("corp_code") == c), "passages": per_passages[c]}
                   for c in CORPS}
        counts = {"facts": len(p.facts), "names": len(p.names.names), "passages": sum(per_passages.values()),
                  "qdrant": qdrant, "by_corp": by_corp,
                  "missing": [c for c in CORPS if not (by_corp[c]["facts"] and by_corp[c]["passages"])]}
        print(f"[factcheck] data dir={data_dir} facts={counts['facts']} names={counts['names']} "
              f"passages={counts['passages']} qdrant={qdrant} missing={','.join(counts['missing']) or '-'}")
        if not allow_no_data and (counts["names"] == 0 or counts["missing"] or qdrant != "ok"):
            raise DataUnavailable(counts, "empty" if qdrant == "ok" else qdrant)
        return p, counts
    except BaseException:
        await close_pipeline(shell)
        raise


def _on_auth_block() -> None:
    """판정 키 차단기가 열렸다(401 연속): 검수를 no_api_key로 닫고(한도·익명 키는 그대로) 시험 호출 작업을 띄운다."""
    global _readiness, _auth_probe_task
    _state["auth_rejected"] = True
    checks = _state.setdefault("checks", {})
    checks["api_key_rejected"] = True
    factcheck.set_not_ready("no_api_key")
    _readiness = Readiness(False, "no_api_key", checks)
    print(f"[WARN] 판정 API 키가 거부돼(401 연속) 검수를 닫았다 — {_state.get('auth_probe_s', AUTH_PROBE_S):g}초마다 "
          "시험 호출로 다시 확인한다")
    if _auth_probe_task is None or _auth_probe_task.done():
        _auth_probe_task = asyncio.ensure_future(_auth_probe_loop(_state.get("auth_probe_s", AUTH_PROBE_S)))


def _on_auth_unblock() -> None:
    """시험 호출이 성공했다: 키 거부 표시를 지운다(검수는 _auth_probe_loop가 _attempt로 다시 연다)."""
    _state["auth_rejected"] = False
    _state.setdefault("checks", {})["api_key_rejected"] = False


async def _auth_probe_loop(interval_s: float) -> None:
    """막혀 있는 동안 interval_s마다 시험 호출 1회: 한도에 예약(AUTH_PROBE_KEY, 하루 실행 횟수에서는 세지 않음) → 원장 안에서
    probe → 원장 값으로 정산. 성공하면 준비 상태를 다시 맞춰 연다."""
    while _state.get("auth_rejected"):
        await asyncio.sleep(interval_s)
        pipeline, quota = _state.get("pipeline"), _state.get("quota")
        if pipeline is None or quota is None:
            continue
        try:
            res = await quota.reserve(AUTH_PROBE_KEY, metering.reservation_for(1, triage=False), count_run=False)
        except Exception as e:  # noqa: BLE001 — 한도가 없으면 시험도 하지 않는다
            print(f"[WARN] 판정 키 시험 호출 예약 실패: {type(e).__name__}")
            continue
        ledger, ok = metering.Ledger(res.est), False
        token = metering.bind(ledger)
        try:
            ok = await pipeline.jev.probe(user_id=AUTH_PROBE_KEY)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 판정 키 시험 호출 실패: {type(e).__name__}")
        finally:
            metering.unbind(token)
            try:
                await quota.settle(res, ledger.close())
            except Exception as e:  # noqa: BLE001 — 미정산 예약은 오래된 예약 복구가 예약량으로 정산한다
                print(f"[WARN] 판정 키 시험 호출 정산 실패: {type(e).__name__}")
        if ok:
            r = await _attempt()
            print("[factcheck] 판정 키가 다시 받아들여져 검수를 연다" if r.ready else
                  f"[WARN] 판정 키는 회복했지만 아직 준비 안 됨: {r.code}")
            return


async def _attempt() -> Readiness:
    """아직 안 된 단계만 다시 돌리고(마이그레이션 → 연결 → 복구 → 키 → 데이터) 라우트를 맞춘다. 여러 번 불러도 된다."""
    global _readiness, _pipeline
    st, checks = _state, _state["checks"]
    if checks["migrations"] is not True:
        try:
            if settings.RUN_MIGRATIONS_ON_STARTUP:
                # alembic command.upgrade()는 내부에서 asyncio.run()을 열므로 별도 스레드에서 돌린다(app/main.py와 같은 이유)
                await asyncio.get_running_loop().run_in_executor(None, _run_migrations)
            checks["migrations"] = True
        except Exception as e:  # noqa: BLE001
            checks["migrations"] = False
            print(f"[WARN] 시작 실패 — 마이그레이션: {type(e).__name__}")
    if checks["migrations"] and not checks["db"]:
        try:
            await postgres.connect_postgres()
            checks["db"] = True
        except Exception as e:  # noqa: BLE001
            await postgres.close_postgres()  # 실패한 엔진을 남기지 않는다(재확인 때 새로 만든다)
            print(f"[WARN] 시작 실패 — PostgreSQL 연결: {type(e).__name__}")
    if checks["db"] and not checks["recovery"]:
        factory = postgres.get_session_factory()
        q = FactcheckQuota(factory, limits_from_env())
        try:
            settled = await q.settle_stale(STALE_S)
            checks["recovery"] = True
            st["quota"], st["keyer"] = q, AnonKeyer(factory)  # 복구가 끝나야 한도를 공개한다
            if settled:
                print(f"[factcheck] 오래된 미정산 예약 {settled}건을 예약량으로 정산")
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 시작 실패 — 미정산 예약 복구(한도 비공개): {type(e).__name__}")
    if not checks["api_key"]:
        try:
            key = load_api_key()
            if not isinstance(key, str) or not key.strip():
                raise ValueError("empty api key")  # 빈 키 파일도 키 없음과 같다(예외 없이 ""를 돌려준다)
            checks["api_key"] = True
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 시작 실패 — 판정 API 키 없음(검수 비활성): {type(e).__name__}")
    if checks["api_key"] and st.get("pipeline") is None:
        try:
            st["pipeline"], checks["data"] = await assemble(st["data_dir"], allow_no_data=st["allow"],
                                                            on_auth_block=_on_auth_block)
        except DataUnavailable as e:
            checks["data"] = e.counts
            print(f"[WARN] 시작 실패 — 데이터(검수 비활성, FACTCHECK_ALLOW_NO_DATA로만 허용): {e.reason}")
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 시작 실패 — 파이프라인 조립: {type(e).__name__}")
    if not (checks["migrations"] and checks["db"] and checks["recovery"]):
        code = "startup_failed"
    elif not checks["api_key"] or st.get("auth_rejected"):
        code = "no_api_key"
    elif st.get("pipeline") is None:
        code = "data_unavailable"
    else:
        code = None
    _pipeline = st.get("pipeline")
    factcheck.configure(quota=st.get("quota"), keyer=st.get("keyer"), pipeline=_pipeline, not_ready=code)
    if st.get("quota") is not None:
        quota = st["quota"]
        factcheck.get_jobs().start_sweeper(also=lambda: quota.settle_stale(STALE_S))
    _readiness = Readiness(code is None, code, checks)
    return _readiness


async def _recheck_loop(interval_s: float) -> None:
    """준비될 때까지 interval_s마다 다시 확인한다(판정 키 거부로 닫힌 것은 _auth_probe_loop가 맡는다)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            r = await _attempt()
        except Exception as e:  # noqa: BLE001 — 다음 주기에 다시 한다
            print(f"[WARN] 준비 재확인 실패: {type(e).__name__}")
            continue
        if r.ready:
            print("[factcheck] 준비됨(재확인) — 검수를 연다")
            return


async def prepare(*, data_dir: Path | str | None = None, allow_no_data: bool | None = None,
                  recheck_s: float = RECHECK_S, auth_probe_s: float = AUTH_PROBE_S) -> Readiness:
    """시작 단계를 돌리고 라우트에 한도·익명 키·파이프라인·준비 안 된 이유를 넣는다(lifespan·테스트 공용).
    준비가 안 됐으면 recheck_s마다 다시 확인하는 작업을 띄워, 회복하면 연다(열린 뒤 닫는 것은 키 차단기뿐)."""
    global _recheck_task
    cfg = fc_settings.load()
    allow = cfg.FACTCHECK_ALLOW_NO_DATA if allow_no_data is None else allow_no_data
    _state.clear()
    _state.update(data_dir=data_dir_path(str(data_dir) if data_dir else None), allow=allow, quota=None, keyer=None,
                  pipeline=None, auth_rejected=False, auth_probe_s=auth_probe_s,
                  checks={"migrations": None, "db": False, "recovery": False, "api_key": False,
                          "api_key_rejected": False, "data": _empty_data(), "allow_no_data": allow})
    r = await _attempt()
    if not r.ready and recheck_s > 0:
        _recheck_task = asyncio.ensure_future(_recheck_loop(recheck_s))
    print("[factcheck] 서버 시작 완료" + ("" if r.ready else f" (검수 비활성: {r.code}, {recheck_s:g}초마다 재확인)"))
    return r


async def shutdown() -> None:
    """재확인을 멈추고, 실행 중 검수를 취소하고 정산을 기다린 뒤 연결을 닫는다."""
    global _readiness, _pipeline, _recheck_task, _auth_probe_task
    for task in (_recheck_task, _auth_probe_task):
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    _recheck_task = _auth_probe_task = None
    await factcheck.get_jobs().close()
    factcheck.configure(quota=None)
    await close_pipeline(_pipeline)
    _pipeline = None
    _state.clear()
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
