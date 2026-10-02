# tests/evidence/test_claim_spans.py
import pytest

from app.services.evidence import claims

TEXTS = [
    "삼성전자는 반도체를 만든다. 매출은 300조다!\n- 주요 제품은 DRAM이다\n1) 네.",
    "첫 문장입니다.  둘째 문장인가요?   셋째!\r\n* 글머리 문장이다\r\n\r\n2. 번호 글머리 문장",
    "　전각 공백으로 시작한다. 끝에도 공백　 • 줄 구분자 뒤 문장\x0c다음 쪽 문장이다",
    "  · 가운뎃점 글머리. 짧음. 아주 길게 이어지는 마지막 문장이다.   ",
    "",
    "줄바꿈 없음",
]


@pytest.mark.parametrize("text", TEXTS)
def test_claim_spans_match_split_sentences_and_offsets(text):
    spans = claims.claim_spans(text)
    assert [s.text for s in spans] == claims.split_sentences(text)
    for s in spans:
        assert text[s.start:s.end] == s.text
    assert [s.start for s in spans] == sorted(s.start for s in spans)


def test_claim_spans_strip_bullets_from_offsets():
    text = "- 주요 제품은 DRAM이다\n1) 매출은 늘었다."
    spans = claims.claim_spans(text)
    assert [(s.start, s.end) for s in spans] == [(2, 15), (19, 27)]


@pytest.mark.parametrize("sentence", [
    "주요 제품의 매출 비중은 얼마인가요?",
    "제공된 문단에서는 영업이익을 확인할 수 없습니다.",
    "제공된 정보만으로는 판단하기 어렵습니다.",
    "배당 정책은 알 수 없습니다.",
    "해당 내용은 언급되어 있지 않습니다.",
    "정확한 답변을 위해 추가 정보가 필요합니다.",
    "문단에 따르면 회사는 반도체를 만든다.",
    "네, 맞습니다.",
    "감사합니다.",
])
def test_not_claim_rules_hit(sentence):
    assert claims.is_not_claim(sentence)


@pytest.mark.parametrize("sentence", [
    "삼성전자 흑자.",
    "매출 1조.",
    "SK 지분 보유.",
    "회사는 메모리 반도체와 스마트폰을 만든다.",
    "영업이익은 전년보다 늘어 6조 원을 넘었다.",
])
def test_not_claim_rules_miss(sentence):
    assert not claims.is_not_claim(sentence)


def test_short_sentence_boundary_is_ten_chars():
    assert claims.is_not_claim("그렇게 볼 수 있습니다.") is False  # 13자: 길이 규칙 밖
    assert claims.is_not_claim("그렇습니다.") is True  # 6자, 숫자·고유명사 후보 없음


def test_not_claim_phrases_are_code_constants():
    assert "확인할 수 없" in claims.NOT_CLAIM_PHRASES and claims.NOT_CLAIM_MAX_SHORT == 10
