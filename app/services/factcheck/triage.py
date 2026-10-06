"""팩트체커 1단계 검수 대상 분류(설계 '처리 흐름' 2번, Codex #11).

- 규칙 먼저: 비주장 규칙(evidence.claims.not_claim_reason)에 걸리면 검수 안 함. 숫자·회사명(상장사명 사전)·기간 표현이
  든 문장은 무조건 검수 대상(분류 오류로 놓치지 않게). 나머지는 '검수 안 함' + 이유 범주(주가·목표주가 → 범위 밖,
  의견·전망 표현 → 의견, 그 밖 → 사실 표지 없음). 건너뛴 문장은 화면에서 이유와 함께 보이고 사용자가 검수를 요청한다.
- JEV 1단계 분류는 함수만 두고 **기본 꺼짐**(JEV_TRIAGE_ENABLED). 리포트 변조 도그푸드(T5)에서 '규칙만' 대비 재현율·비용이
  이길 때 켠다. 켜면 규칙에서 빠진 문장(비주장 규칙에 걸린 것 제외)만 한 번의 묶음 요청(문장마다 선택형 질문 하나)으로
  분류하고, '검수 안 함' 확률이 기준값 이상일 때만 건너뛴다(애매하면 검수). 요청이 실패하면 전부 검수로 대체한다.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.evidence.claims import not_claim_reason
from app.services.factcheck import scope
from app.services.factcheck.scope import CompanyIndex, Period

JEV_TRIAGE_ENABLED = False  # 도그푸드에서 규칙 대비 이길 때 켠다(Codex #11)
JEV_SKIP_THRESHOLD = 0.8  # 잠정값. '검수 안 함' 확률이 이 값 이상일 때만 건너뛴다. 조정 세트에서 정한다(재현율 우선)

_DIGIT = re.compile(r"\d")
OPINION = re.compile(r"것으로\s*보|것이라|것이다|판단한다|판단된다|생각한다|본다|보인다|예상|전망|기대|추정|우려|가능성"
                     r"|여지|할\s*수\s*있|듯하|같다|바람직|필요하다|해야")

TRIAGE_INSTRUCTIONS = ("The state has numbered [Sentence] blocks from a draft article about the Korean listed company "
                       "in [Company]. What kind of statement is [Sentence {j}]?")
TRIAGE_CRITERIA = {
    "fact": "A factual statement about the company that its filings (annual, half-year or quarterly reports, "
            "preliminary earnings) could confirm or refute.",
    "opinion": "The writer's opinion, judgement, forecast or recommendation.",
    "out_of_scope": "A statement about stock price, target price, valuation, analyst estimates or market outlook, "
                    "which filings cannot confirm.",
    "other_company": "A statement whose subject is a different company.",
}
_SKIP_CATEGORIES = ("opinion", "out_of_scope", "other_company")

log = logging.getLogger("app.factcheck.triage")


@dataclass(frozen=True)
class Triage:
    """문장 하나의 1단계 결과. check=False면 category·reason이 회색 표시 이유다."""

    check: bool
    category: str
    reason: str


def rule_triage(text: str, *, corp_code: str, names: Mapping[str, Iterable[str]] | CompanyIndex,
                as_of: Period) -> Triage:
    """규칙 분류. 순서: 비주장 규칙(목록 머리말 포함) → 숫자 → 회사명 → 기간 → (검수 안 함) 주가 → 의견·전망 → 사실 표지 없음."""
    r = not_claim_reason(text)
    if r:
        return Triage(False, "opinion", f"not_claim:{r.split(':')[0]}")
    if scope.is_list_lead(text):  # 목록 머리말: JEV 분류 전에 뺀다(기존 사유 코드 재사용)
        return Triage(False, "opinion", "not_claim:lead")
    if _DIGIT.search(text):
        return Triage(True, "checked", "rule:number")
    if scope.company_mentions(text, names):
        return Triage(True, "checked", "rule:company")
    if scope.extract_periods(text, as_of):
        return Triage(True, "checked", "rule:period")
    if scope.MARKET.search(text):
        return Triage(False, "out_of_scope", "market")
    if OPINION.search(text):
        return Triage(False, "opinion", "opinion")
    return Triage(False, "opinion", "no_fact_marker")


def build_triage_request(company: str, sentences: Sequence[str]) -> tuple[str, dict]:
    """[Company]·[Sentence n] state와 문장마다 선택형 질문 하나(s1..sn)."""
    lines = [f"[Company] {company}"] + [f"[Sentence {j}] {s}" for j, s in enumerate(sentences, 1)]
    questions = {f"s{j}": {"type": "choice", "instructions": TRIAGE_INSTRUCTIONS.format(j=j),
                           "criteria": TRIAGE_CRITERIA} for j in range(1, len(sentences) + 1)}
    return "\n".join(lines), questions


def _from_probs(p: dict[str, float], threshold: float) -> Triage:
    skip = sum(p.get(k, 0.0) for k in _SKIP_CATEGORIES)
    if skip >= threshold:
        cat = max(_SKIP_CATEGORIES, key=lambda k: p.get(k, 0.0))
        return Triage(False, cat, f"jev:{cat}")
    fact_top = p.get("fact", 0.0) >= max(p.get(k, 0.0) for k in _SKIP_CATEGORIES)
    return Triage(True, "checked", "jev:fact" if fact_top else "jev:uncertain")


async def jev_triage(client: Any, company: str, sentences: Sequence[str], *, user_id: str,
                     threshold: float = JEV_SKIP_THRESHOLD) -> list[Triage]:
    """규칙에서 빠진 문장들을 JEV 한 번(묶음)으로 분류한다. client는 ServiceJevClient.ask 모양.
    요청이 실패하거나 응답이 모자라면 전부 검수 대상으로 돌려준다(놓침 방지)."""
    if not sentences:
        return []
    state, questions = build_triage_request(company, sentences)
    try:
        r = await client.ask(state, questions, user_id=user_id, log_ctx={"stage": "triage"})
        if not r.ok:
            code = r.error_code or "error"
        else:
            return [_from_probs(r.answers[f"s{j}"], threshold) for j in range(1, len(sentences) + 1)]
    except Exception as exc:  # noqa: BLE001 — 분류 실패는 전부 검수로 대체한다(설계 실패 모드)
        log.error(json.dumps({"event": "triage_error", "error": type(exc).__name__}))
        code = "error"
    return [Triage(True, "checked", f"jev_failed:{code}") for _ in sentences]


async def triage_all(sentences: Sequence[str], *, corp_code: str, company: str,
                     names: Mapping[str, Iterable[str]] | CompanyIndex, as_of: Period, client: Any = None,
                     enabled: bool = JEV_TRIAGE_ENABLED, user_id: str = "factcheck",
                     threshold: float = JEV_SKIP_THRESHOLD) -> list[Triage]:
    """문장마다 1단계 결과. enabled이고 client가 있으면 규칙에서 빠진 문장(비주장 제외)만 JEV 묶음 분류로 다시 본다."""
    out = [rule_triage(s, corp_code=corp_code, names=names, as_of=as_of) for s in sentences]
    if not (enabled and client is not None):
        return out
    rest = [i for i, t in enumerate(out) if not t.check and not t.reason.startswith("not_claim")]
    for i, t in zip(rest, await jev_triage(client, company, [sentences[i] for i in rest], user_id=user_id,
                                           threshold=threshold), strict=True):
        out[i] = t
    return out
