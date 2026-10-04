# app/services/evidence/subject.py
"""주체 확인(A-3 spec 3절): 숫자 확인처럼 코드로 보는 ✅ 필요조건. 실험 정책(runner.A3_SUBJECT)에서만 켠다.

주장의 회사·부문·제품 이름 후보가 근거 문단에 토큰 경계로 있어야 그 문단이 ✅ 출처가 될 수 있다.
1차 필터의 회사명 조건(lexical.names_in_passage)과 오류 방향이 반대다: 거기서는 후보가 많으면 JEV로 넘어갈 뿐이지만,
여기서는 후보가 많으면 진짜 지지 주장이 ❔가 된다(재현율 손실). 그래서
- 선택 회사 자신(회사명과 DART 종목명·영문명, 법인 표기 무관)은 후보에서 뺀다. 사업보고서 문단은 자기 회사를 '당사'로
  쓴다. 접두만 같은 이름(포스코 ↔ 포스코인터내셔널)은 계열사이므로 빼지 않는다.
- 서술형 활용(늘린다·올렸다·늘리는)은 이름이 아니다. 부문명은 꼬리 변형('건설 부문' ↔ '건설사업부문')을 같은 것으로 본다.
- 일반명사(lexical.GENERIC_NOUNS + SUBJECT_GENERIC)와 조사 두 겹을 뗀 어간이 일반명사인 토큰은 뺀다.
- 표기 차이(㈜·(주)·주식회사·Co., Ltd., 띄어쓰기, 라틴 대소문자, 문단 묶음 안 괄호 약칭 정의)는 같은 이름으로 본다.
- 일반 머리명사(부문·설비·소속·법인 표지 등) 앞의 맨 수식어('건설 부문', '냉연강판 설비')도 후보로 본다.

판정(subject_decision)은 a2-v1 SYS 규칙 위에서 ✅만 좁히는 순수 제한이다: 반박 규칙은 숫자 확인만 본 S_V를 그대로 쓴다.
제품 실행기(runner)와 A-3 평가(lab/evidence/a3)가 이 모듈의 같은 함수를 쓴다. lexical.py는 A-2 사전등록 해시에 묶여 있어
고치지 않고 가져다 쓴다.
"""
from __future__ import annotations

import re

from collections.abc import Sequence

from app.services.evidence import lexical
from app.services.evidence.judge import Judgement, sys_decision

# 사업보고서 문장에 흔한 일반명사(회사·부문·제품 이름이 아니다). AI(Claude Code)가 일반 어휘로 초안을 쓰고, A-2 조정 세트
# 자연 주장 문장(탐색 허용, 확인 세트는 보지 않음)에서 후보로 잡힌 낱말 중 고유명이 아닌 것과 독립 리뷰가 짚은 낱말을 더했다.
# 확정 전 탐색(prereg_a3.json exploration)의 허용 범위 안에서만 더하고, 확정 뒤에는 고치지 않는다(코드 해시로 대조).
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
    # 독립 리뷰(2026-10-04)가 짚은 낱말과 같은 꼴의 흔한 명사
    "성과", "절감", "개선", "확대", "축소", "강화", "확보", "효율", "효율성", "공정", "배터리", "음반", "철광석",
)
_GENERIC = frozenset(lexical.GENERIC_NOUNS) | frozenset(SUBJECT_GENERIC)
# 앞의 맨 토큰을 이름 후보로 만드는 머리명사: 회사·부문·제품을 묶는 말만('건설 부문', '냉연강판 설비', '큐브엔터테인먼트 소속').
# 일반명사 전체로 넓히면 '있으며 제품', '따라 매출' 같은 서술어가 후보가 된다(A-2 조정 세트 탐색에서 확인)
_HEADS = frozenset({"부문", "사업부문", "사업부", "사업본부", "본부", "설비", "공장", "법인", "소속", "브랜드", "라인",
                    "계열사", "자회사", "종속회사", "주식", "지분"})
_ADJ_END = ("적",)  # 지속적·추가적·독자적: '-적' 꼴은 이름이 아니다
_QUOTE_MAX = 20  # 따옴표 안 이름 최대 길이(문장 전체 인용은 이름이 아니다)
# 부문명 변형('건설 부문' ↔ '건설사업부문' ↔ '건설사업부'): 이 꼬리로 끝나는 이름은 핵심어 + 각 꼬리를 대안으로 본다
_DIVISION = ("사업부문", "사업본부", "사업부", "부문", "본부", "사업")
# 서술형 판별: 받침 있는 어간 + '다'(늘린다·올렸다; 서술격 '다'는 받침 없는 명사 뒤에만 붙는다), 짧은 동사 어간 + '는'
_COPULA_TAILS = ("이다", "입니다", "이었다", "였다", "이며")
_VERB_STEM_END = tuple("리히기우추키시치지르드오보주내이")
_SHORT_VERB = 3

_LEGAL = re.compile(r"㈜|\(\s*주\s*\)|주식회사|\bCo\.,?\s*Ltd\.?|\bCo\.(?![A-Za-z])|\bLtd\.?(?![A-Za-z])"
                    r"|\bInc\.?(?![A-Za-z])|\bCorp(?:oration|\.)?(?![A-Za-z])", re.I)
_DIGIT = re.compile(r"\d")
_BARE = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z0-9&.\-]*")
# 괄호 약칭 정의: X(Y), X(이하 'Y'), X(이하 "Y"라 한다). 법인 표지는 먼저 지운 본문에서 찾는다
_ALIAS = re.compile(r"([가-힣A-Za-z0-9&.\-]{2,})\s*\(\s*(?:이하\s*)?['‘\"“「]?([^()'’\"”」,]{2,}?)['’\"”」]?"
                    r"\s*(?:(?:이)?라\s*(?:한다|함|합니다))?\s*\)")


def normalize(name: str) -> str:
    """비교용 이름: 법인 표지·영문 법인 접미·공백을 지우고 라틴 글자는 소문자로."""
    return re.sub(r"\s+", "", _LEGAL.sub("", name)).casefold()


def _strip_legal(text: str) -> str:
    return _LEGAL.sub("", text)


def _edge(tok: str) -> str:
    return lexical._EDGE.sub("", tok)


def _is_generic(name: str) -> bool:
    """일반명사이거나, 조사 하나를 더 뗀 어간이 일반명사면 참(부문에서도 → 부문에서 → 부문)."""
    if name in _GENERIC or (re.fullmatch(r"[가-힣]+", name) and name.endswith(_ADJ_END)):
        return True
    stem = lexical._stem(name) if re.fullmatch(r"[가-힣]+", name) else None
    return stem is not None and stem in _GENERIC


def _is_head(raw: str) -> bool:
    """머리명사 토큰(조사 붙어도 됨)이거나 법인 표지만 있는 토큰('주식회사와')."""
    tok = _edge(raw)
    if lexical._CORP_MARK.search(tok):
        rest = _edge(lexical._CORP_MARK.sub("", tok))
        return rest == "" or rest in lexical._TAILS
    stem = lexical._stem(tok) or tok
    return tok in _HEADS or stem in _HEADS or (lexical._stem(stem) or "") in _HEADS


def _bare(raw: str) -> str | None:
    """조사가 붙지 않은 이름꼴 토큰(숫자·서술형·일반명사 아님, 2자 이상). 수식어·띄어 쓴 이름 앞부분 후보."""
    tok = _edge(raw)
    if (len(tok) < 2 or _DIGIT.search(tok) or not _BARE.fullmatch(tok) or lexical._CORP_MARK.search(tok)
            or lexical._stem(tok) or tok.endswith(lexical._VERBAL) or _is_generic(tok)):
        return None
    return tok


def _has_final(ch: str) -> bool:
    """한글 음절에 받침이 있는가."""
    return "가" <= ch <= "힣" and (ord(ch) - 0xAC00) % 28 != 0


def _is_predicate(tok: str) -> bool:
    """서술형 활용 토큰인가(lexical.name_candidates가 어간을 이름으로 잡는 꼴).

    - '다'로 끝나고 서술격 꼬리(이다·입니다·였다…)가 아니며 '다' 앞 음절에 받침이 있다: 늘린다·이른다·올렸다·만든다.
      서술격 '다'는 받침 없는 명사 뒤에만 붙으므로(삼성전자다) 이름을 잃지 않는다.
    - '는'으로 끝나고 어간이 3자 이하이며 동사 어간 끝 음절(리·히·기·르·드 등)로 끝난다: 늘리는·만드는.
    """
    if tok.endswith("다") and not tok.endswith(_COPULA_TAILS) and len(tok) >= 2:
        return _has_final(tok[-2])
    if tok.endswith("는") and 1 < len(tok) - 1 <= _SHORT_VERB:
        return tok[-2] in _VERB_STEM_END
    return False


def _token_name(raw: str) -> str | None:
    """토큰 하나의 이름 후보(lexical.name_candidates 규칙). 일반명사·서술형 활용이면 None."""
    if _is_predicate(_edge(raw)):
        return None
    names = lexical.name_candidates(raw, "")
    names = {n for n in names if not _is_generic(n)}
    return next(iter(names)) if names else None


def _division_alts(name: str) -> tuple[str, ...]:
    """부문명이면 핵심어 + 부문 꼬리 변형을 대안으로 더한다(건설사업부문 → 건설부문·건설사업부…)."""
    for d in _DIVISION:
        core = name[:-len(d)] if name.endswith(d) else ""
        if len(core) >= 2 and not _is_generic(core):
            return (name, *(core + x for x in _DIVISION if core + x != name))
    return (name,)


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


def subject_groups(claim: str, company: str | Sequence[str],
                   alias: dict[str, set[str]] | None = None) -> list[tuple[str, ...]]:
    """주장의 이름 후보 묶음. 묶음 안의 대안 중 하나만 문단에 있으면 그 묶음은 통과다.

    - 토큰 이름(lexical.name_candidates 규칙): 앞 토큰이 맨 이름꼴이면 붙인 형태를 대안으로 더한다('고려 제강' ↔ '고려제강').
    - 머리명사·법인 표지 토큰 앞의 맨 수식어: ('건설', '건설부문'), ('한빛소재',).
    - 따옴표 안 이름.
    - 부문명은 꼬리 변형을 대안으로 더한다('건설 부문' ↔ '건설사업부문').
    선택 회사 자신(회사명·DART 별칭)은 먼저 '당사'로 바꿔 후보에서 빼고, 남은 묶음도 자기 회사면 뺀다.
    """
    pat = _self_pattern(company)
    if pat is not None:
        claim = pat.sub(" 당사", claim)
    toks = claim.split()
    out: list[tuple[str, ...]] = []
    for i, raw in enumerate(toks):
        prev = _bare(toks[i - 1]) if i else None
        name = _token_name(raw)
        if name:
            alts = _division_alts(name)
            out.append(alts + _division_alts(prev + name) if prev else alts)
        elif prev and _is_head(raw):
            head = lexical._stem(_edge(raw)) or _edge(raw)
            plain = not lexical._CORP_MARK.search(raw)
            out.append((prev, *_division_alts(prev + head)) if plain else (prev,))
    out += [(m.strip(),) for m in lexical._QUOTED.findall(claim)
            if len(m.strip()) <= _QUOTE_MAX and not _is_generic(m.strip())]
    seen, groups = set(), []
    for g in out:
        if any(_is_self(n, company, alias) for n in g) or frozenset(g) in seen:
            continue
        seen.add(frozenset(g))
        groups.append(g)
    return groups


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


def missing_subjects(claim: str, passage: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None) -> list[str]:
    """문단에서 찾지 못한 후보 묶음의 대표 이름(감사·리포트용)."""
    alias = alias or {}
    text = _strip_legal(passage)
    return [g[0] for g in subject_groups(claim, company, alias) if not _group_ok(g, text, alias)]


def subject_ok(claim: str, passage: str, company: str | Sequence[str], alias: dict[str, set[str]] | None = None) -> bool:
    """주장의 이름 후보 묶음이 모두 문단에 있으면 참(후보가 없으면 참)."""
    return not missing_subjects(claim, passage, company, alias)


def subject_valid(claim: str, passages: list[str], company: str | Sequence[str]) -> list[bool]:
    """문단별 주체 확인 통과 여부. 괄호 약칭 정의는 문단 묶음 전체에서 모은다(판정 문단 k개와 같은 범위)."""
    alias = aliases(passages)
    return [subject_ok(claim, p, company, alias) for p in passages]


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
