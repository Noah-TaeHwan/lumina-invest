"""팩트체커 1단계 규칙 분류(triage)와 범위 판별(scope) 표 단위 테스트. 외부 호출 없음.

기간 변형(연도·분기·반기·'2Q25'·상대 표현), 회사명 부분 일치 금지, 파생 지표 구분, JEV 1단계 분류(기본 꺼짐·실패 시 전부 검수).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.services.factcheck import scope, triage
from app.services.factcheck.scope import Period

SAMSUNG, HYNIX, SK, SDI = "00126380", "00164779", "00181712", "00126362"
NAMES = {
    SAMSUNG: ["삼성전자", "Samsung Electronics"],
    HYNIX: ["SK하이닉스", "SK hynix"],
    SK: ["SK"],
    SDI: ["삼성SDI"],
    "00000001": ["대상"],  # 일상어와 겹치는 상장사명(이름으로 보지 않는다)
}
AS_OF = Period.parse("2026H1")


def labels(text: str, as_of: str = "2026H1") -> list[str]:
    return [m.period.label for m in scope.extract_periods(text, Period.parse(as_of))]


# ---- 기준 시점(as_of) 해석 ----

@pytest.mark.parametrize("raw, label", [
    ("2026H1", "2026H1"), ("2025Q3", "2025Q3"), ("2025", "2025"),
    ("2026-08-14", "2026Q2"),  # 날짜면 그날 이전에 끝난 마지막 분기
    ("20260401", "2026Q1"), ("2026-03-31", "2026Q1"), ("2026-01-15", "2025Q4"),
])
def test_as_of_parse(raw, label):
    assert Period.parse(raw).label == label


def test_period_dates():
    q2 = Period(2026, "quarter", 2)
    assert (q2.start.isoformat(), q2.end.isoformat()) == ("2026-04-01", "2026-06-30")
    q3c = Period(2025, "quarter", 3, cumulative=True)
    assert (q3c.start.isoformat(), q3c.end.isoformat()) == ("2025-01-01", "2025-09-30")
    h1 = Period(2026, "half", 1)
    assert (h1.start.isoformat(), h1.end.isoformat(), h1.label) == ("2026-01-01", "2026-06-30", "2026H1")
    y = Period(2025, "year")
    assert (y.start.isoformat(), y.end.isoformat(), y.label) == ("2025-01-01", "2025-12-31", "2025")


# ---- 절대 기간 변형 ----

@pytest.mark.parametrize("text, expected", [
    ("2025년 매출은 333.6조원이다.", ["2025"]),
    ("2025년도 영업이익은 43.6조원이다.", ["2025"]),
    ("FY2025 매출은 333.6조원이다.", ["2025"]),
    ("FY25 매출은 333.6조원이다.", ["2025"]),
    ("'25년 매출은 333.6조원이다.", ["2025"]),
    ("’25년 매출은 333.6조원이다.", ["2025"]),
    ("2026년 2분기 매출은 171.5조원이다.", ["2026Q2"]),
    ("2026년 2Q 매출은 171.5조원이다.", ["2026Q2"]),
    ("2Q25 영업이익은 4.7조원이었다.", ["2025Q2"]),
    ("2Q'25 영업이익은 4.7조원이었다.", ["2025Q2"]),
    ("2Q 2025 영업이익은 4.7조원이었다.", ["2025Q2"]),
    ("'26.2Q 매출액은 171.50조원이다.", ["2026Q2"]),
    ("26.2Q 매출액은 171.50조원이다.", ["2026Q2"]),
    ("26년 2분기 매출은 171.5조원이다.", ["2026Q2"]),
    ("2026년 상반기 매출은 305.4조원이다.", ["2026H1"]),
    ("1H26 매출은 305.4조원이다.", ["2026H1"]),
    ("2025년 3분기 누적 매출은 173조원이다.", ["2025Q3"]),
    ("2025년 1~3분기 매출은 173조원이다.", ["2025Q3"]),
    ("2025년 6월 말 자산총계는 500조원이다.", ["2025Q2"]),
    ("2025년 매출 333.6조원, 2024년 매출 300.9조원이다.", ["2025", "2024"]),
])
def test_absolute_periods(text, expected):
    assert labels(text) == expected


def test_cumulative_flag():
    m = scope.extract_periods("2025년 3분기 누적 매출은 173조원이다.", AS_OF)[0]
    assert m.period.cumulative and m.period.start.isoformat() == "2025-01-01"
    m = scope.extract_periods("2025년 1~3분기 매출", AS_OF)[0]
    assert m.period.cumulative
    m = scope.extract_periods("2025년 3분기 매출", AS_OF)[0]
    assert not m.period.cumulative and m.period.start.isoformat() == "2025-07-01"


@pytest.mark.parametrize("text", [
    "매출 2,025억원을 기록했다.",      # 금액 속 숫자는 연도가 아니다
    "직원 수는 2025명이다.",
    "주소는 20251231번지다.",
    "25년 업력의 회사다.",            # 따옴표 없는 두 자리 연도는 분기·반기가 뒤따를 때만
])
def test_not_a_period(text):
    assert labels(text) == []


# ---- 연도 없는 분기·반기와 상대 표현: 기준 시점 as_of로 해석 ----

@pytest.mark.parametrize("text, as_of, expected", [
    ("올해 상반기 매출은 305.4조원이다.", "2026H1", ["2026H1"]),
    ("금년 1분기 매출은 133.9조원이다.", "2026H1", ["2026Q1"]),
    ("작년 매출은 333.6조원이다.", "2026H1", ["2025"]),
    ("지난해 영업이익은 43.6조원이다.", "2026H1", ["2025"]),
    ("재작년 매출은 300.9조원이다.", "2026H1", ["2024"]),
    ("작년 3분기 영업이익은 12.2조원이다.", "2026H1", ["2025Q3"]),
    ("최근 분기 영업이익은 89.5조원이다.", "2026H1", ["2026Q2"]),
    ("이번 분기 매출은 171.5조원이다.", "2026Q1", ["2026Q1"]),
    ("직전 분기 매출은 133.9조원이다.", "2026H1", ["2026Q1"]),
    ("지난 분기 매출은 86.1조원이다.", "2026Q1", ["2025Q4"]),
    ("2분기 매출은 171.5조원이다.", "2026H1", ["2026Q2"]),
    ("3분기 매출은 86.1조원이다.", "2026H1", ["2025Q3"]),   # as_of 뒤의 분기면 한 해 전
    ("상반기 매출은 305.4조원이다.", "2026H1", ["2026H1"]),
    ("2025년 상반기와 하반기 모두 흑자였다.", "2026H1", ["2025H1", "2025H2"]),  # 앞 연도를 잇는다
    ("작년 매출은 2025년 3분기에 가장 많았다.", "2026H1", ["2025", "2025Q3"]),
])
def test_relative_periods(text, as_of, expected):
    assert labels(text, as_of) == expected


@pytest.mark.parametrize("text, expected", [
    ("매출은 전년 대비 130% 증가했다.", []),          # 비교 기준은 주장 기간이 아니다
    ("영업이익은 전년 동기 대비 크게 늘었다.", []),
    ("매출은 전분기 대비 28% 늘었다.", []),
    ("2분기 매출은 전년 동기 대비 130% 증가했다.", ["2026Q2"]),
])
def test_comparison_base_is_not_claim_period(text, expected):
    assert labels(text) == expected


# ---- 리뷰 반영: 비교 기준·전년 동기·축약 누적·YYYYQn 표기·연도 당김 표시 ----

@pytest.mark.parametrize("text, expected", [
    ("2025년 매출은 2024년 대비 10.9% 늘었다.", ["2025"]),       # 절대 기간 + 대비는 비교 기준
    ("매출은 작년 4분기 대비 늘었다.", []),
    ("2025년 매출이 2024년보다 늘었다.", ["2025"]),
    ("2025년 3분기 매출은 2024년 3분기에 비해 늘었다.", ["2025Q3"]),
    ("2025년 3분기 매출은 86.1조원, 전년 동기는 79.1조원이다.", ["2025Q3", "2024Q3"]),  # 문장 속 절대 기간 기준
    ("전년 동기 매출은 74.6조원이다.", ["2025Q2"]),                # 절대 기간이 없으면 as_of 기준
    ("2026Q2 매출은 171.5조원이다.", ["2026Q2"]),
    ("2026H1 매출은 305.4조원이다.", ["2026H1"]),
    ("2025 Q3 매출은 86.1조원이다.", ["2025Q3"]),
])
def test_review_periods(text, expected):
    assert labels(text) == expected


@pytest.mark.parametrize("text", ["3Q25 누적 매출은 239.8조원이다.", "'25.3Q 누적 매출은 239.8조원이다.",
                                  "2025Q3 누적 매출은 239.8조원이다.", "25.3Q 누계 매출"])
def test_abbreviated_cumulative(text):
    (m,) = scope.extract_periods(text, AS_OF)
    assert m.period.label == "2025Q3" and m.period.cumulative


def test_shifted_bare_quarter_marked():
    assert scope.extract_periods("3분기 매출", AS_OF)[0].shifted is True   # 2026Q3 → 2025Q3로 당김
    assert scope.extract_periods("2분기 매출", AS_OF)[0].shifted is False
    assert scope.extract_periods("2025년 3분기 매출", AS_OF)[0].shifted is False


def test_generic_word_company_names_excluded():
    idx = scope.CompanyIndex({"00000002": ["콘텐츠"], SAMSUNG: ["삼성전자"]})
    assert scope.company_mentions("콘텐츠 매출이 늘었다.", idx) == []
    assert scope.company_mentions("삼성전자 매출이 늘었다.", idx) == [(SAMSUNG, "삼성전자")]


def test_relative_marked():
    ms = scope.extract_periods("작년 매출은 333.6조원이다.", AS_OF)
    assert ms[0].relative is True
    ms = scope.extract_periods("2025년 매출은 333.6조원이다.", AS_OF)
    assert ms[0].relative is False


# ---- 검색 기간 범위: 주장 기간 ~ 2년 뒤 보고서(비교값 포함), 기간 불명이면 전체 ----

def test_search_periods_window():
    got = scope.search_periods([Period(2025, "quarter", 2)])
    assert "2025Q2" in got and "2025H1" in got and "2025" in got and "2026H1" in got and "2027Q2" in got
    assert "2025Q1" not in got and "2024" not in got and "2027Q3" not in got


def test_search_periods_unknown_is_none():
    assert scope.search_periods([]) is None


# ---- 다른 회사가 주어: 상장사명 사전 주입, 부분 일치 금지 ----

@pytest.mark.parametrize("text, corp, other", [
    ("SK하이닉스는 2분기 매출 79.3조원을 기록했다.", HYNIX, None),
    ("SK는 2분기 매출 79.3조원을 기록했다.", HYNIX, "SK"),            # 'SK'는 'SK하이닉스'가 아니다
    ("SK하이닉스는 HBM 1위다.", SK, "SK하이닉스"),                    # 'SK'가 'SK하이닉스' 안에 걸리지 않는다
    ("삼성SDI는 배터리 매출이 늘었다.", SAMSUNG, "삼성SDI"),           # '삼성'으로 시작해도 다른 회사
    ("삼성전자는 SK하이닉스보다 매출이 많다.", SAMSUNG, None),        # 주어는 선택 회사, 비교 대상은 괜찮다
    ("SK하이닉스는 삼성전자 대비 HBM 점유율이 높다.", SAMSUNG, "SK하이닉스"),
    ("삼성전자와 SK하이닉스는 모두 흑자다.", SAMSUNG, None),          # 선택 회사가 주어에 있으면 검수
    ("Samsung Electronics는 2분기 매출 171.5조원을 기록했다.", SAMSUNG, None),
    ("SK hynix의 2분기 매출은 79.3조원이다.", SAMSUNG, "SK hynix"),
    ("고객을 대상으로 한 매출이 늘었다.", SAMSUNG, None),              # 일상어와 겹치는 상장사명
    ("회사는 2분기 매출 171.5조원을 기록했다.", SAMSUNG, None),
])
def test_other_company(text, corp, other):
    assert scope.other_company(text, corp, NAMES) == other


def test_company_mentions_exact_tokens():
    got = scope.company_mentions("SK하이닉스와 SK의 실적", NAMES)
    assert [c for c, _ in got] == [HYNIX, SK]
    assert scope.company_mentions("SKC와 삼성의 실적", NAMES) == []  # 사전에 없는 이름·접두는 걸리지 않는다


# ---- 파생 지표: 잠정실적 증감률로 대조 가능한 것 / 범위 밖 ----

@pytest.mark.parametrize("text, kind", [
    ("2분기 매출은 전년 동기 대비 130% 증가했다.", "growth"),
    ("영업이익은 전분기 대비 56.4% 늘었다.", "growth"),
    ("매출 YoY 98.7% 성장.", "growth"),
    ("영업이익률은 52.2%다.", "margin"),
    ("HBM 매출 비중은 40%다.", "other"),
    ("부채비율은 31%다.", "other"),
    ("영업이익률이 3%p 개선됐다.", "other"),
    ("2분기 매출은 171.5조원이다.", None),
])
def test_derived_kind(text, kind):
    assert scope.derived_kind(text) == kind


@pytest.mark.parametrize("text, category, reason, report_types", [
    ("2026년 2분기 매출은 171.5조원이다.", "checked", None, None),
    ("2분기 매출은 전년 동기 대비 130% 증가했다.", "checked", "derived:growth", ["preliminary"]),
    ("2분기 영업이익은 전분기 대비 56.4% 늘었다.", "checked", "derived:growth", ["preliminary"]),
    ("2분기 순이익은 전년 동기 대비 50% 늘었다.", "derived", "derived:growth_unsupported", None),
    ("HBM 매출 비중은 40%다.", "derived", "derived:other", None),
    ("2분기 영업이익률은 52.2%다.", "checked", "derived:margin", None),
    ("SK는 2분기 매출 79.3조원을 기록했다.", "other_company", "other_company:SK", None),
    ("2027년 매출은 400조원이다.", "out_of_scope", "future_period", None),
    ("올해 매출은 350조원이다.", "out_of_scope", "future_period", None),  # 2026 연간은 as_of(2026H1) 뒤
    # 전망 표지는 기간 검사보다 먼저 본다(검수 안 함)
    ("2027년 매출은 400조원으로 예상된다.", "out_of_scope", "forecast", None),
    ("올해 매출은 350조원을 넘을 것이다.", "out_of_scope", "forecast", None),
    ("3분기 영업이익은 10조원으로 예상된다.", "out_of_scope", "forecast", None),
    ("2분기 영업이익 목표는 10조원이다.", "out_of_scope", "forecast", None),
    ("목표주가 12만원을 제시한다.", "out_of_scope", "market", None),
    ("매출은 350조원으로 예상된다.", "out_of_scope", "forecast", None),
    ("2025년 매출은 333조원으로 추정된다.", "out_of_scope", "forecast", None),
])
def test_assess(text, category, reason, report_types):
    s = scope.assess(text, SAMSUNG, as_of=AS_OF, names=NAMES)
    assert (s.category, s.reason, s.report_types) == (category, reason, report_types)


def test_assess_search_periods_from_claim_period():
    s = scope.assess("2026년 2분기 매출은 171.5조원이다.", SAMSUNG, as_of=AS_OF, names=NAMES)
    assert "2026Q2" in s.search_periods and "2026H1" in s.search_periods and "2025" not in s.search_periods
    s = scope.assess("매출은 171.5조원이다.", SAMSUNG, as_of=AS_OF, names=NAMES)
    assert s.search_periods is None  # 기간 불명이면 전체


# ---- 1단계 규칙 분류 ----

@pytest.mark.parametrize("text, check, category, reason", [
    ("2분기 매출은 171.5조원이다.", True, "checked", "rule:number"),
    ("삼성전자는 HBM 사업을 확대하고 있다.", True, "checked", "rule:company"),
    ("SK하이닉스도 HBM을 늘렸다.", True, "checked", "rule:company"),
    ("작년에 파운드리 사업을 분사했다.", True, "checked", "rule:period"),
    ("HBM 시장은 더 커질 것으로 보인다.", False, "opinion", "opinion"),
    ("주가는 반등할 여지가 크다.", False, "out_of_scope", "market"),
    ("메모리 업황이 좋아질 것이라 판단한다.", False, "opinion", "opinion"),
    ("회사는 HBM 사업을 확대하고 있다.", False, "opinion", "no_fact_marker"),
    ("왜 이렇게 됐을까?", False, "opinion", "not_claim:question"),
    ("주요 내용은 다음과 같습니다.", False, "opinion", "not_claim:lead"),
])
def test_rule_triage(text, check, category, reason):
    t = triage.rule_triage(text, corp_code=SAMSUNG, names=NAMES, as_of=AS_OF)
    assert (t.check, t.category, t.reason) == (check, category, reason)


# ---- JEV 1단계 분류(함수만, 기본 꺼짐) ----

@dataclass
class _R:
    ok: bool
    answers: dict | None
    key: str = "k"
    attempts: int = 1
    input_tokens: int = 100
    cached: bool = False
    error_code: str | None = None
    http_status: int | None = 200


class FakeTriageJev:
    """질문마다 정해 둔 분포를 돌려준다. ok=False면 실패 응답."""

    def __init__(self, dist: dict[int, dict] | None = None, ok: bool = True):
        self.dist, self.ok, self.calls = dist or {}, ok, []

    async def ask(self, state, questions, *, user_id, log_ctx=None, usage=None):
        self.calls.append((state, questions))
        if not self.ok:
            return _R(False, None, error_code="timeout", http_status=None)
        out = {}
        for qid in questions:
            j = int(qid[1:])
            out[qid] = self.dist.get(j, {"fact": 0.9, "opinion": 0.05, "out_of_scope": 0.03, "other_company": 0.02})
        return _R(True, out)


SENTS = ["2분기 매출은 171.5조원이다.", "회사는 HBM 사업을 확대하고 있다.", "메모리 업황이 좋아질 것이라 판단한다."]


def _run_triage(**kw):
    return asyncio.run(triage.triage_all(SENTS, corp_code=SAMSUNG, company="삼성전자", names=NAMES,
                                         as_of=AS_OF, **kw))


def test_jev_triage_off_by_default():
    assert triage.JEV_TRIAGE_ENABLED is False
    client = FakeTriageJev()
    got = _run_triage(client=client)
    assert client.calls == []  # 기본은 규칙만
    assert [t.check for t in got] == [True, False, False]


def test_jev_triage_one_bundled_request_for_rule_skipped_only():
    client = FakeTriageJev({1: {"fact": 0.9, "opinion": 0.05, "out_of_scope": 0.03, "other_company": 0.02},
                            2: {"fact": 0.02, "opinion": 0.95, "out_of_scope": 0.02, "other_company": 0.01}})
    got = _run_triage(client=client, enabled=True)
    assert len(client.calls) == 1  # 한 번의 묶음 요청
    state, questions = client.calls[0]
    assert len(questions) == 2  # 규칙에서 빠진 두 문장만
    assert "171.5" not in state
    assert [(t.check, t.category, t.reason) for t in got] == [
        (True, "checked", "rule:number"), (True, "checked", "jev:fact"), (False, "opinion", "jev:opinion")]


def test_jev_triage_ambiguous_goes_to_check():
    # '검수 안 함' 확률이 기준값보다 낮으면 검수(재현율 우선)
    client = FakeTriageJev({2: {"fact": 0.3, "opinion": 0.6, "out_of_scope": 0.05, "other_company": 0.05}})
    got = _run_triage(client=client, enabled=True)
    assert got[2].check is True and got[2].reason == "jev:uncertain"


def test_jev_triage_failure_checks_everything():
    got = _run_triage(client=FakeTriageJev(ok=False), enabled=True)
    assert [t.check for t in got] == [True, True, True]
    assert got[1].reason == got[2].reason == "jev_failed:timeout"


def test_not_claim_never_sent_to_jev():
    client = FakeTriageJev()
    got = asyncio.run(triage.triage_all(["왜 이렇게 됐을까?"], corp_code=SAMSUNG, company="삼성전자", names=NAMES,
                                        as_of=AS_OF, client=client, enabled=True))
    assert client.calls == [] and got[0].reason == "not_claim:question"


def test_list_lead_skipped_in_rule_stage_not_sent_to_jev():
    # 목록 머리말은 규칙 단계에서 빠진다: JEV 분류 요청에 넣지 않는다(분류 호출 0회 몫)
    lead = "주요 수치를 정리하면 아래 표와 같다."
    assert triage.rule_triage(lead, corp_code=SAMSUNG, names=NAMES, as_of=AS_OF) == \
        triage.Triage(False, "opinion", "not_claim:lead")
    client = FakeTriageJev()
    got = asyncio.run(triage.triage_all([lead, "회사는 HBM 사업을 확대하고 있다."], corp_code=SAMSUNG,
                                        company="삼성전자", names=NAMES, as_of=AS_OF, client=client, enabled=True))
    assert len(client.calls) == 1
    state, questions = client.calls[0]
    assert len(questions) == 1 and "아래 표" not in state
    assert (got[0].check, got[0].category, got[0].reason) == (False, "opinion", "not_claim:lead")
    only_lead = FakeTriageJev()
    asyncio.run(triage.triage_all([lead], corp_code=SAMSUNG, company="삼성전자", names=NAMES, as_of=AS_OF,
                                  client=only_lead, enabled=True))
    assert only_lead.calls == []


# ---- PR #56 후속(2·3·4번) ----

def test_invalid_half_does_not_crash():
    assert labels("2026H3 매출은 10조원이다.") == []
    with pytest.raises(ValueError):
        Period(2026, "half", 3)
    with pytest.raises(ValueError):
        Period(2026, "quarter", 5)
    s = scope.assess("2026H3 매출은 10조원이다.", SAMSUNG, as_of=AS_OF, names=NAMES)
    assert s.category == "checked"


@pytest.mark.parametrize("text, expected", [
    ("2026Q2의 매출은 171.5조원이다.", ["2026Q2"]),
    ("2026H1의 매출은 305.4조원이다.", ["2026H1"]),
    ("2Q의 매출은 171.5조원이다.", ["2026Q2"]),
    ("2Q25의 영업이익은 4.7조원이다.", ["2025Q2"]),
])
def test_period_followed_by_particle(text, expected):
    assert labels(text) == expected


def test_period_spans_include_comparison_bases():
    text = "2025년 매출은 2024년 대비 10.9% 늘었다."
    s = scope.assess(text, SAMSUNG, as_of=AS_OF, names=NAMES)
    assert [m.period.label for m in s.mentions] == ["2025"]   # 주장 기간
    spans = [text[a:b] for a, b in s.period_spans]             # 숫자 제거용 구간(비교 기준 포함)
    assert "2025년" in spans and "2024년" in spans


# ---- 실데이터 스모크 결함 2: 상대 기간 표현만 있는 문장(교차 검수 반영) ----
# ambiguous ⇔ 해석된 기간이 하나도 없고 상대 기간 표현('같은 분기' 등)이 있다

@pytest.mark.parametrize("text, ambiguous", [
    ("같은 분기 연결 영업이익은 89.5조원이다.", True),
    ("같은 기간 매출은 74.6조원이다.", True),
    ("해당 분기 매출은 74.6조원이다.", True),
    ("동분기 영업이익은 4.7조원이다.", True),
    ("동 기간 영업이익은 4.7조원이다.", True),
    ("같은 해 매출은 333.6조원이다.", True),
    ("해당 반기 매출은 150조원이다.", True),
    ("같은 회계연도 매출은 333.6조원이다.", True),
    ("당분기 영업이익은 89.5조원이다.", False),    # as_of로 해석된다(2026Q2) → ambiguous 아님
    ("이번 분기 매출은 171.5조원이다.", False),
    ("2분기 영업이익은 4.7조원이고 같은 분기 매출은 74.6조원이다.", False),   # 연도 없는 '2분기'도 해석된 기간
    ("2025년 2분기 영업이익은 전년 동기 대비 줄었다.", False),
    ("2025년 2분기 매출은 74.6조원, 같은 분기 영업이익은 4.7조원이다.", False),
    ("전년 동기 매출은 74.6조원이다.", False),
    ("전년  동기 매출은 74.6조원이다.", False),
    ("동기 영업이익은 4.7조원이다.", False),       # 맨 '동기'는 목록에서 뺐다
    ("비동기 처리로 매출 10조원을 올렸다.", False),
    ("동기 부여로 영업이익이 4.7조원 늘었다.", False),
    ("동기화 설비 매출은 1조원이다.", False),
    # 한글 경계: 다른 낱말 속 '동 기간·같은 해·동 분기'는 상대 기간이 아니다
    ("가동 기간 동안 매출은 10조원이다.", False),
    ("활동 기간 영업이익은 4.7조원이다.", False),
    ("같은 해외 법인 매출은 10조원이다.", False),
    ("변동 분기 매출은 1조원이다.", False),
    ("이해당 분기 매출은 1조원이다.", False),
    ("같은 해에 매출은 333.6조원이다.", True),
    ("같은 해의 영업이익은 43.6조원이다.", True),
])
def test_relative_only_period_is_ambiguous(text, ambiguous):
    assert scope.assess(text, SAMSUNG, as_of=AS_OF, names=NAMES).ambiguous_period is ambiguous
    assert scope.period_scope(text, AS_OF).ambiguous_period is ambiguous


def test_last_year_same_quarter_spacing_independent():
    a = scope.extract_periods("전년 동기 매출은 74.6조원이다.", AS_OF)
    b = scope.extract_periods("전년  동기 매출은 74.6조원이다.", AS_OF)
    c = scope.extract_periods("전년동기 매출은 74.6조원이다.", AS_OF)
    assert [m.period.label for m in a] == [m.period.label for m in b] == [m.period.label for m in c] == ["2025Q2"]


# ---- 앞 문장 기간 상속(scope 쪽): inherited 기간을 주면 상대 기간 표현 자리에 그 기간을 둔다 ----

def test_assess_with_inherited_period():
    text = "같은 분기 연결 영업이익은 89.5조원이다."
    q2 = Period(2025, "quarter", 2)
    s = scope.assess(text, SAMSUNG, as_of=AS_OF, names=NAMES, inherited=q2)
    assert (s.category, s.ambiguous_period) == ("checked", False)
    assert [m.period.label for m in s.mentions] == ["2025Q2"]
    assert text[s.mentions[0].start:s.mentions[0].end] == "같은 분기"
    assert "2025Q2" in s.search_periods and (0, 5) in s.period_spans
    # 해석된 기간이 있는 문장에는 상속하지 않는다
    s = scope.assess("2026년 2분기 영업이익은 89.5조원이다.", SAMSUNG, as_of=AS_OF, names=NAMES, inherited=q2)
    assert [m.period.label for m in s.mentions] == ["2026Q2"]
    p = scope.period_scope(text, AS_OF, inherited=q2)
    assert [m.period.label for m in p.mentions] == ["2025Q2"] and not p.ambiguous_period


@pytest.mark.parametrize("text, units", [
    ("같은 분기 매출", {"quarter"}), ("당분기 매출", {"quarter"}), ("동 분기 매출", {"quarter"}),
    ("해당 분기 매출", {"quarter"}), ("같은 반기 매출", {"half"}), ("해당 반기 매출", {"half"}),
    ("같은 해 매출", {"year"}), ("같은 연도 매출", {"year"}), ("같은 회계연도 매출", {"year"}),
    ("같은 기간 매출", {"any"}), ("동 기간 매출", {"any"}), ("해당 기간 매출", {"any"}),
])
def test_relative_units(text, units):
    assert scope.relative_units(text) == units
