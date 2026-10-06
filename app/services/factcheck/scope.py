"""팩트체커 범위 판별(설계 D3·D5·D6, Codex #7·#11): 주장 기간 해석, 다른 회사 주어, 파생 지표, 범위 밖 표현.

- 기간 3종 중 **주장 기간**을 문장에서 뽑는다(연도·분기·반기·누적, '2Q25'·'26.2Q'·'1H26'·'FY25'·''25년' 변형).
  상대 표현('올해·작년·재작년·최근 분기·직전 분기')과 연도 없는 분기·반기는 검수 실행일이 아니라 **기준 시점 as_of**로
  해석한다. '전년 대비·전분기 대비' 같은 비교 기준은 주장 기간이 아니다. 해석 못 하면 기간 불명(빈 목록).
- 검색 범위 = 주장 기간 끝 ~ 2년 뒤에 끝나는 보고서 기간(뒤 보고서의 비교값 포함, Codex #7). 기간 불명이면 전체(None).
- 다른 회사: 주입한 상장사명 사전(corp_code → 이름들)과 토큰 단위로 **정확히** 맞춘다(끝 조사만 뗀다, 부분 일치 금지:
  'SK' ≠ 'SK하이닉스'). 주어 자리(첫 은·는·이·가 토큰까지)에 선택 회사가 없고 다른 상장사가 있으면 범위 밖.
- 파생 지표: 매출·영업이익 증감률은 잠정실적 공시(전기·전년 동기 대비 증감율이 공시 안에 있다)로 대조하고,
  영업이익률은 XBRL 두 값으로 계산해 대조한다(xbrl_check). 그 밖의 %(비중·비율·%p)는 범위 밖(파생 지표).
"""
from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date

from app.services.evidence.subject import COMMON_WORD_NAMES, normalize

HORIZON_YEARS = 2  # 주장 기간 뒤 몇 년 안의 보고서까지 검색하나(비교값)
PRELIM_GROWTH_ACCOUNTS = re.compile(r"매출|영업\s*이익(?!\s*률)")  # 잠정실적 공시에 증감율이 있는 계정


@dataclass(frozen=True)
class Period:
    """주장·문서의 기간. kind: year / quarter / half. cumulative는 분기 누적(1월 1일부터)일 때만 True."""

    year: int
    kind: str
    n: int = 0
    cumulative: bool = False

    @property
    def label(self) -> str:
        """데이터 계약의 기간 표기: 'YYYY' / 'YYYYQn' / 'YYYYH1'(하반기는 'YYYYH2')."""
        if self.kind == "year":
            return f"{self.year}"
        return f"{self.year}{'Q' if self.kind == 'quarter' else 'H'}{self.n}"

    @property
    def last_quarter(self) -> int:
        """이 기간이 끝나는 분기(연간 4, 상반기 2)."""
        return {"year": 4, "half": 2 * self.n, "quarter": self.n}[self.kind]

    @property
    def start(self) -> date:
        if self.kind == "year" or self.cumulative or (self.kind == "half" and self.n == 1):
            return date(self.year, 1, 1)
        if self.kind == "half":
            return date(self.year, 7, 1)
        return date(self.year, 3 * self.n - 2, 1)

    @property
    def end(self) -> date:
        m = 3 * self.last_quarter
        return date(self.year, m, calendar.monthrange(self.year, m)[1])

    @classmethod
    def quarter_of(cls, year: int, q: int) -> "Period":
        """분기 단독 기간. q가 0 이하·5 이상이면 앞뒤 해로 넘긴다."""
        year, q = year + (q - 1) // 4, (q - 1) % 4 + 1
        return cls(year, "quarter", q)

    @classmethod
    def parse(cls, raw: str) -> "Period":
        """'2026H1'·'2026Q2'·'2025' 또는 날짜('2026-08-14'·'20260814' → 그날까지 끝난 마지막 분기)."""
        s = raw.strip()
        if m := re.fullmatch(r"(\d{4})", s):
            return cls(int(m.group(1)), "year")
        if m := re.fullmatch(r"(\d{4})Q([1-4])", s):
            return cls(int(m.group(1)), "quarter", int(m.group(2)))
        if m := re.fullmatch(r"(\d{4})H([12])", s):
            return cls(int(m.group(1)), "half", int(m.group(2)))
        if m := re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", s):
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            p = cls.quarter_of(d.year, (d.month - 1) // 3 + 1)
            return p if p.end <= d else cls.quarter_of(p.year, p.n - 1)
        raise ValueError(f"기간 표기를 알 수 없다: {raw!r}")


@dataclass(frozen=True)
class PeriodMention:
    """문장 안 기간 표현 하나. start·end는 문장 안 글자 위치, relative는 as_of로 해석했는지."""

    start: int
    end: int
    period: Period
    relative: bool = False


# ---- 기간 표현 ----

_Y4 = r"(?:19|20)\d{2}"
_APOS = r"['‘’]"
# 비교 기준(주장 기간이 아니다): '전년 대비', '전년 동기 대비', '전분기 대비', '작년보다'
_COMPARE = re.compile(r"(?:전년\s*동기|작년\s*동기|전년도|전년|작년|지난해|직전\s*분기|전\s*분기|지난\s*분기|전기)"
                      r"\s*(?:대비|比|보다|에\s*비해|와\s*비교)")
# 연도 뒤 하위 기간: 1~3분기(누적) / n분기·nQ / 상·하반기 / nH / n월(분기 말 월만 분기로)
_SUB = (r"(?:\s*(?:(?P<c1>1)\s*[~∼\-]\s*(?P<c2>[2-4])\s*분기|(?P<q>[1-4])\s*(?:분기|Q(?![A-Za-z]))"
        r"|(?P<hk>[상하])반기|(?P<h>[12])H(?![A-Za-z])|(?P<mon>1[0-2]|0?[1-9])\s*월))?"
        r"(?P<cum>\s*(?:누적|누계))?")
_YEAR_ABS = re.compile(
    rf"(?:FY\s*{_APOS}?(?P<fy>{_Y4}|\d{{2}})(?!\d)"
    rf"|(?<![\d,.])(?P<y4>{_Y4})(?:\s*(?:년도|년|회계연도|사업연도)|(?=\s*[1-4]Q|\s*[12]H))"
    rf"|{_APOS}(?P<y2>\d{{2}})\s*(?:년도|년)"
    rf"|(?<![\d,.'‘’])(?P<y2b>\d{{2}})년(?=\s*(?:[1-4]\s*분기|[1-4]Q|[상하]반기|[12]H|1\s*[~∼\-])))" + _SUB)
_QYY = re.compile(rf"(?<![\w.])(?P<q>[1-4])Q(?:{_APOS}?(?P<yy>\d{{2}})|\s*(?P<yyyy>{_Y4}))(?![\d.])")
_HYY = re.compile(rf"(?<![\w.])(?P<h>[12])H(?:{_APOS}?(?P<yy>\d{{2}})|\s*(?P<yyyy>{_Y4}))(?![\d.])")
_YYDOT = re.compile(rf"(?<![\d.]){_APOS}?(?P<yy>{_Y4}|\d{{2}})\.\s*(?:(?P<q>[1-4])Q|(?P<h>[12])H)(?![A-Za-z])")
_YEAR_REL = re.compile(r"(?P<rel>올해|금년|당해\s*연도|이번\s*해|재작년|지지난해|작년|지난해|전년도|전년)(?!\s*동기)" + _SUB)
_SAME_Q_LAST_YEAR = re.compile(r"(?:전년|작년)\s*동기")
_QUARTER_REL = re.compile(r"(?P<rel>이번\s*분기|최근\s*분기|당\s*분기|직전\s*분기|전\s*분기|지난\s*분기)")
_BARE = re.compile(r"(?:(?P<c1>1)\s*[~∼\-]\s*(?P<c2>[2-4])\s*분기|(?<![\d.])(?P<q>[1-4])\s*분기"
                   r"|(?<![\w.])(?P<qq>[1-4])Q(?![\w'‘’])|(?P<hk>[상하])반기|(?<![\w.])(?P<h>[12])H(?![\w'‘’]))"
                   r"(?P<cum>\s*(?:누적|누계))?")
_REL_YEAR_DELTA = {"올해": 0, "금년": 0, "당해연도": 0, "이번해": 0, "작년": -1, "지난해": -1, "전년도": -1, "전년": -1,
                   "재작년": -2, "지지난해": -2}


def _yy(s: str) -> int:
    return int(s) if len(s) == 4 else 2000 + int(s)


def _with_sub(year: int, m: re.Match) -> Period:
    """연도에 _SUB 그룹(분기·반기·월·누적)을 붙인다."""
    g = m.groupdict()
    cum = bool(g.get("cum"))
    if g.get("c2"):
        return Period(year, "quarter", int(g["c2"]), cumulative=True)
    if g.get("q"):
        return Period(year, "quarter", int(g["q"]), cumulative=cum and g["q"] != "1")
    if g.get("hk") or g.get("h"):
        return Period(year, "half", 1 if g.get("hk") == "상" or g.get("h") == "1" else 2)
    if g.get("mon") and int(g["mon"]) % 3 == 0:  # 분기 말 월('6월 말')만 분기로 본다
        return Period(year, "quarter", int(g["mon"]) // 3)
    return Period(year, "year")


def extract_periods(text: str, as_of: Period) -> list[PeriodMention]:
    """문장 속 주장 기간을 위치 순서로 돌려준다. 비교 기준('전년 대비')은 빼고, 상대·연도 없는 표현은 as_of로 푼다."""
    taken: list[tuple[int, int]] = [(m.start(), m.end()) for m in _COMPARE.finditer(text)]
    out: list[PeriodMention] = []

    def free(m: re.Match) -> bool:
        return not any(m.start() < e and s < m.end() for s, e in taken)

    def add(m: re.Match, p: Period, relative: bool = False) -> None:
        taken.append((m.start(), m.end()))
        out.append(PeriodMention(m.start(), m.end(), p, relative))

    for m in _QYY.finditer(text):
        if free(m):
            add(m, Period(_yy(m["yy"] or m["yyyy"]), "quarter", int(m["q"])))
    for m in _HYY.finditer(text):
        if free(m):
            add(m, Period(_yy(m["yy"] or m["yyyy"]), "half", int(m["h"])))
    for m in _YYDOT.finditer(text):
        if free(m):
            y = _yy(m["yy"])
            add(m, Period(y, "quarter", int(m["q"])) if m["q"] else Period(y, "half", int(m["h"])))
    for m in _YEAR_ABS.finditer(text):
        if free(m):
            add(m, _with_sub(_yy(m["fy"] or m["y4"] or m["y2"] or m["y2b"]), m))
    last_q = Period.quarter_of(as_of.year, as_of.last_quarter)
    for m in _SAME_Q_LAST_YEAR.finditer(text):
        if free(m):
            add(m, Period(as_of.year - 1, "quarter", last_q.n), True)
    for m in _YEAR_REL.finditer(text):
        if free(m):
            add(m, _with_sub(as_of.year + _REL_YEAR_DELTA[re.sub(r"\s+", "", m["rel"])], m), True)
    for m in _QUARTER_REL.finditer(text):
        if free(m):
            prev = re.sub(r"\s+", "", m["rel"]) in ("직전분기", "전분기", "지난분기")
            add(m, Period.quarter_of(last_q.year, last_q.n - 1) if prev else last_q, True)
    for m in _BARE.finditer(text):
        if not free(m):
            continue
        before = [x for x in out if x.start < m.start()]
        year = max(before, key=lambda x: x.start).period.year if before else as_of.year
        g = m.groupdict()
        if g["c2"]:
            p = Period(year, "quarter", int(g["c2"]), cumulative=True)
        elif g["q"] or g["qq"]:
            q = int(g["q"] or g["qq"])
            p = Period(year, "quarter", q, cumulative=bool(g["cum"]) and q != 1)
        else:
            p = Period(year, "half", 1 if g["hk"] == "상" or g["h"] == "1" else 2)
        if not before and p.end > as_of.end:  # 연도 없는 분기가 기준 시점 뒤면 가장 최근의 그 분기(한 해 전)
            p = Period(p.year - 1, p.kind, p.n, p.cumulative)
        add(m, p, relative=not before)
    return sorted(out, key=lambda x: x.start)


def _all_labels(year: int) -> list[Period]:
    return [Period(year, "quarter", q) for q in range(1, 5)] + [Period(year, "half", 1), Period(year, "year")]


def search_periods(periods: Iterable[Period], horizon_years: int = HORIZON_YEARS) -> list[str] | None:
    """검색할 문서 기간 표기 목록: 주장 기간 끝 ~ horizon_years 뒤 사이에 끝나는 보고서 기간. 기간이 없으면 None(전체)."""
    ps = list(periods)
    if not ps:
        return None
    hit: dict[str, date] = {}
    for p in ps:
        lo = p.end
        hi = date(lo.year + horizon_years, lo.month, calendar.monthrange(lo.year + horizon_years, lo.month)[1])
        for y in range(lo.year, hi.year + 1):
            for c in _all_labels(y):
                if lo <= c.end <= hi:
                    hit[c.label] = c.end
    return sorted(hit, key=lambda k: (hit[k], k))


# ---- 회사명 ----

# 이름 뒤에 붙는 조사(긴 것부터). 조사만 떼고 정확히 맞춘다(접두·부분 일치 없음)
_PARTICLES = tuple(sorted((
    "으로서", "으로써", "에서는", "에서도", "에게서", "으로는", "이라는", "보다는", "와는", "과는", "에는", "라는",
    "에서", "에게", "으로", "까지", "부터", "보다", "처럼", "마저", "조차", "이나", "이며", "이고", "대비",
    "은", "는", "이", "가", "을", "를", "의", "에", "도", "와", "과", "로", "만", "나"), key=len, reverse=True))
_TOKEN = re.compile(r"[^\s,·/()\[\]\"“”‘’'「」]+")
_EDGE = re.compile(r"[.!?。:;…]+$")
_SUBJECT_END = ("은", "는", "이", "가")
_NGRAM = 3


class CompanyIndex:
    """상장사명 사전(corp_code → 이름들)을 비교용 표기로 색인한다. 일상어와 겹치는 이름('대상')·1자 이름은 뺀다."""

    def __init__(self, names: Mapping[str, Iterable[str]]):
        self.names = {c: list(ns) for c, ns in names.items()}
        self._idx: dict[str, tuple[str, str]] = {}
        common = {normalize(n) for n in COMMON_WORD_NAMES}
        for corp, ns in self.names.items():
            for n in ns:
                k = normalize(n)
                if len(k) >= 2 and k not in common:
                    self._idx.setdefault(k, (corp, n))

    def display(self, corp_code: str) -> str:
        """화면·JEV state용 회사 이름(사전의 첫 이름, 없으면 corp_code)."""
        ns = self.names.get(corp_code) or []
        return ns[0] if ns else corp_code

    def lookup(self, surface: str) -> tuple[str, str] | None:
        """토큰 표기(끝 조사 포함 가능)를 정확히 맞춘다. (corp_code, 사전 이름) 또는 None."""
        s = _EDGE.sub("", surface)
        for cand in [s] + [s[:-len(p)] for p in _PARTICLES if s.endswith(p) and len(s) > len(p)]:
            hit = self._idx.get(normalize(cand))
            if hit:
                return hit
        return None

    def mentions(self, text: str) -> list[tuple[str, str, int, int]]:
        """문장 속 상장사 언급 (corp_code, 사전 이름, 시작, 끝). 연속 토큰 n개(≤3)까지 묶어 긴 이름부터 맞춘다."""
        toks = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]
        out, i = [], 0
        while i < len(toks):
            for n in range(min(_NGRAM, len(toks) - i), 0, -1):
                hit = self.lookup(" ".join(t for t, _, _ in toks[i:i + n]))
                if hit:
                    out.append((hit[0], hit[1], toks[i][1], toks[i + n - 1][2]))
                    i += n
                    break
            else:
                i += 1
        return out


def _as_index(names: Mapping[str, Iterable[str]] | CompanyIndex) -> CompanyIndex:
    return names if isinstance(names, CompanyIndex) else CompanyIndex(names)


def company_mentions(text: str, names: Mapping[str, Iterable[str]] | CompanyIndex) -> list[tuple[str, str]]:
    """문장 속 상장사 언급 (corp_code, 사전 이름) 목록(나온 순서)."""
    return [(c, n) for c, n, _, _ in _as_index(names).mentions(text)]


def _subject_end(text: str) -> int:
    """주어 자리 끝 위치: 첫 은·는·이·가로 끝나는 토큰의 끝. 없으면 문장 끝."""
    for m in _TOKEN.finditer(text):
        if _EDGE.sub("", m.group(0)).endswith(_SUBJECT_END):
            return m.end()
    return len(text)


def other_company(text: str, corp_code: str, names: Mapping[str, Iterable[str]] | CompanyIndex) -> str | None:
    """주어 자리에 선택 회사가 없고 다른 상장사가 있으면 그 이름. 선택 회사가 함께 있으면 None(검수, 재현율 우선)."""
    end = _subject_end(text)
    found = [(c, n) for c, n, s, _ in _as_index(names).mentions(text) if s < end]
    if any(c == corp_code for c, _ in found):
        return None
    return next((n for c, n in found if c != corp_code), None)


# ---- 파생 지표·범위 밖 표현 ----

_PCT = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?P<u>%p|%포인트|%|퍼센트)")
_MARGIN = re.compile(r"영업\s*이익률|이익률|마진율")
_GROWTH = re.compile(r"YoY|QoQ|전년\s*동기\s*대비|전년\s*대비|전분기\s*대비|전\s*분기\s*대비|전기\s*대비|직전\s*분기\s*대비"
                     r"|증가율|감소율|증감률|증감율|성장률|성장|증가|감소|늘|줄|급증|급감|상승|하락")
MARKET = re.compile(r"목표\s*주가|목표가|주가|시가\s*총액|시총|PER(?![A-Za-z])|PBR(?![A-Za-z])|EV/EBITDA|투자\s*의견"
                    r"|컨센서스|밸류에이션|배당\s*수익률|매수\s*의견|매도\s*의견")
FORECAST = re.compile(r"예상|전망|추정|예정|관측|것으로\s*보|할\s*것|될\s*것|넘을\s*것|기대")


def derived_kind(text: str) -> str | None:
    """퍼센트 주장의 종류: growth(증감률) / margin(이익률) / other(비중·비율·%p) / None(퍼센트 없음)."""
    units = [m["u"] for m in _PCT.finditer(text)]
    if not units:
        return None
    if any(u in ("%p", "%포인트") for u in units):
        return "other"
    if _MARGIN.search(text):
        return "margin"
    if _GROWTH.search(text):
        return "growth"
    return "other"


@dataclass
class Scope:
    """범위 판별 결과. category: checked / out_of_scope / other_company / derived. reason은 회색 표시 이유 또는
    검수 경로 표시(derived:growth·derived:margin). search_periods가 None이면 기간 불명(전체 검색)."""

    category: str
    reason: str | None
    mentions: list[PeriodMention] = field(default_factory=list)
    search_periods: list[str] | None = None
    report_types: list[str] | None = None
    derived: str | None = None

    @property
    def periods(self) -> list[Period]:
        return [m.period for m in self.mentions]


def assess(text: str, corp_code: str, *, as_of: Period, names: Mapping[str, Iterable[str]] | CompanyIndex) -> Scope:
    """검수 대상 문장(1단계 통과)을 범위 밖으로 돌릴지 정한다. 순서: 다른 회사 → 주가·추정 → 기준 시점 뒤 기간 →
    기간 없는 전망 → 파생 지표. 통과하면 검색 기간·보고서 종류를 붙인다."""
    other = other_company(text, corp_code, names)
    if other:
        return Scope("other_company", f"other_company:{other}")
    if MARKET.search(text):
        return Scope("out_of_scope", "market")
    mentions = extract_periods(text, as_of)
    if any(m.period.end > as_of.end for m in mentions):
        return Scope("out_of_scope", "future_period", mentions)
    if not mentions and FORECAST.search(text):
        return Scope("out_of_scope", "forecast")
    sp = search_periods(m.period for m in mentions)
    kind = derived_kind(text)
    if kind == "growth":
        if PRELIM_GROWTH_ACCOUNTS.search(text):
            return Scope("checked", "derived:growth", mentions, sp, ["preliminary"], kind)
        return Scope("derived", "derived:growth_unsupported", mentions, derived=kind)
    if kind == "margin":
        return Scope("checked", "derived:margin", mentions, sp, None, kind)
    if kind == "other":
        return Scope("derived", "derived:other", mentions, derived=kind)
    return Scope("checked", None, mentions, sp)
