"""공시 팩트체커 익명 API(설계 T3, D7·D8·Outside Voice #5·#6·#9).

  GET  /api/factcheck/companies                 – 데모 범위 회사 두 개(삼성전자·SK하이닉스)와 검색 범위·알려진 약점
  POST /api/factcheck                           – 익명 검수 시작 {corp_code, text, source} → 202 {job_id, …}
  GET  /api/factcheck/{job_id}                  – 문장별 결과 누적(폴링). job_id + 익명 쿠키가 둘 다 맞아야 한다(아니면 404)
  POST /api/factcheck/{job_id}/recheck/{idx}    – 건너뛴 문장 하나를 수동 검수(문장마다 1회, 하루 실행 횟수에서 세지 않음)

- 로그인 없음(1주차 공개는 익명만, Outside Voice #6). 이 라우터는 Redis·Neo4j·원본 라우터를 import하지 않는다.
- 입력: 2,000자, 검수 대상 30문장(claim_spans 문장 중 비주장 규칙에 안 걸린 것). 넘으면 입력 단계에서 422 안내(D8, R2-16).
  본문은 직접 파싱한다: FastAPI 검증 오류(422)는 입력 값을 응답에 되돌려 주므로 원문이 새어 나갈 수 있다.
- 한도: 파이프라인을 부르기 전에 factcheck_quota에서 원자적으로 예약한다. 예약에 실패하면 부르지 않고 429.
  끝나면(성공·실패·취소 모두) 실제 토큰으로 정산한다. 결과에 input_tokens가 없으면 예약 전액을 쓴 것으로 센다.
- 익명 원문은 DB·로그에 남기지 않는다. 결과는 메모리 job 저장소(TTL 15분)에만 있다.
- 파이프라인은 함수 계약 `check(corp_code, text, *, as_of) -> AsyncIterator[SentenceResult]`만 쓴다(T2,
  app/services/factcheck/pipeline.py). 모듈이 없으면 503. 수동 검수는 같은 함수에 force=True를 더해 부른다(T2 연결 지점).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import secrets
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.config import settings
from app.services.evidence.claims import claim_spans, is_not_claim
from app.services.factcheck.jobs import Job, JobStore, owner_hash
from app.services.factcheck.quota import (MAX_TARGETS, AnonKeyer, FactcheckQuota, QuotaExceeded, Reservation,
                                          estimate)

log = logging.getLogger("app.factcheck.api")

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
NOT_FOUND = {"code": "not_found", "message": "검수 결과를 찾을 수 없습니다. 15분이 지나 만료됐거나 다른 브라우저에서 시작한 검수입니다."}

Checker = Callable[..., AsyncIterator[Any]]

router = APIRouter(prefix="/api/factcheck", tags=["factcheck"])


# ── 주입 지점(진입점 lifespan이 채우고, 테스트는 dependency_overrides로 바꾼다) ─────────────

_quota: FactcheckQuota | None = None
_jobs = JobStore()
_keyer = AnonKeyer()


def configure(*, quota: FactcheckQuota | None) -> None:
    """진입점 lifespan이 PostgreSQL 연결 뒤 한도 객체를 넣는다(None이면 끊는다)."""
    global _quota
    _quota = quota


def get_quota() -> FactcheckQuota:
    """한도 객체. PostgreSQL이 연결되지 않았으면 검수를 받지 않는다(503)."""
    if _quota is None:
        raise HTTPException(503, {"code": "quota_unavailable", "message": "검수 한도를 확인할 수 없어 지금은 검수를 받지 않습니다."})
    return _quota


def get_jobs() -> JobStore:
    """프로세스 메모리 job 저장소."""
    return _jobs


def get_keyer() -> AnonKeyer:
    """익명 키 생성기(일별 솔트)."""
    return _keyer


def get_checker() -> Checker:
    """T2 파이프라인 pipeline.check. 아직 없으면 503."""
    try:
        from app.services.factcheck.pipeline import check
    except ImportError:
        raise HTTPException(503, {"code": "pipeline_unavailable", "message": "검수 엔진이 아직 준비되지 않았습니다."})
    return check


# ── 도우미 ──────────────────────────────────────────────────────────────────

def _bad(code: str, message: str, **extra) -> HTTPException:
    return HTTPException(422, {"code": code, "message": message, **extra})


def client_ip(request: Request) -> str:
    """요청 IP. TRUST_PROXY일 때만 X-Forwarded-For의 마지막 값(앞단 프록시가 붙인 값)을 쓴다 — 앞쪽 값과 헤더 자체는
    사용자가 꾸밀 수 있어 그대로 믿으면 익명 한도를 우회한다."""
    if settings.TRUST_PROXY:
        fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if fwd:
            return fwd[-1]
    return request.client.host if request.client else ""


def _anon_cookie(request: Request) -> str | None:
    value = request.cookies.get(COOKIE) or ""
    return value if _COOKIE_RE.match(value) else None


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
    """SentenceResult를 계약 필드만 남긴 dict로(evidence·xbrl도 계약 필드만). input_tokens 등 나머지는 버린다."""
    d = _as_dict(r)
    xbrl = d.get("xbrl")
    return {"idx": int(d.get("idx", 0)), "text": d.get("text") or "", "category": d.get("category"),
            "status": d.get("status"), "evidence": [_pick(_as_dict(e), EVIDENCE_FIELDS) for e in d.get("evidence") or []],
            "xbrl": _pick(_as_dict(xbrl), XBRL_FIELDS) if xbrl else None, "reason": d.get("reason")}


def _tokens(r: Any) -> int | None:
    t = r.get("input_tokens") if isinstance(r, dict) else getattr(r, "input_tokens", None)
    return t if isinstance(t, int) and not isinstance(t, bool) else None


def job_view(job: Job, jobs: JobStore) -> dict:
    """GET 응답: 결과는 idx 순서(화면이 ⚠️·❔를 앞으로 정렬한다), 개수는 코드가 센다."""
    running = job.status == "running"
    return {"job_id": job.id, "status": job.status, "corp_code": job.corp_code, "source": job.source,
            "total": job.total, "targets": job.targets, "results": [job.results[i] for i in sorted(job.results)],
            "counts": job.counts(), "error": job.error, "poll_interval_ms": POLL_MS if running else None,
            "expires_in_s": jobs.expires_in(job)}


async def _run(job: Job, checker: Checker, quota: FactcheckQuota, res: Reservation, text: str,
               recheck_idx: int | None = None) -> None:
    """파이프라인을 돌려 결과를 job에 쌓고, 어떻게 끝나든 한도를 정산한다. 원문·예외 메시지는 로그에 남기지 않는다."""
    tokens, known = 0, True
    kw: dict[str, Any] = {"as_of": None}
    if recheck_idx is not None:
        kw["force"] = True
    try:
        async with asyncio.timeout(JOB_DEADLINE_S):
            async for r in checker(job.corp_code, text, **kw):
                view, t = result_view(r), _tokens(r)
                if t is not None:
                    tokens += t
                elif view["status"] != "skipped":
                    known = False  # JEV를 거쳤을 문장인데 토큰을 모른다
                if recheck_idx is not None:
                    job.results[recheck_idx] = {**view, "idx": recheck_idx}
                    break  # 문장 하나만 보냈다
                job.results[view["idx"]] = view
        job.status, job.error = "done", None
    except asyncio.CancelledError:
        job.status, job.error = "failed", {"code": "cancelled", "message": "서버가 검수를 멈췄습니다. 다시 시도해 주세요."}
        raise
    except TimeoutError:
        job.status, job.error = "failed", {"code": "timeout", "message": "검수가 너무 오래 걸려 멈췄습니다. 남은 문장은 판정하지 못했습니다."}
    except Exception as exc:  # noqa: BLE001 — 어떤 실패든 화면에 알린다(조용한 실패 없음)
        job.status, job.error = "failed", {"code": "pipeline_error", "message": "검수 중 오류가 났습니다. 나온 결과까지만 보입니다."}
        log.warning(json.dumps({"event": "factcheck_failed", "error": type(exc).__name__}))
    finally:
        actual = tokens if known else max(tokens, res.est)
        try:
            await quota.settle(res, actual)
        except Exception as exc:  # noqa: BLE001
            log.error(json.dumps({"event": "factcheck_settle_failed", "error": type(exc).__name__, "est": res.est}))


async def _reserve(quota: FactcheckQuota, key: str, est: int, *, count_run: bool) -> Reservation:
    try:
        return await quota.reserve(key, est, count_run=count_run)
    except QuotaExceeded as exc:
        raise HTTPException(429, {"code": exc.code, "message": CAP_MESSAGES[exc.code]})
    except Exception as exc:  # noqa: BLE001 — 한도를 셀 수 없으면 부르지 않는다
        log.error(json.dumps({"event": "factcheck_quota_failed", "error": type(exc).__name__}))
        raise HTTPException(503, {"code": "quota_unavailable", "message": "검수 한도를 확인할 수 없어 지금은 검수를 받지 않습니다."})


# ── 경로 ────────────────────────────────────────────────────────────────────

@router.get("/companies")
async def companies():
    """데모 범위 회사 두 개와 상시 표시 문구."""
    return {"companies": COMPANIES, "scope": SCOPE, "weaknesses": WEAKNESSES, "max_chars": MAX_CHARS,
            "max_targets": MAX_TARGETS, "sources": SOURCES}


@router.post("", status_code=202)
async def start(request: Request, response: Response, checker: Checker = Depends(get_checker),
                quota: FactcheckQuota = Depends(get_quota), jobs: JobStore = Depends(get_jobs),
                keyer: AnonKeyer = Depends(get_keyer)):
    """입력 검증 → 한도 예약(실패하면 호출 안 함) → job 생성·파이프라인 시작 → 202."""
    try:
        body = json.loads(await request.body())
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
    spans = claim_spans(text)
    targets = sum(1 for s in spans if not is_not_claim(s.text))
    if targets > MAX_TARGETS:
        raise _bad("too_many_sentences", f"익명 검수는 한 번에 검수 대상 문장 {MAX_TARGETS}개까지입니다. 지금 {targets}개입니다. "
                   "나눠서 붙여 넣어 주세요.", limit=MAX_TARGETS, targets=targets)

    cookie = _anon_cookie(request) or secrets.token_urlsafe(32)
    res = await _reserve(quota, keyer.key(client_ip(request)), estimate(targets), count_run=True)
    job = jobs.create(owner_hash(cookie), corp_code, source, total=len(spans), targets=targets)
    jobs.spawn(job, _run(job, checker, quota, res, text))
    log.info(json.dumps({"event": "factcheck_started", "corp_code": corp_code, "source": source,
                         "sentences": len(spans), "targets": targets}))
    response.set_cookie(COOKIE, cookie, max_age=COOKIE_MAX_AGE_S, httponly=True, samesite=settings.COOKIE_SAMESITE,
                        secure=settings.COOKIE_SECURE, path="/api/factcheck")
    return {"job_id": job.id, "status": job.status, "total": job.total, "targets": job.targets,
            "poll_interval_ms": POLL_MS, "expires_in_s": jobs.expires_in(job)}


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


@router.post("/{job_id}/recheck/{idx}", status_code=202)
async def recheck(job_id: str, idx: int, request: Request, checker: Checker = Depends(get_checker),
                  quota: FactcheckQuota = Depends(get_quota), jobs: JobStore = Depends(get_jobs),
                  keyer: AnonKeyer = Depends(get_keyer)):
    """건너뛴 문장 하나를 강제로 검수한다. 문장은 서버의 job 결과에서 꺼낸다(요청 본문을 받지 않는다).
    문장마다 1회, 토큰은 예약·정산하지만 하루 실행 횟수에서는 세지 않는다."""
    job = _owned(request, jobs, job_id)
    if job.status == "running":
        raise HTTPException(409, {"code": "busy", "message": "검수가 끝난 뒤에 다시 눌러 주세요."})
    current = job.results.get(idx)
    if current is None or current.get("status") != "skipped" or idx in job.rechecked:
        raise HTTPException(409, {"code": "not_skipped", "message": "건너뛴 문장만 한 번씩 검수를 요청할 수 있습니다."})
    res = await _reserve(quota, keyer.key(client_ip(request)), estimate(1), count_run=False)
    job.rechecked.add(idx)
    job.status, job.error = "running", None
    jobs.spawn(job, _run(job, checker, quota, res, current["text"], recheck_idx=idx))
    return {"job_id": job.id, "idx": idx, "status": job.status, "poll_interval_ms": POLL_MS}
