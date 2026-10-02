"""근거 문단 저장소: Qdrant `evidence_passages` 컬렉션 적재와 검색(A-2 spec 결정 4-3·6-4, P3).

- 벡터는 nomic-embed-text 문서 임베딩(`retrieve.DOC_PREFIX` + 본문), 거리는 cosine이다. 평가(lab/evidence의
  embed·retrieve)와 같은 모델·접두어라 같은 질문이면 메모리 내 `retrieve.top_k`와 같은 문단·순위가 나온다.
  단, 점수가 같은(동점) 문단끼리의 순서는 다를 수 있다(로컬 실데이터 확인: 주장 1,111건 중 집합 1,111건 일치,
  순서 1,108건 일치, 3건은 동점 순서만 다름).
- 포인트 id는 passage_id의 uuid5다. 다시 적재하면 같은 문단을 덮어쓴다.
- 적재는 회사 단위로 한다: (rcept_no, sha256)가 같은 문단은 임베딩을 건너뛰고, 바뀐 문단만 임베딩해 덮어쓰고,
  새 보고서에 없는 문단(정정으로 줄어든 꼬리)은 지운다. 임베딩이 모두 끝난 뒤에 쓰므로 임베딩 도중 실패하면
  이전 적재가 그대로 남는다. upsert 도중 실패하면 한 회사 안에 rcept_no가 섞일 수 있다. 로더를 다시 돌리면
  고쳐지고, 로더는 그 회사를 실패로 세어 종료 코드 1로 알린다.
- 임베딩 함수(async text -> vector)와 Qdrant 클라이언트(AsyncQdrantClient)는 주입한다. 테스트는 :memory:를 쓴다.
- 앱 시작 시 wire()는 EVIDENCE_CHAT_ENABLED만 보고 저장소를 근거 모드 API에 연결한다. Qdrant·컬렉션 상태는
  보지 않는다: 없으면 search()·companies()가 예외를 내고 라우트가 503으로 바꾼다. 나중에 적재되면 재시작 없이
  검색된다. 앱은 컬렉션을 만들지 않는다(빈 컬렉션이 생기면 "저장소 없음" 503이 "문단 없음" 422로 바뀐다).
- 질문 임베딩은 QUERY_EMBED_TIMEOUT_S(10초) 안에 끝나야 한다(OLLAMA_TIMEOUT 300초를 요청 경로에 쓰지 않게).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from qdrant_client.http import models as qm

from app.config import settings
from app.services.evidence.dart import Corp
from app.services.evidence.passages import Passage
from app.services.evidence.retrieve import DOC_PREFIX, QUERY_PREFIX

log = logging.getLogger("app.evidence.store")

COLLECTION = "evidence_passages"
EMBED_MODEL = "nomic-embed-text"  # 평가와 같은 모델. 앱 EMBED_MODEL 설정 변경에 끌려가지 않게 따로 둔다
K = 8
UPSERT_BATCH = 64
QUERY_EMBED_TIMEOUT_S = 10.0
_SCROLL_PAGE = 256
_NS = uuid.UUID("5d0f3c55-0d47-4c43-9a3e-7c1e2b8a6f10")  # passage_id → 포인트 id 이름공간(바꾸면 재적재 필요)
SEARCH_FIELDS = ("passage_id", "rcept_no", "section", "idx", "sha256", "text")
SKIP_FIELDS = ("passage_id", "rcept_no", "sha256")  # 재적재 건너뛰기 판단에 필요한 것만
COMPANY_FIELDS = ("corp_code", "corp_name", "stock_code", "rcept_no")

Embed = Callable[[str], Awaitable[Sequence[float]]]


def point_id(passage_id: str) -> str:
    """passage_id에서 결정적으로 만든 포인트 id(UUID 문자열)."""
    return str(uuid.uuid5(_NS, passage_id))


def _corp_filter(corp_code: str) -> qm.Filter:
    return qm.Filter(must=[qm.FieldCondition(key="corp_code", match=qm.MatchValue(value=corp_code))])


@dataclass(frozen=True)
class LoadResult:
    """회사 하나 적재 결과: 문단 수, 새로 임베딩한 수, 그대로 둔 수, 지운 수."""

    passages: int
    embedded: int
    kept: int
    removed: int


class PassageStore:
    def __init__(self, client, embed: Embed, collection: str = COLLECTION,
                 query_timeout: float = QUERY_EMBED_TIMEOUT_S):
        self.client, self.embed, self.collection = client, embed, collection
        self.query_timeout = query_timeout

    async def exists(self) -> bool:
        """컬렉션이 있는가(로더용. 검색 경로는 확인하지 않고 실패를 예외로 받는다)."""
        return await self.client.collection_exists(self.collection)

    async def _ensure(self, dim: int) -> None:
        if await self.exists():
            return
        await self.client.create_collection(self.collection,
                                            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE))
        await self.client.create_payload_index(self.collection, "corp_code", qm.PayloadSchemaType.KEYWORD)

    async def _scroll(self, flt: qm.Filter | None, fields: Sequence[str]) -> list[dict]:
        """payload 필드 일부만 전부 읽는다. 컬렉션이 없으면 Qdrant 예외가 그대로 난다."""
        out, offset = [], None
        while True:
            points, offset = await self.client.scroll(self.collection, scroll_filter=flt, limit=_SCROLL_PAGE,
                                                      offset=offset, with_payload=list(fields), with_vectors=False)
            out += [p.payload for p in points]
            if offset is None:
                return out

    async def existing(self, corp_code: str) -> dict[str, dict]:
        """그 회사의 적재된 문단: passage_id → {passage_id, rcept_no, sha256}. 컬렉션이 없으면 빈 dict."""
        if not await self.exists():
            return {}
        return {p["passage_id"]: p for p in await self._scroll(_corp_filter(corp_code), SKIP_FIELDS)}

    async def load(self, corp: Corp, passages: Sequence[Passage]) -> LoadResult:
        """회사 하나의 문단을 적재한다(같은 문단 건너뛰기, 바뀐 문단 덮어쓰기, 사라진 문단 삭제)."""
        if any(p.corp_code != corp.corp_code for p in passages):
            raise ValueError("passages belong to another corp")
        have = await self.existing(corp.corp_code)
        todo = [p for p in passages
                if (have.get(p.id) or {}).get("rcept_no") != p.rcept_no or have[p.id].get("sha256") != p.sha256]
        vectors = [list(await self.embed(DOC_PREFIX + p.text)) for p in todo]  # 쓰기 전에 모두 임베딩
        if vectors:
            await self._ensure(len(vectors[0]))
        points = [qm.PointStruct(id=point_id(p.id), vector=v,
                                 payload={"passage_id": p.id, "corp_code": p.corp_code, "rcept_no": p.rcept_no,
                                          "section": p.section, "idx": p.idx, "sha256": p.sha256, "text": p.text,
                                          "corp_name": corp.corp_name, "stock_code": corp.stock_code})
                  for p, v in zip(todo, vectors)]
        for i in range(0, len(points), UPSERT_BATCH):
            await self.client.upsert(self.collection, points=points[i:i + UPSERT_BATCH], wait=True)
        stale = sorted(set(have) - {p.id for p in passages})
        if stale:
            await self.client.delete(self.collection, wait=True,
                                     points_selector=qm.PointIdsList(points=[point_id(x) for x in stale]))
        return LoadResult(len(passages), len(todo), len(passages) - len(todo), len(stale))

    async def search(self, corp_code: str, question: str, k: int = K) -> list[dict]:
        """질문 임베딩과 cosine 상위 k개 문단(그 회사만). 근거 모드 API의 PassageSearch 형태."""
        qv = list(await asyncio.wait_for(self.embed(QUERY_PREFIX + question), self.query_timeout))
        res = await self.client.query_points(self.collection, query=qv, query_filter=_corp_filter(corp_code),
                                             limit=k, with_payload=True, with_vectors=False)
        return [{f: p.payload[f] for f in SEARCH_FIELDS} for p in res.points]

    async def companies(self) -> list[dict]:
        """적재된 회사 목록(회사명 순): 회사명·종목코드·접수번호·문단 수."""
        out: dict[str, dict] = {}
        for p in await self._scroll(None, COMPANY_FIELDS):
            c = out.setdefault(p["corp_code"], {"corp_code": p["corp_code"], "corp_name": p.get("corp_name", ""),
                                                "stock_code": p.get("stock_code", ""), "rcept_no": p["rcept_no"],
                                                "passages": 0})
            c["passages"] += 1
        return sorted(out.values(), key=lambda c: (c["corp_name"], c["corp_code"]))

    async def aclose(self) -> None:
        await self.client.close()


# ── 기본 구성과 앱 연결 ─────────────────────────────────────────────────────────

def ollama_embed(model: str = EMBED_MODEL) -> Embed:
    """앱의 Ollama(OLLAMA_BASE_URL)로 임베딩하는 함수."""
    from app.lib.ollama import OllamaClient

    client = OllamaClient(settings.OLLAMA_BASE_URL, settings.OLLAMA_TIMEOUT)

    async def embed(text: str) -> list[float]:
        vec = await client.embed(model, text)
        if not vec:
            raise RuntimeError("empty embedding")
        return vec
    return embed


def default_store() -> PassageStore:
    from qdrant_client import AsyncQdrantClient

    return PassageStore(AsyncQdrantClient(url=settings.QDRANT_URL), ollama_embed())


_wired: PassageStore | None = None


async def wire(store: PassageStore | None = None) -> bool:
    """EVIDENCE_CHAT_ENABLED이면 저장소를 근거 모드 API에 연결한다(Qdrant 상태는 보지 않는다). 연결했으면 True."""
    global _wired
    from app.routes import evidence

    if not settings.EVIDENCE_CHAT_ENABLED:
        return False
    _wired = store or default_store()
    evidence.set_passage_store(_wired)
    return True


async def unwire() -> None:
    """lifespan 종료 시 부른다. 연결을 끊고 클라이언트를 닫는다."""
    global _wired
    from app.routes import evidence

    s, _wired = _wired, None
    evidence.set_passage_store(None)
    if s is not None:
        await s.aclose()
