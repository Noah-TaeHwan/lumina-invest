# app/services/evidence/retrieve.py
"""기업 문단 중 질문과 코사인 유사도가 높은 상위 k개를 고른다.

nomic-embed-text는 질의·문서 접두어를 붙여 임베딩한다.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

QUERY_PREFIX = "search_query: "
DOC_PREFIX = "search_document: "


def top_k(query: Sequence[float], vectors: Sequence[Sequence[float]], k: int = 8) -> list[int]:
    """코사인 유사도 내림차순 상위 k개 인덱스. 동점은 앞 인덱스가 먼저다."""
    q = np.asarray(query, dtype=float)
    m = np.asarray(vectors, dtype=float)
    sims = (m @ q) / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-12)
    return [int(i) for i in np.argsort(-sims, kind="stable")[:k]]
