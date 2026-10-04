# app/services/evidence/subject.py
"""주체 확인(A-3 spec 3절): 숫자 확인처럼 코드로 보는 ✅ 필요조건. 실험 정책(runner.A3_SUBJECT)에서만 켠다.

주장의 회사·부문·제품 이름 후보가 근거 문단에 토큰 경계로 있어야 그 문단이 ✅ 출처가 될 수 있다.
후보는 **허용 목록(엔티티)** 만이다(초안 3판, 2026-10-04 A-2 조정 세트 탐색에서 일반명사 제외 목록 방식의 재현율 손실
0.0845 → 제외 목록은 수렴하지 않는다고 판단):
  (a) 법인 표지(㈜·(주)·주식회사)가 붙은 토큰(띄어 쓴 표지면 앞 토큰), (b) 라틴 대문자 약칭(ERP·SOC·ESCADA),
  (c) 따옴표 안 이름, (d) 상장사 이름 사전(DART corpCode.xml의 상장사 corp_name·corp_eng_name)에 있는 이름,
  (e) '~부문·~사업부·~본부·~사업본부'로 끝나는 부문 이름(같은 문단의 부문 이름과 대조, 문단에 부문 이름이 없으면 묻지 않음).
일반명사·서술어는 후보가 되지 않는다. 대가로 일반명사로 된 제품·설비 이름 교체(아토젠정·냉연강판)는 놓친다.

- 선택 회사 자신(회사명과 DART 종목명·영문명, 법인 표기 무관)은 후보에서 뺀다. 접두만 같은 이름(포스코 ↔
  포스코인터내셔널)은 계열사이므로 빼지 않는다.
- 표기 차이(㈜·(주)·주식회사·Co., Ltd., 띄어쓰기, 라틴 대소문자, 문단 묶음 안 괄호 약칭 정의, 부문 꼬리 변형)는 같은
  이름으로 본다.

판정(subject_decision)은 a2-v1 SYS 규칙 위에서 ✅만 좁힌다: 반박 규칙은 숫자 확인만 본 S_V를 그대로 쓴다.
제품 실행기(runner)와 A-3 평가(lab/evidence/a3)가 이 모듈의 같은 함수를 쓴다. lexical.py는 A-2 사전등록 해시에 묶여 있어
고치지 않고 가져다 쓴다.
"""
from __future__ import annotations

import functools
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from pathlib import Path

from app.services.evidence import lexical
from app.services.evidence.judge import Judgement, sys_decision

# 일반명사(이름이 아니다). 허용 목록 방식에서는 부문 이름의 핵심어와 상장사 이름 사전 항목을 거르는 데만 쓴다.
# AI(Claude Code)가 일반 어휘로 초안을 쓰고, A-2 조정 세트 자연 주장 문장(탐색 허용, 확인 세트는 보지 않음)에서 잡힌 낱말과
# 독립 리뷰가 짚은 낱말을 더했다. 확정 뒤에는 고치지 않는다(코드 해시로 대조).
SUBJECT_GENERIC = (
    "기준", "거래", "보고서", "제출일", "기준일", "사업연도", "연도", "분기", "반기", "상반기", "하반기", "올해", "작년",
    "품목", "상품", "원료", "주원료", "부품", "소재", "재료", "가동률", "생산능력", "생산량", "실적", "수량", "금액",
    "인증", "특허", "상표권", "솔루션", "시스템", "플랫폼", "엔지니어", "인력", "직원", "임직원", "연구원", "연구소",
    "연예인", "배우", "아티스트", "사무소", "영업소", "지점", "공사", "도급", "수주", "수주잔고", "발주처",
    "소비자", "사용자", "이용자", "정부", "규제", "정책", "경기", "업황", "산업", "업계", "업종", "분야", "제조",
    "점유율", "시장점유율", "경쟁", "경쟁력", "전략", "목표", "계획", "목적", "과제", "역할", "기능", "성능", "품질",
    "자금", "현금", "차입금", "부채", "부채비율", "자산", "자본", "배당", "주식", "손익", "당기손익", "손실", "수익",
    "매입", "매입액", "비율", "규모", "증가", "감소", "변동", "영향", "단가", "원가", "구성", "구조", "형태", "방식",
    "구축", "운영", "관리", "유지", "설계", "시공", "분석", "정보", "데이터", "프로젝트", "프로세스", "장비", "기반",
    "글로벌", "별도", "기존", "전반", "경우", "때문", "통해", "위해", "따라", "대비", "특징", "특성", "변화", "상황",
    "천원", "백만원", "억원", "원", "콘텐츠", "조직", "체제", "가동", "최고", "최적", "트렌드", "전망", "평균", "사업부문",
    "성과", "절감", "개선", "확대", "축소", "강화", "확보", "효율", "효율성", "공정", "배터리", "음반", "철광석",
)
_GENERIC = frozenset(lexical.GENERIC_NOUNS) | frozenset(SUBJECT_GENERIC)
# (e) 부문 이름 꼬리와 대조할 때의 변형. 핵심어가 일반 부문어면(영업부문·기타부문·공공부문) 이름이 아니다
DIVISION_SUFFIXES = ("사업본부", "사업부문", "사업부", "본부", "부문")
DIVISION_GENERIC_CORES = ("영업", "사업", "기타", "공공", "민간", "해외", "국내", "전체", "주요", "각", "일부", "신규",
                          "기존", "주력", "전사", "연결", "보고", "당사", "회사", "모든", "개별", "단일", "복수", "여러")
_DIVISION_ALTS = ("부문", "사업부문", "사업부", "사업본부", "본부", "사업")
# (d) 상장사 이름 중 일상어와 겹쳐 이름으로 볼 수 없는 것('고객을 대상으로')
COMMON_WORD_NAMES = ("대상", "한국", "동방", "국보", "세방", "대교", "한일", "동양", "신성", "대성", "삼양", "태양")
_QUOTE_MAX = 20  # 따옴표 안 이름 최대 길이(문장 전체 인용은 이름이 아니다)
_NGRAM = 4  # 상장사 이름 사전과 맞출 연속 토큰 수 상한('Hyundai Motor Company', '고려 제강')

_LEGAL = re.compile(r"㈜|\(\s*주\s*\)|주식회사|\bCo\.,?\s*Ltd\.?|\bCo\.(?![A-Za-z])|\bLtd\.?(?![A-Za-z])"
                    r"|\bInc\.?(?![A-Za-z])|\bCorp(?:oration|\.)?(?![A-Za-z])", re.I)
_MARK = re.compile(r"㈜|\(\s*주\s*\)|주식회사")
_SPLIT = re.compile(r"[\s(),/·:;\[\]]+")
_CAPS = re.compile(r"[A-Z][A-Z0-9&\-]*[A-Z0-9]")
_DIGIT = re.compile(r"\d")
# 괄호 약칭 정의: X(Y), X(이하 'Y'), X(이하 "Y"라 한다). 법인 표지는 먼저 지운 본문에서 찾는다
_ALIAS = re.compile(r"([가-힣A-Za-z0-9&.\-]{2,})\s*\(\s*(?:이하\s*)?['‘\"“「]?([^()'’\"”」,]{2,}?)['’\"”」]?"
                    r"\s*(?:(?:이)?라\s*(?:한다|함|합니다))?\s*\)")


def normalize(name: str) -> str:
    """비교용 이름: 법인 표지·영문 법인 접미·공백을 지우고 라틴 글자는 소문자로."""
    return re.sub(r"\s+", "", _LEGAL.sub("", name)).casefold()


def _strip_legal(text: str) -> str:
    return _LEGAL.sub("", text)


class CompanyNames:
    """(d) 상장사 이름 사전: 정규화한 회사명·영문명 집합. 일반명사·일상어와 겹치는 이름, 1자 이름은 넣지 않는다."""

    def __init__(self, names: Iterable[str] = (), eng: Iterable[str] = ()):
        skip = _GENERIC | frozenset(COMMON_WORD_NAMES)
        self.names = frozenset(n for n in (normalize(x) for x in (*names, *eng) if x)
                               if len(n) >= 2 and n not in skip)

    def __contains__(self, name: str) -> bool:
        return normalize(name) in self.names

    def __len__(self) -> int:
        return len(self.names)

    @classmethod
    def from_corpcode(cls, xml_bytes: bytes) -> "CompanyNames":
        """DART CORPCODE.xml에서 종목코드가 있는(상장) 회사의 corp_name·corp_eng_name(있으면)."""
        names, eng = [], []
        for e in ET.fromstring(xml_bytes).iter("list"):
            if (e.findtext("stock_code") or "").strip():
                names.append((e.findtext("corp_name") or "").strip())
                eng.append((e.findtext("corp_eng_name") or "").strip())
        return cls(names, eng)


EMPTY_NAMES = CompanyNames()
# 런타임 사전: load_passages가 받아 둔 corpCode.xml(app.services.evidence.load_passages.DEFAULT_DATA_DIR)
DEFAULT_CORPCODE = Path("data/evidence_passages/corpCode.xml")


@functools.lru_cache(maxsize=4)
def load_company_names(path: str | Path = DEFAULT_CORPCODE) -> CompanyNames:
    """corpCode.xml을 한 번만 읽는다. 파일이 없으면 빈 사전((d) 규칙만 꺼진다)."""
    p = Path(path)
    return CompanyNames.from_corpcode(p.read_bytes()) if p.exists() else EMPTY_NAMES


def _name(tok: str) -> str:
    """토큰의 이름 부분: 앞뒤 부호와 뒤 조사(두 겹까지)를 뗀다."""
    t = lexical._EDGE.sub("", tok)
    for _ in range(2):
        s = lexical._stem(t)
        if not s:
            break
        t = s
    return t


def _is_caps(name: str) -> bool:
    """(b) 라틴 대문자 약칭(대문자 2자 이상)."""
    return bool(_CAPS.fullmatch(name)) and sum(c.isupper() for c in name) >= 2


def _division_core(name: str) -> str | None:
    """(e) 부문 이름의 핵심어('토목사업부문' → '토목'). 일반 부문어·일반명사면 None."""
    for d in DIVISION_SUFFIXES:
        if name.endswith(d):
            core = name[:-len(d)]
            core = core[:-2] if core.endswith("사업") and len(core) > 3 else core
            return core if _division_word(core) else None
    return None


def _division_word(core: str) -> bool:
    return (len(core) >= 2 and not _DIGIT.search(core) and core not in DIVISION_GENERIC_CORES
            and core not in _GENERIC and re.fullmatch(r"[가-힣A-Za-z][가-힣A-Za-z0-9&\-]*", core) is not None)


def _division_alts(core: str) -> tuple[str, ...]:
    return (core, *(core + x for x in _DIVISION_ALTS))


def _self_names(company: str | Sequence[str]) -> list[str]:
    """선택 회사 자신의 표기: DART 회사명과 근거 있는 별칭(종목명·영문명). 문자열 하나면 그 이름만."""
    names = [company] if isinstance(company, str) else list(company)
    return [n for n in names if n and normalize(n)]


def _self_pattern(company: str | Sequence[str]) -> re.Pattern | None:
    """주장에서 자기 회사 표기를 찾는 정규식(앞 법인 표지·글자 사이 공백 허용, 대소문자 무시, 토큰 경계, 뒤 조사 허용).
    정규화 전 표기(Hyundai Motor Company)와 법인 표지를 뗀 표기(NAVER)를 모두 보고, 긴 것부터 맞춘다."""
    forms = set()
    for n in _self_names(company):
        forms |= {re.sub(r"\s+", "", n).casefold(), normalize(n)}
    forms = sorted((f for f in forms if len(f) >= 2), key=len, reverse=True)
    if not forms:
        return None
    alts = "|".join(r"\s*".join(map(re.escape, f)) for f in forms)
    return re.compile(rf"(?:㈜|\(\s*주\s*\)|주식회사)?\s*(?<!{lexical._BOUND})(?:{alts})"
                      rf"(?=(?:{lexical._TAIL_RE}){{0,2}}(?!{lexical._BOUND}))", re.I)


def _is_self(name: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None) -> bool:
    """선택 회사 자신인가: 정규화 후 회사명·별칭과 정확히 같다(괄호 별칭 포함). 접두만 같은 계열사는 아니다."""
    selves = {normalize(n) for n in _self_names(company)}
    forms = {normalize(name)} | {normalize(a) for a in (alias or {}).get(normalize(name), ())}
    return bool(selves & forms)


def _candidates(claim: str, names: CompanyNames) -> list[tuple[tuple[str, ...], bool]]:
    """허용 목록 후보: (대안 묶음, 부문 이름 여부) 목록. 선택 회사 처리는 하지 않는다."""
    toks = [t for t in _SPLIT.split(_MARK.sub("㈜", claim)) if t]
    plain = [_name(t.replace("㈜", "")) for t in toks]
    out: list[tuple[tuple[str, ...], bool]] = []
    used: set[int] = set()
    for i in range(len(toks)):  # (d) 사전: 긴 연속 토큰부터('Hyundai Motor Company', '고려 제강')
        for n in range(min(_NGRAM, len(toks) - i), 0, -1):
            span = range(i, i + n)
            if used & set(span):
                continue
            joined = " ".join(plain[j] for j in span)
            if plain[i + n - 1] and joined in names:
                out.append(((joined, joined.replace(" ", "")) if n > 1 else (joined,), False))
                used |= set(span)
                break
    for i, t in enumerate(toks):
        if i in used:
            continue
        nm = plain[i]
        if "㈜" in t:  # (a) 법인 표지: 붙은 토큰이면 그 이름. 표지만 있으면 앞에 둔 표지('주식회사 X')는 뒤 토큰,
            # 조사가 붙은 표지('X 주식회사와')는 앞 토큰
            j = i + 1 if t == "㈜" else i - 1
            if len(nm) >= 2:
                out.append(((nm,), False))
            elif 0 <= j < len(toks) and j not in used and len(plain[j]) >= 2 and "㈜" not in toks[j]:
                out.append(((plain[j],), False))
                used.add(j)
        elif _is_caps(nm):  # (b)
            out.append(((nm,), False))
        elif (core := _division_core(nm)) is not None:  # (e) 붙여 쓴 부문 이름
            out.append((_division_alts(core), True))
        elif nm in DIVISION_SUFFIXES and i and _division_word(plain[i - 1]) and "㈜" not in toks[i - 1]:
            out.append((_division_alts(plain[i - 1]), True))  # (e) 띄어 쓴 부문 이름('토목 부문')
    out += [((m.strip(),), False) for m in lexical._QUOTED.findall(claim)  # (c)
            if 2 <= len(m.strip()) <= _QUOTE_MAX and m.strip() not in _GENERIC]
    return out


def _groups(claim: str, company: str | Sequence[str], alias: dict[str, set[str]] | None,
            names: CompanyNames | None) -> list[tuple[tuple[str, ...], bool]]:
    pat = _self_pattern(company)
    if pat is not None:
        claim = pat.sub(" 당사", claim)
    seen, groups = set(), []
    for g, div in _candidates(claim, names if names is not None else EMPTY_NAMES):
        if any(_is_self(n, company, alias) for n in g) or frozenset(g) in seen:
            continue
        seen.add(frozenset(g))
        groups.append((g, div))
    return groups


def subject_groups(claim: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None,
                   names: CompanyNames | None = None) -> list[tuple[str, ...]]:
    """주장의 이름 후보 묶음(허용 목록). 묶음 안의 대안 중 하나만 문단에 있으면 그 묶음은 통과다.
    선택 회사 자신(회사명·DART 별칭)은 먼저 '당사'로 바꿔 빼고, 남은 묶음도 자기 회사면 뺀다."""
    return [g for g, _ in _groups(claim, company, alias, names)]


def _passage_divisions(text: str) -> bool:
    """문단에 (e) 규칙의 부문 이름이 하나라도 있는가(부문 이름은 이것과 대조한다)."""
    return any(div for _, div in _candidates(text, EMPTY_NAMES))


def aliases(passages: list[str]) -> dict[str, set[str]]:
    """문단 묶음의 괄호 약칭 정의에서 정규화 이름 → 별칭(원래 표기) 사전을 만든다(양방향)."""
    out: dict[str, set[str]] = {}
    for p in passages:
        for a, b in _ALIAS.findall(_strip_legal(p)):
            a, b = a.strip(), b.strip()
            if normalize(a) and normalize(b) and normalize(a) != normalize(b):
                out.setdefault(normalize(a), set()).add(b)
                out.setdefault(normalize(b), set()).add(a)
    return out


def _found(name: str, text: str) -> bool:
    """정규화 이름이 법인 표지를 지운 문단에 토큰 경계로 있는가. 글자 사이 공백은 허용, 뒤 조사 허용, 라틴 대소문자 무시."""
    n = normalize(name)
    if not n:
        return True
    pat = (rf"(?<!{lexical._BOUND})" + r"\s*".join(map(re.escape, n))
           + rf"(?=(?:{lexical._TAIL_RE}){{0,2}}(?!{lexical._BOUND}))")
    return re.search(pat, text, re.I) is not None


def _group_ok(group: tuple[str, ...], text: str, alias: dict[str, set[str]]) -> bool:
    return any(_found(n, text) or any(_found(a, text) for a in alias.get(normalize(n), ())) for n in group)


def missing_subjects(claim: str, passage: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None,
                     names: CompanyNames | None = None) -> list[str]:
    """문단에서 찾지 못한 후보 묶음의 대표 이름(감사·리포트용). 부문 이름은 문단에 부문 이름이 있을 때만 대조한다."""
    alias = alias or {}
    text = _strip_legal(passage)
    gs = _groups(claim, company, alias, names)
    has_div = any(div for _, div in gs) and _passage_divisions(passage)
    return [g[0] for g, div in gs if (has_div or not div) and not _group_ok(g, text, alias)]


def subject_ok(claim: str, passage: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None,
               names: CompanyNames | None = None) -> bool:
    """주장의 이름 후보 묶음이 모두 문단에 있으면 참(후보가 없으면 참)."""
    return not missing_subjects(claim, passage, company, alias, names)


def subject_valid(claim: str, passages: list[str], company: str | Sequence[str],
                  names: CompanyNames | None = None) -> list[bool]:
    """문단별 주체 확인 통과 여부. 괄호 약칭 정의는 문단 묶음 전체에서 모은다(판정 문단 k개와 같은 범위)."""
    alias = aliases(passages)
    return [subject_ok(claim, p, company, alias, names) for p in passages]


def subject_decision(j: Judgement, valid: list[bool], subj: list[bool], tau_s: float,
                     tau_c: float) -> tuple[str, int | None, float]:
    """a2-v1 SYS 규칙(judge.sys_decision) 위에서 ✅만 좁힌다.

    반박·미판정은 sys_decision 그대로(숫자 확인만 본 S_V). 그 밖에는 숫자 확인과 주체 확인을 함께 통과한 문단의
    지지 최댓값 S_VS가 τ_s 이상이면 지지됨(그 문단이 출처), 아니면 근거 없음. 점수는 S_VS.
    """
    base = sys_decision(j, valid, tau_s, tau_c)
    if base[0] in ("unjudged", "contradicted"):
        return base
    cand = [(s, i) for i, (s, v, ok) in enumerate(zip(j.s, valid, subj)) if v and ok]
    s_vs, idx = max(cand) if cand else (0.0, None)
    if idx is not None and s_vs >= tau_s:
        return "supported", idx, s_vs
    return "no_evidence", None, s_vs


def subject_high_ok(high_ok: bool, best: int | None, subj: list[bool]) -> bool:
    """1차 필터 상단 구간 조건에 주체 확인을 더한다: 최고 점수 문단도 주체 확인을 통과해야 한다."""
    return high_ok and best is not None and 0 <= best < len(subj) and subj[best]


def route_claim(policy, lex: float, high_ok: bool, best: int | None, subj: list[bool] | None) -> str:
    """1차 필터 경로(제품 실행기·A-3 평가 공용). policy는 runner.Policy(τ·θ·subject_check). 주체 확인 정책이면 상단 구간에 최고 점수 문단의 주체 확인을 더한다."""
    if policy.subject_check:
        high_ok = subject_high_ok(high_ok, best, subj or [])
    return lexical.tier_route(lex, high_ok, policy.theta_low, policy.theta_high)


def decide_claim(policy, s: list[float], c: list[float], valid: list[bool],
                 subj: list[bool] | None) -> tuple[str, int | None, float]:
    """JEV 확률에 정책의 SYS 규칙을 적용한다(제품 실행기·A-3 평가 공용). 주체 확인 정책이면 subject_decision."""
    j = Judgement(s, c, True, 1)
    if policy.subject_check:
        return subject_decision(j, valid, subj, policy.tau_s, policy.tau_c)
    return sys_decision(j, valid, policy.tau_s, policy.tau_c)
