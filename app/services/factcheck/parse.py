# app/services/factcheck/parse.py
"""공시 원문 → 근거 문단. 정기보고서 I·II·III절과 잠정실적(공정공시) xforms HTML.

- 정기보고서: 근거 모드의 `evidence.passages.blocks()`·`pack()`을 그대로 쓰고(표 행에 제목·단위·열 머리글),
  절 찾기만 새로 한다. 근거 모드의 `sections()`는 I절의 '1. 회사의 개요'와 II절만 찾고 닫는 태그가 있어야
  잡히므로, 여기서는 `<SECTION-1` 시작 위치로 잘라(다음 시작 또는 문서 끝까지) I·II·III절 전체를 잡는다.
  III절(재무에 관한 사항)에서는 분량 때문에 제목에 '주석'이 든 하위 절을 뺀다(설계 T0: 주석은 2주 밖).
- 잠정실적: DART 문서 XML이 아니라 xforms HTML(TABLE 대신 table·td, 단위 조원)이라 전용 파서를 쓴다.
  정정 공시는 본표에 정정 후 값이 실리고, 맨 앞 '정정신고(보고)' 블록에 정정 전/후 값이 따로 있다.
  정정 전 값은 숫자 대조에서 맞는 값으로 잡히지 않도록 본표 문단(PRELIM)에 넣지 않고 정정 문단(CORR)에만 둔다.
- 문단 ID = `{corp_code}-{rcept_no}-{section}-{idx}`(설계 Outside Voice #3, 문서 단위 적재·삭제용).
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.services.evidence.passages import Passage, _key, _text, blocks, pack
from app.services.factcheck.xbrl import period_label

SECTIONS = ("I", "II", "III")
EXCLUDED_SUBSECTION_WORDS = ("주석",)  # III절에서 빼는 하위 절(제목에 이 말이 들면)
PRELIM, CORR = "PRELIM", "CORR"
UNIT_MULTIPLIER = {"원": 1, "천원": 10**3, "백만원": 10**6, "억원": 10**8, "조원": 10**12}

_SEC1 = re.compile(r"<SECTION-1\b")
_SEC2 = re.compile(r"<SECTION-2\b")
_TITLE = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>", re.S)
_ROMAN_HEAD = re.compile(r"(I{1,3})\.")


def passage_id(corp_code: str, rcept_no: str, section: str, idx: int) -> str:
    """문단 ID `{corp}-{rcept_no}-{section}-{idx}`."""
    return f"{corp_code}-{rcept_no}-{section}-{idx}"


def decode(data: bytes) -> str:
    """원문 바이트를 문자열로. UTF-8이 아니면 CP949(EUC-KR 상위 호환)로 읽는다."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp949", errors="replace")


# ── 정기보고서 ──────────────────────────────────────────────────────────────────

def _segments(xml: str, start: re.Pattern) -> list[str]:
    """시작 태그 위치마다 다음 시작 태그(또는 끝)까지 자른 조각들. 닫는 태그가 없어도 된다."""
    pos = [m.start() for m in start.finditer(xml)]
    return [xml[a:b] for a, b in zip(pos, pos[1:] + [len(xml)])]


def _seg_title(seg: str) -> str:
    m = _TITLE.search(seg)
    return _text(m.group(1)) if m else ""


def _drop_subsections(section_xml: str, words=EXCLUDED_SUBSECTION_WORDS) -> str:
    """제목에 words가 든 SECTION-2 조각을 뺀다(첫 SECTION-2 앞부분은 그대로)."""
    first = _SEC2.search(section_xml)
    if not first:
        return section_xml
    kept = [s for s in _segments(section_xml, _SEC2) if not any(w in _seg_title(s) for w in words)]
    return section_xml[:first.start()] + "".join(kept)


def split_sections(xml: str) -> dict[str, str]:
    """정기보고서에서 I·II·III절 원문 조각을 찾는다. III절은 주석 하위 절을 뺀다. 첫 SECTION-1 앞은 버린다."""
    out: dict[str, str] = {}
    for seg in _segments(xml, _SEC1):
        m = _ROMAN_HEAD.match(_key(_seg_title(seg)))
        if m and m.group(1) in SECTIONS and m.group(1) not in out:
            out[m.group(1)] = _drop_subsections(seg) if m.group(1) == "III" else seg
    return out


_TABLE_GROUP = re.compile(r"</?TABLE-GROUP\b[^>]*>")
_TABLE = re.compile(r"<TABLE\b[^>]*>(.*?)</TABLE>", re.S)
_ROW = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.S)
_CELL = re.compile(r"<(TD|TH|TE|TU)\b[^>]*>(.*?)</\1>", re.S)
_UNIT_TEXT = re.compile(r"단위\s*[:：]")


def _cover_table(m: re.Match) -> str:
    """재무제표 머리 표(한 칸짜리 행 여러 개: 표 이름·기간·단위)를 한 행 표(표 이름 | 단위)로 바꾼다.

    `blocks()`는 한 행짜리 표만 캡션·단위 줄로 보므로, 이렇게 해야 다음 데이터 표 행에 단위가 붙는다.
    """
    rows = [[_text(c) for _, c in _CELL.findall(r)] for r in _ROW.findall(m.group(1))]
    cells = [[c for c in r if c] for r in rows]
    cells = [c for c in cells if c]
    if len(cells) < 2 or any(len(c) != 1 for c in cells):
        return m.group(0)
    units = [c[0] for c in cells if _UNIT_TEXT.search(c[0])]
    if not units:
        return m.group(0)
    return f"<TABLE><TR><TD>{htmllib.escape(cells[0][0])}</TD><TD>{htmllib.escape(units[0])}</TD></TR></TABLE>"


def normalize_tables(section_xml: str) -> str:
    """`blocks()` 전에 표 구조를 고친다(근거 모드 코드는 고치지 않는다).

    1) `<TABLE-GROUP>` 태그를 지운다. `blocks()`의 `<TABLE\\b`가 `<TABLE-GROUP`에도 걸려 재무제표 제목과 머리 표를
       삼키기 때문이다(III절 재무제표가 TABLE-GROUP으로 묶여 있다).
    2) 재무제표 머리 표를 한 행 캡션·단위 표로 바꾼다(_cover_table).
    """
    return _TABLE.sub(_cover_table, _TABLE_GROUP.sub("", section_xml))


def regular_passages(corp_code: str, rcept_no: str, xml: str,
                     require: tuple[str, ...] = SECTIONS) -> list[Passage]:
    """정기보고서(사업·반기·분기)를 I·II·III절 문단으로 나눈다. require 절이 없으면 ValueError."""
    secs = split_sections(xml)
    missing = [s for s in require if s not in secs]
    if missing:
        raise ValueError(f"missing sections: {missing}")
    out: list[Passage] = []
    for sec in SECTIONS:
        if sec in secs:
            for i, text in enumerate(pack(blocks(normalize_tables(secs[sec])))):
                out.append(Passage(passage_id(corp_code, rcept_no, sec, i), corp_code, rcept_no, sec, i, text))
    return out


# ── 잠정실적(xforms HTML) ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PrelimFigure:
    """잠정실적 본표 한 행: 계정·당해/누계와 당기·전기·전년동기 값(단위는 PrelimReport.unit). 없는 칸은 None."""

    account: str
    basis: str  # 당해실적 | 누계실적
    current: Decimal | None
    prior_q: Decimal | None
    qoq_pct: Decimal | None
    prior_y: Decimal | None
    yoy_pct: Decimal | None
    qoq_turn: str | None = None  # 흑자·적자 전환 여부(있을 때만)
    yoy_turn: str | None = None


@dataclass(frozen=True)
class PrelimCorrection:
    """정정 공시의 정정사항 한 항목(정정 전 → 정정 후)."""

    group: str  # 당기실적 | 전기대비증감율(%) | 전년동기대비증감율(%)
    item: str
    basis: str
    before: Decimal
    after: Decimal


@dataclass(frozen=True)
class PrelimReport:
    """잠정실적 공시 하나. period는 당기실적 기간(예: 2026Q2), original_date는 정정 대상 원 공시 제출일."""

    title: str
    period: str
    period_start: str
    period_end: str
    unit: str
    is_correction: bool
    original_date: str | None
    figures: list[PrelimFigure]
    corrections: list[PrelimCorrection] = field(default_factory=list)
    periods: dict[str, tuple[str, str]] = field(default_factory=dict)


_STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)
_BR = re.compile(r"<br\b[^>]*>", re.I)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_KDATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_UNIT_CELL = re.compile(r"단위\s*[:：]\s*([^,)\s]+)")
_ITEM = re.compile(r"-\s*(.+?)\((당해실적|누계실적)\)\s*$")
_GROUP_TAG = re.compile(r"\(['’]?\d{2}\.\d[QH]\)\s*$")
_BASES = ("당해실적", "누계실적")
_EXPECTED_HEADER = ["구분", "당기실적", "전기실적", "전기대비", "전년동기실적", "전년동기대비"]


def _cell_lines(fragment: str) -> list[str]:
    """셀 하나를 <br> 기준 줄 목록으로(태그 제거·엔티티 해제·공백 정리, 빈 줄 제거)."""
    txt = htmllib.unescape(_TAGS.sub(" ", _BR.sub("\n", fragment)))
    return [ln for ln in (re.sub(r"\s+", " ", x).strip() for x in txt.split("\n")) if ln]


def _rows(doc: str) -> list[list[list[str]]]:
    """문서의 모든 표 행: 행 → 셀 → 줄."""
    doc = _STYLE.sub("", doc)
    return [[_cell_lines(c) for c in _TD.findall(tr)] for tr in _TR.findall(doc)]


def _num(s: str | None) -> Decimal | None:
    """'1,813.83' → Decimal. '-'·빈 칸·숫자 아님은 None."""
    s = (s or "").replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def to_won(value: Decimal, unit: str) -> int:
    """단위 값(예: 171.50 조원)을 정수 원으로. Decimal로 곱해 부동소수 오차가 없다."""
    return int(value * UNIT_MULTIPLIER[unit])


def _flat(cells: list[list[str]]) -> list[str]:
    return [" ".join(c) for c in cells]


def _turn(s: str) -> str | None:
    return None if s in ("", "-") else s


def _corrections(rows: list[list[list[str]]]) -> list[PrelimCorrection]:
    """'정정항목 | 정정전 | 정정후' 표에서 숫자 항목만 꺼낸다(줄 단위로 항목과 값을 맞춘다)."""
    out: list[PrelimCorrection] = []
    start = next((i for i, r in enumerate(rows) if _flat(r) == ["정정항목", "정정전", "정정후"]), None)
    if start is None:
        return out
    for r in rows[start + 1:]:
        if len(r) != 3:
            break
        labels, before, after = r
        group, items = "", []
        for ln in labels:
            if ln.startswith("·"):
                group = _GROUP_TAG.sub("", ln[1:].strip()).strip()
            elif m := _ITEM.match(ln):
                items.append((group, m.group(1).strip(), m.group(2)))
        b, a = [_num(x) for x in before], [_num(x) for x in after]
        if not items or len(b) != len(items) or len(a) != len(items) or None in b or None in a:
            continue
        out += [PrelimCorrection(g, it, bs, x, y) for (g, it, bs), x, y in zip(items, b, a)]
    return out


def parse_prelim(doc: str) -> PrelimReport:
    """연결(또는 별도)재무제표기준 영업(잠정)실적(공정공시) xforms HTML을 읽는다. 본표가 예상 형태가 아니면 ValueError."""
    rows = _rows(doc)
    flat = [_flat(r) for r in rows]
    periods: dict[str, tuple[str, str]] = {}
    for f in flat:
        if f and f[0] in ("당기실적", "전기실적", "전년동기실적", "당기누계실적", "전년동기누적실적"):
            dates = _DATE.findall(" ".join(f[1:]))
            if len(dates) == 2:
                periods[f[0]] = tuple("-".join(d) for d in dates)  # type: ignore[assignment]
    if "당기실적" not in periods:
        raise ValueError("prelim: 실적기간(당기실적)을 찾지 못했다")
    start, end = periods["당기실적"]
    header = next((f for f in flat if f and f[0] == "구분"), None)
    if header != _EXPECTED_HEADER:
        raise ValueError(f"prelim: 예상과 다른 본표 머리글 {header}")
    unit = next((m.group(1) for f in flat for c in f if (m := _UNIT_CELL.search(c))), "")
    if unit not in UNIT_MULTIPLIER:
        raise ValueError(f"prelim: 알 수 없는 단위 {unit!r}")

    figures: list[PrelimFigure] = []
    account = ""
    for f in flat:
        if len(f) == 9 and f[1] in _BASES:
            account, basis, vals = f[0], f[1], f[2:]
        elif len(f) == 8 and f[0] in _BASES and account:
            basis, vals = f[0], f[1:]
        else:
            continue
        nums = [_num(v) for v in vals]
        fig = PrelimFigure(account, basis, nums[0], nums[1], nums[2], nums[4], nums[5], _turn(vals[3]), _turn(vals[6]))
        if any(v is not None for v in nums):
            figures.append(fig)
    if not figures:
        raise ValueError("prelim: 본표 값이 없다")

    text = _TAGS.sub(" ", doc)
    is_correction = "정정신고(보고)" in text or any(f and f[0] == "정정일자" for f in flat)
    original = next((f[1] for f in flat if len(f) > 1 and "정정관련 공시서류제출일" in f[0]), None)
    m = _KDATE.search(original or "")
    original_date = f"{int(m.group(1)):04d}{int(m.group(2)):02d}{int(m.group(3)):02d}" if m else None
    tm = re.search(r'class="xforms_title".*?<span[^>]*>(.*?)</span>', doc, re.S)
    title = _text(tm.group(1)) if tm else "영업(잠정)실적(공정공시)"
    return PrelimReport(title=title, period=period_label(date.fromisoformat(start), date.fromisoformat(end)),
                        period_start=start, period_end=end, unit=unit, is_correction=is_correction,
                        original_date=original_date if is_correction else None, figures=figures,
                        corrections=_corrections(rows) if is_correction else [], periods=periods)


def _fmt(v: Decimal | None) -> str:
    return "-" if v is None else format(v, ",")


def _span(rep: PrelimReport, key: str) -> str:
    s = rep.periods.get(key)
    return f"{s[0]}~{s[1]}" if s else ""


def prelim_passages(corp_code: str, rcept_no: str, rep: PrelimReport) -> list[Passage]:
    """잠정실적을 문단으로: 계정마다 본표 문단 하나(PRELIM), 정정 공시면 정정 전/후 문단(CORR)."""
    head = f"[{rep.title} {rep.period}({rep.period_start}~{rep.period_end}), 단위 {rep.unit}]"
    by_acc: dict[str, list[str]] = {}
    for f in rep.figures:
        span = _span(rep, "당기누계실적") if f.basis == "누계실적" else ""
        parts = [f"당기실적 {_fmt(f.current)}"]
        if f.basis == "당해실적":
            parts += [f"전기실적 {_fmt(f.prior_q)}", f"전기대비 증감율(%) {_fmt(f.qoq_pct)}"]
            parts += [f"전기대비 {f.qoq_turn}"] if f.qoq_turn else []
        parts += [f"전년동기실적 {_fmt(f.prior_y)}", f"전년동기대비 증감율(%) {_fmt(f.yoy_pct)}"]
        parts += [f"전년동기대비 {f.yoy_turn}"] if f.yoy_turn else []
        label = f"{f.account}({f.basis}{', ' + span if span else ''})"
        by_acc.setdefault(f.account, []).append(f"{label}: " + " | ".join(parts))
    out = [Passage(passage_id(corp_code, rcept_no, PRELIM, i), corp_code, rcept_no, PRELIM, i,
                   f"{head} " + " / ".join(lines)) for i, lines in enumerate(by_acc.values())]
    if rep.corrections:
        orig = f"{rep.original_date[:4]}-{rep.original_date[4:6]}-{rep.original_date[6:]}" if rep.original_date else "?"
        items = [("para", f"{c.group} {c.item}({c.basis}): 정정 전 {_fmt(c.before)} → 정정 후 {_fmt(c.after)}")
                 for c in rep.corrections]
        chunks = pack([("title", f"{rep.title} {rep.period} 정정 공시(원 공시 {orig} 제출분의 값을 정정)")] + items)
        out += [Passage(passage_id(corp_code, rcept_no, CORR, i), corp_code, rcept_no, CORR, i, t)
                for i, t in enumerate(chunks)]
    return out
