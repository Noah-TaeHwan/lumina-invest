# app/services/evidence/dart.py
"""OpenDART 공시 수집: 상장사 목록, 사업보고서 접수번호 선택, 원문 XML 다운로드.

키는 쿼리 파라미터로만 보낸다. HTTP 오류는 URL(키 포함)이 섞이지 않은 메시지로 바꾼다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = "https://opendart.fss.or.kr/api"


@dataclass(frozen=True)
class Corp:
    """상장사 한 곳(OpenDART 고유번호·이름·종목코드)."""

    corp_code: str
    corp_name: str
    stock_code: str


def load_dart_key() -> str:
    """DART_API_KEY 환경 변수, 없으면 소유자 전용(0600) ~/.config/opendart/api_key에서 읽는다."""
    key = os.environ.get("DART_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".config/opendart/api_key"
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PermissionError(f"{path} 권한은 0600이어야 한다")
    return path.read_text().strip()


def _get(client: httpx.Client, path: str, params: dict) -> httpx.Response:
    """GET 요청. 실패 메시지에 URL(키 포함)을 넣지 않는다."""
    try:
        resp = client.get(f"{BASE}/{path}", params=params)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"DART {path} {type(exc).__name__}") from None
    return resp


def _zip_member(content: bytes) -> bytes:
    """zip 안에서 가장 큰 .xml 파일 본문을 꺼낸다. zip이 아니면 ValueError."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ValueError(f"DART 응답이 zip이 아니다: {content[:120]!r}") from None
    names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    if not names:
        raise ValueError("zip 안에 xml이 없다")
    return zf.read(max(names, key=lambda n: zf.getinfo(n).file_size))


def _json(client: httpx.Client, path: str, params: dict) -> dict:
    """JSON API 호출. 상태 000(정상)·013(데이터 없음)만 허용한다."""
    data = _get(client, path, params).json()
    if data.get("status") not in ("000", "013"):
        raise RuntimeError(f"DART {path} status {data.get('status')}: {data.get('message')}")
    return data


def parse_corp_codes(xml_bytes: bytes) -> list[Corp]:
    """CORPCODE.xml에서 종목코드가 있는(상장) 회사만 꺼낸다."""
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.iter("list"):
        stock = (e.findtext("stock_code") or "").strip()
        if stock:
            out.append(Corp(e.findtext("corp_code").strip(), e.findtext("corp_name").strip(), stock))
    return out


def fetch_corp_codes(client: httpx.Client, key: str) -> bytes:
    """전체 고유번호 zip을 받아 CORPCODE.xml 본문을 돌려준다."""
    return _zip_member(_get(client, "corpCode.xml", {"crtfc_key": key}).content)


def list_annual_reports(client: httpx.Client, key: str, corp_code: str,
                        bgn: str = "20260101", end: str = "20261001") -> list[dict]:
    """정기공시(A) 목록. 데이터가 없으면 빈 목록."""
    data = _json(client, "list.json", {"crtfc_key": key, "corp_code": corp_code, "bgn_de": bgn,
                                       "end_de": end, "pblntf_ty": "A", "page_count": 100})
    return data.get("list", [])


def pick_annual_report(items: list[dict], period: str = "2025.12", until: str = "20261001") -> dict | None:
    """기준일까지 제출된 해당 기간 사업보고서 중 최종본(정정 포함)을 고른다."""
    cands = [r for r in items if f"사업보고서 ({period})" in r["report_nm"] and r["rcept_dt"] <= until]
    return max(cands, key=lambda r: (r["rcept_dt"], r["rcept_no"])) if cands else None


def company_info(client: httpx.Client, key: str, corp_code: str) -> dict:
    """기업개황(업종코드 induty_code 포함)."""
    return _json(client, "company.json", {"crtfc_key": key, "corp_code": corp_code})


def download_document(client: httpx.Client, key: str, rcept_no: str, dest_dir: Path, ledger: Path) -> Path:
    """공시 원문 XML을 저장하고 원장에 SHA-256을 남긴다. 이미 있으면 다시 받지 않는다."""
    path = Path(dest_dir) / f"{rcept_no}.xml"
    if path.exists():
        return path
    data = _zip_member(_get(client, "document.xml", {"crtfc_key": key, "rcept_no": rcept_no}).content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    Path(ledger).parent.mkdir(parents=True, exist_ok=True)
    with Path(ledger).open("a") as f:
        f.write(json.dumps({"rcept_no": rcept_no, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                            "downloaded_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    return path
