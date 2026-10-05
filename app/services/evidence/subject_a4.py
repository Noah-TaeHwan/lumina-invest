# app/services/evidence/subject_a4.py
"""A-4 주체 판정(A-4 spec 3절): ①c 코드 주체 확인 + ② JEV 주체 질문(후속 요청). 실험 정책(runner.A4_SUBJECT)에서만 켠다.

subject.py·judge.py·lexical.py는 A-2·A-3 사전등록 해시에 묶여 있어 고치지 않고 가져다 쓴다. 바꾸는 부분만 여기 둔다.
제품 실행기(runner)와 A-4 평가(lab/evidence/a4)가 이 모듈의 같은 함수를 쓴다.

①c 코드 규칙(회사형 이름). 팔(arm)마다 다르다:
  - "c"(주 실험 정책 C의 코드 쪽): X0 (e) 부문 규칙을 뺀다(부문·사업 이름은 ②가 본다) + N1 + N2.
  - "a"(보조 팔 A): (e)를 남기고 이름 아닌 말 제외 X1·X2를 더한다 + N1 + N2.
  - "a3x0"(감사 팔, spec 2.1): A-3 규칙에 X0만.  "a3": A-3 규칙 그대로(subject 모듈).
  N1 법인 표지를 지울 때 공백으로 바꾼다(문단 쪽). N2 (b) 라틴 대문자 약칭은 라틴 → 한글 전환을 오른쪽 경계로 본다(문단 쪽).
  한글 → 라틴 전환은 경계가 아니다. X1 띄어 쓴 부문 이름 앞 토큰이 수량 표현(수 관형사 + 단위명사)이면 후보가 아니다.
  X2 부문 이름 핵심어가 명사 + 부사성 접미('~상'·'~별'·'~간'·'~내'·'~외')면 후보가 아니다. X1·X2는 낱말 목록이 아니라
  형태 규칙이다(수 관형사·접미는 닫힌 부류).

② 주체 질문: 판정 질문(judge.INSTRUCTIONS)과 별도로, ✅ 후보 문단만 담은 두 번째 요청으로 "주장의 주체가 이 문단의 주체와
같은가"를 묻는다. 주 판정 요청은 a2-v1과 바이트 단위로 같다(캐시 적중, 짝 비교가 가정 없이 성립).
신호는 두 가지뿐이다(spec 3.2): P(different_subject) < τ_d, P(different_subject) + P(unclear) < τ_d. 통과해야 출처가 된다.

판정(C): a2-v1 SYS 규칙으로 먼저 판정하고(반박·미판정은 그대로), 숫자 확인 ∧ ①c ∧ ② 통과 문단의 지지 최댓값이 τ_s 이상이면
✅. 후속 요청이 실패하면 ❔(사유 subject_unjudged). 모든 팔에서 ✅ ⊆ a2-v1 ✅(✅만 좁힌다).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

from app.services.evidence import judge, lexical, subject
from app.services.evidence.judge import Judgement, sys_decision

SUBJECT_UNJUDGED = "subject_unjudged"  # 후속 요청 실패·마감 초과: 제품에서 ❔ 사유
ARMS = ("c", "a", "a3x0", "a3")
SIGNALS = ("p_diff", "p_diff_unclear")  # spec 3.2에서 고정한 신호 후보 둘
CONTEXTS = ("candidates", "all")  # 후속 요청에 후보 문단만(기본) / 문단 k개 전부(결정 2-2 전환형)

# X1 수 관형사(닫힌 부류). 뒤 토큰은 단위명사로 쓰인 것이다('두 가지'·'세 곳'·'몇 가지'·'여러 가지')
NUMERAL_DETERMINERS = ("한", "두", "세", "석", "네", "넉", "다섯", "여섯", "일곱", "여덟", "아홉", "열", "몇", "여러",
                       "수십", "수백", "수천")
# X2 부사성 접미(닫힌 부류). 핵심어가 명사 + 이 접미이면 이름이 아니다(공시상·지역별·국가간·회사내·해외 등)
ADVERBIAL_SUFFIXES = ("상", "별", "간", "내", "외")
_X2_MIN = 3  # 명사(2자 이상) + 접미 1자

# ② 주체 질문 문구(초안 1판, spec 3.2). 탐색 중 최대 2회 고칠 수 있다(문구 판 최대 3개, lab/evidence/a4.MAX_PROMPT_VERSIONS).
SUBJECT_INSTRUCTIONS = (
    "The state has one [Claim] about a Korean listed company and numbered [Passage] blocks from its annual report. "
    "[Company] wrote the report, and '당사' in a passage means [Company]. Is the [Claim] about the same entity as "
    "[Passage {j}]? Compare only whom or what the facts are about: the company, its business division or segment, "
    "product or brand, counterparty or subsidiary. Do not judge whether the numbers or other facts match. "
    "Judge only from that passage.")
SUBJECT_CRITERIA = {
    "same_subject": "Every company, division, product, brand, counterparty or subsidiary that the claim names is the "
                    "one the passage describes, possibly written differently (abbreviation, spacing, legal suffix, '당사').",
    "different_subject": "The claim names a company, division, product, brand, counterparty or subsidiary that differs "
                         "from the one the passage describes for those facts.",
    "unclear": "The passage does not name the claim's subject clearly enough to compare.",
}
PROMPT_VERSION = "v1"
PROMPTS = {"v1": (SUBJECT_INSTRUCTIONS, SUBJECT_CRITERIA)}


def question_sha(version: str = PROMPT_VERSION) -> str:
    """문구 판의 해시(judge.QUESTION_SHA와 같은 방식). 사전등록에 적는다."""
    instructions, criteria = PROMPTS[version]
    return hashlib.sha256(json.dumps([instructions, criteria], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


SUBJECT_QUESTION_SHA = question_sha()


# --- ①c 코드 주체 확인 ---------------------------------------------------------------------------------------
def _x1(core: str, claim: str) -> bool:
    """X1: 띄어 쓴 부문 이름의 앞 토큰(core) 바로 앞이 수 관형사나 숫자인가('두 가지 사업부문')."""
    toks = [subject._name(t) for t in subject._SPLIT.split(claim) if t]
    return any(t == core and i and (toks[i - 1] in NUMERAL_DETERMINERS or toks[i - 1].isdigit())
               for i, t in enumerate(toks))


def _x2(core: str) -> bool:
    """X2: 핵심어가 명사 + 부사성 접미인가('공시상'·'지역별')."""
    return len(core) >= _X2_MIN and core.endswith(ADVERBIAL_SUFFIXES)


def _groups(claim: str, company: str | Sequence[str], alias: dict[str, set[str]] | None,
            names: subject.CompanyNames | None, arm: str) -> list[tuple[tuple[str, ...], bool]]:
    """subject._groups와 같되(자기 회사 처리 동일) 팔의 후보 규칙(X0·X1·X2)을 적용한다."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm}")
    if arm == "a3":
        return subject._groups(claim, company, alias, names)
    pat = subject._self_pattern(company)
    if pat is not None:
        claim = pat.sub(" 당사", claim)
    seen, groups = set(), []
    for g, div in subject._candidates(claim, names if names is not None else subject.EMPTY_NAMES):
        if div and arm in ("c", "a3x0"):  # X0
            continue
        if div and arm == "a" and (_x1(g[0], claim) or _x2(g[0])):  # X1·X2
            continue
        if any(subject._is_self(n, company, alias) for n in g) or frozenset(g) in seen:
            continue
        seen.add(frozenset(g))
        groups.append((g, div))
    return groups


def code_groups(claim: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None,
                names: subject.CompanyNames | None = None, arm: str = "c") -> list[tuple[str, ...]]:
    """팔의 이름 후보 묶음."""
    return [g for g, _ in _groups(claim, company, alias, names, arm)]


def _found(name: str, text: str, caps: bool) -> bool:
    """subject._found에 N2를 더한 것: (b) 라틴 대문자 약칭이면 라틴 → 한글 전환도 오른쪽 경계로 본다."""
    n = subject.normalize(name)
    if not n:
        return True
    right = rf"(?:{lexical._TAIL_RE}){{0,2}}(?!{lexical._BOUND})"
    if caps and re.search(r"[a-z0-9]$", n):
        right = rf"(?:{right}|[가-힣])"
    pat = rf"(?<!{lexical._BOUND})" + r"\s*".join(map(re.escape, n)) + rf"(?={right})"
    return re.search(pat, text, re.I) is not None


def _group_ok(group: tuple[str, ...], text: str, alias: dict[str, set[str]], n2: bool) -> bool:
    def hit(x: str) -> bool:
        return _found(x, text, n2 and subject._is_caps(x)) if n2 else subject._found(x, text)
    return any(hit(n) or any(hit(a) for a in alias.get(subject.normalize(n), ())) for n in group)


def code_missing(claim: str, passage: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None,
                 names: subject.CompanyNames | None = None, arm: str = "c") -> list[str]:
    """문단에서 찾지 못한 후보 묶음의 대표 이름(감사용). 부문 이름은 문단에 부문 이름이 있을 때만 대조한다(A-3와 같음)."""
    if arm == "a3":
        return subject.missing_subjects(claim, passage, company, alias, names)
    alias = alias or {}
    n1n2 = arm in ("c", "a")
    text = subject._LEGAL.sub(" ", passage) if n1n2 else subject._strip_legal(passage)  # N1
    gs = _groups(claim, company, alias, names, arm)
    has_div = any(div for _, div in gs) and subject._passage_divisions(passage)
    return [g[0] for g, div in gs if (has_div or not div) and not _group_ok(g, text, alias, n1n2)]


def code_valid(claim: str, passages: list[str], company: str | Sequence[str],
               names: subject.CompanyNames | None = None, arm: str = "c") -> list[bool]:
    """문단별 ①c 코드 주체 확인 통과 여부. 괄호 약칭 정의는 문단 묶음 전체에서 모은다(subject.aliases)."""
    alias = subject.aliases(passages)
    return [not code_missing(claim, p, company, alias, names, arm) for p in passages]


# --- ② JEV 주체 질문(후속 요청) ------------------------------------------------------------------------------
def candidate_passages(s: list[float] | None, c: list[float] | None, valid: list[bool], tau_s: float, tau_c: float,
                       best: int | None = None, ok: bool = True) -> list[int]:
    """후속 요청에 넣을 후보 문단(0 기준, 오름차순): a2-v1 SYS 규칙으로 반박·미판정이 아니고 숫자 확인을 통과했고 s ≥ τ_s인
    문단. best(1차 필터 상단 구간 최고 점수 문단)가 있으면 더한다. 주 판정 전이면(s 없음) best만."""
    out = set()
    if s is not None and c is not None:
        dec = sys_decision(Judgement(s, c, ok, 1), valid, tau_s, tau_c)[0]
        if dec not in ("unjudged", "contradicted"):
            out = {i for i, (x, v) in enumerate(zip(s, valid)) if v and x >= tau_s}
    if best is not None:
        out.add(best)
    return sorted(out)


def build_subject_questions(ids: list[int], version: str = PROMPT_VERSION) -> dict:
    """문단 번호(1 기준)마다 주체 질문 하나(s{j})."""
    instructions, criteria = PROMPTS[version]
    return {f"s{j}": {"type": "choice", "instructions": instructions.format(j=j), "criteria": criteria} for j in ids}


def build_followup(company: str, claim: str, passages: list[str], cand: list[int], context: str = "candidates",
                   version: str = PROMPT_VERSION) -> tuple[str, dict, dict[str, int]]:
    """후속 요청 (state, 질문, 질문 id → 원래 문단 번호(0 기준)).

    candidates: 후보 문단만 [Passage 1]..[Passage m]으로 다시 번호를 매긴다(주 판정과 같은 judge.build_state 꼴).
    all: 문단 k개 전부를 보이고 후보 문단에만 묻는다(결정 2-2 전환형)."""
    if context == "candidates":
        state = judge.build_state(company, claim, [passages[i] for i in cand])
        qmap = {f"s{j}": i for j, i in enumerate(cand, 1)}
    elif context == "all":
        state = judge.build_state(company, claim, passages)
        qmap = {f"s{i + 1}": i for i in cand}
    else:
        raise ValueError(f"unknown context {context}")
    return state, build_subject_questions([int(q[1:]) for q in qmap], version), qmap


def followup_probs(answers: dict, qmap: dict[str, int]) -> dict[int, dict[str, float]]:
    """후속 응답(jev.parse_answers 검증 뒤)을 문단 번호(0 기준) → 선택지 확률로."""
    return {i: dict(answers[q]) for q, i in qmap.items()}


def signal_value(probs: dict[str, float], signal: str) -> float:
    if signal == "p_diff":
        return probs["different_subject"]
    if signal == "p_diff_unclear":
        return probs["different_subject"] + probs["unclear"]
    raise ValueError(f"unknown signal {signal}")


def question_pass(probs: dict[str, float] | None, signal: str, tau_d: float) -> bool:
    """② 주체 통과: 신호 < τ_d. 묻지 않은 문단(probs 없음)은 통과가 아니다."""
    if signal not in SIGNALS:
        raise ValueError(f"unknown signal {signal}")
    return probs is not None and signal_value(probs, signal) < tau_d


def q_mask(qprobs: dict[int, dict[str, float]] | None, n: int, signal: str, tau_d: float) -> list[bool] | None:
    """문단별 ② 통과 여부. 후속 요청이 실패했거나 없으면 None."""
    if qprobs is None:
        return None
    return [question_pass(qprobs.get(i), signal, tau_d) for i in range(n)]


# --- 판정 ---------------------------------------------------------------------------------------------------
def decide(s: list[float], c: list[float], valid: list[bool], code_ok: list[bool] | None, q_ok: list[bool] | None,
           tau_s: float, tau_c: float, *, code: bool = True, question: bool = True) -> tuple[str, int | None, float]:
    """a2-v1 SYS 규칙 위에서 ✅만 좁힌다. code·question이 켜진 조건(①c 코드, ② 질문)을 함께 통과한 문단의 지지 최댓값이
    τ_s 이상이면 ✅. C = code+question, A = code(팔 a 마스크), B = question, a3 = code(A-3 마스크).
    question이 켜졌는데 q_ok가 None(후속 실패)이면 a2-v1 ✅ 주장은 근거 없음(점수 0)이다."""
    base = sys_decision(Judgement(s, c, True, 1), valid, tau_s, tau_c)
    if base[0] in ("unjudged", "contradicted") or not (code or question):
        return base
    if question and q_ok is None:  # 후속 요청이 필요한 것은 a2-v1 ✅뿐이다
        return ("no_evidence", None, 0.0) if base[0] == "supported" else base
    if code and code_ok is None:
        raise ValueError("code mask is required")
    n = len(s)
    mask = [(not code or code_ok[i]) and (not question or q_ok[i]) for i in range(n)]
    return subject.subject_decision(Judgement(s, c, True, 1), valid, mask, tau_s, tau_c)


def route(lex: float, high_ok: bool, best: int | None, code_ok: list[bool] | None, q_ok: list[bool] | None,
          theta_low: float | None, theta_high: float | None, *, code: bool = True, question: bool = True) -> str:
    """1차 필터 경로: 상단 구간은 최고 점수 문단이 켜진 조건(①c·②)을 모두 통과해야 JEV 주 판정 없이 ✅."""
    def passes(mask: list[bool] | None) -> bool:
        return mask is not None and best is not None and 0 <= best < len(mask) and mask[best]
    ok = high_ok and (not code or passes(code_ok)) and (not question or passes(q_ok))
    return lexical.tier_route(lex, ok, theta_low, theta_high)


def _policy_ready(policy) -> None:
    if policy.tau_d is None or policy.subject_signal not in SIGNALS:
        raise ValueError(f"{policy.version}: tau_d·subject_signal이 사전등록 값으로 정해지지 않았다")


def route_claim(policy, lex: float, high_ok: bool, best: int | None, code_ok: list[bool] | None,
                q_ok: list[bool] | None) -> str:
    """정책(runner.Policy, subject_question)의 경로. 제품 실행기와 A-4 평가 공용."""
    _policy_ready(policy)
    return route(lex, high_ok, best, code_ok, q_ok, policy.theta_low, policy.theta_high)


def decide_claim(policy, s: list[float], c: list[float], valid: list[bool], code_ok: list[bool] | None,
                 q_ok: list[bool] | None) -> tuple[str, int | None, float]:
    """정책(runner.Policy, subject_question)의 JEV 판정(C). 제품 실행기와 A-4 평가 공용."""
    _policy_ready(policy)
    return decide(s, c, valid, code_ok, q_ok, policy.tau_s, policy.tau_c)
