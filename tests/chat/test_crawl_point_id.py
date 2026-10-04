# tests/chat/test_crawl_point_id.py
"""크롤링 청크의 Qdrant point ID는 프로세스가 바뀌어도 같아야 한다(A-1 spec 12절 기록 결함).

str hash()는 프로세스마다 PYTHONHASHSEED로 달라져, 같은 URL을 다시 크롤링하면 upsert가 덮어쓰지
못하고 청크가 중복으로 쌓였다. 서로 다른 해시 시드로 띄운 두 프로세스에서 같은 값인지 본다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODE = "from app.services.crawl import _point_id; print(_point_id('https://example.com/docs/a.md', 3))"


def _id_under_seed(seed: str) -> int:
    env = {**os.environ, "PYTHONHASHSEED": seed,
           "DATABASE_URL": os.environ.get("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x"),
           "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:6379/0")}
    out = subprocess.run([sys.executable, "-c", CODE], cwd=ROOT, env=env, capture_output=True, text=True,
                         check=True)
    return int(out.stdout.strip().splitlines()[-1])


def test_point_id_is_stable_across_processes():
    a, b = _id_under_seed("1"), _id_under_seed("2")
    assert a == b
    assert 0 <= a < 2 ** 63  # 기존 정수 ID 범위


def test_point_id_differs_by_url_and_index():
    from app.services.crawl import _point_id

    ids = {_point_id("https://example.com/a", 0), _point_id("https://example.com/a", 1),
           _point_id("https://example.com/b", 0)}
    assert len(ids) == 3
