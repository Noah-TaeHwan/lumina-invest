# app/lib/jev.py
"""TypeSafe JEV 판정 API 클라이언트(근거 판정 엔진·평가 공용).

- 모델은 jev-1.13.0으로 고정하고, 응답 모델이 다르면 실패로 본다.
- 요청 하나에 Choice 질문 여러 개를 담고, 응답 확률을 보낸 선택지와 대조한다.
- HTTP 시도마다 JSONL 한 줄을 남긴다(state 원문은 남기지 않는다). 성공 응답만 캐시한다.
- 누적 입력 토큰이 상한에 닿으면 호출하지 않는다. API 키 값은 어디에도 남기지 않는다.
- lab/jev_gate의 캐시·예산 로직을 의도적으로 복제했다(끝난 실험 코드를 import하지 않기 위해).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
TIMEOUT_S = 10.0


class TokenCapExceeded(RuntimeError):
    """누적 입력 토큰이 상한에 닿았다."""


@dataclass
class JevResult:
    """요청 하나(재시도 포함)의 결과. ok=False면 answers는 None이다."""

    key: str
    ok: bool
    answers: dict[str, dict[str, float]] | None
    latency_ms: float
    input_tokens: int
    attempts: int
    error: str | None
    cached: bool = False


def load_api_key() -> str:
    """TYPESAFE_API_KEY 환경 변수, 없으면 소유자 전용(0600) ~/.config/typesafe/api_key에서 읽는다."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".config/typesafe/api_key"
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PermissionError(f"{path} 권한은 0600이어야 한다")
    return path.read_text().strip()


def request_key(state: str, questions: dict) -> str:
    """모델·state·질문을 정렬 직렬화한 SHA-256. 캐시 키로 쓴다."""
    body = json.dumps({"model": MODEL, "state": state, "questions": questions},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def parse_answers(payload: dict, questions: dict) -> dict[str, dict[str, float]]:
    """응답에서 질문별 확률 분포를 꺼내 검증한다. 어긋나면 ValueError."""
    if payload.get("model") != MODEL:
        raise ValueError(f"model mismatch: {payload.get('model')}")
    out: dict[str, dict[str, float]] = {}
    for qid, q in questions.items():
        probs = payload["answers"][qid]["probabilities"]
        if set(probs) != set(q["criteria"]):
            raise ValueError(f"{qid}: option keys mismatch")
        vals = {k: float(v) for k, v in probs.items()}
        if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in vals.values()):
            raise ValueError(f"{qid}: probability out of range")
        if not 0.99 <= sum(vals.values()) <= 1.01:
            raise ValueError(f"{qid}: probabilities do not sum to 1")
        out[qid] = vals
    return out


class JevClient:
    """순차 호출 JEV 클라이언트. 호출 기록 파일이 곧 캐시이자 토큰 원장이다."""

    def __init__(self, log_path: Path, token_cap: int, client: httpx.Client | None = None,
                 api_key: str | None = None, clock=time.perf_counter):
        self._log = Path(log_path)
        self._cap = token_cap
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._api_key = api_key
        self._clock = clock
        self._cache: dict[str, dict] = {}
        self.used_tokens = 0
        if self._log.exists():
            for line in self._log.read_text().splitlines():
                rec = json.loads(line)
                self.used_tokens += rec["input_tokens"]
                if rec["ok"]:
                    self._cache[rec["key"]] = rec["answers"]

    def _write(self, rec: dict) -> None:
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with self._log.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def ask(self, state: str, questions: dict, *, use_cache: bool = True, tag: str = "") -> JevResult:
        """질문 묶음을 보낸다. 실패하면 한 번 다시 보내고, 그래도 실패면 ok=False."""
        key = request_key(state, questions)
        if use_cache and key in self._cache:
            return JevResult(key, True, self._cache[key], 0.0, 0, 0, None, cached=True)
        if self.used_tokens >= self._cap:
            raise TokenCapExceeded(f"used {self.used_tokens} >= cap {self._cap}")
        if self._api_key is None:
            self._api_key = load_api_key()
        body = {"model": MODEL, "state": state, "questions": questions}
        latency, error = 0.0, None
        for attempt in (1, 2):
            t0 = self._clock()
            answers, tokens, error = None, 0, None
            try:
                resp = self._client.post(API_URL, json=body, headers={"Authorization": f"Bearer {self._api_key}"})
                resp.raise_for_status()
                payload = resp.json()
                tokens = int(payload["usage"]["input_tokens"])
                answers = parse_answers(payload, questions)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
            latency = (self._clock() - t0) * 1000
            self.used_tokens += tokens
            self._write({"key": key, "tag": tag, "attempt": attempt, "ok": error is None, "answers": answers,
                         "latency_ms": round(latency, 1), "input_tokens": tokens, "error": error,
                         "use_cache": use_cache, "called_at": datetime.now(timezone.utc).isoformat()})
            if error is None:
                self._cache[key] = answers
                return JevResult(key, True, answers, latency, tokens, attempt, None)
        return JevResult(key, False, None, latency, 0, 2, error)
