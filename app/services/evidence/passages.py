# app/services/evidence/passages.py
"""DART 사업보고서 XML에서 지정 절을 꺼내 근거 후보 문단으로 나눈다.

DART XML은 SECTION-1/2의 TITLE, P, TABLE(셀은 TD·TH·TE·TU)로 이뤄진다.
단위는 데이터 표 앞의 한 칸짜리 표나 문단에 "(단위 : 억원)"처럼 따로 적힌다.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

MAX_CHARS = 600
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_UNIT = re.compile(r"단위\s*[:：]\s*([^)\]]+)")
_SENT_END = re.compile(r"(?<=[.!?])\s+")
_BLOCK = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>|<P\b[^>]*>(.*?)</P>|<TABLE\b[^>]*>(.*?)</TABLE>", re.S)
_ROW = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.S)
_CELL = re.compile(r"<(TD|TH|TE|TU)\b[^>]*>(.*?)</\1>", re.S)


@dataclass(frozen=True)
class Passage:
    """근거 후보 문단 하나."""

    id: str
    corp_code: str
    rcept_no: str
    section: str
    idx: int
    text: str

    @property
    def sha256(self) -> str:
        """본문 SHA-256(커밋하는 매니페스트용)."""
        return hashlib.sha256(self.text.encode()).hexdigest()


def _text(fragment: str) -> str:
    """태그를 지우고 엔티티를 풀고 공백을 하나로 줄인다."""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _title(block: str) -> str:
    m = re.search(r"<TITLE\b[^>]*>(.*?)</TITLE>", block, re.S)
    return _text(m.group(1)) if m else ""


_ROMAN = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV"}


def _key(title: str) -> str:
    """제목 비교 키: 공백을 지우고 유니코드 로마 숫자를 ASCII로 바꾼다."""
    for u, a in _ROMAN.items():
        title = title.replace(u, a)
    return title.replace(" ", "")


def sections(xml: str) -> dict[str, str]:
    """'I. 회사의 개요 > 1. 회사의 개요'(I1)와 'II. 사업의 내용'(II) 블록을 찾는다. 제목 공백은 무시한다."""
    out: dict[str, str] = {}
    for m in re.finditer(r"<SECTION-1\b.*?</SECTION-1>", xml, re.S):
        block = m.group(0)
        title = _key(_title(block))
        if title.startswith("II.사업의내용"):
            out["II"] = block
        elif title.startswith("I.회사의개요"):
            for m2 in re.finditer(r"<SECTION-2\b.*?</SECTION-2>", block, re.S):
                if _key(_title(m2.group(0))).startswith("1.회사의개요"):
                    out["I1"] = m2.group(0)
                    break
    return out


def blocks(section_xml: str) -> list[tuple[str, str]]:
    """절 안의 제목·문단·표 행을 문서 순서대로 펼친다. 표 행에는 제목·캡션·단위·열 머리글을 붙인다.

    한 행짜리 표는 캡션·단위 줄로 보고 다음 데이터 표에만 붙인다(데이터 표 하나가 쓰면 비운다).
    """
    out: list[tuple[str, str]] = []
    heading, unit, caption = "", "", ""
    for m in _BLOCK.finditer(section_xml):
        t, p, tb = m.groups()
        if t is not None:
            heading, unit, caption = _text(t), "", ""
            out.append(("title", heading))
            continue
        if p is not None:
            txt = _text(p)
            if not txt:
                continue
            u = _UNIT.search(txt)
            if u and len(txt) < 60:
                unit = u.group(1).strip()
                continue
            out.append(("para", txt))
            continue
        rows = [[_text(c) for _, c in _CELL.findall(r)] for r in _ROW.findall(tb)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        if len(rows) == 1:
            cells = [c for c in rows[0] if c]
            units = [_UNIT.search(c) for c in cells]
            if any(units):
                unit = next(u for u in units if u).group(1).strip()
                caption = " ".join(c for c, u in zip(cells, units) if not u)
            else:
                out.append(("para", " | ".join(cells)))
            continue
        header = rows[0]
        prefix = f"[{heading} 표" + (f", {caption}" if caption else "") + (f", 단위 {unit}" if unit else "") + "] "
        for r in rows[1:]:
            cells = [f"{h}: {c}" if h else c for h, c in zip(header, r)] if len(r) == len(header) else r
            out.append(("row", prefix + " | ".join(cells)))
        unit, caption = "", ""
    return out


def pack(blocks: list[tuple[str, str]], max_chars: int = MAX_CHARS) -> list[str]:
    """제목 경계에서 끊고, 문단·표 행을 max_chars 이하로 묶는다. 한 문장·한 행이 넘치면 그대로 둔다."""
    units: list[tuple[str, str]] = []
    for kind, txt in blocks:
        if kind == "para" and len(txt) > max_chars:
            units += [("para", s) for s in _SENT_END.split(txt) if s]
        else:
            units.append((kind, txt))
    chunks: list[str] = []
    cur: list[str] = []
    heading = ""
    for kind, txt in units:
        if kind == "title":
            if cur:
                chunks.append(" ".join(cur))
                cur = []
            heading = txt
            continue
        if cur and len(" ".join(cur)) + 1 + len(txt) > max_chars:
            chunks.append(" ".join(cur))
            cur = []
        if not cur and kind == "para" and heading:
            txt = f"[{heading}] {txt}"
        cur.append(txt)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def build_passages(corp_code: str, rcept_no: str, xml: str) -> list[Passage]:
    """지정 절 두 개를 문단 목록으로 만든다. 하나라도 없으면 ValueError."""
    secs = sections(xml)
    missing = [s for s in ("I1", "II") if s not in secs]
    if missing:
        raise ValueError(f"missing sections: {missing}")
    out: list[Passage] = []
    for sec in ("I1", "II"):
        for i, text in enumerate(pack(blocks(secs[sec]))):
            out.append(Passage(f"{corp_code}-{sec}-{i:04d}", corp_code, rcept_no, sec, i, text))
    return out
