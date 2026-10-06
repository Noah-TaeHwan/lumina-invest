"""팩트체커 범위 판별(설계 D3·D5·D6, Codex #7·#11): 주장 기간 해석, 다른 회사 주어, 파생 지표, 범위 밖 표현.

- 기간 3종 중 **주장 기간**을 문장에서 뽑는다(연도·분기·반기·누적, '2Q25'·'26.2Q'·'1H26'·'FY25'·''25년' 변형).
  상대 표현('올해·작년·재작년·최근 분기·직전 분기')과 연도 없는 분기·반기는 검수 실행일이 아니라 **기준 시점 as_of**로
  해석한다. '전년 대비·전분기 대비' 같은 비교 기준은 주장 기간이 아니다. 해석 못 하면 기간 불명(빈 목록).
- 기간 바로 뒤에 '대비·보다·에 비해'가 오면 비교 기준이라 뺀다('2024년 대비'). '전년 동기'는 문장 속 가장 가까운 절대
  기간의 한 해 전(없으면 as_of 기준). 연도 없는 분기를 한 해 당겨 풀었으면 shifted로 표시한다(⚠️를 내지 않는 근거).
- 해석된 기간이 없는 문장은 기준 시점 끝에서 RECENT_MONTHS(12)개월 안에 끝나는 보고서만 검색한다(period_assumed).
- 검색 범위 = 주장 기간 끝 ~ 2년 뒤에 끝나는 보고서 기간(뒤 보고서의 비교값 포함, Codex #7). 기간 불명이면 전체(None).
- 다른 회사: 주입한 상장사명 사전(corp_code → 이름들)과 토큰 단위로 **정확히** 맞춘다(끝 조사만 뗀다, 부분 일치 금지:
  'SK' ≠ 'SK하이닉스'). 주어 자리(첫 은·는·이·가 토큰까지)에 선택 회사가 없고 다른 상장사가 있으면 범위 밖.
- 파생 지표: 매출·영업이익 증감률은 잠정실적 공시(전기·전년 동기 대비 증감율이 공시 안에 있다)로 대조하고,
  영업이익률은 XBRL 두 값으로 계산해 대조한다(xbrl_check). 그 밖의 %(비중·비율·%p)는 범위 밖(파생 지표).
"""
from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from app.services.evidence.subject import _GENERIC as GENERIC_WORDS
from app.services.evidence.subject import COMMON_WORD_NAMES
from app.services.evidence.subject import normalize as _subject_normalize
from app.services.factcheck import corp_names


def normalize(name: str) -> str:
    """회사명 비교 키: T1 corp_names.normalize(NFKC·(주)·공백·대문자) 뒤 영문 법인 접미(Co., Ltd. 등)도 뗀다."""
    return corp_names.normalize(_subject_normalize(name))

HORIZON_YEARS = 2  # 주장 기간 뒤 몇 년 안의 보고서까지 검색하나(비교값)
RECENT_MONTHS = 12  # 기간 없는 문장의 검색 범위: 기준 시점 끝에서 이 개월 안에 끝나는 보고서 기간(설계 D3)
PRELIM_GROWTH_ACCOUNTS = re.compile(r"매출|영업\s*이익(?!\s*률)")  # 잠정실적 공시에 증감율이 있는 계정


@dataclass(frozen=True)
class Period:
    """주장·문서의 기간. kind: year / quarter / half. cumulative는 분기 누적(1월 1일부터)일 때만 True."""

    year: int
    kind: str
    n: int = 0
    cumulative: bool = False

    def __post_init__(self) -> None:
        ok = {"year": (0,), "quarter": (1, 2, 3, 4), "half": (1, 2)}.get(self.kind)
        if ok is None or (self.kind != "year" and self.n not in ok):
            raise ValueError(f"잘못된 기간: {self.kind} {self.n}")

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
    """문장 안 기간 표현 하나. start·end는 문장 안 글자 위치, relative는 as_of로 해석했는지,
    shifted는 연도 없는 분기·반기가 as_of 뒤라 한 해 당겨 해석했는지(확신할 수 없는 해석)."""

    start: int
    end: int
    period: Period
    relative: bool = False
    shifted: bool = False


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
_CUM = r"(?P<cum>\s*(?:누적|누계))?"
# 2026Q2·2026H1·2025 Q3. 뒤에 한글 조사('2026Q2의')는 와도 되고, H는 1·2만('2026H3'은 기간이 아니다)
_YQ = re.compile(rf"(?<![\w.])(?P<y>{_Y4})\s*(?:Q(?P<qn>[1-4])|H(?P<hn>[12]))(?![0-9A-Za-z])" + _CUM)
_QYY = re.compile(rf"(?<![\w.])(?P<q>[1-4])Q(?:{_APOS}?(?P<yy>\d{{2}})|\s*(?P<yyyy>{_Y4}))(?![\d.])" + _CUM)
_HYY = re.compile(rf"(?<![\w.])(?P<h>[12])H(?:{_APOS}?(?P<yy>\d{{2}})|\s*(?P<yyyy>{_Y4}))(?![\d.])")
_YYDOT = re.compile(rf"(?<![\d.]){_APOS}?(?P<yy>{_Y4}|\d{{2}})\.\s*(?:(?P<q>[1-4])Q|(?P<h>[12])H)(?![A-Za-z])" + _CUM)
# 기간 바로 뒤 비교 표현: '2024년 대비', '작년 4분기 대비', '2024년보다' — 그 기간은 비교 기준이지 주장 기간이 아니다
_COMPARE_AFTER = re.compile(r"\s*(?:대비|比|보다|에\s*비해|와\s*비교|과\s*비교)")
_YEAR_REL = re.compile(r"(?P<rel>올해|금년|당해\s*연도|이번\s*해|재작년|지지난해|작년|지난해|전년도|전년)(?!\s*동기)" + _SUB)
_SAME_Q_LAST_YEAR = re.compile(r"(?:전년|작년)\s*동기")
# '당분기'는 단어 첫머리만('해당 분기'의 '당 분기'는 as_of 분기가 아니다)
_QUARTER_REL = re.compile(r"(?P<rel>이번\s*분기|최근\s*분기|(?<![가-힣])당\s*분기|직전\s*분기|전\s*분기|지난\s*분기)")
_BARE = re.compile(r"(?:(?P<c1>1)\s*[~∼\-]\s*(?P<c2>[2-4])\s*분기|(?<![\d.])(?P<q>[1-4])\s*분기"
                   r"|(?<![\w.])(?P<qq>[1-4])Q(?![0-9A-Za-z'‘’])|(?P<hk>[상하])반기"
                   r"|(?<![\w.])(?P<h>[12])H(?![0-9A-Za-z'‘’]))"
                   r"(?P<cum>\s*(?:누적|누계))?")
# 앞 문장에 기대는 상대 기간('같은 분기·같은 기간·해당 분기·당분기·이번 분기·동분기·같은 해·동 기간·해당 반기·같은
# 회계연도'). 문장에서 해석된 기간이 하나도 없는데 이것이 있으면 어느 기간인지 알 수 없다(앞 문장 상속은 다음 단계)
# → 판정을 최대 ❔로 둔다. 맨 '동기'는 '비동기·동기 부여'와 겹쳐 넣지 않는다
# 한글 경계: 앞말 속('가동 기간'·'변동 분기'·'이해당')은 제외, '해'는 조사(에·의·는·도·가)만 뒤에 올 수 있다('같은 해외' 제외)
SAME_PERIOD = re.compile(r"(?<![가-힣])(?:(?:같은|해당)\s*(?:분기|기간|해(?:(?![가-힣])|(?=[에의는도가]))|연도|반기"
                         r"|회계\s*연도)|당\s*분기|이번\s*분기|동\s*분기|동\s*기간)")
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
    return _scan(text, as_of)[0]


def period_spans(text: str, as_of: Period) -> list[tuple[int, int]]:
    """문장 속 모든 기간 표현 구간(주장 기간 + 비교 기준 '2024년 대비'·'전년 대비'). 숫자 확인 전에 지우는 데 쓴다."""
    return _scan(text, as_of)[1]


def _scan(text: str, as_of: Period) -> tuple[list[PeriodMention], list[tuple[int, int]]]:
    """(주장 기간 목록, 모든 기간 표현 구간)."""
    taken: list[tuple[int, int]] = [(m.start(), m.end()) for m in _COMPARE.finditer(text)]
    out: list[PeriodMention] = []

    def free(m: re.Match) -> bool:
        return not any(m.start() < e and s < m.end() for s, e in taken)

    def add(m: re.Match, p: Period, relative: bool = False, shifted: bool = False) -> None:
        taken.append((m.start(), m.end()))
        if not _COMPARE_AFTER.match(text, m.end()):
            out.append(PeriodMention(m.start(), m.end(), p, relative, shifted))

    def quarter(year: int, q: int, cum) -> Period:
        return Period(year, "quarter", q, cumulative=bool(cum) and q != 1)

    for m in _YQ.finditer(text):
        if free(m):
            y = int(m["y"])
            add(m, quarter(y, int(m["qn"]), m["cum"]) if m["qn"] else Period(y, "half", int(m["hn"])))
    for m in _QYY.finditer(text):
        if free(m):
            add(m, quarter(_yy(m["yy"] or m["yyyy"]), int(m["q"]), m["cum"]))
    for m in _HYY.finditer(text):
        if free(m):
            add(m, Period(_yy(m["yy"] or m["yyyy"]), "half", int(m["h"])))
    for m in _YYDOT.finditer(text):
        if free(m):
            y = _yy(m["yy"])
            add(m, quarter(y, int(m["q"]), m["cum"]) if m["q"] else Period(y, "half", int(m["h"])))
    for m in _YEAR_ABS.finditer(text):
        if free(m):
            add(m, _with_sub(_yy(m["fy"] or m["y4"] or m["y2"] or m["y2b"]), m))
    last_q = Period.quarter_of(as_of.year, as_of.last_quarter)
    absolute = list(out)
    for m in _SAME_Q_LAST_YEAR.finditer(text):
        if free(m):
            if absolute:  # 문장 속 가장 가까운 절대 기간의 한 해 전 같은 기간
                b = min(absolute, key=lambda x: abs(x.start - m.start())).period
                add(m, Period(b.year - 1, b.kind, b.n, b.cumulative))
            else:
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
        shifted = not before and p.end > as_of.end
        if shifted:  # 연도 없는 분기가 기준 시점 뒤면 가장 최근의 그 분기(한 해 전) — 확신할 수 없는 해석
            p = Period(p.year - 1, p.kind, p.n, p.cumulative)
        add(m, p, relative=not before, shifted=shifted)
    return sorted(out, key=lambda x: x.start), sorted(taken)


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
    """상장사명 사전(corp_code → 이름들)을 비교용 표기로 색인한다. 일상어와 겹치는 이름('대상')·일반명사
    (evidence.subject의 일반명사 목록, '콘텐츠')·1자 이름은 뺀다."""

    def __init__(self, names: Mapping[str, Iterable[str]]):
        self.names = {c: list(ns) for c, ns in names.items()}
        self._idx: dict[str, tuple[str, str]] = {}
        common = {normalize(n) for n in COMMON_WORD_NAMES} | {normalize(n) for n in GENERIC_WORDS}
        for corp, ns in self.names.items():
            for n in ns:
                k = normalize(n)
                if len(k) >= 2 and k not in common:
                    self._idx.setdefault(k, (corp, n))

    @classmethod
    def from_entries(cls, entries: Iterable[Mapping]) -> "CompanyIndex":
        """T1 상장사명 사전(corp_names.build/load 항목)에서 만든다(corp_names.by_corp_code 형식)."""
        return cls(corp_names.by_corp_code(list(entries)))

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


def company_names_in(text: str, names: Mapping[str, Iterable[str]] | CompanyIndex) -> dict[str, str]:
    """문장 속 상장사 언급 corp_code → 사전 이름(나온 순서, 회사마다 첫 이름). 주어 없는 문장의 회사 상속에 쓴다."""
    out: dict[str, str] = {}
    for c, n in company_mentions(text, names):
        out.setdefault(c, n)
    return out


def subject_company_names(text: str, names: Mapping[str, Iterable[str]] | CompanyIndex) -> dict[str, str]:
    """주어 자리(첫 은·는·이·가 토큰까지 — other_company와 같은 규칙)에 나온 상장사 corp_code → 사전 이름."""
    end = _subject_end(text)
    out: dict[str, str] = {}
    for c, n, s, _ in _as_index(names).mentions(text):
        if s < end:
            out.setdefault(c, n)
    return out


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
FORECAST = re.compile(r"예상|전망|추정|예정|관측|목표|것으로\s*보|할\s*것|될\s*것|넘을\s*것|기대")


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
    period_spans: list[tuple[int, int]] = field(default_factory=list)  # 숫자 확인 전에 지울 기간 표현 구간(비교 기준 포함)
    ambiguous_period: bool = False  # 명시 기간 없이 '같은 분기' 같은 상대 기간만 있다(판정 최대 ❔)
    period_assumed: bool = False  # 해석된 기간이 없어 최근 보고서(recent_periods)로 검색 범위를 정했다

    @property
    def periods(self) -> list[Period]:
        return [m.period for m in self.mentions]


def relative_units(text: str) -> set[str]:
    """문장 속 상대 기간 표현의 단위: quarter('같은 분기·당분기·동 분기·해당 분기·이번 분기') / half('반기') /
    year('해·연도·회계연도') / any('같은 기간·동 기간·해당 기간'). 앞 문장 기간을 이어받을 때 단위를 맞추는 데 쓴다."""
    out = set()
    for m in SAME_PERIOD.finditer(text):
        t = m.group(0)
        out.add("quarter" if "분기" in t else "half" if "반기" in t else "any" if "기간" in t else "year")
    return out


def unit_accepts(unit: str, p: Period) -> bool:
    """상대 기간 단위가 이어받을 기간을 받을 수 있는가: 분기류 ← 분기 단독(YYYYQn), 반기류 ← YYYYH1/H2,
    해·연도류 ← YYYY, 기간류 ← 무엇이든."""
    if unit == "quarter":
        return p.kind == "quarter" and not p.cumulative
    if unit == "half":
        return p.kind == "half"
    if unit == "year":
        return p.kind == "year"
    return True


def relative_only(text: str, mentions: Sequence[PeriodMention]) -> bool:
    """해석된 기간이 하나도 없고(연도 없는 '2분기'·'당분기'도 해석되면 기간이 있다) '같은 분기' 같은 상대 기간 표현이
    있는가."""
    return not mentions and bool(SAME_PERIOD.search(text))


def _scan_with(text: str, as_of: Period, inherited: Period | None) -> tuple[list[PeriodMention],
                                                                            list[tuple[int, int]]]:
    """_scan에 앞 문장 기간 상속을 더한다: 해석된 기간이 없고 상대 기간 표현만 있으면, 그 표현 자리마다 inherited
    기간을 둔다(relative=True). 그 밖에는 _scan 그대로."""
    mentions, spans = _scan(text, as_of)
    if inherited is None or not relative_only(text, mentions):
        return mentions, spans
    got = [PeriodMention(m.start(), m.end(), inherited, relative=True) for m in SAME_PERIOD.finditer(text)]
    return got, sorted(spans + [(m.start, m.end) for m in got])


def recent_periods(as_of: Period, months: int = RECENT_MONTHS) -> list[str]:
    """기간 없는 문장의 검색 기간: as_of 끝에서 months개월 안(이전 끝은 제외)에 끝나는 보고서 기간 표기.
    예: as_of 2026Q2·2026H1 → 2025Q3·2025Q4·2025·2026Q1·2026Q2·2026H1."""
    hi = as_of.end
    y, m = hi.year, hi.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    lo = date(y, m, calendar.monthrange(y, m)[1])
    hit: dict[str, date] = {}
    for year in range(lo.year, hi.year + 1):
        for c in _all_labels(year):
            if lo < c.end <= hi:
                hit[c.label] = c.end
    return sorted(hit, key=lambda k: (hit[k], k))


def _periods_or_recent(mentions: Sequence[PeriodMention], as_of: Period) -> tuple[list[str] | None, bool]:
    """(검색 기간, 최근 보고서로 가정했는가). 해석된 기간이 있으면 search_periods 그대로."""
    if mentions:
        return search_periods(m.period for m in mentions), False
    return recent_periods(as_of), True


def period_scope(text: str, as_of: Period, *, inherited: Period | None = None) -> Scope:
    """범위 밖 판별 없이 검색 범위만 붙인 Scope(force_check용). 증감률이면 잠정실적만 검색한다.
    inherited는 앞 문장에서 이어받은 기간(상대 기간 표현만 있는 문장에만 쓴다)."""
    mentions, spans = _scan_with(text, as_of, inherited)
    growth = derived_kind(text) == "growth" and bool(PRELIM_GROWTH_ACCOUNTS.search(text))
    sp, assumed = _periods_or_recent(mentions, as_of)
    return Scope("checked", None, mentions, sp, ["preliminary"] if growth else None, derived_kind(text), spans,
                 relative_only(text, mentions), assumed)


def assess(text: str, corp_code: str, *, as_of: Period, names: Mapping[str, Iterable[str]] | CompanyIndex,
           inherited: Period | None = None) -> Scope:
    """검수 대상 문장(1단계 통과)을 범위 밖으로 돌릴지 정한다. 순서: 다른 회사 → 주가 → 전망·추정·목표 표지 →
    기준 시점 뒤 기간 → 파생 지표. 전망 표지는 기간과 상관없이 검수 안 함('3분기 영업이익은 10조원으로 예상된다').
    통과하면 검색 기간·보고서 종류를 붙인다. inherited는 앞 문장에서 이어받은 기간(해석된 기간 없이 상대 기간
    표현만 있는 문장에만 쓴다 — 그 표현 자리에 그 기간을 둔다)."""
    other = other_company(text, corp_code, names)
    if other:
        return Scope("other_company", f"other_company:{other}")
    if MARKET.search(text):
        return Scope("out_of_scope", "market")
    if FORECAST.search(text):
        return Scope("out_of_scope", "forecast")
    mentions, spans = _scan_with(text, as_of, inherited)
    amb = relative_only(text, mentions)
    if any(m.period.end > as_of.end for m in mentions):
        return Scope("out_of_scope", "future_period", mentions, period_spans=spans, ambiguous_period=amb)
    sp, assumed = _periods_or_recent(mentions, as_of)
    kind = derived_kind(text)
    if kind == "growth":
        if PRELIM_GROWTH_ACCOUNTS.search(text):
            return Scope("checked", "derived:growth", mentions, sp, ["preliminary"], kind, spans, amb, assumed)
        return Scope("derived", "derived:growth_unsupported", mentions, derived=kind, period_spans=spans,
                     ambiguous_period=amb)
    if kind == "margin":
        return Scope("checked", "derived:margin", mentions, sp, None, kind, spans, amb, assumed)
    if kind == "other":
        return Scope("derived", "derived:other", mentions, derived=kind, period_spans=spans, ambiguous_period=amb)
    return Scope("checked", None, mentions, sp, period_spans=spans, ambiguous_period=amb, period_assumed=assumed)
