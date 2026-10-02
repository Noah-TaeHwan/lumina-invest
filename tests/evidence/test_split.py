# tests/evidence/test_split.py
from app.services.evidence.dart import Corp
from lab.evidence import split


def test_seed_groups_cover_15_seed_stocks():
    codes = [c for g in split.SEED_GROUPS.values() for c in g]
    assert len(codes) == 15 == len(set(codes)) and "005930" in codes


def test_shuffled_is_deterministic_and_order_independent():
    corps = [Corp(f"{i:08d}", f"회사{i}", f"{i:06d}") for i in range(50)]
    assert split.shuffled(corps) == split.shuffled(list(reversed(corps)))


def test_candidate_and_finance_filters():
    assert not split.is_candidate(Corp("1", "삼성물산", "1")) and not split.is_candidate(Corp("1", "LG이노텍", "1"))
    assert split.is_candidate(Corp("1", "한미반도체", "1"))
    assert split.is_finance("64191") and split.is_finance("661") and not split.is_finance("26110")


def test_clusters_merge_seed_groups_and_cross_mentions():
    names = {"a": "에이전자", "b": "비화학", "c": "씨바이오", "d": "디소재"}
    texts = {"a": "주요 고객은 비화학이다", "b": "", "c": "", "d": ""}
    assert split.clusters(names, texts, [["c", "d"]]) == [["a", "b"], ["c", "d"]]


def test_assign_respects_cap_and_is_deterministic():
    cl = [["a"], ["b", "c"], ["d"], ["e", "f", "g"], ["h"]]
    take, rest = split.assign(cl, 4)
    assert sum(len(x) for x in take) <= 4 and sorted(sum(take + rest, [])) == list("abcdefgh")
    assert split.assign(cl, 4) == (take, rest)
