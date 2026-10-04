# tests/chat/test_crawl_point_id.py
"""크롤링 청크의 Qdrant point ID는 프로세스가 바뀌어도 같아야 한다(A-1 spec 12절 기록 결함).

str hash()는 프로세스마다 PYTHONHASHSEED로 달라 같은 URL을 다시 크롤링하면 청크가 중복으로 쌓였다.
sha256("{url}-{i}") 앞 8바이트 % 2**63으로 미리 계산한 고정값과 같은지 본다(hash()라면 실행마다 달라 실패한다).
"""
from __future__ import annotations

from app.services.crawl import _point_id


def test_point_id_is_fixed_value():
    assert _point_id("https://example.com/docs/a.md", 3) == 3262284307577977719
    assert _point_id("https://example.com/a", 0) == 7639576217380655046


def test_point_id_differs_by_url_and_index():
    ids = {_point_id("https://example.com/a", 0), _point_id("https://example.com/a", 1),
           _point_id("https://example.com/b", 0)}
    assert len(ids) == 3
