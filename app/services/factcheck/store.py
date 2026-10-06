# app/services/factcheck/store.py
"""팩트체커 문단 저장소: Qdrant `factcheck_passages` 컬렉션에 문서 단위로 적재·삭제·검색한다.

근거 모드의 `evidence_passages`(회사 단위 적재 — 새 적재에 없는 회사 문단을 모두 지운다)와 섞지 않으려고 새로 뒀다
(설계 D1·Outside Voice #3).
- 문단 ID = `{corp_code}-{rcept_no}-{section}-{idx}`, 포인트 id = 그 uuid5(근거 모드 `point_id` 재사용).
- 적재 단위 = 문서(corp_code, rcept_no) 하나. 같은 문서의 문단만 건너뛰기·덮어쓰기·꼬리 삭제를 한다. 같은 회사의
  다른 문서는 건드리지 않는다. 본문(sha256)이 같고 메타만 바뀌면(예: superseded) 임베딩 없이 payload만 고친다.
  임베딩이 모두 끝난 뒤에 쓰므로 임베딩 도중 실패하면 이전 적재가 그대로 남는다.
- payload(설계 데이터 계약): passage_id·corp_code·rcept_no·report_type·report_nm·period·rcept_dt·section·idx·text.
  확장: sha256(재적재 건너뛰기), is_correction(정정 공시), superseded(같은 기간에 더 늦은 공시가 있음), corp_name,
  fs_div(잠정실적의 연결/별도 기준).
- 검색은 기본으로 현재값만 본다(superseded 문서·CORR 이력 문단 제외, include_history=True면 포함).
- payload 색인: corp_code·rcept_no·period·report_type(keyword).
- 임베딩은 근거 모드와 같은 nomic-embed-text 문서/질문 접두어. 클라이언트(AsyncQdrantClient)·임베딩은 주입한다.

CLI(로컬 전용, Ollama·Qdrant 필요):
    python -m app.services.factcheck.store load [--data lab/data/factcheck] [--prune]  # collect 산출을 적재
    python -m app.services.factcheck.store snapshot                           # 컬렉션 스냅샷 생성
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from qdrant_client.http import models as qm

from app.services.evidence.passages import Passage
from app.services.evidence.retrieve import DOC_PREFIX, QUERY_PREFIX
from app.services.evidence.store import Embed, point_id
from app.services.factcheck.parse import decode, parse_prelim, passage_id, prelim_passages, regular_passages


__all__ = ["COLLECTION", "DocMeta", "FactcheckStore", "LoadResult", "passage_id", "point_id", "QUERY_PREFIX"]

COLLECTION = "factcheck_passages"
REPORT_TYPES = frozenset({"annual", "half", "quarter", "preliminary", "major"})
INDEXED_FIELDS = ("corp_code", "rcept_no", "period", "report_type")
K = 8
UPSERT_BATCH = 64
_SCROLL_PAGE = 256
PAYLOAD_FIELDS = ("passage_id", "corp_code", "corp_name", "rcept_no", "report_type", "report_nm", "period",
                  "rcept_dt", "section", "idx", "text", "sha256", "is_correction", "superseded", "fs_div")
SEARCH_FIELDS = ("passage_id", "corp_code", "rcept_no", "report_type", "report_nm", "period", "rcept_dt",
                 "section", "idx", "text", "is_correction", "superseded", "fs_div")
HISTORY_SECTIONS = ("CORR",)  # 정정 전/후 이력 문단(기본 검색에서 뺀다)


@dataclass(frozen=True)
class DocMeta:
    """적재할 문서 하나의 메타(collect의 documents.json 항목과 같은 이름)."""

    corp_code: str
    rcept_no: str
    report_type: str  # annual | half | quarter | preliminary | major
    report_nm: str
    period: str  # "YYYY" | "YYYYQn" | "YYYYH1"
    rcept_dt: str  # YYYYMMDD
    is_correction: bool = False
    superseded: bool = False
    corp_name: str = ""
    fs_div: str | None = None  # 잠정실적의 연결(CFS)/별도(OFS) 기준. 정기보고서는 None(한 문서에 둘 다 있음)


@dataclass(frozen=True)
class LoadResult:
    """문서 하나 적재 결과: 문단 수, 새로 임베딩한 수, payload만 고친 수, 그대로 둔 수, 지운 꼬리 수."""

    passages: int
    embedded: int
    updated: int
    kept: int
    removed: int


def period_key(period: str) -> tuple[int, int]:
    """기간 이름의 순서 키(끝나는 시점): 'YYYYQ1'(1) < 'YYYYH1'='YYYYQ2'(2) < 'YYYYQ3'(3) < 'YYYYQ4'='YYYY'(4)."""
    y, rest = int(period[:4]), period[4:]
    return y, {"": 4, "H1": 2}.get(rest) or int(rest[1:])


def _doc_filter(corp_code: str, rcept_no: str) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key="corp_code", match=qm.MatchValue(value=corp_code)),
                           qm.FieldCondition(key="rcept_no", match=qm.MatchValue(value=rcept_no))])


def _payload(doc: DocMeta, p: Passage) -> dict:
    return {"passage_id": p.id, "corp_code": doc.corp_code, "corp_name": doc.corp_name, "rcept_no": doc.rcept_no,
            "report_type": doc.report_type, "report_nm": doc.report_nm, "period": doc.period,
            "rcept_dt": doc.rcept_dt, "section": p.section, "idx": p.idx, "text": p.text, "sha256": p.sha256,
            "is_correction": doc.is_correction, "superseded": doc.superseded, "fs_div": doc.fs_div}


class FactcheckStore:
    """`factcheck_passages` 컬렉션 저장소(문서 단위)."""

    def __init__(self, client, embed: Embed, collection: str = COLLECTION):
        self.client, self.embed, self.collection = client, embed, collection

    async def exists(self) -> bool:
        """컬렉션이 있는가."""
        return await self.client.collection_exists(self.collection)

    async def _ensure(self, dim: int) -> None:
        """컬렉션이 없으면 만들고 payload 색인을 단다."""
        if await self.exists():
            return
        await self.client.create_collection(self.collection,
                                            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE))
        for f in INDEXED_FIELDS:
            await self.client.create_payload_index(self.collection, f, qm.PayloadSchemaType.KEYWORD)

    async def _scroll(self, flt: qm.Filter | None, fields: Sequence[str]) -> list[dict]:
        """payload 필드 일부를 전부 읽는다. 컬렉션이 없으면 빈 목록."""
        if not await self.exists():
            return []
        out, offset = [], None
        while True:
            points, offset = await self.client.scroll(self.collection, scroll_filter=flt, limit=_SCROLL_PAGE,
                                                      offset=offset, with_payload=list(fields), with_vectors=False)
            out += [p.payload for p in points]
            if offset is None:
                return out

    async def load_document(self, doc: DocMeta, passages: Sequence[Passage]) -> LoadResult:
        """문서 하나의 문단을 적재한다(같은 문단 건너뛰기, 메타만 바뀐 문단 payload 갱신, 바뀐 문단 덮어쓰기, 꼬리 삭제)."""
        if doc.report_type not in REPORT_TYPES:
            raise ValueError(f"unknown report_type: {doc.report_type}")
        if any(p.corp_code != doc.corp_code or p.rcept_no != doc.rcept_no for p in passages):
            raise ValueError("passages belong to another document")
        have = {p["passage_id"]: p for p in await self._scroll(_doc_filter(doc.corp_code, doc.rcept_no),
                                                              PAYLOAD_FIELDS)}
        new = {p.id: _payload(doc, p) for p in passages}
        embed_ids = [i for i, pl in new.items() if (have.get(i) or {}).get("sha256") != pl["sha256"]]
        meta_ids = [i for i, pl in new.items() if i in have and i not in embed_ids
                    and {k: have[i].get(k) for k in pl} != pl]
        vectors = [list(await self.embed(DOC_PREFIX + new[i]["text"])) for i in embed_ids]  # 쓰기 전에 모두 임베딩
        if vectors:
            await self._ensure(len(vectors[0]))
        points = [qm.PointStruct(id=point_id(i), vector=v, payload=new[i]) for i, v in zip(embed_ids, vectors)]
        for b in range(0, len(points), UPSERT_BATCH):
            await self.client.upsert(self.collection, points=points[b:b + UPSERT_BATCH], wait=True)
        for i in meta_ids:
            await self.client.set_payload(self.collection, payload=new[i], points=[point_id(i)], wait=True)
        stale = sorted(set(have) - set(new))
        if stale:
            await self.client.delete(self.collection, wait=True,
                                     points_selector=qm.PointIdsList(points=[point_id(x) for x in stale]))
        return LoadResult(len(new), len(embed_ids), len(meta_ids), len(new) - len(embed_ids) - len(meta_ids),
                          len(stale))

    async def delete_document(self, corp_code: str, rcept_no: str) -> int:
        """문서 하나의 문단을 모두 지운다. 지운 수를 돌려준다."""
        ids = [p["passage_id"] for p in await self._scroll(_doc_filter(corp_code, rcept_no), ("passage_id",))]
        if ids:
            await self.client.delete(self.collection, wait=True,
                                     points_selector=qm.PointIdsList(points=[point_id(x) for x in ids]))
        return len(ids)

    async def search(self, corp_code: str, query: str, k: int = K, *, periods: Sequence[str] | None = None,
                     report_types: Sequence[str] | None = None, include_history: bool = False) -> list[dict]:
        """질문 임베딩과 cosine 상위 k개 문단. 회사는 필수, 기간·보고서 종류는 고르면 그 안에서만.

        기본은 현재값만: 더 늦은 공시로 대체된 문서(superseded)와 정정 전/후 이력 문단(CORR)을 뺀다.
        이력까지 보려면 include_history=True.
        """
        must = [qm.FieldCondition(key="corp_code", match=qm.MatchValue(value=corp_code))]
        must_not = [] if include_history else [
            qm.FieldCondition(key="superseded", match=qm.MatchValue(value=True)),
            qm.FieldCondition(key="section", match=qm.MatchAny(any=list(HISTORY_SECTIONS)))]
        if periods:
            must.append(qm.FieldCondition(key="period", match=qm.MatchAny(any=list(periods))))
        if report_types:
            must.append(qm.FieldCondition(key="report_type", match=qm.MatchAny(any=list(report_types))))
        qv = list(await self.embed(QUERY_PREFIX + query))
        res = await self.client.query_points(self.collection, query=qv,
                                             query_filter=qm.Filter(must=must, must_not=must_not or None),
                                             limit=k, with_payload=True, with_vectors=False)
        return [{**{f: p.payload.get(f) for f in SEARCH_FIELDS}, "score": p.score} for p in res.points]

    async def documents(self) -> list[dict]:
        """적재된 문서 목록(회사·접수번호 순): 메타와 문단 수."""
        out: dict[tuple[str, str], dict] = {}
        for p in await self._scroll(None, ("corp_code", "rcept_no", "report_type", "report_nm", "period",
                                           "rcept_dt", "is_correction", "superseded", "fs_div")):
            d = out.setdefault((p["corp_code"], p["rcept_no"]), {**p, "passages": 0})
            d["passages"] += 1
        return [out[k] for k in sorted(out)]

    async def latest_period(self, corp_code: str) -> str | None:
        """그 회사의 대체되지 않은 문서(잠정실적 포함) 중 가장 늦은 기간. 없으면 None.

        같은 시점에 끝나는 기간(반기 'YYYYH1'와 잠정실적 'YYYYQ2')은 접수일이 늦은 쪽을 돌려준다.
        """
        docs = [d for d in await self.documents() if d["corp_code"] == corp_code and not d.get("superseded")
                and d.get("period")]
        if not docs:
            return None
        return max(docs, key=lambda d: (period_key(d["period"]), d["rcept_dt"], d["rcept_no"]))["period"]

    async def snapshot(self) -> str:
        """컬렉션 스냅샷을 만들고 이름을 돌려준다(서버 모드 Qdrant에서만)."""
        snap = await self.client.create_snapshot(self.collection, wait=True)
        return snap.name

    async def aclose(self) -> None:
        """클라이언트를 닫는다."""
        await self.client.close()


# ── collect 산출 → 문단 ────────────────────────────────────────────────────────

def document_passages(data_dir: Path, d: dict) -> list[Passage]:
    """documents.json 항목 하나의 원문을 문단으로(정기보고서 I·II·III절, 잠정실적 본표·정정)."""
    raw = decode((Path(data_dir) / d["path"]).read_bytes())
    if d["report_type"] == "preliminary":
        return prelim_passages(d["corp_code"], d["rcept_no"], parse_prelim(raw))
    return regular_passages(d["corp_code"], d["rcept_no"], raw)


def doc_meta(d: dict) -> DocMeta:
    """documents.json 항목 → DocMeta."""
    return DocMeta(**{k: d[k] for k in DocMeta.__dataclass_fields__ if k in d})


async def load_all(st: FactcheckStore, data_dir: Path, *, prune: bool = False) -> dict:
    """documents.json의 모든 문서를 적재한다. 기간을 못 읽은 문서·분해 실패 문서는 건너뛰고 센다.

    - 실패한 문서에 예전 적재본이 있으면 지운다(cleared). 정정 공시를 이번에 못 읽었는데 옛 문단이 남아 현재값처럼
      검색되지 않게 한다.
    - prune=True면 documents.json에 있는 회사의 적재 문서 중 목록에서 빠진 문서(예: 정정 공시로 최종본에서 밀린
      정기보고서)를 지운다(pruned). 부분 목록으로 돌려 유효 문서를 지우는 일을 막으려고 기본은 끈다.
    """
    docs = json.loads((Path(data_dir) / "documents.json").read_text())
    summary = {"documents": 0, "passages": 0, "embedded": 0, "updated": 0, "removed": 0, "pruned": [],
               "cleared": {}, "failed": []}
    if prune:
        listed = {(d["corp_code"], d["rcept_no"]) for d in docs}
        corps = {c for c, _ in listed}
        for have in await st.documents():
            key = (have["corp_code"], have["rcept_no"])
            if have["corp_code"] in corps and key not in listed:
                await st.delete_document(*key)
                summary["pruned"].append(have["rcept_no"])
    for d in docs:
        error = None
        if not d.get("period") or d.get("parse_error"):
            error = d.get("parse_error", "no period")
        else:
            try:
                res = await st.load_document(doc_meta(d), document_passages(data_dir, d))
            except ValueError as exc:
                error = str(exc)
        if error is not None:
            summary["failed"].append({"rcept_no": d["rcept_no"], "error": error})
            n = await st.delete_document(d["corp_code"], d["rcept_no"])
            if n:
                summary["cleared"][d["rcept_no"]] = n
            continue
        summary["documents"] += 1
        for k in ("passages", "embedded", "updated", "removed"):
            summary[k] += asdict(res)[k]
    return summary


def default_store() -> FactcheckStore:
    """앱 설정의 Qdrant(QDRANT_URL)·Ollama 임베딩으로 만든 저장소."""
    from qdrant_client import AsyncQdrantClient

    from app.config import settings
    from app.services.evidence.store import ollama_embed

    # 근거 모드 store.default_store와 같은 이유로 버전 확인을 끈다(서버 v1.13.4, 클라이언트 1.19.1).
    return FactcheckStore(AsyncQdrantClient(url=settings.QDRANT_URL, check_compatibility=False), ollama_embed())


async def _amain(args) -> int:
    st = default_store()
    try:
        if args.cmd == "load":
            summary = await load_all(st, args.data, prune=args.prune)
            summary["collection_documents"] = len(await st.documents())
            print(json.dumps(summary, ensure_ascii=False))
            return 1 if summary["failed"] else 0
        print(json.dumps({"snapshot": await st.snapshot()}, ensure_ascii=False))
        return 0
    finally:
        await st.aclose()


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점(load·snapshot)."""
    ap = argparse.ArgumentParser(description="팩트체커 문단 적재(Qdrant factcheck_passages)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ld = sub.add_parser("load", help="collect 산출(documents.json·docs/)을 문단으로 적재")
    ld.add_argument("--data", type=Path, default=Path("lab/data/factcheck"))
    ld.add_argument("--prune", action="store_true",
                    help="목록(documents.json)에 있는 회사의 적재 문서 중 목록에서 빠진 문서를 지운다")
    sub.add_parser("snapshot", help="컬렉션 스냅샷 생성")
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
