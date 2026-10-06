# app/services/factcheck/parse.py
"""공시 원문 → 근거 문단. 정기보고서 I·II·III절과 잠정실적(공정공시) xforms HTML.

- 정기보고서: 근거 모드의 `evidence.passages.pack()`·`_text()`를 import해 쓰고, 표 펼치기는 `table_blocks()`로
  새로 한다(같은 출력 형식). 근거 모드 `blocks()`는 TABLE-GROUP·여러 줄 머리·ROWSPAN/COLSPAN·하위 머리·재무제표
  기간을 다루지 못하는데 사전등록 해시 대상이라 고칠 수 없다. 절 찾기도 새로 한다: 근거 모드 `sections()`는
  I절의 '1. 회사의 개요'와 II절만 찾고 닫는 태그가 있어야 잡히므로, `<SECTION-1` 시작 위치로 잘라 I·II·III절
  전체를 잡는다. III절에서는 제목에 '주석'이 든 하위 절을 뺀다(1주차 범위).
- 잠정실적: DART 문서 XML이 아니라 xforms HTML(TABLE 대신 table·td, 단위 조원)이라 전용 파서를 쓴다.
  정정 공시는 본표에 정정 후 값이 실리고, 맨 앞 '정정신고(보고)' 블록에 정정 전/후 값이 따로 있다.
  정정 전 값은 숫자 대조에서 맞는 값으로 잡히지 않도록 본표 문단(PRELIM)에 넣지 않고 정정 문단(CORR)에만 둔다.
  문단의 금액에는 단위(조원), 증감률에는 %를 값마다 붙인다. 당기 값은 XBRL 계약 행으로도 낸다(prelim_facts).
- 문단 ID = `{corp_code}-{rcept_no}-{section}-{idx}`(설계 Outside Voice #3, 문서 단위 적재·삭제용).
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.services.evidence.passages import Passage, _key, _text, pack
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


_BLOCK = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>|<P\b[^>]*>(.*?)</P>|<TABLE\b(?!-)[^>]*>(.*?)</TABLE>", re.S)
_THEAD = re.compile(r"<THEAD\b[^>]*>(.*?)</THEAD>", re.S)
_ROW = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.S)
_CELL = re.compile(r"<(TD|TH|TE|TU)\b([^>]*)>(.*?)</\1>", re.S)
_UNIT = re.compile(r"단위\s*[:：]\s*([^)\]]+)")
_SUBHEAD = re.compile(r"[가-하]\.\s*\S")
_SUBHEAD_MAX = 40
_NUMERIC = re.compile(r"[\d,.\s△()%\-]*")
_KI = re.compile(r"제\s*(\d+)\s*기")
_DOT_DATE = re.compile(r"\d{4}\.\d{2}\.\d{2}")


def _attr_int(attrs: str, name: str) -> int:
    m = re.search(rf'{name}\s*=\s*"?(\d+)', attrs, re.I)
    return max(1, int(m.group(1))) if m else 1


def _grid(rows_xml: list[str]) -> list[list[tuple[str, bool]]]:
    """표 행들을 ROWSPAN·COLSPAN을 펼친 격자로. 칸 = (글자, COLSPAN으로 복사된 칸인가)."""
    grid: list[list[tuple[str, bool]]] = []
    carry: dict[int, tuple[str, int]] = {}  # 열 → (글자, 아래로 더 차지할 행 수)
    for r in rows_xml:
        row: dict[int, tuple[str, bool]] = {}
        new_carry: dict[int, tuple[str, int]] = {}
        col = 0
        for _, attrs, frag in _CELL.findall(r):
            text, cs, rs = _text(frag), _attr_int(attrs, "COLSPAN"), _attr_int(attrs, "ROWSPAN")
            while col in carry:
                col += 1
            for k in range(cs):
                row[col + k] = (text, k > 0)
                if rs > 1:
                    new_carry[col + k] = (text, rs - 1)
            col += cs
        for c, (text, left) in carry.items():
            row[c] = (text, False)
            if left > 1:
                new_carry[c] = (text, left - 1)
        carry = new_carry
        grid.append([row.get(i, ("", False)) for i in range(max(row) + 1)] if row else [])
    return grid


def _cover_periods(lines: list[str]) -> dict[str, tuple[str, str]]:
    """재무제표 머리 표의 기간 줄 → {기 번호: (시작, 끝)}. 시점이면 시작 = 끝. 예: '제 58 기 반기말 2026.06.30 현재'."""
    out: dict[str, tuple[str, str]] = {}
    for ln in lines:
        ki, dates = _KI.search(ln), _DOT_DATE.findall(ln)
        if ki and dates:
            out[ki.group(1)] = (dates[0], dates[-1])
    return out


def _three_months(end: str) -> str:
    """'2026.06.30' → 그 분기의 첫날 '2026.04.01'."""
    y, m, _ = end.split(".")
    return f"{y}.{int(m) - 2:02d}.01"


def _with_period(label: str, periods: dict[str, tuple[str, str]]) -> str:
    """열 이름에 머리 표의 실제 기간을 붙인다. '3개월' 열은 끝 날짜가 든 분기만."""
    ki = _KI.search(label)
    if not ki or ki.group(1) not in periods:
        return label
    start, end = periods[ki.group(1)]
    if start == end:
        return f"{label}({end})"
    if "3개월" in label:
        return f"{label}({_three_months(end)}~{end})"
    return f"{label}({start}~{end})"


def _header_labels(grid: list[list[tuple[str, bool]]], n_head: int) -> list[str]:
    """머리 행(여러 줄일 수 있음)을 열마다 위→아래로 이어 붙인 열 이름. 같은 글자가 이어지면 한 번만."""
    width = max(len(r) for r in grid[:n_head])
    out = []
    for c in range(width):
        parts: list[str] = []
        for r in grid[:n_head]:
            t = r[c][0] if c < len(r) else ""
            if t and (not parts or parts[-1] != t):
                parts.append(t)
        out.append(" ".join(parts))
    return out


def table_blocks(section_xml: str) -> list[tuple[str, str]]:
    """절 안의 제목·문단·표 행을 문서 순서대로 펼친다(근거 모드 `blocks()`의 팩트체커판, 출력 형식은 같다).

    `blocks()`와 다른 점(근거 모드 코드는 사전등록 해시 대상이라 고치지 않고 여기서 새로 한다):
    - `<TABLE-GROUP>`을 표로 잘못 잡지 않는다(재무제표 제목·머리 표가 삼켜지던 문제).
    - '가. 요약연결재무정보'처럼 짧은 '가.~하.' 문단을 하위 머리로 보고 '상위 > 하위'로 행 접두에 넣는다.
    - ROWSPAN·COLSPAN을 펼친다. 머리가 여러 줄(THEAD)이면 열마다 이어 붙인다('제 58 기 반기 3개월').
    - 재무제표 머리 표(한 칸짜리 행: 표 이름·기간·단위)에서 캡션·단위와 함께 기 번호별 실제 기간을 읽어 열 이름에
      붙인다('제 58 기 반기 3개월(2026.04.01~2026.06.30)', '제 58 기 반기말(2026.06.30)').
    - 표 안의 기간 행(첫 칸이 비고 나머지가 숫자 아닌 글자, 예: '2026년 6월말')은 행으로 내지 않고 그 아래 행의
      열 이름에 붙인다('제58기(2026년 6월말)'). 표 중간에 다시 나오면 바꾼다.
    """
    out: list[tuple[str, str]] = []
    title, sub, unit, caption = "", "", "", ""
    periods: dict[str, tuple[str, str]] = {}
    for m in _BLOCK.finditer(section_xml):
        t, p, tb = m.groups()
        if t is not None:
            title, sub, unit, caption, periods = _text(t), "", "", "", {}
            out.append(("title", title))
            continue
        heading = f"{title} > {sub}" if sub else title
        if p is not None:
            txt = _text(p)
            if not txt:
                continue
            u = _UNIT.search(txt)
            if u and len(txt) < 60:
                unit = u.group(1).strip()
                continue
            if _SUBHEAD.match(txt):
                if len(txt) <= _SUBHEAD_MAX:  # 짧은 '가.~하.' 문단 = 하위 머리
                    sub = txt
                    out.append(("title", f"{title} > {sub}" if title else sub))
                    continue
                sub = ""  # 본문이 붙은 다음 항목('라. … 당사는 …')이 시작되면 앞 하위 머리는 끝난다
            out.append(("para", txt))
            continue
        thead = _THEAD.search(tb)
        rows_xml = _ROW.findall(tb)
        grid = [r for r in _grid(rows_xml) if any(t for t, _ in r)]
        if not grid:
            continue
        cells = [[t for t, copy in r if t and not copy] for r in grid]
        flat = [c for r in cells for c in r]
        if any(c.startswith("※") for c in flat) and not any(_NUMERIC.fullmatch(c) for c in flat):
            out += [("para", " ".join(r)) for r in cells if r]  # 표로 그린 주석 줄(※ …)은 문단으로
            continue
        if len(grid) == 1 or all(len(c) == 1 for c in cells):  # 캡션·단위 줄(한 행) 또는 재무제표 머리 표
            units = [_UNIT.search(c) for c in flat]
            if any(units):
                unit = next(u for u in units if u).group(1).strip()
                rest = [c for c, u in zip(flat, units) if not u]
                found = _cover_periods(rest)
                if found:
                    periods = found
                    rest = [c for c in rest if not (_KI.search(c) and _DOT_DATE.search(c))]
                caption = " ".join(rest)
            else:
                out.append(("para", " | ".join(flat)))
            continue
        n_head = len(_ROW.findall(thead.group(1))) if thead else 1
        n_head = max(1, min(n_head, len(grid) - 1))
        labels = [_with_period(h, periods) for h in _header_labels(grid, n_head)]
        current = list(labels)
        prefix = f"[{heading} 표" + (f", {caption}" if caption else "") + (f", 단위 {unit}" if unit else "") + "] "
        for r in grid[n_head:]:
            vals = [("" if copy else t) for t, copy in r]
            if len(vals) == len(labels) and not vals[0] and any(vals[1:]) \
                    and not any(_NUMERIC.fullmatch(v) for v in vals[1:] if v):
                current = [labels[0]] + [f"{h}({v})" if v else h for h, v in zip(labels[1:], vals[1:])]
                continue
            if len(vals) == len(current):
                parts = [f"{h}: {v}" if h else v for h, v in zip(current, vals) if v]
            else:
                parts = [v for v in vals if v]
            if parts:
                out.append(("row", prefix + " | ".join(parts)))
        unit, caption, periods = "", "", {}
    return out


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
            for i, text in enumerate(pack(table_blocks(secs[sec]))):
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


def _fmt(v: Decimal | None, suffix: str = "") -> str:
    """값 표기. 금액은 단위(조원), 증감률은 %를 값 바로 뒤에 붙여 숫자 대조가 금액·비율을 섞지 않게 한다."""
    return "-" if v is None else f"{format(v, ',')}{suffix}"


def _span(rep: PrelimReport, key: str) -> str:
    s = rep.periods.get(key)
    return f"{s[0]}~{s[1]}" if s else ""


def prelim_passages(corp_code: str, rcept_no: str, rep: PrelimReport) -> list[Passage]:
    """잠정실적을 문단으로: 계정마다 본표 문단 하나(PRELIM), 정정 공시면 정정 전/후 문단(CORR).

    금액마다 단위(예: 171.50조원), 증감률마다 %(예: 28.11%)를 붙인다. 머리에 '표'가 없어 `numbers.py`가 표 단위를
    적용하지 않으므로 값마다 단위가 있어야 '171.5조원'은 맞고 '171.50% 증가'는 틀리게 대조된다.
    """
    u = rep.unit
    head = f"[{rep.title} {rep.period}({rep.period_start}~{rep.period_end})]"
    by_acc: dict[str, list[str]] = {}
    for f in rep.figures:
        span = _span(rep, "당기누계실적") if f.basis == "누계실적" else ""
        parts = [f"당기실적 {_fmt(f.current, u)}"]
        if f.basis == "당해실적":
            parts += [f"전기실적 {_fmt(f.prior_q, u)}", f"전기대비 증감율 {_fmt(f.qoq_pct, '%')}"]
            parts += [f"전기대비 {f.qoq_turn}"] if f.qoq_turn else []
        parts += [f"전년동기실적 {_fmt(f.prior_y, u)}", f"전년동기대비 증감율 {_fmt(f.yoy_pct, '%')}"]
        parts += [f"전년동기대비 {f.yoy_turn}"] if f.yoy_turn else []
        label = f"{f.account}({f.basis}{', ' + span if span else ''})"
        by_acc.setdefault(f.account, []).append(f"{label}: " + " | ".join(parts))
    out = [Passage(passage_id(corp_code, rcept_no, PRELIM, i), corp_code, rcept_no, PRELIM, i,
                   f"{head} " + " / ".join(lines)) for i, lines in enumerate(by_acc.values())]
    if rep.corrections:
        orig = f"{rep.original_date[:4]}-{rep.original_date[4:6]}-{rep.original_date[6:]}" if rep.original_date else "?"
        items = []
        for c in rep.corrections:
            suf = "%" if "%" in c.group else u
            items.append(("para", f"{c.group.replace('(%)', '')} {c.item}({c.basis}): "
                                  f"정정 전 {_fmt(c.before, suf)} → 정정 후 {_fmt(c.after, suf)}"))
        chunks = pack([("title", f"{rep.title} {rep.period} 정정 공시(원 공시 {orig} 제출분의 값을 정정)")] + items)
        out += [Passage(passage_id(corp_code, rcept_no, CORR, i), corp_code, rcept_no, CORR, i, t)
                for i, t in enumerate(chunks)]
    return out


PRELIM_ACCOUNTS = {"매출액": "ifrs-full_Revenue", "영업이익": "dart_OperatingIncomeLoss",
                   "당기순이익": "ifrs-full_ProfitLoss",
                   "지배기업 소유주지분 순이익": "ifrs-full_ProfitLossAttributableToOwnersOfParent"}


def prelim_fs_div(title: str) -> str:
    """잠정실적 제목으로 연결(CFS)/별도(OFS) 기준. '연결'이 들면 CFS."""
    return "CFS" if "연결" in title else "OFS"


def prelim_facts(corp_code: str, rcept_no: str, rep: PrelimReport, *, rcept_dt: str | None = None,
                 is_correction: bool = False, superseded: bool = False) -> list[dict]:
    """잠정실적의 당기 값을 XBRL 계약 행으로(report_type="preliminary").

    당해실적 = 당기 분기 단독(cumulative=False), 누계실적 = 연초부터 누적(cumulative=True, 1분기는 단독과 같아 뺀다).
    값은 정정 공시면 정정 후 값(본표)이다. 전기·전년동기 값은 정기 XBRL에 있으므로 넣지 않는다.
    rounding_unit은 공시 값의 마지막 자리(예: 171.50조원 → 10^10원)라 대조 쪽이 반올림 허용 폭으로 쓴다.
    """
    from app.services.factcheck.xbrl import ACCOUNTS, UNIT  # 순환 import 없음(xbrl은 parse를 import하지 않는다)

    mult = UNIT_MULTIPLIER[rep.unit]
    out: list[dict] = []
    for f in rep.figures:
        acc = PRELIM_ACCOUNTS.get(f.account)
        if acc is None or f.current is None:
            continue
        if f.basis == "당해실적":
            start, end, cumulative = rep.period_start, rep.period_end, False
        else:
            span = rep.periods.get("당기누계실적")
            if not span or span[0] == rep.period_start:
                continue
            start, end, cumulative = span[0], span[1], True
        out.append({
            "corp_code": corp_code,
            "period": period_label(date.fromisoformat(start), date.fromisoformat(end)),
            "period_start": start, "period_end": end, "value_kind": "duration", "cumulative": cumulative,
            "fs_div": prelim_fs_div(rep.title), "account_id": acc, "account_nm": ACCOUNTS[acc],
            "amount": to_won(f.current, rep.unit), "currency": "KRW", "unit": UNIT,
            "rcept_no": rcept_no, "rcept_dt": rcept_dt or rcept_no[:8], "is_correction": bool(is_correction),
            "report_type": "preliminary", "superseded": bool(superseded),
            "rounding_unit": int(Decimal(1).scaleb(f.current.as_tuple().exponent) * mult),
            "column": "thstrm_add" if cumulative else "thstrm", "sj_div": "PRELIM", "reprt_code": None,
            "bsns_year": start[:4],
        })
    return out
