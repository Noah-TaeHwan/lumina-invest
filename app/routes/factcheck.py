"""공시 팩트체커 익명 API(설계 T3, D7·D8·Outside Voice #5·#6·#9, PR #57 검수 결과 1~14).

  GET  /api/factcheck/companies                 – 데모 범위 회사 두 개(삼성전자·SK하이닉스)와 검색 범위·알려진 약점
  POST /api/factcheck                           – 익명 검수 시작 {corp_code, text, source} → 202 {job_id, …}
  GET  /api/factcheck/{job_id}                  – 문장별 결과 누적(폴링). job_id + 익명 쿠키가 둘 다 맞아야 한다(아니면 404)
  POST /api/factcheck/{job_id}/recheck/{idx}    – 건너뛴 문장 하나를 수동 검수(문장마다 1회, 하루 실행 횟수에서 세지 않음)

- 로그인 없음(1주차 공개는 익명만). 이 라우터는 Redis·Neo4j·원본 라우터를 import하지 않는다.
- POST는 같은 출처만: Origin이 있으면 FACTCHECK_ALLOWED_ORIGINS(없으면 요청 Host)와 맞아야 하고(403),
  Content-Type은 application/json이어야 한다(415). Origin이 없는 요청(브라우저 밖)은 받는다 — 브라우저는 POST에 늘 Origin을
  붙이고, JSON Content-Type은 폼 기반 교차 출처 요청을 막는다.
- 입력: 본문 16KB(읽는 단계에서 413), 2,000자, 입력 전체 문장 30개(claim_spans 기준, 비주장 포함). 넘으면 422 안내.
  본문은 직접 파싱한다: FastAPI 검증 오류(422)는 입력 값을 응답에 되돌려 주므로 원문이 새어 나갈 수 있다.
- 비용 상한: 파이프라인을 부르기 전에 factcheck_quota에서 상한 기준(metering.reservation_for)으로 원자적으로 예약한다.
  실패하면 부르지 않고 429. 유료 JEV 호출은 검수 작업에 묶인 원장(metering.Ledger)을 거쳐서만 나가고, 원장 예산 = 예약량.
  끝나면(성공·실패·시간 초과·취소, 작업이 시작되기 전 취소 포함) 원장 값으로 예약 ID에 한 번 정산한다(shield).
  정산이 DB 오류로 실패하면 예약은 미정산으로 남고 settle_stale이 예약량으로 정산한다.
- 익명 원문은 DB·로그에 남기지 않는다. 결과는 메모리 job 저장소(TTL 15분, 상한·sweeper)에만 있다.
- 파이프라인(T2 FactcheckPipeline)은 요청마다 for_user(익명 키) 사본을 쓰고,
  `check(corp_code, text, *, as_of=None, force_check=False)`만 부른다(수동 검수는 force_check=True).
  진입점이 configure(pipeline=…)로 넣고, 그 jev가 MeteredJev가 아니면 검수를 받지 않는다(503 unmetered).
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.config import settings
from app.services.evidence.claims import claim_spans
from app.services.factcheck import metering
from app.services.factcheck import settings as fc_settings
from app.services.factcheck.jobs import Job, JobStore, StoreBusy, owner_hash, size_estimate
from app.services.factcheck.quota import MAX_SENTENCES, AnonKeyer, FactcheckQuota, QuotaExceeded, Reservation

log = logging.getLogger("app.factcheck.api")

FC = fc_settings.load()
COMPANIES = [
    {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"},
    {"corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660"},
]
SCOPE = "데모 범위: 삼성전자·SK하이닉스 최근 3년 정기보고서(사업·반기·분기)·잠정실적·XBRL 재무 수치"
WEAKNESSES = [
    "주어가 바뀐 문장(다른 부문·제품·고객사 이름)을 잘 못 거릅니다.",
    "증감률·영업이익률 밖의 파생 지표와 명시하지 않은 기간은 검수하지 않거나 범위 밖으로 표시합니다.",
    "✅는 검색된 공시와 일치한다는 뜻이고, 사실 보증·발행 승인이 아닙니다.",
]
SOURCES = {"my_draft": "내 초안", "ai_answer": "AI 답변", "others": "남의 글"}
MAX_CHARS = 2000
MAX_BODY_BYTES = 16 * 1024
POLL_MS = 1000
JOB_DEADLINE_S = 180.0  # 파이프라인 전체 상한(문장별 혼잡 ⊘ 처리는 파이프라인 몫, D9)
COOKIE = "fc_anon"
COOKIE_MAX_AGE_S = 86400
_COOKIE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
EVIDENCE_FIELDS = ("rcept_no", "report_nm", "period", "section", "text")
XBRL_FIELDS = ("account_nm", "period", "fs_div", "amount")
CAP_MESSAGES = {
    "cap_runs": "익명 검수는 하루 3회까지입니다. 오늘 3회를 모두 썼습니다. 내일(한국 시간 자정 이후) 다시 써 주세요.",
    "cap_key_tokens": "오늘 이 연결에서 쓸 수 있는 검수량을 모두 썼습니다. 내일(한국 시간 자정 이후) 다시 써 주세요.",
    "cap_global": "오늘 검수 한도에 도달했습니다. 내일(한국 시간 자정 이후) 다시 써 주세요.",
}
BUSY = {"code": "busy", "message": "지금 검수 요청이 많습니다. 잠시 뒤 다시 시도해 주세요."}
NOT_FOUND = {"code": "not_found", "message": "검수 결과를 찾을 수 없습니다. 15분이 지나 만료됐거나 다른 브라우저에서 시작한 검수입니다."}
UNAVAILABLE = {"code": "quota_unavailable", "message": "검수 한도를 확인할 수 없어 지금은 검수를 받지 않습니다."}

router = APIRouter(prefix="/api/factcheck", tags=["factcheck"])


# ── 주입 지점(진입점 lifespan이 채우고, 테스트는 dependency_overrides로 바꾼다) ─────────────

_quota: FactcheckQuota | None = None
_pipeline: Any = None
_jobs = JobStore()
_keyer: AnonKeyer | None = None


def configure(*, quota: FactcheckQuota | None, pipeline: Any = None, keyer: AnonKeyer | None = None) -> None:
    """진입점 lifespan이 PostgreSQL 연결 뒤 한도·익명 키·파이프라인(MeteredJev를 넣은 T2 FactcheckPipeline)을 넣는다."""
    global _quota, _pipeline, _keyer
    _quota, _pipeline, _keyer = quota, pipeline, keyer


def get_quota() -> FactcheckQuota:
    """한도 객체. PostgreSQL이 연결되지 않았으면 검수를 받지 않는다(503)."""
    if _quota is None:
        raise HTTPException(503, UNAVAILABLE)
    return _quota


def get_jobs() -> JobStore:
    """프로세스 메모리 job 저장소."""
    return _jobs


def get_keyer() -> AnonKeyer:
    """익명 키 생성기(DB 일별 솔트)."""
    if _keyer is None:
        raise HTTPException(503, UNAVAILABLE)
    return _keyer


def require_metered(pipeline: Any) -> Any:
    """파이프라인의 JEV가 계량 래퍼가 아니면 검수를 받지 않는다(계량 안 된 유료 호출 경로 차단)."""
    if not isinstance(getattr(pipeline, "jev", None), metering.MeteredJev):
        log.error(json.dumps({"event": "factcheck_unmetered_pipeline"}))
        raise HTTPException(503, {"code": "unmetered", "message": "검수 엔진 설정이 올바르지 않아 지금은 검수를 받지 않습니다."})
    return pipeline


def get_pipeline() -> Any:
    """진입점이 넣은 T2 파이프라인(요청마다 for_user 사본을 쓴다). 없으면 503."""
    if _pipeline is None:
        raise HTTPException(503, {"code": "pipeline_unavailable", "message": "검수 엔진이 아직 준비되지 않았습니다."})
    return require_metered(_pipeline)


# ── 요청 검사 ───────────────────────────────────────────────────────────────

def _bad(code: str, message: str, **extra) -> HTTPException:
    return HTTPException(422, {"code": code, "message": message, **extra})


def _trusted(ip: str) -> bool:
    nets = [n.strip() for n in FC.FACTCHECK_TRUSTED_PROXIES.split(",") if n.strip()]
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(n, strict=False) for n in nets)
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """요청 IP. TRUST_PROXY가 켜져 있고 접속 상대가 FACTCHECK_TRUSTED_PROXIES 안일 때만 X-Forwarded-For를 쓰고,
    오른쪽부터 처음 나오는 신뢰하지 않는 주소를 사용자로 본다(왼쪽 값은 사용자가 꾸밀 수 있다)."""
    peer = request.client.host if request.client else ""
    if not (settings.TRUST_PROXY and _trusted(peer)):
        return peer
    hops = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    for hop in reversed(hops):
        if not _trusted(hop):
            return hop
    return hops[0] if hops else peer


def check_same_origin(request: Request) -> None:
    """교차 출처 POST를 막는다: Origin(있으면)과 Content-Type(application/json)."""
    origin = request.headers.get("origin")
    if origin is not None:
        allowed = [o.strip().rstrip("/") for o in FC.FACTCHECK_ALLOWED_ORIGINS.split(",") if o.strip()]
        ok = origin.rstrip("/") in allowed if allowed else (
            urlsplit(origin).netloc != "" and urlsplit(origin).netloc == request.headers.get("host", ""))
        if not ok:
            raise HTTPException(403, {"code": "bad_origin", "message": "다른 사이트에서 보낸 요청은 받지 않습니다."})
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(415, {"code": "bad_content_type", "message": "요청 형식이 올바르지 않습니다."})


async def read_body(request: Request) -> bytes:
    """본문을 MAX_BODY_BYTES까지만 읽는다(넘으면 413, 나머지는 읽지 않는다)."""
    too_large = HTTPException(413, {"code": "too_large", "message": "요청이 너무 큽니다. 2,000자까지 붙여 넣어 주세요."})
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise too_large
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY_BYTES:
            raise too_large
    return bytes(buf)


def _anon_cookie(request: Request) -> str | None:
    value = request.cookies.get(COOKIE) or ""
    return value if _COOKIE_RE.match(value) else None


# ── 결과 보기 ───────────────────────────────────────────────────────────────

def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return dict(obj)
    return dict(vars(obj))


def _pick(d: dict, fields: tuple[str, ...]) -> dict:
    return {f: d.get(f) for f in fields}


def result_view(r: Any) -> dict:
    """SentenceResult를 계약 필드만 남긴 dict로(evidence·xbrl도 계약 필드만). 나머지 필드는 버린다."""
    d = _as_dict(r)
    xbrl = d.get("xbrl")
    return {"idx": int(d.get("idx", 0)), "text": d.get("text") or "", "category": d.get("category"),
            "status": d.get("status"), "evidence": [_pick(_as_dict(e), EVIDENCE_FIELDS) for e in d.get("evidence") or []],
            "xbrl": _pick(_as_dict(xbrl), XBRL_FIELDS) if xbrl else None, "reason": d.get("reason")}


def job_view(job: Job, jobs: JobStore) -> dict:
    """GET 응답: 결과는 idx 순서(화면이 ⚠️·❔를 앞으로 정렬한다), 개수는 코드가 센다."""
    running = job.status == "running"
    return {"job_id": job.id, "status": job.status, "corp_code": job.corp_code, "source": job.source,
            "total": job.total, "results": [job.results[i] for i in sorted(job.results)], "counts": job.counts(),
            "error": job.error, "poll_interval_ms": POLL_MS if running else None, "expires_in_s": jobs.expires_in(job)}


# ── 실행·정산 ───────────────────────────────────────────────────────────────

async def _settle(quota: FactcheckQuota, res: Reservation, amount: int) -> None:
    """예약 ID로 정산한다(한 번 다시 시도). 실패하면 예약은 미정산으로 남고 settle_stale이 예약량으로 정산한다."""
    for attempt in (1, 2):
        try:
            await quota.settle(res, amount)
            return
        except Exception as exc:  # noqa: BLE001
            log.error(json.dumps({"event": "factcheck_settle_failed", "error": type(exc).__name__, "attempt": attempt}))


def ensure_settled(jobs: JobStore, quota: FactcheckQuota, res: Reservation, ledger: metering.Ledger) -> asyncio.Future:
    """정산 작업을 한 번만 만든다(원장을 닫고 그 값으로). 작업 끝·작업 시작 전 취소 어느 쪽이 먼저 불러도 같다."""
    if ledger.settlement is None:
        amount = ledger.close()
        ledger.settlement = jobs.track(asyncio.ensure_future(_settle(quota, res, amount)))
    return ledger.settlement


async def _run(job: Job, pipeline: Any, jobs: JobStore, quota: FactcheckQuota, res: Reservation,
               ledger: metering.Ledger, text: str, recheck_idx: int | None = None) -> None:
    """파이프라인을 돌려 결과를 job에 쌓고, 어떻게 끝나든 원장 값으로 정산한다. 원문·예외 메시지는 로그에 남기지 않는다."""
    token = metering.bind(ledger)
    final: tuple[str, dict | None] = ("failed", {"code": "cancelled", "message": "서버가 검수를 멈췄습니다. 다시 시도해 주세요."})
    try:
        gen = (pipeline.check(job.corp_code, text, as_of=None) if recheck_idx is None
               else pipeline.check(job.corp_code, text, as_of=None, force_check=True))
        async with asyncio.timeout(JOB_DEADLINE_S):
            async with contextlib.aclosing(gen) as agen:  # 취소돼도 파이프라인의 finally(안쪽 작업 취소)가 돈다
                async for r in agen:
                    view = result_view(r)
                    if recheck_idx is not None:
                        job.results[recheck_idx] = {**view, "idx": recheck_idx}
                        break  # 문장 하나만 보냈다
                    job.results[view["idx"]] = view
        final = ("done", None)
    except TimeoutError:
        final = ("failed", {"code": "timeout", "message": "검수가 너무 오래 걸려 멈췄습니다. 남은 문장은 판정하지 못했습니다."})
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 화면에 알린다(조용한 실패 없음)
        final = ("failed", {"code": "pipeline_error", "message": "검수 중 오류가 났습니다. 나온 결과까지만 보입니다."})
        log.warning(json.dumps({"event": "factcheck_failed", "error": type(exc).__name__}))
    finally:
        metering.unbind(token)
        try:
            await asyncio.shield(ensure_settled(jobs, quota, res, ledger))  # 다시 취소돼도 정산은 끝까지 간다
        finally:
            job.status, job.error = final  # 'done'은 정산이 끝난 뒤에 보인다(취소면 cancelled)


def _launch(job: Job, pipeline: Any, jobs: JobStore, quota: FactcheckQuota, res: Reservation, text: str,
            recheck_idx: int | None = None) -> None:
    """원장을 만들고 작업을 띄운다. 작업이 첫 단계 전에 취소돼도(코루틴의 finally가 안 돈다) 완료 콜백이 정산한다."""
    ledger = metering.Ledger(res.est)
    task = jobs.spawn(job, _run(job, pipeline, jobs, quota, res, ledger, text, recheck_idx))

    def done(t: asyncio.Task) -> None:
        if t.cancelled() and job.status == "running":
            job.status, job.error = "failed", {"code": "cancelled", "message": "서버가 검수를 멈췄습니다. 다시 시도해 주세요."}
        ensure_settled(jobs, quota, res, ledger)

    task.add_done_callback(done)


async def _reserve(quota: FactcheckQuota, key: str, est: int, *, count_run: bool) -> Reservation:
    try:
        return await quota.reserve(key, est, count_run=count_run)
    except QuotaExceeded as exc:
        raise HTTPException(429, {"code": exc.code, "message": CAP_MESSAGES[exc.code]})
    except Exception as exc:  # noqa: BLE001 — 한도를 셀 수 없으면 부르지 않는다
        log.error(json.dumps({"event": "factcheck_quota_failed", "error": type(exc).__name__}))
        raise HTTPException(503, UNAVAILABLE)


async def _anon_key(keyer: AnonKeyer, request: Request) -> str:
    try:
        return await keyer.key(client_ip(request))
    except Exception as exc:  # noqa: BLE001
        log.error(json.dumps({"event": "factcheck_salt_failed", "error": type(exc).__name__}))
        raise HTTPException(503, UNAVAILABLE)


# ── 경로 ────────────────────────────────────────────────────────────────────

@router.get("/companies")
async def companies():
    """데모 범위 회사 두 개와 상시 표시 문구."""
    return {"companies": COMPANIES, "scope": SCOPE, "weaknesses": WEAKNESSES, "max_chars": MAX_CHARS,
            "max_sentences": MAX_SENTENCES, "sources": SOURCES}


@router.post("", status_code=202, dependencies=[Depends(check_same_origin)])
async def start(request: Request, response: Response, pipeline: Any = Depends(get_pipeline),
                quota: FactcheckQuota = Depends(get_quota), jobs: JobStore = Depends(get_jobs),
                keyer: AnonKeyer = Depends(get_keyer)):
    """(경로 의존성으로 출처·형식 검사가 먼저) 크기 검사 → 입력 검증 → job 자리 → 한도 예약(실패하면 호출 안 함)
    → job 생성·파이프라인 시작 → 202."""
    raw = await read_body(request)
    try:
        body = json.loads(raw)
    except ValueError:
        raise _bad("bad_json", "요청 형식이 올바르지 않습니다.")
    if not isinstance(body, dict):
        raise _bad("bad_json", "요청 형식이 올바르지 않습니다.")
    corp_code, source, text = body.get("corp_code"), body.get("source"), body.get("text")
    if corp_code not in {c["corp_code"] for c in COMPANIES}:
        raise _bad("bad_company", "데모 범위(삼성전자·SK하이닉스)의 회사만 고를 수 있습니다.")
    if source not in SOURCES:
        raise _bad("bad_source", "출처를 골라 주세요(내 초안·AI 답변·남의 글).")
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise _bad("empty", "검수할 글을 붙여 넣어 주세요.")
    if len(text) > MAX_CHARS:
        raise _bad("too_long", f"익명 검수는 한 번에 {MAX_CHARS:,}자까지입니다. 지금 {len(text):,}자입니다. 나눠서 붙여 넣어 주세요.",
                   limit=MAX_CHARS, length=len(text))
    n = len(claim_spans(text))
    if n > MAX_SENTENCES:
        raise _bad("too_many_sentences", f"익명 검수는 한 번에 문장 {MAX_SENTENCES}개까지입니다. 지금 {n}개입니다. "
                   "나눠서 붙여 넣어 주세요.", limit=MAX_SENTENCES, sentences=n)

    size = size_estimate(len(text), n)
    try:
        jobs.admit(size)
    except StoreBusy:
        raise HTTPException(503, BUSY)
    try:
        key = await _anon_key(keyer, request)
        res = await _reserve(quota, key, metering.reservation_for(n), count_run=True)
    except BaseException:
        jobs.release(size)
        raise
    cookie = _anon_cookie(request) or secrets.token_urlsafe(32)
    job = jobs.create(owner_hash(cookie), corp_code, source, total=n, size=size)
    _launch(job, pipeline.for_user(key), jobs, quota, res, text)
    log.info(json.dumps({"event": "factcheck_started", "corp_code": corp_code, "source": source, "sentences": n}))
    response.set_cookie(COOKIE, cookie, max_age=COOKIE_MAX_AGE_S, httponly=True, samesite=settings.COOKIE_SAMESITE,
                        secure=settings.COOKIE_SECURE, path="/api/factcheck")
    return {"job_id": job.id, "status": job.status, "total": job.total, "poll_interval_ms": POLL_MS,
            "expires_in_s": jobs.expires_in(job)}


def _owned(request: Request, jobs: JobStore, job_id: str) -> Job:
    cookie = _anon_cookie(request)
    job = jobs.get(job_id, owner_hash(cookie)) if cookie else None
    if job is None:
        raise HTTPException(404, NOT_FOUND)  # 없는 job과 남의 job을 구별하지 않는다
    return job


@router.get("/{job_id}")
async def poll(job_id: str, request: Request, jobs: JobStore = Depends(get_jobs)):
    """문장별 결과 누적. job_id와 익명 쿠키가 둘 다 맞아야 한다."""
    return job_view(_owned(request, jobs, job_id), jobs)


@router.post("/{job_id}/recheck/{idx}", status_code=202, dependencies=[Depends(check_same_origin)])
async def recheck(job_id: str, idx: int, request: Request, pipeline: Any = Depends(get_pipeline),
                  quota: FactcheckQuota = Depends(get_quota), jobs: JobStore = Depends(get_jobs),
                  keyer: AnonKeyer = Depends(get_keyer)):
    """건너뛴 문장 하나를 강제로 검수한다. 문장은 서버의 job 결과에서 꺼낸다(요청 본문을 쓰지 않는다).
    그 문장의 수동 검수 소유권은 첫 await 전에 동기로 잡고, 예약이 실패하면 되돌린다. 토큰은 예약·정산하지만
    하루 실행 횟수에서는 세지 않는다(출처·형식 검사는 경로 의존성으로 먼저)."""
    job = _owned(request, jobs, job_id)
    if job.status == "running":
        raise HTTPException(409, {"code": "busy", "message": "검수가 끝난 뒤에 다시 눌러 주세요."})
    current = job.results.get(idx)
    if current is None or current.get("status") != "skipped" or idx in job.rechecked:
        raise HTTPException(409, {"code": "not_skipped", "message": "건너뛴 문장만 한 번씩 검수를 요청할 수 있습니다."})
    try:
        jobs.admit_running()
    except StoreBusy:
        raise HTTPException(503, BUSY)
    prev = (job.status, job.error)
    job.rechecked.add(idx)  # 여기까지 await 없음: 동시 요청은 위의 running/rechecked 검사에서 막힌다
    job.status, job.error = "running", None
    try:
        key = await _anon_key(keyer, request)
        res = await _reserve(quota, key, metering.reservation_for(1, triage=False), count_run=False)
    except BaseException:
        job.rechecked.discard(idx)
        job.status, job.error = prev
        raise
    finally:
        jobs.release_running()
    _launch(job, pipeline.for_user(key), jobs, quota, res, current["text"], recheck_idx=idx)
    return {"job_id": job.id, "idx": idx, "status": job.status, "poll_interval_ms": POLL_MS}
