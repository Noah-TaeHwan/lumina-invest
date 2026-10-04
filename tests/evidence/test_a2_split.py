# tests/evidence/test_a2_split.py
"""A-2 추첨·분할(spec 6.2절): 시드 20261103, A-1 40개사 제외, A-1과 교차 언급 10회 이상 제외, 군집 단위 확인 ≤ 20."""
import pytest

from app.services.evidence.dart import Corp
from lab.evidence import split


def test_a2_constants():
    assert (split.A2_SEED, split.A2_RANDOM_N, split.A2_CHECK_CAP) == (20261103, 40, 20)


def test_shuffle_and_assign_take_a_seed_and_keep_a1_default():
    corps = [Corp(f"{i:08d}", f"회사{i}", f"{i:06d}") for i in range(50)]
    assert split.shuffled(corps) == split.shuffled(corps, seed=split.SEED)
    assert split.shuffled(corps, seed=split.A2_SEED) != split.shuffled(corps)
    assert split.shuffled(corps, seed=split.A2_SEED) == split.shuffled(list(reversed(corps)), seed=split.A2_SEED)
    cl = [[c] for c in "abcdefghij"]
    assert split.assign(cl, 4) == split.assign(cl, 4, seed=split.SEED)
    assert split.assign(cl, 4, seed=split.A2_SEED) != split.assign(cl, 4)


def test_a2_candidate_excludes_a1_companies_and_prefixes():
    a1 = {"00000001"}
    assert split.a2_skip(Corp("00000001", "케이씨건설", "1"), a1) == "a1_company"
    assert split.a2_skip(Corp("00000002", "삼성물산", "2"), a1) == "prefix"
    assert split.a2_skip(Corp("00000003", "한미반도체", "3"), a1) is None


def test_a1_link_both_directions_threshold_ten():
    a1_names = {"x": "에이전자", "y": "비화학"}
    a1_texts = {"x": "고객사 씨소재 " * 10, "y": "기타"}
    # A-1 회사 본문에 후보 이름이 10회 → 제외
    assert split.a1_link("씨소재", "", a1_names, a1_texts) == "x"
    # 후보 본문에 A-1 이름이 10회 → 제외, 9회는 통과
    assert split.a1_link("디소재", "비화학 " * 10, a1_names, a1_texts) == "y"
    assert split.a1_link("디소재", "비화학 " * 9, a1_names, a1_texts) is None
    # 두 글자 이름은 보지 않는다(A-1과 같은 MIN_NAME_LEN)
    assert split.a1_link("SK", "", {"x": "에이전자"}, {"x": "SK " * 30}) is None


def test_check_split_sizes_for_a2():
    split.check_split_sizes({"check": 20, "tune": 20}, sealed="check", others=("tune",))
    split.check_split_sizes({"check": 15, "tune": 25}, sealed="check", others=("tune",))
    with pytest.raises(SystemExit):
        split.check_split_sizes({"check": 14, "tune": 26}, sealed="check", others=("tune",))
    with pytest.raises(SystemExit):
        split.check_split_sizes({"check": 20, "tune": 0}, sealed="check", others=("tune",))
    with pytest.raises(SystemExit):  # A-1 기본 동작 유지
        split.check_split_sizes({"tune": 5, "check": 5, "holdout": 14})


def test_a2_assign_puts_whole_clusters_in_check_up_to_cap():
    cl = [[f"c{i}"] for i in range(30)] + [[f"g{i}" for i in range(10)]]
    check, tune = split.assign(cl, split.A2_CHECK_CAP, seed=split.A2_SEED)
    flat = sum(check, [])
    assert len(flat) <= 20 and sorted(flat + sum(tune, [])) == sorted(sum(cl, []))
    big = [f"g{i}" for i in range(10)]
    assert big in check or big in tune  # 군집은 쪼개지지 않는다


def test_take_excludes_report_whose_document_is_missing(tmp_path, monkeypatch):
    """DART가 원문 대신 오류 XML(status 014 '파일이 존재하지 않습니다')을 주면 추첨을 멈추지 않고 그 회사만 뺀다."""
    from app.services.evidence import dart
    from lab.evidence import __main__ as cli

    monkeypatch.setattr(dart, "list_annual_reports", lambda *a, **k: [])
    monkeypatch.setattr(dart, "pick_annual_report", lambda items: {"rcept_no": "1", "report_nm": "사업보고서"})
    monkeypatch.setattr(dart, "company_info", lambda *a, **k: {"induty_code": "264"})

    def missing(*a, **k):
        raise ValueError("DART 응답이 zip이 아니다: b'<result><status>014</status>'")

    monkeypatch.setattr(dart, "download_document", missing)
    corp = dart.Corp("00000001", "가나다", "000001")
    result, row, text = cli._take(cli.Paths(tmp_path), None, "K", corp, "random")
    assert result.startswith("document:") and "014" in result and row is None and text == ""


def test_take_reraises_other_dart_errors(tmp_path, monkeypatch):
    """014(원문 없음)가 아닌 오류(020 요청 제한·키 오류 등)는 조용히 빼지 않고 멈춘다(표본이 바뀌지 않게)."""
    import pytest

    from app.services.evidence import dart
    from lab.evidence import __main__ as cli

    monkeypatch.setattr(dart, "list_annual_reports", lambda *a, **k: [])
    monkeypatch.setattr(dart, "pick_annual_report", lambda items: {"rcept_no": "1", "report_nm": "사업보고서"})
    monkeypatch.setattr(dart, "company_info", lambda *a, **k: {"induty_code": "264"})

    def limited(*a, **k):
        raise ValueError("DART 응답이 zip이 아니다: b'<result><status>020</status>'")

    monkeypatch.setattr(dart, "download_document", limited)
    with pytest.raises(ValueError, match="020"):
        cli._take(cli.Paths(tmp_path), None, "K", dart.Corp("00000001", "가나다", "000001"), "random")
