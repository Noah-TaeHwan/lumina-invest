# lab/evidence/split.py
"""기업 표본 추첨, 교차 언급 군집, 군집 단위 분할(spec 2절)."""
from __future__ import annotations

import random

from app.services.evidence.dart import Corp

SEED = 20261002
HOLDOUT_CAP = 20
TUNE_CAP = 10
RANDOM_N = 25
MIN_CHARS = 3000
SEED_GROUPS: dict[str, tuple[str, ...]] = {
    "삼성": ("005930", "006400", "207940"),
    "SK": ("000660", "034730", "096770"),
    "현대차": ("005380", "000270", "012330"),
    "LG": ("051910", "373220"),
    "NAVER": ("035420",),
    "카카오": ("035720",),
    "POSCO": ("005490",),
    "셀트리온": ("068270",),
}
EXCLUDED_PREFIXES = ("삼성", "SK", "에스케이", "현대", "기아", "LG", "엘지", "NAVER", "네이버", "카카오",
                     "POSCO", "포스코", "셀트리온")
FINANCE_KSIC = ("64", "65", "66")


def shuffled(corps: list[Corp]) -> list[Corp]:
    """corp_code 오름차순 정렬 뒤 고정 시드로 섞은 추첨 순서."""
    out = sorted(corps, key=lambda c: c.corp_code)
    random.Random(SEED).shuffle(out)
    return out


def is_candidate(corp: Corp) -> bool:
    """시드 그룹 이름 접두어로 시작하지 않는 회사만 무작위 후보다."""
    return not corp.corp_name.startswith(EXCLUDED_PREFIXES)


def is_finance(induty_code: str) -> bool:
    """KSIC 64~66(금융·보험)이면 참."""
    return str(induty_code).startswith(FINANCE_KSIC)


def clusters(names: dict[str, str], texts: dict[str, str], groups: list[list[str]]) -> list[list[str]]:
    """시드 그룹과 본문 교차 언급(한쪽 본문에 다른 쪽 이름)으로 묶은 연결 요소."""
    parent = {c: c for c in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for g in groups:
        for other in g[1:]:
            union(g[0], other)
    codes = sorted(names)
    for a in codes:
        for b in codes:
            if a != b and names[b] in texts.get(a, ""):
                union(a, b)
    comp: dict[str, list[str]] = {}
    for c in codes:
        comp.setdefault(find(c), []).append(c)
    return sorted((sorted(v) for v in comp.values()), key=lambda v: v[0])


def assign(cluster_list: list[list[str]], cap: int) -> tuple[list[list[str]], list[list[str]]]:
    """고정 시드로 섞은 군집을 앞에서부터 보며, 넣어도 cap 이하면 앞 묶음에, 아니면 뒤 묶음에 둔다."""
    order = list(cluster_list)
    random.Random(SEED).shuffle(order)
    take, rest, n = [], [], 0
    for cl in order:
        if n + len(cl) <= cap:
            take.append(cl)
            n += len(cl)
        else:
            rest.append(cl)
    return take, rest
