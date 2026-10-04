# lab/evidence/split.py
"""기업 표본 추첨, 교차 언급 군집, 군집 단위 분할(spec 2절). A-2(A-2 spec 6.2절)는 시드·제외 규칙만 더한다."""
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

# A-2: 무작위 40개사(시드 그룹 없음), 확인 세트 상한 20(나머지가 조정 세트)
A2_SEED = 20261103
A2_RANDOM_N = 40
A2_CHECK_CAP = 20


def shuffled(corps: list[Corp], seed: int = SEED) -> list[Corp]:
    """corp_code 오름차순 정렬 뒤 고정 시드로 섞은 추첨 순서."""
    out = sorted(corps, key=lambda c: c.corp_code)
    random.Random(seed).shuffle(out)
    return out


def is_candidate(corp: Corp) -> bool:
    """시드 그룹 이름 접두어로 시작하지 않는 회사만 무작위 후보다."""
    return not corp.corp_name.startswith(EXCLUDED_PREFIXES)


def is_finance(induty_code: str) -> bool:
    """KSIC 64~66(금융·보험)이면 참."""
    return str(induty_code).startswith(FINANCE_KSIC)


MIN_NAME_LEN = 3
MIN_MENTIONS = 10


def mention_edges(names: dict[str, str], texts: dict[str, str]) -> list[tuple[str, str, str]]:
    """(a, b, b의 이름): a 본문에 b 이름이 MIN_MENTIONS번 이상 나오는 쌍. 군집 병합 근거로 원장에 남긴다.

    두 글자 약칭(SK·DB 등)은 다른 낱말 안에서도 걸리므로 보지 않는다(그런 계열은 시드 그룹으로 묶는다).
    고객사로 몇 번 언급되는 정도는 상대 회사 사업을 서술한 것이 아니므로 병합하지 않는다.
    """
    codes = sorted(names)
    return [(a, b, names[b]) for a in codes for b in codes
            if a != b and len(names[b]) >= MIN_NAME_LEN and texts.get(a, "").count(names[b]) >= MIN_MENTIONS]


def a2_skip(corp: Corp, a1_codes: set[str]) -> str | None:
    """A-2 추첨에서 본문을 받기 전에 거르는 사유: A-1 40개사, 시드 그룹 이름 접두어."""
    if corp.corp_code in a1_codes:
        return "a1_company"
    return None if is_candidate(corp) else "prefix"


def a1_link(name: str, text: str, a1_names: dict[str, str], a1_texts: dict[str, str]) -> str | None:
    """후보와 A-1 회사 사이 교차 언급(어느 쪽 본문이든 상대 이름이 MIN_MENTIONS번 이상)이면 그 A-1 회사 코드.

    이름 길이·횟수 기준은 A-1 군집 규칙(mention_edges)과 같다.
    """
    for code in sorted(a1_names):
        a1 = a1_names[code]
        if len(a1) >= MIN_NAME_LEN and text.count(a1) >= MIN_MENTIONS:
            return code
        if len(name) >= MIN_NAME_LEN and a1_texts.get(code, "").count(name) >= MIN_MENTIONS:
            return code
    return None


def check_split_sizes(counts: dict[str, int], holdout_min: int = 15, sealed: str = "holdout",
                      others: tuple[str, ...] = ("tune", "check")) -> None:
    """봉인 분할(A-1 홀드아웃, A-2 확인)이 하한보다 작거나 나머지 분할이 비면 멈춘다(군집이 한 덩어리로 뭉친 경우)."""
    if any(counts.get(o, 0) == 0 for o in others) or counts.get(sealed, 0) < holdout_min:
        raise SystemExit(f"split sizes unusable: {counts}")


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
    for a, b, _ in mention_edges(names, texts):
        union(a, b)
    comp: dict[str, list[str]] = {}
    for c in codes:
        comp.setdefault(find(c), []).append(c)
    return sorted((sorted(v) for v in comp.values()), key=lambda v: v[0])


def assign(cluster_list: list[list[str]], cap: int, seed: int = SEED) -> tuple[list[list[str]], list[list[str]]]:
    """고정 시드로 섞은 군집을 앞에서부터 보며, 넣어도 cap 이하면 앞 묶음에, 아니면 뒤 묶음에 둔다."""
    order = list(cluster_list)
    random.Random(seed).shuffle(order)
    take, rest, n = [], [], 0
    for cl in order:
        if n + len(cl) <= cap:
            take.append(cl)
            n += len(cl)
        else:
            rest.append(cl)
    return take, rest
