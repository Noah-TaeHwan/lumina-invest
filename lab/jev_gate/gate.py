"""TypeSafe JEV 진입 게이트 클라이언트.

- 모델은 jev-1.13.0으로 고정하고, 응답의 실제 모델 ID를 매 호출 기록한다.
- 한 번에 하나씩 순차 호출한다(동시 호출로 지연이 부풀지 않게). 타임아웃 10초, 재시도 없음.
- 모든 호출을 JSONL에 남긴다(익명 state·결과·태그). 같은 입력은 캐시를 쓰고, 반복 측정은 use_cache=False.
- 실패(HTTP 오류·타임아웃·스키마 위반)는 차단으로 처리하고 캐시하지 않는다.
- 누적 입력 토큰 비용이 예산에 닿으면 호출하지 않는다. API 키 값은 어디에도 기록하지 않는다.
- 입력 토큰 단가는 계약상 비공개라 저장소에 두지 않는다. 환경 변수나 ~/.config/typesafe의 파일에서 읽고, 없으면 호출하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
PRICE_ENV = "JEV_PRICE_PER_MILLION_INPUT_TOKENS"
PRICE_FILE_NAME = ".config/typesafe/price_per_million_input_tokens"
PRICE_HINT = (f"입력 토큰 단가를 모릅니다(비공개 값). 100만 토큰당 USD를 환경 변수 {PRICE_ENV}나 "
              f"~/{PRICE_FILE_NAME} 파일에 넣으세요")
TIMEOUT_S = 10.0
QUESTION_ID = "fail"
QUESTION = {
    "type": "noul",
    "instructions": (
        "A long breakout entry signal just fired on a 5-minute chart of a liquid market. "
        "Using only the anonymized features in the state, will this trade more likely fail than succeed?"
    ),
    "criteria": {
        "true": (
            "Price is more likely to close 2 ATR below entry, or reach the 2-hour limit without "
            "closing 3 ATR above entry, before it closes 3 ATR above entry."
        ),
        "false": "Price is more likely to close 3 ATR above entry before closing 2 ATR below entry, within 2 hours.",
    },
}


class BudgetExceeded(RuntimeError):
    """누적 JEV 비용이 예산 상한에 닿았다."""


class PriceUnknown(RuntimeError):
    """입력 토큰 단가를 모르면 예산을 지킬 수 없으므로 과금 호출을 하지 않는다."""


@dataclass
class GateResult:
    """JEV 호출 한 번의 결과. ok=False면 게이트는 차단한다."""

    key: str
    ok: bool
    p_fail: float | None
    latency_ms: float
    model: str | None
    input_tokens: int
    status: int | None
    error: str | None
    called_at: str
    cached: bool = False


def question_hash() -> str:
    """질문 정의의 SHA-256(사전등록 대조용)."""
    return hashlib.sha256(json.dumps(QUESTION, sort_keys=True).encode()).hexdigest()


def cache_key(state: dict) -> str:
    """state·질문·모델로 만든 캐시 키. dict 키 순서와 무관하다."""
    payload = json.dumps({"state": state, "question": QUESTION, "model": MODEL}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def load_api_key() -> str:
    """TYPESAFE_API_KEY 환경 변수, 없으면 소유자 전용(0600) ~/.config/typesafe/api_key에서 읽는다."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key.strip()
    path = Path.home() / ".config/typesafe/api_key"
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        raise PermissionError(f"{path}는 소유자 전용(0600) 일반 파일이어야 합니다")
    return path.read_text().strip()


def load_price_per_input_token() -> float | None:
    """입력 토큰 하나의 단가(USD). 환경 변수, 없으면 ~/.config/typesafe의 파일(100만 토큰당 USD)에서 읽는다.

    @returns 단가, 둘 다 없으면 None(미확인)
    """
    raw = os.environ.get(PRICE_ENV, "").strip()
    path = Path.home() / PRICE_FILE_NAME
    if not raw and path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
    return float(raw) / 1_000_000 if raw else None


PRICE_PER_INPUT_TOKEN = load_price_per_input_token()  # None이면 단가 미확인


def is_blocked(result: GateResult, tau: float) -> bool:
    """실패는 차단, 성공은 p_fail ≥ τ이면 차단."""
    return (not result.ok) or result.p_fail >= tau


class JevGate:
    """캐시·예산 상한이 있는 순차 JEV 호출기."""

    def __init__(self, cache_path: Path, budget_usd: float = 5.0, client: httpx.Client | None = None,
                 api_key: str | None = None, clock=None):
        self.cache_path = cache_path
        self.budget_usd = budget_usd
        self._client = client if client is not None else httpx.Client(timeout=TIMEOUT_S)
        self._api_key = api_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))  # 호출 시각 기록용(테스트에서 주입)
        self._cache: dict[str, GateResult] = {}
        self.spent_usd = 0.0
        if cache_path.exists():
            for line in cache_path.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                rec.pop("state", None)
                rec.pop("tag", None)
                r = GateResult(**rec)
                if PRICE_PER_INPUT_TOKEN is not None:  # 단가 미확인이면 합산하지 않는다(새 호출은 ask가 막는다)
                    self.spent_usd += r.input_tokens * PRICE_PER_INPUT_TOKEN
                if r.ok and r.key not in self._cache:
                    self._cache[r.key] = r

    def ask(self, state: dict, use_cache: bool = True, tag: str | None = None) -> GateResult:
        """state에 대한 p_fail을 묻는다. 캐시가 있으면 호출하지 않는다."""
        key = cache_key(state)
        if use_cache and key in self._cache:
            return replace(self._cache[key], cached=True)
        if PRICE_PER_INPUT_TOKEN is None:
            raise PriceUnknown(PRICE_HINT)
        if self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(f"누적 ${self.spent_usd:.6f} ≥ 예산 ${self.budget_usd}")
        result = self._call(key, state)
        self.spent_usd += result.input_tokens * PRICE_PER_INPUT_TOKEN
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**asdict(result), "state": state, "tag": tag}) + "\n")
        if result.ok and key not in self._cache:
            self._cache[key] = result
        return result

    def _call(self, key: str, state: dict) -> GateResult:
        if self._api_key is None:
            self._api_key = load_api_key()
        body = {"model": MODEL, "state": state, "questions": {QUESTION_ID: QUESTION}}
        called_at = self._clock().isoformat()
        t0 = time.perf_counter()

        def fail(error: str, status: int | None = None) -> GateResult:
            return GateResult(key, False, None, (time.perf_counter() - t0) * 1000, None, 0, status, error, called_at)

        try:
            resp = self._client.post(API_URL, json=body, headers={"Authorization": f"Bearer {self._api_key}"},
                                     timeout=TIMEOUT_S)
        except httpx.TimeoutException:
            return fail("timeout")
        except httpx.HTTPError as e:
            return fail(type(e).__name__)
        latency = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            return fail(f"http_{resp.status_code}", resp.status_code)
        try:
            payload = resp.json()
            p = float(payload["answers"][QUESTION_ID]["noul"])
            tokens = int(payload["usage"]["input_tokens"])
            model = str(payload["model"])
            if not 0.0 <= p <= 1.0:
                raise ValueError(p)
        except (ValueError, KeyError, TypeError):
            return fail("schema", 200)
        if model != MODEL:  # 별칭이 다른 버전을 가리키면 확률이 섞이므로 차단(과금·모델은 기록)
            return GateResult(key, False, None, latency, model, tokens, 200, "model_mismatch", called_at)
        return GateResult(key, True, p, latency, model, tokens, 200, None, called_at)
