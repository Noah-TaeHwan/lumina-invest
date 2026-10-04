"""LangChain LCEL 기반 RAG 파이프라인.

구성:
  OllamaEmbeddings  →  QdrantVectorStore  →  similarity_search
  LCEL 체인: retriever | format_docs | prompt | llm | StrOutputParser
"""
from __future__ import annotations
import json
import logging
import uuid

from langchain_ollama import OllamaEmbeddings
from langchain_qdrant import QdrantVectorStore
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from langchain_ollama import ChatOllama
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams

from app.config import settings

log = logging.getLogger("app.rag")

# 비동기 경로는 QdrantVectorStore를 쓰지 않는다: QdrantVectorStore는 동기 QdrantClient만 받아서
# AsyncQdrantClient를 넘기면 생성자에서 예외가 나고, 예전 코드는 그 예외를 빈 결과로 삼켜
# 검색은 늘 [], 저장은 늘 0이었다. AsyncQdrantClient를 직접 부르고 임베딩만 LangChain을 쓴다.
# 저장 payload는 LangChain 배치(page_content + metadata)를 따른다(build_rag_chain이 읽는 모양).
CONTENT_KEY = QdrantVectorStore.CONTENT_KEY
METADATA_KEY = QdrantVectorStore.METADATA_KEY


# ── 내부 팩토리 ───────────────────────────────────────────────────────────────

def _make_embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(
        base_url=settings.OLLAMA_BASE_URL,
        model=settings.EMBED_MODEL,
    )


async def _get_or_create_collection(client: AsyncQdrantClient, collection: str) -> None:
    """Qdrant 컬렉션이 없으면 nomic-embed-text 기준 dim=768로 생성한다.

    존재 여부를 묻는다: 조회 실패(연결 오류 등)를 '없음'으로 보고 만들러 가지 않는다."""
    if not await client.collection_exists(collection):
        await client.create_collection(
            collection,
            vectors_config=VectorParams(size=768, distance=Distance.COSINE),
        )


def _fail(event: str, collection: str, exc: Exception) -> None:
    """실패를 빈 결과로 바꾸지 않는다: 로그를 남기고 호출부가 예외를 받는다(질문·예외 메시지는 남기지 않는다)."""
    log.error(json.dumps({"event": event, "collection": collection, "error": type(exc).__name__}))


def _hit(payload: dict, score: float) -> dict:
    meta = payload.get(METADATA_KEY) or {}
    return {
        "text":   payload.get(CONTENT_KEY, ""),
        "url":    meta.get("url", ""),
        "title":  meta.get("title", ""),
        "source": meta.get("source", ""),
        "score":  float(score),
    }


def _source_filter(source: str) -> Filter:
    return Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))])


# ── 공개 함수 ─────────────────────────────────────────────────────────────────

async def rag_search(
    query:      str,
    top_k:      int  = 5,
    collection: str | None = None,
    filter_source: str | None = None,
) -> list[dict]:
    """
    Qdrant에서 유사 문서를 검색한다(임베딩은 LangChain OllamaEmbeddings).

    Args:
        query:         검색 쿼리
        top_k:         반환할 최대 문서 수
        collection:    Qdrant 컬렉션명 (None이면 settings.QDRANT_COLLECTION 사용)
        filter_source: 특정 source만 필터링 (예: "upload", "github:...")

    Returns:
        [{"text": ..., "url": ..., "title": ..., "source": ..., "score": ...}, ...]

    Raises:
        Qdrant·임베딩 오류를 그대로 올린다(빈 결과와 구별되게). 로그는 여기서 남긴다.
    """
    coll = collection or settings.QDRANT_COLLECTION
    client = AsyncQdrantClient(url=settings.QDRANT_URL)
    try:
        await _get_or_create_collection(client, coll)
        vector = await _make_embeddings().aembed_query(query)
        res = await client.query_points(
            coll,
            query=vector,
            limit=top_k,
            query_filter=_source_filter(filter_source) if filter_source else None,
            with_payload=True,
        )
        return [_hit(p.payload or {}, p.score) for p in res.points]
    except Exception as exc:
        _fail("rag_search_failed", coll, exc)
        raise
    finally:
        await client.close()


async def store_chunks(
    chunks:     list[str],
    metadata:   dict,
    collection: str | None = None,
) -> int:
    """
    텍스트 청크 목록을 OllamaEmbeddings로 임베딩하여 Qdrant에 저장한다.

    Returns:
        실제 저장된 청크 수 (실패하면 예외를 올린다)
    """
    if not chunks:
        return 0

    coll = collection or settings.QDRANT_COLLECTION
    client = AsyncQdrantClient(url=settings.QDRANT_URL)
    try:
        await _get_or_create_collection(client, coll)
        vectors = await _make_embeddings().aembed_documents(chunks)
        points = [
            PointStruct(id=uuid.uuid4().hex, vector=v,
                        payload={CONTENT_KEY: chunk, METADATA_KEY: dict(metadata)})
            for chunk, v in zip(chunks, vectors)
        ]
        await client.upsert(collection_name=coll, points=points)
        return len(points)
    except Exception as exc:
        _fail("rag_store_failed", coll, exc)
        raise
    finally:
        await client.close()


def build_rag_chain(collection: str | None = None):
    """
    LCEL 기반 RAG 체인을 반환한다.

    사용 예:
        chain = build_rag_chain()
        answer = await chain.ainvoke({"question": "..."})
    """
    coll = collection or settings.QDRANT_COLLECTION

    # 동기 Qdrant 클라이언트 (LCEL retriever는 sync 인터페이스 사용)
    from qdrant_client import QdrantClient
    sync_client = QdrantClient(url=settings.QDRANT_URL)

    vector_store = QdrantVectorStore(
        client=sync_client,
        collection_name=coll,
        embedding=_make_embeddings(),
    )
    retriever = vector_store.as_retriever(search_kwargs={"k": settings.TOP_K})

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "너는 금융 AI 어시스턴트다. 아래 참고 문서를 바탕으로 질문에 한국어로 답하라.\n\n"
            "[참고 문서]\n{context}",
        ),
        ("human", "{question}"),
    ])

    llm = ChatOllama(
        base_url=settings.OLLAMA_BASE_URL,
        model=settings.LLM_MODEL,
        temperature=0.2,
        num_predict=2048,
    )

    def format_docs(docs: list[Document]) -> str:
        return "\n\n".join(
            f"[{d.metadata.get('title', '문서')}]\n{d.page_content}" for d in docs
        )

    chain = (
        {"context": retriever | RunnableLambda(format_docs), "question": RunnablePassthrough()}
        | prompt
        | llm
        | StrOutputParser()
    )
    return chain


async def delete_chunks_by_source(source: str, collection: str | None = None) -> int:
    """
    특정 source 메타데이터를 가진 모든 벡터를 Qdrant에서 삭제한다.

    Returns:
        삭제 요청이 성공하면 1 (실패하면 예외를 올린다)
    """
    coll = collection or settings.QDRANT_COLLECTION
    client = AsyncQdrantClient(url=settings.QDRANT_URL)
    try:
        await client.delete(collection_name=coll, points_selector=_source_filter(source))
        return 1
    except Exception as exc:
        _fail("rag_delete_failed", coll, exc)
        raise
    finally:
        await client.close()
