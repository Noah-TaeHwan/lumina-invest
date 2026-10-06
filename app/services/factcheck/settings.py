# app/services/factcheck/settings.py
"""팩트체커 설정(환경변수·ENV_FILE). 앱 공용 Settings(app/config.py)를 고치지 않으려고 따로 둔다.

- FACTCHECK_DAILY_*: 하루 상한. 금액이 아니라 실행 횟수·JEV 입력 토큰 수다. 공개 값은 노아가 공개 전에 정한다.
  예약은 상한 기준이라(metering.reservation_for) 30문장 검수 하나가 1,736,000 토큰((30 + 1) × 56,000)을 예약한다.
  키별·전체 상한은 '그날 실제 사용량 + 검수 하나의 예약'보다 커야 큰 검수가 시작될 수 있다.
- FACTCHECK_TRUSTED_PROXIES: X-Forwarded-For를 믿을 앞단 프록시 주소(쉼표, IP 또는 CIDR). 비어 있으면 XFF를 쓰지 않는다
  (app/config.py의 TRUST_PROXY도 켜져 있어야 한다).
- FACTCHECK_ALLOWED_ORIGINS: POST를 받을 Origin(쉼표, 예 https://fc.example). **배포에서는 반드시 정한다.** 비어 있으면
  Origin의 스킴·호스트가 요청의 스킴(request.url.scheme)·Host와 같아야 한다 — TLS를 앞단 프록시에서 끝내면 앱이 보는
  스킴은 http라 브라우저 요청(https Origin)이 모두 거부된다.
- FACTCHECK_DATA_DIR: T1 수집 산출(xbrl_facts.json·corp_names.json) 폴더. 상대 경로는 저장소 루트 기준. 배포에서는 절대 경로로
  두고 데이터를 마운트한다(Dockerfile은 lab/을 복사하지 않는다).
- FACTCHECK_ALLOW_NO_DATA: XBRL 행·상장사명·문단 중 하나라도 비어 있어도 검수를 받는다(개발용). 기본 끔 — 꺼져 있으면
  데이터가 없을 때 검수 요청은 503(data_unavailable)이다(조용히 ❔만 내는 잘못된 판정 방지).
"""
from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class FactcheckSettings(BaseSettings):
    """팩트체커 전용 설정."""

    model_config = SettingsConfigDict(env_file=os.getenv("ENV_FILE", ".env.dev"), extra="ignore")  # app/config.py와 같은 파일

    FACTCHECK_DAILY_ANON_RUNS: int = 3
    # 익명 키 하루 토큰: 30문장 검수 예약(1,736,000)이 들어가고 실제 사용량이 쌓일 여유(764,000)
    FACTCHECK_DAILY_KEY_TOKENS: int = 2_500_000
    # 서버 전체 하루 토큰(보수적 기본값). 근거 모드 전체 기본값(3,000,000)과 같다. 공개 값은 노아가 정한다
    FACTCHECK_DAILY_GLOBAL_TOKENS: int = 3_000_000
    FACTCHECK_TRUSTED_PROXIES: str = ""
    FACTCHECK_ALLOWED_ORIGINS: str = ""
    FACTCHECK_DATA_DIR: str = "lab/data/factcheck"
    FACTCHECK_ALLOW_NO_DATA: bool = False


def load() -> FactcheckSettings:
    """현재 환경에서 설정을 읽는다."""
    return FactcheckSettings()
