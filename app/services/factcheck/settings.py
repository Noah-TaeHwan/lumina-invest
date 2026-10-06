# app/services/factcheck/settings.py
"""팩트체커 설정(환경변수·ENV_FILE). 앱 공용 Settings(app/config.py)를 고치지 않으려고 따로 둔다.

- FACTCHECK_DAILY_*: 하루 상한. 금액이 아니라 실행 횟수·JEV 입력 토큰 수다. 공개 값은 노아가 공개 전에 정한다.
  예약은 상한 기준이라(metering.reservation_for) 30문장 검수 하나가 1,488,000 토큰을 예약한다. 전체 상한은
  '하루 실제 사용량 + 검수 하나의 예약'보다 커야 큰 검수가 시작될 수 있다(PR 본문의 계산 예).
- FACTCHECK_TRUSTED_PROXIES: X-Forwarded-For를 믿을 앞단 프록시 주소(쉼표, IP 또는 CIDR). 비어 있으면 XFF를 쓰지 않는다
  (app/config.py의 TRUST_PROXY도 켜져 있어야 한다).
- FACTCHECK_ALLOWED_ORIGINS: POST를 받을 Origin(쉼표, 예 https://fc.example). 비어 있으면 Origin의 호스트가 요청 Host와
  같아야 한다.
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class FactcheckSettings(BaseSettings):
    """팩트체커 전용 설정."""

    model_config = SettingsConfigDict(env_file=os.getenv("ENV_FILE", ".env.dev"), extra="ignore")  # app/config.py와 같은 파일

    FACTCHECK_DAILY_ANON_RUNS: int = 3
    # 익명 키 하루 토큰: 30문장 검수 예약(1,488,000)이 들어가고 실제 사용량이 쌓일 여유
    FACTCHECK_DAILY_KEY_TOKENS: int = 2_000_000
    # 서버 전체 하루 토큰(보수적 기본값). 근거 모드 전체 기본값(3,000,000)과 같다. 공개 값은 노아가 정한다
    FACTCHECK_DAILY_GLOBAL_TOKENS: int = 3_000_000
    FACTCHECK_TRUSTED_PROXIES: str = ""
    FACTCHECK_ALLOWED_ORIGINS: str = ""


def load() -> FactcheckSettings:
    """현재 환경에서 설정을 읽는다."""
    return FactcheckSettings()
