# app/services/factcheck/corp_names.py
"""상장사명 사전: OpenDART 고유번호 목록(CORPCODE.xml)의 상장사 → JSON, 정규화한 이름으로 정확히 찾기.

'다른 회사가 주어' 판별(설계 D6, T2 scope.py)이 쓴다. 이 모듈은 사전과 정규화만 만든다.
- 정규화: NFKC(전각 → 반각), 법인 표기((주)·㈜·주식회사) 제거, 공백 제거, 라틴 문자 대문자.
- 찾기는 정규화한 전체 이름이 같을 때만이다. 'SK'와 'SK하이닉스'는 다른 키라 앞부분이 같아도 섞이지 않는다
  (부분 일치 방지). 문장 속에서 이름을 찾는 규칙(가장 긴 이름 우선 등)은 T2에서 정한다.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from app.services.evidence.dart import parse_corp_codes

_CORP_MARKS = re.compile(r"\(주\)|\(유\)|주식회사|유한회사")


def normalize(name: str) -> str:
    """회사명 비교 키: NFKC → 법인 표기·공백 제거 → 라틴 대문자. 예: '(주) sk 하이닉스' → 'SK하이닉스'."""
    s = unicodedata.normalize("NFKC", name)  # ㈜ → (주), 전각 → 반각
    s = _CORP_MARKS.sub("", s)
    s = re.sub(r"\s+", "", s)
    return s.upper()


def build(corp_code_xml: bytes) -> list[dict]:
    """CORPCODE.xml에서 상장사(종목코드 있음)만 사전 항목으로: corp_code·corp_name·stock_code·norm."""
    return [{"corp_code": c.corp_code, "corp_name": c.corp_name, "stock_code": c.stock_code,
             "norm": normalize(c.corp_name)} for c in parse_corp_codes(corp_code_xml)]


def save(path: Path, entries: list[dict]) -> None:
    """사전 JSON을 쓴다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=1))


def load(path: Path) -> list[dict]:
    """사전 JSON을 읽는다."""
    return json.loads(Path(path).read_text())


def index(entries: list[dict]) -> dict[str, list[str]]:
    """정규화 이름 → corp_code 목록(같은 이름의 상장사가 둘 이상일 수 있어 목록)."""
    out: dict[str, list[str]] = {}
    for e in entries:
        out.setdefault(e["norm"], []).append(e["corp_code"])
    return out


def lookup(idx: dict[str, list[str]], name: str) -> list[str]:
    """이름을 정규화해 정확히 같은 키의 corp_code 목록. 없으면 빈 목록(부분 일치는 하지 않는다)."""
    return list(idx.get(normalize(name), []))
