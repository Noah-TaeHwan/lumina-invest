# tests/chat/conftest.py
"""기존 채팅 경로 테스트 공용: 저장 테스트는 근거 판정 테스트와 같은 빈 PostgreSQL 픽스처를 쓴다."""
from tests.evidence.conftest import pg, pg_migrated  # noqa: F401
