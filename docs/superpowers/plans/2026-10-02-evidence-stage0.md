# 근거 판정 엔진 Stage 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 40개사 DART 사업보고서로 개발 세트(조정·확인)의 질문·답변·주장·AI 참조 라벨을 만들고, JEV 한국어 지지 판정의 Stage 0 관문(코퍼스·라벨 품질·H-ko·질문 묶기·지연/실패·반복 일관성)을 측정해 GO/NO-GO를 낸다.

**Architecture:**
- 제품 코드는 `app/services/evidence/`와 `app/lib/jev.py`에 둔다. 순수 함수 중심이고 네트워크·LLM은 인자로 받는다.
- 평가 하네스는 `lab/evidence/`에 둔다. 명령줄 `python -m lab.evidence <cmd>`이 단계를 하나씩 실행한다.
- 실행 위치는 단계마다 다르다.
  - DART와 JEV 호출은 호스트에서 `uv`로 돈다.
  - Ollama 임베딩과 생성은 compose 네트워크 안의 일회용 앱 이미지 컨테이너에서 돈다. Ollama 포트가 호스트에 열려 있지 않기 때문이다.
- 질문 작성, 통제 주장 작성, 라벨링은 AI 서브에이전트(Claude Opus)와 Codex가 파일로 주고받는다.

**Tech Stack:** Python 3.12, httpx 0.28.1, numpy 2.5.3, scikit-learn 1.9.1, scipy 1.18.1, pytest, OpenDART API, TypeSafe JEV `jev-1.13.0`, Ollama `nomic-embed-text`·`llama3.2:1b`

**Spec:** `docs/superpowers/specs/2026-10-02-evidence-assistant-design.md`(4판, 커밋 a46903f). 실행자는 spec과 이 계획을 함께 읽는다.

## Global Constraints

- JEV 모델은 `jev-1.13.0` 고정이다. 응답 모델이 다르면 실패로 본다.
- 판정 단위 k = 8(고정), 시드 20261002, 홀드아웃 상한 20개사, 조정 상한 10개사.
- 누적 입력 토큰 하드 상한은 20,000,000이다.
- `lab/jev_gate`를 수정하거나 import하지 않는다.
- 키 위치:
  - OpenDART 키는 `DART_API_KEY` 또는 `~/.config/opendart/api_key`(0600)다.
  - JEV 키는 `TYPESAFE_API_KEY` 또는 `~/.config/typesafe/api_key`(0600)다.
  - 키 값을 출력·로그·예외 메시지·Git에 남기지 않는다. lumina `.env*`에 넣지 않는다.
- Git에 넣지 않는 것: 공시 원문 XML, 문단 본문, JEV 호출 원기록. 모두 `lab/data/`(이미 gitignore)에 둔다.
- 커밋하는 것: 원장(접수번호·SHA-256·시각), 문단 ID·해시, 질문, 답변, 주장, 라벨, 집계 결과.
- 홀드아웃 기업은 Stage 0에서 문단 분해까지만 한다. 질문·검색·생성·주장·라벨·판정 명령은 `--split holdout`을 거부한다.
- 공개 문서에는 달러 금액·단가를 쓰지 않고 호출 수와 입력 토큰 수만 쓴다.
- JEV 출력으로 어떤 모델도 학습하지 않는다.
- 모든 모듈·클래스·함수에 한국어 docstring을 단다. 기존 lab 코드처럼 짧게 쓴다.
- 커밋 메시지는 Conventional + 한국어 본문, 끝에 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`를 단다. 커밋 전 `bash ~/.agents/hooks/record-ponytail-review.sh`를 실행한다.

## Review Focus

| 순서 | 입력·조건 | 기대 동작 | 고정하는 테스트 |
|---|---|---|---|
| 1 | DART 응답이 zip이 아니다(키 오류·한도 초과 시 JSON 본문) | 키가 들어 있지 않은 메시지로 실패한다 | Task 2 `test_download_rejects_non_zip_without_key` |
| 2 | 1분 안에 JEV 실패 응답이 온다(5xx, 모델 불일치, 확률 누락, 키 불일치) | 재시도 1회 뒤 `ok=False`, 실패를 캐시하지 않는다, 토큰 상한을 지킨다 | Task 1 테스트 묶음 |
| 3 | 사업보고서 XML에 지정 절 제목 공백이 다르다(`II.사업의 내용`) | 같은 절로 찾는다 | Task 3 `test_sections_tolerates_title_spacing` |
| 4 | 단위 표가 데이터 표와 분리돼 있다(`(단위 : 억원, %)`) | 다음 표 행에 단위를 붙인다 | Task 3 `test_table_rows_carry_unit_and_header` |
| 5 | 홀드아웃 기업으로 질문·판정 명령을 실행한다 | 사전등록 동결 전에는 거부한다 | Task 9 `test_holdout_refused_before_freeze` |

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `app/lib/jev.py` | JEV 요청·검증·캐시·토큰 상한·호출 기록 |
| `app/services/evidence/__init__.py` | 빈 패키지 |
| `app/services/evidence/dart.py` | OpenDART 상장사 목록·사업보고서 선택·원문 다운로드 |
| `app/services/evidence/passages.py` | XML 절 추출, 표 펴기, 600자 문단 묶기 |
| `app/services/evidence/claims.py` | 문장 분해, 숫자 토큰, 통제 변형 값 검사 |
| `app/services/evidence/retrieve.py` | 코사인 상위 k, 임베딩 접두어 |
| `app/services/evidence/generate.py` | 공통 답변 생성 함수 |
| `app/services/evidence/judge.py` | JEV 판정 요청 구성과 점수 |
| `lab/evidence/__init__.py` | 패키지 설명 |
| `lab/evidence/split.py` | 추첨 순서, 후보 조건, 교차 언급 군집, 분할 배정 |
| `lab/evidence/metrics.py` | AUC, κ, 2단 군집 부트스트랩, Clopper–Pearson 상한 |
| `lab/evidence/__main__.py` | 명령줄(단계별 명령) |
| `lab/evidence/prompts/*.md` | 질문 작성자·통제 주장 작성자·라벨러 지시문 |
| `lab/evidence/prereg.json` | 사전등록 v1 |
| `tests/evidence/test_*.py` | 단위 테스트 |
| `docs/lab/evidence-stage0-report.md` | Stage 0 리포트(Task 14에서 생성) |

## 실행 환경

테스트(호스트):

```bash
cat > /tmp/evt.sh <<'EOF'
#!/bin/bash
cd /Users/noah/portfolios/lumina-invest
exec uv run -q --no-project --python 3.12 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 --with scipy==1.18.1 --with pytest python -m pytest -p no:cacheprovider "$@"
EOF
chmod +x /tmp/evt.sh
```

명령줄(호스트, DART·JEV):

```bash
cat > /tmp/ev.sh <<'EOF'
#!/bin/bash
cd /Users/noah/portfolios/lumina-invest
exec uv run -q --no-project --python 3.12 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 --with scipy==1.18.1 python -m lab.evidence "$@"
EOF
chmod +x /tmp/ev.sh
```

명령줄(컨테이너, Ollama):

```bash
cat > /tmp/evd.sh <<'EOF'
#!/bin/bash
exec docker run --rm --network lumina-portfolio_default -v /Users/noah/portfolios/lumina-invest:/w -w /w \
  -e OLLAMA_BASE_URL=http://ollama:11434 lumina-portfolio-app python -m lab.evidence "$@"
EOF
chmod +x /tmp/evd.sh
```

---

### Task 1: JEV 클라이언트

**Files:**
- Create: `app/lib/jev.py`
- Test: `tests/evidence/test_jev.py`

**Interfaces:**
- Produces:
  - `MODEL: str`
  - `class TokenCapExceeded(RuntimeError)`
  - `@dataclass JevResult(key: str, ok: bool, answers: dict[str, dict[str, float]] | None, latency_ms: float, input_tokens: int, attempts: int, error: str | None, cached: bool = False)`
  - `load_api_key() -> str`
  - `request_key(state: str, questions: dict) -> str`
  - `parse_answers(payload: dict, questions: dict) -> dict[str, dict[str, float]]`
  - `class JevClient(log_path: Path, token_cap: int, client: httpx.Client | None = None, api_key: str | None = None, clock=time.perf_counter)`, 속성 `used_tokens: int`, 메서드 `ask(state: str, questions: dict, *, use_cache: bool = True, tag: str = "") -> JevResult`
  - 호출 기록 JSONL 한 줄(HTTP 시도마다): `{"key","tag","attempt","ok","answers","latency_ms","input_tokens","error","use_cache","called_at"}`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_jev.py
import json

import httpx
import pytest

from app.lib import jev

Q = {"p1": {"type": "choice", "instructions": "x", "criteria": {"supports": "a", "contradicts": "b", "says_nothing": "c"}}}


def _payload(probs=None, model=jev.MODEL, tokens=100):
    probs = probs or {"supports": 0.7, "contradicts": 0.1, "says_nothing": 0.2}
    return {"model": model, "answers": {"p1": {"type": "choice", "choice": "supports", "confidence": 0.5, "probabilities": probs}},
            "usage": {"input_tokens": tokens, "output_tokens": 5}}


def _client(responses, seen):
    it = iter(responses)

    def handler(request):
        seen.append(request)
        status, body = next(it)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _jc(tmp_path, responses, seen, cap=10_000):
    return jev.JevClient(tmp_path / "calls.jsonl", cap, client=_client(responses, seen), api_key="SECRET-KEY")


def test_ask_parses_probabilities_and_logs_without_key(tmp_path):
    seen = []
    r = _jc(tmp_path, [(200, _payload())], seen).ask("state", Q, tag="t")
    assert r.ok and r.answers["p1"]["supports"] == 0.7 and r.input_tokens == 100
    body = json.loads(seen[0].content)
    assert body["model"] == "jev-1.13.0" and body["questions"] == Q
    log = (tmp_path / "calls.jsonl").read_text()
    assert "SECRET-KEY" not in log and json.loads(log)["tag"] == "t"


def test_cache_hit_skips_http_and_survives_restart(tmp_path):
    seen = []
    _jc(tmp_path, [(200, _payload())], seen).ask("state", Q)
    again = jev.JevClient(tmp_path / "calls.jsonl", 10_000, client=_client([], seen), api_key="k")
    r = again.ask("state", Q)
    assert r.cached and len(seen) == 1 and again.used_tokens == 100


def test_no_cache_forces_new_request(tmp_path):
    seen = []
    c = _jc(tmp_path, [(200, _payload()), (200, _payload())], seen)
    c.ask("state", Q)
    assert not c.ask("state", Q, use_cache=False).cached and len(seen) == 2


@pytest.mark.parametrize("bad", [
    _payload(model="jev-latest"),
    _payload(probs={"supports": 1.0}),
    _payload(probs={"supports": 0.7, "contradicts": 0.1, "says_nothing": 0.1}),
    _payload(probs={"supports": 1.5, "contradicts": -0.5, "says_nothing": 0.0}),
])
def test_invalid_answers_fail_after_one_retry_and_are_not_cached(tmp_path, bad):
    seen = []
    c = _jc(tmp_path, [(200, bad), (200, bad)], seen)
    r = c.ask("state", Q)
    assert not r.ok and r.attempts == 2 and len(seen) == 2
    lines = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [x["attempt"] for x in lines] == [1, 2] and not any(x["ok"] for x in lines)


def test_retry_recovers_from_server_error(tmp_path):
    seen = []
    r = _jc(tmp_path, [(500, {"error": "x"}), (200, _payload())], seen).ask("state", Q)
    assert r.ok and r.attempts == 2


def test_token_cap_blocks_before_http(tmp_path):
    seen = []
    c = _jc(tmp_path, [(200, _payload(tokens=100))], seen, cap=100)
    c.ask("a", Q)
    with pytest.raises(jev.TokenCapExceeded):
        c.ask("b", Q)
    assert len(seen) == 1


def test_load_api_key_rejects_open_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / ".config/typesafe/api_key"
    p.parent.mkdir(parents=True)
    p.write_text("k\n")
    p.chmod(0o644)
    with pytest.raises(PermissionError):
        jev.load_api_key()
    p.chmod(0o600)
    assert jev.load_api_key() == "k"
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_jev.py -v`
Expected: FAIL. `ModuleNotFoundError: No module named 'app.lib.jev'`

- [ ] **Step 3: 구현**

```python
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
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_jev.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add app/lib/jev.py tests/evidence/test_jev.py
git commit -m "feat: JEV 판정 클라이언트(질문 묶음·검증·캐시·토큰 상한)"
```

### Task 2: DART 수집기

**Files:**
- Create: `app/services/evidence/__init__.py` (빈 파일), `app/services/evidence/dart.py`
- Test: `tests/evidence/test_dart.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Corp(corp_code: str, corp_name: str, stock_code: str)`
  - `load_dart_key() -> str`
  - `parse_corp_codes(xml_bytes: bytes) -> list[Corp]`(상장사만)
  - `fetch_corp_codes(client: httpx.Client, key: str) -> bytes`(CORPCODE.xml 본문)
  - `list_annual_reports(client, key, corp_code, bgn="20260101", end="20261001") -> list[dict]`
  - `pick_annual_report(items: list[dict], period="2025.12", until="20261001") -> dict | None`
  - `company_info(client, key, corp_code) -> dict`
  - `download_document(client, key, rcept_no: str, dest_dir: Path, ledger: Path) -> Path`
  - 원장 JSONL: `{"rcept_no","sha256","bytes","downloaded_at"}`
  - 모든 HTTP 오류는 `RuntimeError("DART <path> <오류 종류>")`로 바꾼다(URL의 키가 메시지에 섞이지 않게).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_dart.py
import io
import json
import zipfile

import httpx
import pytest

from app.services.evidence import dart

CORP_XML = """<?xml version="1.0" encoding="UTF-8"?><result>
<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name><stock_code>005930</stock_code></list>
<list><corp_code>00999999</corp_code><corp_name>비상장</corp_name><stock_code> </stock_code></list>
</result>""".encode()


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _client(routes, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        status, content = routes[request.url.path]
        if isinstance(content, (dict, list)):
            return httpx.Response(status, json=content)
        return httpx.Response(status, content=content)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parse_corp_codes_keeps_listed_only():
    assert dart.parse_corp_codes(CORP_XML) == [dart.Corp("00126380", "삼성전자", "005930")]


def test_fetch_corp_codes_unzips():
    c = _client({"/api/corpCode.xml": (200, _zip({"CORPCODE.xml": CORP_XML}))})
    assert dart.fetch_corp_codes(c, "K") == CORP_XML


def test_pick_annual_report_prefers_latest_correction_before_cutoff():
    items = [
        {"report_nm": "사업보고서 (2025.12)", "rcept_dt": "20260310", "rcept_no": "1"},
        {"report_nm": "[기재정정]사업보고서 (2025.12)", "rcept_dt": "20260601", "rcept_no": "2"},
        {"report_nm": "[기재정정]사업보고서 (2025.12)", "rcept_dt": "20261005", "rcept_no": "3"},
        {"report_nm": "반기보고서 (2026.06)", "rcept_dt": "20260814", "rcept_no": "4"},
    ]
    assert dart.pick_annual_report(items)["rcept_no"] == "2"
    assert dart.pick_annual_report(items[3:]) is None


def test_list_annual_reports_handles_no_data_and_errors():
    ok = _client({"/api/list.json": (200, {"status": "013", "message": "조회된 데이타가 없습니다."})})
    assert dart.list_annual_reports(ok, "K", "1") == []
    bad = _client({"/api/list.json": (200, {"status": "020", "message": "요청 제한"})})
    with pytest.raises(RuntimeError, match="020"):
        dart.list_annual_reports(bad, "K", "1")


def test_download_writes_largest_xml_and_ledger(tmp_path):
    doc = _zip({"a.xml": "<small/>", "b.xml": "<DOCUMENT>" + "x" * 100 + "</DOCUMENT>"})
    c = _client({"/api/document.xml": (200, doc)})
    p = dart.download_document(c, "K", "2026", tmp_path / "docs", tmp_path / "ledger.jsonl")
    assert p.read_text().startswith("<DOCUMENT>")
    rec = json.loads((tmp_path / "ledger.jsonl").read_text())
    assert rec["rcept_no"] == "2026" and len(rec["sha256"]) == 64
    again = _client({})
    assert dart.download_document(again, "K", "2026", tmp_path / "docs", tmp_path / "ledger.jsonl") == p


def test_download_rejects_non_zip_without_key(tmp_path):
    c = _client({"/api/document.xml": (200, {"status": "010", "message": "등록되지 않은 키"})})
    with pytest.raises(ValueError) as e:
        dart.download_document(c, "SECRET-KEY", "1", tmp_path, tmp_path / "l.jsonl")
    assert "SECRET-KEY" not in str(e.value)


def test_http_error_message_hides_key():
    c = _client({"/api/company.json": (500, {"x": 1})})
    with pytest.raises(RuntimeError) as e:
        dart.company_info(c, "SECRET-KEY", "1")
    assert "SECRET-KEY" not in str(e.value)
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_dart.py -v`
Expected: FAIL. `ModuleNotFoundError: No module named 'app.services.evidence'`

- [ ] **Step 3: 구현**

```python
# app/services/evidence/dart.py
"""OpenDART 공시 수집: 상장사 목록, 사업보고서 접수번호 선택, 원문 XML 다운로드.

키는 쿼리 파라미터로만 보낸다. HTTP 오류는 URL(키 포함)이 섞이지 않은 메시지로 바꾼다.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = "https://opendart.fss.or.kr/api"


@dataclass(frozen=True)
class Corp:
    """상장사 한 곳(OpenDART 고유번호·이름·종목코드)."""

    corp_code: str
    corp_name: str
    stock_code: str


def load_dart_key() -> str:
    """DART_API_KEY 환경 변수, 없으면 소유자 전용(0600) ~/.config/opendart/api_key에서 읽는다."""
    key = os.environ.get("DART_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".config/opendart/api_key"
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PermissionError(f"{path} 권한은 0600이어야 한다")
    return path.read_text().strip()


def _get(client: httpx.Client, path: str, params: dict) -> httpx.Response:
    """GET 요청. 실패 메시지에 URL(키 포함)을 넣지 않는다."""
    try:
        resp = client.get(f"{BASE}/{path}", params=params)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"DART {path} {type(exc).__name__}") from None
    return resp


def _zip_member(content: bytes) -> bytes:
    """zip 안에서 가장 큰 .xml 파일 본문을 꺼낸다. zip이 아니면 ValueError."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ValueError(f"DART 응답이 zip이 아니다: {content[:120]!r}") from None
    names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    if not names:
        raise ValueError("zip 안에 xml이 없다")
    return zf.read(max(names, key=lambda n: zf.getinfo(n).file_size))


def _json(client: httpx.Client, path: str, params: dict) -> dict:
    """JSON API 호출. 상태 000(정상)·013(데이터 없음)만 허용한다."""
    data = _get(client, path, params).json()
    if data.get("status") not in ("000", "013"):
        raise RuntimeError(f"DART {path} status {data.get('status')}: {data.get('message')}")
    return data


def parse_corp_codes(xml_bytes: bytes) -> list[Corp]:
    """CORPCODE.xml에서 종목코드가 있는(상장) 회사만 꺼낸다."""
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.iter("list"):
        stock = (e.findtext("stock_code") or "").strip()
        if stock:
            out.append(Corp(e.findtext("corp_code").strip(), e.findtext("corp_name").strip(), stock))
    return out


def fetch_corp_codes(client: httpx.Client, key: str) -> bytes:
    """전체 고유번호 zip을 받아 CORPCODE.xml 본문을 돌려준다."""
    return _zip_member(_get(client, "corpCode.xml", {"crtfc_key": key}).content)


def list_annual_reports(client: httpx.Client, key: str, corp_code: str,
                        bgn: str = "20260101", end: str = "20261001") -> list[dict]:
    """정기공시(A) 목록. 데이터가 없으면 빈 목록."""
    data = _json(client, "list.json", {"crtfc_key": key, "corp_code": corp_code, "bgn_de": bgn,
                                       "end_de": end, "pblntf_ty": "A", "page_count": 100})
    return data.get("list", [])


def pick_annual_report(items: list[dict], period: str = "2025.12", until: str = "20261001") -> dict | None:
    """기준일까지 제출된 해당 기간 사업보고서 중 최종본(정정 포함)을 고른다."""
    cands = [r for r in items if f"사업보고서 ({period})" in r["report_nm"] and r["rcept_dt"] <= until]
    return max(cands, key=lambda r: (r["rcept_dt"], r["rcept_no"])) if cands else None


def company_info(client: httpx.Client, key: str, corp_code: str) -> dict:
    """기업개황(업종코드 induty_code 포함)."""
    return _json(client, "company.json", {"crtfc_key": key, "corp_code": corp_code})


def download_document(client: httpx.Client, key: str, rcept_no: str, dest_dir: Path, ledger: Path) -> Path:
    """공시 원문 XML을 저장하고 원장에 SHA-256을 남긴다. 이미 있으면 다시 받지 않는다."""
    path = Path(dest_dir) / f"{rcept_no}.xml"
    if path.exists():
        return path
    data = _zip_member(_get(client, "document.xml", {"crtfc_key": key, "rcept_no": rcept_no}).content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    Path(ledger).parent.mkdir(parents=True, exist_ok=True)
    with Path(ledger).open("a") as f:
        f.write(json.dumps({"rcept_no": rcept_no, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                            "downloaded_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    return path
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_dart.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/evidence/__init__.py app/services/evidence/dart.py tests/evidence/test_dart.py
git commit -m "feat: OpenDART 사업보고서 수집기(최종 정정본 선택·원장·키 비노출)"
```

### Task 3: 문단 분해기

**Files:**
- Create: `app/services/evidence/passages.py`
- Test: `tests/evidence/test_passages.py`

**Interfaces:**
- Produces:
  - `MAX_CHARS = 600`
  - `@dataclass(frozen=True) Passage(id: str, corp_code: str, rcept_no: str, section: str, idx: int, text: str)`, 속성 `sha256 -> str`
  - `sections(xml: str) -> dict[str, str]`(키 `"I1"`, `"II"`)
  - `blocks(section_xml: str) -> list[tuple[str, str]]`(종류 `"title"|"para"|"row"`)
  - `pack(blocks: list[tuple[str, str]], max_chars: int = MAX_CHARS) -> list[str]`
  - `build_passages(corp_code: str, rcept_no: str, xml: str) -> list[Passage]`. 절이 빠지면 `ValueError("missing sections: ...")`. ID는 `f"{corp_code}-{section}-{idx:04d}"`다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_passages.py
from app.services.evidence import passages as ps

XML = """<DOCUMENT><BODY>
<SECTION-1><TITLE ATOC="Y">I. 회사의 개요</TITLE>
<SECTION-2><TITLE>1. 회사의 개요</TITLE><P>당사는 반도체를 만듭니다.</P></SECTION-2>
<SECTION-2><TITLE>2. 회사의 연혁</TITLE><P>연혁 문단.</P></SECTION-2>
</SECTION-1>
<SECTION-1><TITLE ENG="II">II.사업의  내용</TITLE>
<SECTION-2><TITLE>1. 사업의 개요</TITLE>
<P>첫 문장입니다. 둘째 문장입니다.</P>
<TABLE><TBODY><TR><TD>(단위 : 억원, %)</TD></TR></TBODY></TABLE>
<TABLE><THEAD><TR><TH>부문</TH><TH>매출액</TH></TR></THEAD>
<TBODY><TR><TD>DX 부문</TD><TD>1,006,771</TD></TR><TR><TE>DS 부문</TE><TE>2,092,317</TE></TR></TBODY></TABLE>
</SECTION-2></SECTION-1>
<SECTION-1><TITLE>III. 재무에 관한 사항</TITLE><P>제외.</P></SECTION-1>
</BODY></DOCUMENT>"""


def test_sections_tolerates_title_spacing():
    s = ps.sections(XML)
    assert set(s) == {"I1", "II"}
    assert "연혁" not in s["I1"] and "제외" not in s["II"]


def test_table_rows_carry_unit_and_header():
    rows = [t for k, t in ps.blocks(ps.sections(XML)["II"]) if k == "row"]
    assert rows == ["[1. 사업의 개요 표, 단위 억원, %] 부문: DX 부문 | 매출액: 1,006,771",
                    "[1. 사업의 개요 표, 단위 억원, %] 부문: DS 부문 | 매출액: 2,092,317"]


def test_pack_keeps_chunks_under_limit_and_splits_long_paragraph():
    long = "가" * 400 + ". " + "나" * 400 + "."
    chunks = ps.pack([("title", "제목"), ("para", long), ("para", "짧은 문단.")], max_chars=600)
    assert len(chunks) == 2 and chunks[0].startswith("[제목] ")
    assert chunks[1].endswith("짧은 문단.")


def test_oversized_single_sentence_is_kept_whole():
    one = "다" * 700 + "."
    assert ps.pack([("para", one)], max_chars=600) == [one]


def test_build_passages_ids_and_missing_section():
    out = ps.build_passages("00126380", "2026", XML)
    assert out[0].id == "00126380-I1-0000" and out[0].text == "[1. 회사의 개요] 당사는 반도체를 만듭니다."
    assert {p.section for p in out} == {"I1", "II"} and len(out[0].sha256) == 64
    import pytest
    with pytest.raises(ValueError, match="missing sections"):
        ps.build_passages("1", "2", "<DOCUMENT></DOCUMENT>")
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_passages.py -v`
Expected: FAIL. `ImportError: cannot import name 'passages'`

- [ ] **Step 3: 구현**

```python
# app/services/evidence/passages.py
"""DART 사업보고서 XML에서 지정 절을 꺼내 근거 후보 문단으로 나눈다.

DART XML은 SECTION-1/2의 TITLE, P, TABLE(셀은 TD·TH·TE·TU)로 이뤄진다.
단위는 데이터 표 앞의 한 칸짜리 표나 문단에 "(단위 : 억원)"처럼 따로 적힌다.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

MAX_CHARS = 600
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_UNIT = re.compile(r"단위\s*[:：]\s*([^)\]]+)")
_SENT_END = re.compile(r"(?<=[.!?])\s+")
_BLOCK = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>|<P\b[^>]*>(.*?)</P>|<TABLE\b[^>]*>(.*?)</TABLE>", re.S)
_ROW = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.S)
_CELL = re.compile(r"<(TD|TH|TE|TU)\b[^>]*>(.*?)</\1>", re.S)


@dataclass(frozen=True)
class Passage:
    """근거 후보 문단 하나."""

    id: str
    corp_code: str
    rcept_no: str
    section: str
    idx: int
    text: str

    @property
    def sha256(self) -> str:
        """본문 SHA-256(커밋하는 매니페스트용)."""
        return hashlib.sha256(self.text.encode()).hexdigest()


def _text(fragment: str) -> str:
    """태그를 지우고 엔티티를 풀고 공백을 하나로 줄인다."""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _title(block: str) -> str:
    m = re.search(r"<TITLE\b[^>]*>(.*?)</TITLE>", block, re.S)
    return _text(m.group(1)) if m else ""


def _key(title: str) -> str:
    return title.replace(" ", "")


def sections(xml: str) -> dict[str, str]:
    """'I. 회사의 개요 > 1. 회사의 개요'(I1)와 'II. 사업의 내용'(II) 블록을 찾는다. 제목 공백은 무시한다."""
    out: dict[str, str] = {}
    for m in re.finditer(r"<SECTION-1\b.*?</SECTION-1>", xml, re.S):
        block = m.group(0)
        title = _key(_title(block))
        if title.startswith("II.사업의내용"):
            out["II"] = block
        elif title.startswith("I.회사의개요"):
            for m2 in re.finditer(r"<SECTION-2\b.*?</SECTION-2>", block, re.S):
                if _key(_title(m2.group(0))).startswith("1.회사의개요"):
                    out["I1"] = m2.group(0)
                    break
    return out


def blocks(section_xml: str) -> list[tuple[str, str]]:
    """절 안의 제목·문단·표 행을 문서 순서대로 펼친다. 표 행에는 제목·단위·열 머리글을 붙인다."""
    out: list[tuple[str, str]] = []
    heading, unit = "", ""
    for m in _BLOCK.finditer(section_xml):
        t, p, tb = m.groups()
        if t is not None:
            heading, unit = _text(t), ""
            out.append(("title", heading))
            continue
        if p is not None:
            txt = _text(p)
            if not txt:
                continue
            u = _UNIT.search(txt)
            if u and len(txt) < 60:
                unit = u.group(1).strip()
                continue
            out.append(("para", txt))
            continue
        rows = [[_text(c) for _, c in _CELL.findall(r)] for r in _ROW.findall(tb)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        if len(rows) == 1 and len(rows[0]) == 1:
            u = _UNIT.search(rows[0][0])
            if u:
                unit = u.group(1).strip()
            else:
                out.append(("para", rows[0][0]))
            continue
        header = rows[0]
        prefix = f"[{heading} 표" + (f", 단위 {unit}" if unit else "") + "] "
        for r in rows[1:]:
            cells = [f"{h}: {c}" if h else c for h, c in zip(header, r)] if len(r) == len(header) else r
            out.append(("row", prefix + " | ".join(cells)))
    return out


def pack(blocks: list[tuple[str, str]], max_chars: int = MAX_CHARS) -> list[str]:
    """제목 경계에서 끊고, 문단·표 행을 max_chars 이하로 묶는다. 한 문장·한 행이 넘치면 그대로 둔다."""
    units: list[tuple[str, str]] = []
    for kind, txt in blocks:
        if kind == "para" and len(txt) > max_chars:
            units += [("para", s) for s in _SENT_END.split(txt) if s]
        else:
            units.append((kind, txt))
    chunks: list[str] = []
    cur: list[str] = []
    heading = ""
    for kind, txt in units:
        if kind == "title":
            if cur:
                chunks.append(" ".join(cur))
                cur = []
            heading = txt
            continue
        if cur and len(" ".join(cur)) + 1 + len(txt) > max_chars:
            chunks.append(" ".join(cur))
            cur = []
        if not cur and kind == "para" and heading:
            txt = f"[{heading}] {txt}"
        cur.append(txt)
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def build_passages(corp_code: str, rcept_no: str, xml: str) -> list[Passage]:
    """지정 절 두 개를 문단 목록으로 만든다. 하나라도 없으면 ValueError."""
    secs = sections(xml)
    missing = [s for s in ("I1", "II") if s not in secs]
    if missing:
        raise ValueError(f"missing sections: {missing}")
    out: list[Passage] = []
    for sec in ("I1", "II"):
        for i, text in enumerate(pack(blocks(secs[sec]))):
            out.append(Passage(f"{corp_code}-{sec}-{i:04d}", corp_code, rcept_no, sec, i, text))
    return out
```

참고: `test_pack_keeps_chunks_under_limit_and_splits_long_paragraph`에서 제목 접두어 `[제목] `은 첫 문장 단위에 붙는다. 400자 문장 두 개는 합치면 600자를 넘으므로 두 청크가 되고, "짧은 문단."은 둘째 청크에 붙는다.

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_passages.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/evidence/passages.py tests/evidence/test_passages.py
git commit -m "feat: 사업보고서 절 추출·표 펴기·문단 묶기"
```

### Task 4: 주장 분해와 통제 변형 검사

**Files:**
- Create: `app/services/evidence/claims.py`
- Test: `tests/evidence/test_claims.py`

**Interfaces:**
- Produces:
  - `split_sentences(text: str) -> list[str]`
  - `numeric_tokens(text: str) -> set[str]`(쉼표 제거)
  - `new_values_absent(variant: str, source: str, passages: list[str], names: tuple[str, ...] = ()) -> bool`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_claims.py
from app.services.evidence import claims


def test_split_sentences_handles_bullets_and_newlines():
    text = "삼성전자는 반도체를 만든다. 매출은 300조다!\n- 주요 제품은 DRAM이다\n1) 네."
    assert claims.split_sentences(text) == ["삼성전자는 반도체를 만든다.", "매출은 300조다!", "주요 제품은 DRAM이다"]


def test_numeric_tokens_strip_commas():
    assert claims.numeric_tokens("매출 1,006,771억원, 비중 33.0%, 2025년") == {"1006771", "33.0", "2025"}


def test_new_values_absent():
    passages = ["DX 부문 매출액: 1,006,771", "2025년 사업"]
    assert claims.new_values_absent("DX 매출은 2,000,000억원", "DX 매출은 1,006,771억원", passages)
    assert not claims.new_values_absent("2025년 매출", "2024년 매출", passages)
    assert not claims.new_values_absent("LG화학이 만든다", "삼성전자가 만든다", ["LG화학 공급"], ("LG화학",))
    assert claims.new_values_absent("A는 만들지 않는다", "A는 만든다", passages)
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_claims.py -v`
Expected: FAIL. `ImportError: cannot import name 'claims'`

- [ ] **Step 3: 구현**

```python
# app/services/evidence/claims.py
"""답변을 주장(문장) 단위로 나누고, 통제 변형에 새로 넣은 값이 문단에 없는지 검사한다."""
from __future__ import annotations

import re

_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s*")
_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def split_sentences(text: str) -> list[str]:
    """줄바꿈과 문장부호로 나누고 글머리표를 뗀다. 4자 미만 조각은 버린다."""
    out = []
    for line in text.splitlines():
        line = _BULLET.sub("", line).strip()
        for s in _SPLIT.split(line):
            s = s.strip()
            if len(s) >= 4:
                out.append(s)
    return out


def numeric_tokens(text: str) -> set[str]:
    """숫자 표기(쉼표 제거)를 모은다. 단위 환산은 하지 않는다(Stage 1 숫자 대조에서 한다)."""
    return {t.replace(",", "") for t in _NUM.findall(text)}


def new_values_absent(variant: str, source: str, passages: list[str], names: tuple[str, ...] = ()) -> bool:
    """변형이 원문 참 문장에 없던 숫자·회사명을 넣었다면, 그 값이 문단 어디에도 없어야 한다.

    부분 문자열로 보수적으로 검사한다(예: '12'가 '2012' 안에 있어도 있음으로 본다).
    """
    blob = " ".join(passages).replace(",", "")
    if any(n in blob for n in numeric_tokens(variant) - numeric_tokens(source)):
        return False
    return not any(n in variant and n not in source and n in blob for n in names)
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_claims.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/evidence/claims.py tests/evidence/test_claims.py
git commit -m "feat: 주장 분해와 통제 변형 값 부재 검사"
```

### Task 5: 검색과 답변 생성

**Files:**
- Create: `app/services/evidence/retrieve.py`, `app/services/evidence/generate.py`
- Test: `tests/evidence/test_retrieve_generate.py`

**Interfaces:**
- Consumes: LLM 객체. `async chat(model, messages, options) -> str`, `async embed(model, text) -> list[float]`(`app/lib/ollama.OllamaClient`와 같은 모양)
- Produces:
  - `retrieve.QUERY_PREFIX = "search_query: "`, `retrieve.DOC_PREFIX = "search_document: "`
  - `retrieve.top_k(query: Sequence[float], vectors: Sequence[Sequence[float]], k: int = 8) -> list[int]`(동점은 앞 인덱스 우선)
  - `generate.SYSTEM: str`, `generate.OPTIONS: dict`, `generate.PROMPT_SHA: str`
  - `generate.build_messages(company: str, question: str, passages: list[str]) -> list[dict]`
  - `async generate.generate_answer(llm, model: str, company: str, question: str, passages: list[str]) -> str`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_retrieve_generate.py
import asyncio

from app.services.evidence import generate, retrieve


def test_top_k_orders_by_cosine_and_breaks_ties_by_index():
    vecs = [[1, 0], [0, 1], [1, 0], [0.9, 0.1]]
    assert retrieve.top_k([1, 0], vecs, k=3) == [0, 2, 3]


def test_build_messages_numbers_passages():
    m = generate.build_messages("삼성전자", "주요 제품은?", ["DRAM", "TV"])
    assert m[0]["role"] == "system" and "[문단 1] DRAM\n[문단 2] TV" in m[1]["content"]
    assert m[1]["content"].endswith("[질문] 주요 제품은?")


def test_generate_answer_uses_fixed_options():
    seen = {}

    class LLM:
        async def chat(self, model, messages, options=None):
            seen.update(model=model, options=options)
            return "  답변입니다.  "

    out = asyncio.run(generate.generate_answer(LLM(), "llama3.2:1b", "삼성전자", "Q", ["P"]))
    assert out == "답변입니다." and seen["options"] == {"temperature": 0, "seed": 20261002}
    assert len(generate.PROMPT_SHA) == 64
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_retrieve_generate.py -v`
Expected: FAIL. `ImportError`

- [ ] **Step 3: 구현**

```python
# app/services/evidence/retrieve.py
"""기업 문단 중 질문과 코사인 유사도가 높은 상위 k개를 고른다.

nomic-embed-text는 질의·문서 접두어를 붙여 임베딩한다.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

QUERY_PREFIX = "search_query: "
DOC_PREFIX = "search_document: "


def top_k(query: Sequence[float], vectors: Sequence[Sequence[float]], k: int = 8) -> list[int]:
    """코사인 유사도 내림차순 상위 k개 인덱스. 동점은 앞 인덱스가 먼저다."""
    q = np.asarray(query, dtype=float)
    m = np.asarray(vectors, dtype=float)
    sims = (m @ q) / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-12)
    return [int(i) for i in np.argsort(-sims, kind="stable")[:k]]
```

```python
# app/services/evidence/generate.py
"""질문과 근거 문단을 명시적으로 받아 한국어 답변을 한 번 생성한다(평가·채팅 연결 공용)."""
from __future__ import annotations

import hashlib
import json

SYSTEM = ("당신은 상장사 사업보고서를 읽고 질문에 답하는 리서치 보조입니다. "
          "아래 [문단]에 적힌 내용만 근거로 한국어 3~5문장으로 답하세요. 문단에 없는 내용은 추측하지 마세요.")
OPTIONS = {"temperature": 0, "seed": 20261002}
PROMPT_SHA = hashlib.sha256((SYSTEM + json.dumps(OPTIONS, sort_keys=True)).encode()).hexdigest()


def build_messages(company: str, question: str, passages: list[str]) -> list[dict]:
    """시스템 지시와 [회사]·[문단 n]·[질문]으로 된 사용자 메시지를 만든다."""
    ctx = "\n".join(f"[문단 {i}] {t}" for i, t in enumerate(passages, 1))
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"[회사] {company}\n{ctx}\n\n[질문] {question}"}]


async def generate_answer(llm, model: str, company: str, question: str, passages: list[str]) -> str:
    """고정 옵션(temperature 0, seed)으로 한 번 생성한다."""
    return (await llm.chat(model, build_messages(company, question, passages), OPTIONS)).strip()
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_retrieve_generate.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/evidence/retrieve.py app/services/evidence/generate.py tests/evidence/test_retrieve_generate.py
git commit -m "feat: 코사인 상위 k 검색과 공통 답변 생성 함수"
```

### Task 6: JEV 판정기(Stage 0 부분)

**Files:**
- Create: `app/services/evidence/judge.py`
- Test: `tests/evidence/test_judge.py`

**Interfaces:**
- Consumes: Task 1 `JevClient.ask(...) -> JevResult`
- Produces:
  - `INSTRUCTIONS: str`(`{j}` 자리 표시자 포함), `CRITERIA: dict[str, str]`, `QUESTION_SHA: str`
  - `build_state(company: str, claim: str, passages: list[str]) -> str`
  - `build_questions(n: int, only: list[int] | None = None) -> dict`
  - `@dataclass Judgement(s: list[float], c: list[float], ok: bool, requests: int)`
  - `judge_claim(client, company: str, claim: str, passages: list[str], *, use_cache: bool = True, tag: str = "", single: bool = False) -> Judgement`
  - `jev_score(j: Judgement) -> float`(Stage 0: `max(s)`, 실패면 0)
  - `passage_labels(j: Judgement) -> list[str]`(문단별 최댓값 선택지. 반복·묶기 검사용)

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_judge.py
from app.lib.jev import JevResult
from app.services.evidence import judge


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def ask(self, state, questions, *, use_cache=True, tag=""):
        self.calls.append((state, sorted(questions), use_cache, tag))
        if self.fail:
            return JevResult("k", False, None, 1.0, 0, 2, "x")
        ans = {q: {"supports": 0.1 * int(q[1:]), "contradicts": 0.05, "says_nothing": 1 - 0.1 * int(q[1:]) - 0.05}
               for q in questions}
        return JevResult("k", True, ans, 1.0, 10, 1, None)


def test_state_and_questions_shape():
    st = judge.build_state("삼성전자", "주장", ["A", "B"])
    assert st == "[Company] 삼성전자\n[Claim] 주장\n[Passage 1] A\n[Passage 2] B"
    qs = judge.build_questions(3, only=[2])
    assert list(qs) == ["p2"] and qs["p2"]["type"] == "choice" and "[Passage 2]" in qs["p2"]["instructions"]
    assert set(qs["p2"]["criteria"]) == {"supports", "contradicts", "says_nothing"}


def test_batched_judge_one_request_and_score():
    c = FakeClient()
    j = judge.judge_claim(c, "회사", "주장", ["a", "b", "c"], tag="t")
    assert j.ok and j.requests == 1 and len(c.calls) == 1
    assert j.s == [0.1, 0.2, 0.30000000000000004] or abs(j.s[2] - 0.3) < 1e-9
    assert abs(judge.jev_score(j) - 0.3) < 1e-9
    assert judge.passage_labels(j)[0] == "says_nothing"


def test_single_mode_sends_one_question_per_request_with_same_state():
    c = FakeClient()
    j = judge.judge_claim(c, "회사", "주장", ["a", "b"], single=True)
    assert j.requests == 2 and [q for _, q, _, _ in c.calls] == [["p1"], ["p2"]]
    assert c.calls[0][0] == c.calls[1][0]


def test_failure_scores_zero():
    j = judge.judge_claim(FakeClient(fail=True), "회사", "주장", ["a", "b"])
    assert not j.ok and judge.jev_score(j) == 0.0 and j.s == [0.0, 0.0]
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_judge.py -v`
Expected: FAIL. `ImportError`

- [ ] **Step 3: 구현**

```python
# app/services/evidence/judge.py
"""주장 하나와 문단 k개로 JEV Choice 요청을 만들고 문단별 지지·반박 확률을 받는다.

Stage 0은 JEV 단독 점수(문단별 지지 확률 최댓값)만 쓴다. 숫자 대조와 SYS 규칙은 Stage 1에서 붙인다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

INSTRUCTIONS = ("The state has one [Claim] about a Korean listed company and numbered [Passage] blocks "
                "from its annual report. How does [Passage {j}] relate to the [Claim]? "
                "Judge only from that passage, not from outside knowledge.")
CRITERIA = {
    "supports": "The passage states every fact in the claim, possibly in other words, "
                "for the same company, period and metric.",
    "contradicts": "The passage states something about the same company, period and metric "
                   "that conflicts with the claim.",
    "says_nothing": "The passage neither states nor conflicts with the claim's facts, "
                    "or it covers only part of them.",
}
QUESTION_SHA = hashlib.sha256(json.dumps([INSTRUCTIONS, CRITERIA], ensure_ascii=False,
                                         sort_keys=True).encode()).hexdigest()


@dataclass
class Judgement:
    """문단별 지지(s)·반박(c) 확률. ok=False면 모두 0이다."""

    s: list[float]
    c: list[float]
    ok: bool
    requests: int


def build_state(company: str, claim: str, passages: list[str]) -> str:
    """[Company]·[Claim]·[Passage n] 줄로 된 state."""
    lines = [f"[Company] {company}", f"[Claim] {claim}"]
    lines += [f"[Passage {j}] {t}" for j, t in enumerate(passages, 1)]
    return "\n".join(lines)


def build_questions(n: int, only: list[int] | None = None) -> dict:
    """문단마다 Choice 질문 하나(p1..pn). only가 있으면 그 문단만."""
    return {f"p{j}": {"type": "choice", "instructions": INSTRUCTIONS.format(j=j), "criteria": CRITERIA}
            for j in (only or range(1, n + 1))}


def judge_claim(client, company: str, claim: str, passages: list[str], *, use_cache: bool = True,
                tag: str = "", single: bool = False) -> Judgement:
    """묶음 모드는 요청 1회, single 모드는 같은 state로 문단마다 요청 1회. 하나라도 실패하면 전부 0."""
    n = len(passages)
    state = build_state(company, claim, passages)
    groups = [[j] for j in range(1, n + 1)] if single else [list(range(1, n + 1))]
    s, c = [0.0] * n, [0.0] * n
    for g in groups:
        r = client.ask(state, build_questions(n, g), use_cache=use_cache, tag=tag)
        if not r.ok:
            return Judgement([0.0] * n, [0.0] * n, False, len(groups))
        for j in g:
            p = r.answers[f"p{j}"]
            s[j - 1], c[j - 1] = p["supports"], p["contradicts"]
    return Judgement(s, c, True, len(groups))


def jev_score(j: Judgement) -> float:
    """JEV 단독 점수: 문단별 지지 확률 최댓값. 실패면 0."""
    return max(j.s) if j.ok and j.s else 0.0


def passage_labels(j: Judgement) -> list[str]:
    """문단별 확률 최댓값 선택지(반복·묶기 일치 검사용)."""
    return [max((("supports", s), ("contradicts", c), ("says_nothing", 1 - s - c)), key=lambda x: x[1])[0]
            for s, c in zip(j.s, j.c)]
```

`test_batched_judge_one_request_and_score`의 첫 단언은 부동소수점 오차를 견디도록 `abs` 비교를 함께 쓴다. 구현자는 이 단언을 `assert [round(x, 9) for x in j.s] == [0.1, 0.2, 0.3]`로 바꿔도 된다.

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_judge.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/evidence/judge.py tests/evidence/test_judge.py
git commit -m "feat: JEV 근거 판정 요청 구성(묶음·단건)과 Stage 0 점수"
```

### Task 7: 추첨·군집·분할

**Files:**
- Create: `lab/evidence/__init__.py`, `lab/evidence/split.py`
- Test: `tests/evidence/test_split.py`

**Interfaces:**
- Consumes: Task 2 `Corp`
- Produces:
  - `SEED = 20261002`, `HOLDOUT_CAP = 20`, `TUNE_CAP = 10`, `RANDOM_N = 25`, `MIN_CHARS = 3000`
  - `SEED_GROUPS: dict[str, tuple[str, ...]]`(그룹명 → 종목코드)
  - `EXCLUDED_PREFIXES: tuple[str, ...]`
  - `shuffled(corps: list[Corp]) -> list[Corp]`
  - `is_candidate(corp: Corp) -> bool`
  - `is_finance(induty_code: str) -> bool`
  - `clusters(names: dict[str, str], texts: dict[str, str], groups: list[list[str]]) -> list[list[str]]`(키는 corp_code, 군집은 정렬, 군집 목록은 첫 코드로 정렬)
  - `assign(clusters: list[list[str]], cap: int) -> tuple[list[list[str]], list[list[str]]]`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_split.py -v`
Expected: FAIL. `ModuleNotFoundError: No module named 'lab.evidence'`

- [ ] **Step 3: 구현**

```python
# lab/evidence/__init__.py
"""근거 확인 판정 엔진 평가 하네스(spec: docs/superpowers/specs/2026-10-02-evidence-assistant-design.md)."""
```

```python
# lab/evidence/split.py
"""기업 표본 추첨, 교차 언급 군집, 군집 단위 분할(spec 2절)."""
from __future__ import annotations

import random

from app.services.evidence.dart import Corp

SEED = 20261002
HOLDOUT_CAP = 20
TUNE_CAP = 10
RANDOM_N = 25
MIN_CHARS = 3000
SEED_GROUPS: dict[str, tuple[str, ...]] = {
    "삼성": ("005930", "006400", "207940"),
    "SK": ("000660", "034730", "096770"),
    "현대차": ("005380", "000270", "012330"),
    "LG": ("051910", "373220"),
    "NAVER": ("035420",),
    "카카오": ("035720",),
    "POSCO": ("005490",),
    "셀트리온": ("068270",),
}
EXCLUDED_PREFIXES = ("삼성", "SK", "에스케이", "현대", "기아", "LG", "엘지", "NAVER", "네이버", "카카오",
                     "POSCO", "포스코", "셀트리온")
FINANCE_KSIC = ("64", "65", "66")


def shuffled(corps: list[Corp]) -> list[Corp]:
    """corp_code 오름차순 정렬 뒤 고정 시드로 섞은 추첨 순서."""
    out = sorted(corps, key=lambda c: c.corp_code)
    random.Random(SEED).shuffle(out)
    return out


def is_candidate(corp: Corp) -> bool:
    """시드 그룹 이름 접두어로 시작하지 않는 회사만 무작위 후보다."""
    return not corp.corp_name.startswith(EXCLUDED_PREFIXES)


def is_finance(induty_code: str) -> bool:
    """KSIC 64~66(금융·보험)이면 참."""
    return str(induty_code).startswith(FINANCE_KSIC)


def clusters(names: dict[str, str], texts: dict[str, str], groups: list[list[str]]) -> list[list[str]]:
    """시드 그룹과 본문 교차 언급(한쪽 본문에 다른 쪽 이름)으로 묶은 연결 요소."""
    parent = {c: c for c in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for g in groups:
        for other in g[1:]:
            union(g[0], other)
    codes = sorted(names)
    for a in codes:
        for b in codes:
            if a != b and names[b] in texts.get(a, ""):
                union(a, b)
    comp: dict[str, list[str]] = {}
    for c in codes:
        comp.setdefault(find(c), []).append(c)
    return sorted((sorted(v) for v in comp.values()), key=lambda v: v[0])


def assign(cluster_list: list[list[str]], cap: int) -> tuple[list[list[str]], list[list[str]]]:
    """고정 시드로 섞은 군집을 앞에서부터 보며, 넣어도 cap 이하면 앞 묶음에, 아니면 뒤 묶음에 둔다."""
    order = list(cluster_list)
    random.Random(SEED).shuffle(order)
    take, rest, n = [], [], 0
    for cl in order:
        if n + len(cl) <= cap:
            take.append(cl)
            n += len(cl)
        else:
            rest.append(cl)
    return take, rest
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_split.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add lab/evidence/__init__.py lab/evidence/split.py tests/evidence/test_split.py
git commit -m "feat: 평가 기업 추첨·교차 언급 군집·군집 단위 분할"
```

### Task 8: 지표

**Files:**
- Create: `lab/evidence/metrics.py`
- Test: `tests/evidence/test_metrics.py`

**Interfaces:**
- Produces:
  - `auc(y: list[int], s: list[float]) -> float | None`
  - `kappa(a: list, b: list) -> float`
  - `cluster_bootstrap(rows: list[dict], stat, n: int = 2000, seed: int = 20261002) -> tuple[float | None, float | None, float | None]`. rows에는 `cluster`와 `qid` 키가 있어야 한다. 반환값은 (점추정, 2.5%, 97.5%)다.
  - `cp_upper(failures: int, n: int, alpha: float = 0.05) -> float`
  - `percentile(values: list[float], q: float) -> float`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_metrics.py
from lab.evidence import metrics


def test_auc_and_kappa():
    assert metrics.auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == 0.75
    assert metrics.auc([1, 1], [0.1, 0.2]) is None
    assert metrics.kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0


def test_cluster_bootstrap_is_reproducible_and_brackets_point():
    rows = [{"cluster": c, "qid": f"{c}-{q}", "y": (c + q + i) % 2, "s": ((c + q + i) % 2) * 0.6 + 0.2 * i}
            for c in range(6) for q in range(3) for i in range(3)]
    stat = lambda rs: metrics.auc([r["y"] for r in rs], [r["s"] for r in rs])
    a = metrics.cluster_bootstrap(rows, stat, n=200)
    assert a == metrics.cluster_bootstrap(rows, stat, n=200)
    assert a[1] <= a[0] <= a[2]


def test_cp_upper_matches_rule_of_three_region():
    assert 0.0295 < metrics.cp_upper(0, 100) < 0.0300
    assert metrics.cp_upper(5, 5) == 1.0


def test_percentile():
    assert metrics.percentile([1, 2, 3, 4, 5], 50) == 3
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_metrics.py -v`
Expected: FAIL. `ImportError`

- [ ] **Step 3: 구현**

```python
# lab/evidence/metrics.py
"""Stage 0 지표: AUC, Cohen κ, 2단 군집 부트스트랩(군집→질문), Clopper–Pearson 단측 상한."""
from __future__ import annotations

import numpy as np
from scipy.stats import beta
from sklearn.metrics import cohen_kappa_score, roc_auc_score

SEED = 20261002


def auc(y: list[int], s: list[float]) -> float | None:
    """라벨이 한 종류뿐이면 None."""
    return None if len(set(y)) < 2 else float(roc_auc_score(y, s))


def kappa(a: list, b: list) -> float:
    """두 라벨러의 Cohen κ."""
    return float(cohen_kappa_score(a, b))


def cluster_bootstrap(rows: list[dict], stat, n: int = 2000, seed: int = SEED):
    """군집을 복원추출하고, 뽑힌 군집 안에서 질문을 복원추출한다. 통계가 None인 반복은 버린다."""
    point = stat(rows)
    by_c: dict = {}
    for r in rows:
        by_c.setdefault(r["cluster"], {}).setdefault(r["qid"], []).append(r)
    keys = sorted(by_c)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        sample: list[dict] = []
        for ci in rng.integers(0, len(keys), len(keys)):
            qs = by_c[keys[ci]]
            qkeys = sorted(qs)
            for qi in rng.integers(0, len(qkeys), len(qkeys)):
                sample.extend(qs[qkeys[qi]])
        v = stat(sample)
        if v is not None:
            vals.append(v)
    if not vals:
        return point, None, None
    return point, float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


def cp_upper(failures: int, n: int, alpha: float = 0.05) -> float:
    """실패율의 단측 (1-alpha) Clopper–Pearson 상한."""
    if failures >= n:
        return 1.0
    return float(beta.ppf(1 - alpha, failures + 1, n - failures))


def percentile(values: list[float], q: float) -> float:
    """백분위수(numpy 기본 선형 보간)."""
    return float(np.percentile(values, q))
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_metrics.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add lab/evidence/metrics.py tests/evidence/test_metrics.py
git commit -m "feat: Stage 0 지표(AUC·κ·2단 군집 부트스트랩·CP 상한)"
```

### Task 9: 명령줄 — 경로·분할 보호·코퍼스

**Files:**
- Create: `lab/evidence/__main__.py`
- Test: `tests/evidence/test_cli.py`

**Interfaces:**
- Consumes: Task 2·3·7
- Produces:
  - `Paths(root: Path)`. 속성은 `ev`(=root/lab/evidence), `data`(=ev/data), `priv`(=root/lab/data/evidence), `split_json`, `prereg`, `prereg_holdout`, `attempts`, `calls`(=priv/jev_calls.jsonl)이고, 메서드는 `jsonl(name) -> Path`(=data/name)다.
  - `read_jsonl(path) -> list[dict]`, `write_jsonl(path, rows) -> None`
  - `load_split(P) -> dict`
  - `companies(P, split_name: str) -> list[dict]`. `split_name`은 `"tune"|"check"|"dev"|"holdout"|"all"`이고, `"holdout"`은 `P.prereg_holdout`가 없으면 `SystemExit("holdout is frozen until prereg_holdout.json")`을 낸다.
  - `log_attempt(P, event: str, **detail) -> None`
  - 명령: `split`, `passages`
  - `split.json` 구조: `{"seed","corpcode_sha256","companies":[{"corp_code","corp_name","stock_code","rcept_no","report_nm","source","chars","cluster","split"}],"clusters":[[...]]}`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_cli.py
import json

import pytest

from lab.evidence import __main__ as cli


def _split(tmp_path):
    P = cli.Paths(tmp_path)
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    comps = [{"corp_code": "a", "corp_name": "A", "split": "tune", "cluster": 0},
             {"corp_code": "b", "corp_name": "B", "split": "check", "cluster": 1},
             {"corp_code": "c", "corp_name": "C", "split": "holdout", "cluster": 2}]
    P.split_json.write_text(json.dumps({"companies": comps}))
    return P


def test_holdout_refused_before_freeze(tmp_path):
    P = _split(tmp_path)
    assert [c["corp_code"] for c in cli.companies(P, "dev")] == ["a", "b"]
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "holdout")
    P.prereg_holdout.write_text("{}")
    assert [c["corp_code"] for c in cli.companies(P, "holdout")] == ["c"]


def test_jsonl_roundtrip_and_attempts(tmp_path):
    P = cli.Paths(tmp_path)
    cli.write_jsonl(P.jsonl("x.jsonl"), [{"a": 1}, {"a": "한글"}])
    assert cli.read_jsonl(P.jsonl("x.jsonl")) == [{"a": 1}, {"a": "한글"}]
    cli.log_attempt(P, "test", n=1)
    rec = json.loads(P.attempts.read_text())
    assert rec["event"] == "test" and rec["n"] == 1 and "at" in rec
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_cli.py -v`
Expected: FAIL. `ImportError`

- [ ] **Step 3: 구현** (이 Task에서는 공통부와 `split`·`passages`만 작성하고, 뒤 Task에서 명령을 덧붙인다)

```python
# lab/evidence/__main__.py
"""근거 판정 엔진 평가 명령줄. `python -m lab.evidence <명령>`.

호스트(uv)에서 돌리는 명령: split, passages, question-packets, questions-merge, claims, controlled-packets,
controlled-check, label-packets, labels-merge, judge, stage0-report.
컨테이너(Ollama 접근)에서 돌리는 명령: embed, retrieve, generate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class Paths:
    """저장소 루트 기준 경로. 테스트·검증은 임시 루트를 쓴다."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.ev = self.root / "lab/evidence"
        self.data = self.ev / "data"
        self.priv = self.root / "lab/data/evidence"
        self.split_json = self.ev / "split.json"
        self.prereg = self.ev / "prereg.json"
        self.prereg_holdout = self.ev / "prereg_holdout.json"
        self.attempts = self.ev / "attempts.jsonl"
        self.calls = self.priv / "jev_calls.jsonl"

    def jsonl(self, name: str) -> Path:
        """커밋하는 데이터 파일 경로."""
        return self.data / name


def read_jsonl(path: Path) -> list[dict]:
    """JSONL을 읽는다. 파일이 없으면 빈 목록."""
    path = Path(path)
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """JSONL로 덮어쓴다(한글 그대로)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def log_attempt(P: Paths, event: str, **detail) -> None:
    """시도 원장에 한 줄을 남긴다."""
    P.attempts.parent.mkdir(parents=True, exist_ok=True)
    with P.attempts.open("a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **detail},
                           ensure_ascii=False) + "\n")


def load_split(P: Paths) -> dict:
    """split.json을 읽는다."""
    return json.loads(P.split_json.read_text())


def companies(P: Paths, split_name: str) -> list[dict]:
    """분할 이름에 속한 회사 목록. 홀드아웃은 동결 파일이 생기기 전까지 거부한다."""
    if split_name == "holdout" and not P.prereg_holdout.exists():
        raise SystemExit("holdout is frozen until prereg_holdout.json")
    names = {"dev": {"tune", "check"}, "all": {"tune", "check", "holdout"}}.get(split_name, {split_name})
    return [c for c in load_split(P)["companies"] if c["split"] in names]


def passages_by_corp(P: Paths) -> dict[str, list[dict]]:
    """비공개 문단 파일을 회사별로 묶는다."""
    out: dict[str, list[dict]] = {}
    for r in read_jsonl(P.priv / "passages.jsonl"):
        out.setdefault(r["corp_code"], []).append(r)
    return out


def cmd_split(P: Paths, args) -> None:
    """상장사 목록에서 시드 15 + 무작위 25개사를 고르고 군집 단위로 분할한다. JEV 호출 뒤에는 거부."""
    import httpx

    from app.services.evidence import dart, passages
    from lab.evidence import split as sp

    if P.calls.exists():
        raise SystemExit("split is frozen once JEV calls exist")
    key = dart.load_dart_key()
    client = httpx.Client(timeout=60)
    xml_path = P.priv / "corpCode.xml"
    if not xml_path.exists():
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_bytes(dart.fetch_corp_codes(client, key))
    raw = xml_path.read_bytes()
    corps = dart.parse_corp_codes(raw)
    by_stock = {c.stock_code: c for c in corps}
    chosen: list[dict] = []
    texts: dict[str, str] = {}
    ledger: list[dict] = []

    def take(corp, source: str) -> str:
        rep = dart.pick_annual_report(dart.list_annual_reports(client, key, corp.corp_code))
        if rep is None:
            return "no_annual_report"
        if source == "random" and sp.is_finance(dart.company_info(client, key, corp.corp_code).get("induty_code", "")):
            return "finance"
        path = dart.download_document(client, key, rep["rcept_no"], P.priv / "docs", P.jsonl("dart_ledger.jsonl"))
        try:
            ps = passages.build_passages(corp.corp_code, rep["rcept_no"], path.read_text(encoding="utf-8", errors="ignore"))
        except ValueError as exc:
            return f"parse: {exc}"
        chars = sum(len(p.text) for p in ps)
        if source == "random" and chars < sp.MIN_CHARS:
            return "short"
        chosen.append({**asdict(corp), "rcept_no": rep["rcept_no"], "report_nm": rep["report_nm"],
                       "source": source, "chars": chars})
        texts[corp.corp_code] = " ".join(p.text for p in ps)
        return "ok"

    for group in sp.SEED_GROUPS.values():
        for stock in group:
            result = take(by_stock[stock], "seed")
            ledger.append({"corp_code": by_stock[stock].corp_code, "corp_name": by_stock[stock].corp_name,
                           "source": "seed", "result": result})
            if result != "ok":
                raise SystemExit(f"seed {stock} failed: {result}")
    for corp in sp.shuffled(corps):
        if sum(c["source"] == "random" for c in chosen) >= sp.RANDOM_N:
            break
        if not sp.is_candidate(corp):
            continue
        ledger.append({"corp_code": corp.corp_code, "corp_name": corp.corp_name, "source": "random",
                       "result": take(corp, "random")})
    names = {c["corp_code"]: c["corp_name"] for c in chosen}
    code_of = {c["stock_code"]: c["corp_code"] for c in chosen}
    groups = [[code_of[s] for s in g] for g in sp.SEED_GROUPS.values()]
    cl = sp.clusters(names, texts, groups)
    holdout, dev = sp.assign(cl, sp.HOLDOUT_CAP)
    tune, check = sp.assign(sorted(dev, key=lambda v: v[0]), sp.TUNE_CAP)
    label = {}
    for name, part in (("holdout", holdout), ("tune", tune), ("check", check)):
        for members in part:
            for code in members:
                label[code] = name
    index = {code: i for i, members in enumerate(cl) for code in members}
    for c in chosen:
        c.update(cluster=index[c["corp_code"]], split=label[c["corp_code"]])
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"seed": sp.SEED, "corpcode_sha256": hashlib.sha256(raw).hexdigest(),
                                        "companies": sorted(chosen, key=lambda c: c["corp_code"]),
                                        "clusters": cl}, ensure_ascii=False, indent=1) + "\n")
    write_jsonl(P.jsonl("draw_ledger.jsonl"), ledger)
    counts = {s: sum(c["split"] == s for c in chosen) for s in ("tune", "check", "holdout")}
    log_attempt(P, "split", companies=len(chosen), clusters=len(cl), **counts)
    print(json.dumps(counts))


def cmd_passages(P: Paths, args) -> None:
    """분할된 모든 회사의 문단을 비공개 파일에 쓰고, ID·해시 매니페스트를 커밋용으로 쓴다."""
    from app.services.evidence import passages

    rows, manifest = [], []
    for c in load_split(P)["companies"]:
        xml = (P.priv / "docs" / f"{c['rcept_no']}.xml").read_text(encoding="utf-8", errors="ignore")
        for p in passages.build_passages(c["corp_code"], c["rcept_no"], xml):
            rows.append(asdict(p))
            manifest.append({"id": p.id, "sha256": p.sha256, "chars": len(p.text)})
    write_jsonl(P.priv / "passages.jsonl", rows)
    write_jsonl(P.jsonl("passages_manifest.jsonl"), manifest)
    print(f"{len(rows)} passages")


COMMANDS = {"split": cmd_split, "passages": cmd_passages}


def main(argv: list[str] | None = None) -> None:
    """명령 분기."""
    ap = argparse.ArgumentParser(prog="lab.evidence")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("cmd", choices=sorted(COMMANDS))
    ap.add_argument("--split", default="dev")
    ap.add_argument("--labeler", choices=["opus", "codex"])
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--tag", default="")
    ap.add_argument("--single", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    COMMANDS[args.cmd](Paths(args.root), args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_cli.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add lab/evidence/__main__.py tests/evidence/test_cli.py
git commit -m "feat: 평가 명령줄 골격·홀드아웃 보호·추첨/분할·문단 명령"
```

### Task 10: 명령줄 — 질문·검색·생성·주장

**Files:**
- Modify: `lab/evidence/__main__.py`(명령 추가, `COMMANDS` 갱신)
- Create: `lab/evidence/prompts/question_writer.md`, `lab/evidence/prompts/controlled_writer.md`
- Test: `tests/evidence/test_cli_data.py`

**Interfaces:**
- Consumes: Task 4·5·9
- Produces:
  - `CATEGORIES = ["사업 개요", "주요 제품·서비스", "매출 구성·수치", "원재료·생산설비", "위험·파생", "연구개발·주요계약"]`
  - `VARIANTS = ["숫자 변경", "기간 바꾸기", "주체 교체", "부정", "다른 기업 사실"]`
  - `questions.jsonl`: `{"qid": f"{corp}-q{n}", "corp_code", "category", "question", "split"}`(n은 CATEGORIES 순서 1~6)
  - `retrieval.jsonl`: `{"qid","passage_ids":[8개]}`
  - `answers.jsonl`: `{"qid","model","digest","prompt_sha","answer"}`
  - `claims.jsonl`: `{"cid","qid","source":"natural"|"controlled","text","variant","expected"}`
  - 명령:
    - `question-packets --split dev`: `priv/packets/questions/<corp>.md`
    - `questions-merge --split dev`
    - `embed`(컨테이너)
    - `retrieve --split dev`(컨테이너)
    - `generate --split dev`(컨테이너)
    - `claims --split dev`
    - `controlled-packets --split dev`: `priv/packets/controlled/<corp>.md`
    - `controlled-check --split dev`
  - 서브에이전트 출력 위치: `data/questions/<corp>.jsonl`, `data/controlled/<corp>.jsonl`
  - `merge_questions(rows: list[dict], corp_code: str, split: str) -> list[dict]`. 6개·범주 정확 일치를 검증하고, 어긋나면 `ValueError`를 낸다.
  - `natural_claims(answer_rows: list[dict]) -> list[dict]`. 답변마다 앞 5문장을 쓰고, cid는 `f"{qid}-n{i}"`(i는 1부터)다.
  - `variant_for(i: int) -> str`, 값은 `VARIANTS[i % 5]`다.
  - `check_controlled(row: dict, passages: list[str], names: tuple[str, ...]) -> tuple[bool, str]`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_cli_data.py
import pytest

from lab.evidence import __main__ as cli


def _q(cat, text="질문?"):
    return {"category": cat, "question": text}


def test_merge_questions_orders_by_category_and_validates():
    rows = [_q(c, f"{c} 질문?") for c in reversed(cli.CATEGORIES)]
    out = cli.merge_questions(rows, "000001", "tune")
    assert [r["qid"] for r in out] == [f"000001-q{i}" for i in range(1, 7)]
    assert out[0]["category"] == "사업 개요" and out[0]["split"] == "tune"
    with pytest.raises(ValueError):
        cli.merge_questions(rows[:5], "000001", "tune")
    with pytest.raises(ValueError):
        cli.merge_questions(rows[:5] + [_q("엉뚱한 범주")], "000001", "tune")


def test_natural_claims_take_first_five_sentences():
    ans = [{"qid": "x-q1", "answer": "하나입니다. 둘입니다. 셋입니다. 넷입니다. 다섯입니다. 여섯입니다."}]
    out = cli.natural_claims(ans)
    assert [c["cid"] for c in out] == [f"x-q1-n{i}" for i in range(1, 6)]
    assert out[0] == {"cid": "x-q1-n1", "qid": "x-q1", "source": "natural", "text": "하나입니다.",
                      "variant": None, "expected": None}


def test_variant_rotation_and_controlled_check():
    assert [cli.variant_for(i) for i in range(6)] == cli.VARIANTS + [cli.VARIANTS[0]]
    row = {"qid": "x-q1", "true_text": "DX 매출은 100억원이다.", "variant_text": "DX 매출은 900억원이다.",
           "variant_type": "숫자 변경"}
    assert cli.check_controlled(row, ["DX 매출액: 100"], ()) == (True, "ok")
    bad = dict(row, variant_text="DX 매출은 100억원이고 2025년이다.")
    assert cli.check_controlled(bad, ["2025년 DX 매출액: 100"], ())[0] is False
    same = dict(row, variant_text=row["true_text"])
    assert cli.check_controlled(same, ["x"], ())[1] == "variant equals true text"
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_cli_data.py -v`
Expected: FAIL. `AttributeError: module 'lab.evidence.__main__' has no attribute 'CATEGORIES'`

- [ ] **Step 3: 구현** — `lab/evidence/__main__.py`의 `COMMANDS = ...` 줄 위에 다음을 추가하고, `COMMANDS`를 바꾼다.

```python
CATEGORIES = ["사업 개요", "주요 제품·서비스", "매출 구성·수치", "원재료·생산설비", "위험·파생", "연구개발·주요계약"]
VARIANTS = ["숫자 변경", "기간 바꾸기", "주체 교체", "부정", "다른 기업 사실"]
K = 8
GEN_MODEL = "llama3.2:1b"
EMBED_MODEL = "nomic-embed-text"


def merge_questions(rows: list[dict], corp_code: str, split: str) -> list[dict]:
    """작성자 출력 6줄을 범주 순서로 정렬해 qid를 붙인다. 범주가 정확히 하나씩이 아니면 ValueError."""
    cats = [r["category"] for r in rows]
    if sorted(cats) != sorted(CATEGORIES):
        raise ValueError(f"{corp_code}: categories {cats}")
    by_cat = {r["category"]: r["question"].strip() for r in rows}
    return [{"qid": f"{corp_code}-q{i}", "corp_code": corp_code, "category": cat, "question": by_cat[cat],
             "split": split} for i, cat in enumerate(CATEGORIES, 1)]


def natural_claims(answer_rows: list[dict]) -> list[dict]:
    """답변마다 앞 5문장을 자연 주장으로 만든다."""
    from app.services.evidence.claims import split_sentences

    out = []
    for a in answer_rows:
        for i, s in enumerate(split_sentences(a["answer"])[:5], 1):
            out.append({"cid": f"{a['qid']}-n{i}", "qid": a["qid"], "source": "natural", "text": s,
                        "variant": None, "expected": None})
    return out


def variant_for(i: int) -> str:
    """질문 순번 i(0부터)에 순환 배정하는 변형 유형."""
    return VARIANTS[i % len(VARIANTS)]


def check_controlled(row: dict, passages: list[str], names: tuple[str, ...]) -> tuple[bool, str]:
    """통제 변형이 참 문장과 다르고, 새로 넣은 숫자·회사명이 문단 8개에 없는지 본다."""
    from app.services.evidence.claims import new_values_absent

    if row["variant_text"].strip() == row["true_text"].strip():
        return False, "variant equals true text"
    if not new_values_absent(row["variant_text"], row["true_text"], passages, names):
        return False, "new value present in passages"
    return True, "ok"


def _corp_names(P: Paths) -> dict[str, str]:
    return {c["corp_code"]: c["corp_name"] for c in load_split(P)["companies"]}


def cmd_question_packets(P: Paths, args) -> None:
    """회사마다 지시문 + 지정 절 문단 전체를 담은 질문 작성 꾸러미를 비공개 폴더에 쓴다."""
    guide = (P.ev / "prompts/question_writer.md").read_text()
    pbc = passages_by_corp(P)
    for c in companies(P, args.split):
        body = "\n".join(f"[{p['id']}] {p['text']}" for p in pbc[c["corp_code"]])
        out = P.priv / "packets/questions" / f"{c['corp_code']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 회사: {c['corp_name']} ({c['corp_code']})\n"
                       f"# 출력 파일: lab/evidence/data/questions/{c['corp_code']}.jsonl\n\n{body}\n")
    print("ok")


def cmd_questions_merge(P: Paths, args) -> None:
    """회사별 질문 파일을 검증·병합해 questions.jsonl을 쓴다(다른 분할의 기존 행은 보존)."""
    keep = [r for r in read_jsonl(P.jsonl("questions.jsonl")) if r["split"] not in
            {c["split"] for c in companies(P, args.split)}]
    rows = []
    for c in companies(P, args.split):
        rows += merge_questions(read_jsonl(P.data / "questions" / f"{c['corp_code']}.jsonl"), c["corp_code"], c["split"])
    write_jsonl(P.jsonl("questions.jsonl"), sorted(keep + rows, key=lambda r: r["qid"]))
    print(f"{len(rows)} questions")


def _ollama():
    import os

    from app.lib.ollama import OllamaClient
    return OllamaClient(os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434"), 600.0)


def cmd_embed(P: Paths, args) -> None:
    """모든 문단을 임베딩해 비공개 파일에 캐시한다(이미 있는 ID는 건너뜀). 컨테이너에서 실행."""
    import asyncio

    from app.services.evidence.retrieve import DOC_PREFIX

    out = P.priv / "passage_vecs.jsonl"
    done = {r["id"] for r in read_jsonl(out)}
    llm = _ollama()

    async def run():
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as f:
            for rows in passages_by_corp(P).values():
                for p in rows:
                    if p["id"] not in done:
                        vec = await llm.embed(EMBED_MODEL, DOC_PREFIX + p["text"])
                        f.write(json.dumps({"id": p["id"], "vec": vec}) + "\n")

    asyncio.run(run())
    print("ok")


def cmd_retrieve(P: Paths, args) -> None:
    """분할의 질문마다 같은 회사 문단 중 상위 K개를 고른다. 컨테이너에서 실행."""
    import asyncio

    from app.services.evidence.retrieve import QUERY_PREFIX, top_k

    vecs = {r["id"]: r["vec"] for r in read_jsonl(P.priv / "passage_vecs.jsonl")}
    pbc = passages_by_corp(P)
    codes = {c["corp_code"] for c in companies(P, args.split)}
    qs = [q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes]
    llm = _ollama()
    keep = [r for r in read_jsonl(P.jsonl("retrieval.jsonl")) if r["qid"] not in {q["qid"] for q in qs}]

    async def run():
        rows = []
        for q in qs:
            ids = [p["id"] for p in pbc[q["corp_code"]]]
            qv = await llm.embed(EMBED_MODEL, QUERY_PREFIX + q["question"])
            rows.append({"qid": q["qid"], "passage_ids": [ids[i] for i in top_k(qv, [vecs[i] for i in ids], K)]})
        return rows

    rows = asyncio.run(run())
    write_jsonl(P.jsonl("retrieval.jsonl"), sorted(keep + rows, key=lambda r: r["qid"]))
    print(f"{len(rows)} retrievals")


def _passage_text(P: Paths) -> dict[str, str]:
    return {r["id"]: r["text"] for r in read_jsonl(P.priv / "passages.jsonl")}


def cmd_generate(P: Paths, args) -> None:
    """질문마다 검색 문단 8개로 답변을 한 번 생성해 동결한다(이미 있는 qid는 건너뜀). 컨테이너에서 실행."""
    import asyncio

    import httpx

    from app.services.evidence.generate import PROMPT_SHA, generate_answer

    llm = _ollama()
    tags = httpx.get(f"{llm._base}/api/tags", timeout=30).json()["models"]
    digest = next(m["digest"][:12] for m in tags if m["name"] == GEN_MODEL)
    text = _passage_text(P)
    names = _corp_names(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    codes = {c["corp_code"] for c in companies(P, args.split)}
    existing = read_jsonl(P.jsonl("answers.jsonl"))
    done = {a["qid"] for a in existing}
    todo = [q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes and q["qid"] not in done]

    async def run():
        out = []
        for q in todo:
            ans = await generate_answer(llm, GEN_MODEL, names[q["corp_code"]], q["question"],
                                        [text[i] for i in ret[q["qid"]]])
            out.append({"qid": q["qid"], "model": GEN_MODEL, "digest": digest, "prompt_sha": PROMPT_SHA,
                        "answer": ans})
            write_jsonl(P.jsonl("answers.jsonl"), sorted(existing + out, key=lambda r: r["qid"]))
        return out

    print(f"{len(asyncio.run(run()))} answers")


def cmd_claims(P: Paths, args) -> None:
    """분할의 답변에서 자연 주장을 만든다(통제 주장 행은 보존)."""
    codes = {c["corp_code"] for c in companies(P, args.split)}
    answers = [a for a in read_jsonl(P.jsonl("answers.jsonl")) if a["qid"].split("-q")[0] in codes]
    keep = [c for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["source"] == "controlled" or c["qid"].split("-q")[0] not in codes]
    rows = natural_claims(answers)
    write_jsonl(P.jsonl("claims.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    print(f"{len(rows)} natural claims")


def cmd_controlled_packets(P: Paths, args) -> None:
    """회사마다 질문·검색 문단 8개·배정 변형 유형을 담은 통제 주장 작성 꾸러미를 쓴다."""
    guide = (P.ev / "prompts/controlled_writer.md").read_text()
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    codes = [c["corp_code"] for c in companies(P, args.split)]
    qs = [q for q in read_jsonl(P.jsonl("questions.jsonl")) if q["corp_code"] in codes]
    for corp in codes:
        parts = []
        for i, q in enumerate(qs):
            if q["corp_code"] != corp:
                continue
            ps = "\n".join(f"[문단 {j}] {text[pid]}" for j, pid in enumerate(ret[q["qid"]], 1))
            parts.append(f"## {q['qid']} — 변형 유형: {variant_for(i)}\n질문: {q['question']}\n{ps}\n")
        out = P.priv / "packets/controlled" / f"{corp}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 출력 파일: lab/evidence/data/controlled/{corp}.jsonl\n\n" + "\n".join(parts))
    print("ok")


def cmd_controlled_check(P: Paths, args) -> None:
    """통제 주장 작성 결과를 검사해 claims.jsonl에 c1(의역, 기대 지지됨)·c2(변형, 기대 지지 안 됨)로 넣는다."""
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    names = tuple(_corp_names(P).values())
    codes = {c["corp_code"] for c in companies(P, args.split)}
    rows, rejected = [], []
    for corp in sorted(codes):
        for r in read_jsonl(P.data / "controlled" / f"{corp}.jsonl"):
            ok, why = check_controlled(r, [text[i] for i in ret[r["qid"]]], names)
            if not ok:
                rejected.append({"qid": r["qid"], "reason": why})
                continue
            rows.append({"cid": f"{r['qid']}-c1", "qid": r["qid"], "source": "controlled", "text": r["true_text"],
                         "variant": "의역", "expected": "supported"})
            rows.append({"cid": f"{r['qid']}-c2", "qid": r["qid"], "source": "controlled", "text": r["variant_text"],
                         "variant": r["variant_type"], "expected": "not_supported"})
    keep = [c for c in read_jsonl(P.jsonl("claims.jsonl"))
            if not (c["source"] == "controlled" and c["qid"].split("-q")[0] in codes)]
    write_jsonl(P.jsonl("claims.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    write_jsonl(P.jsonl("controlled_rejected.jsonl"), rejected)
    log_attempt(P, "controlled-check", accepted=len(rows) // 2, rejected=len(rejected))
    print(f"{len(rows) // 2} accepted, {len(rejected)} rejected")
```

`COMMANDS`를 다음으로 바꾼다.

```python
COMMANDS = {"split": cmd_split, "passages": cmd_passages, "question-packets": cmd_question_packets,
            "questions-merge": cmd_questions_merge, "embed": cmd_embed, "retrieve": cmd_retrieve,
            "generate": cmd_generate, "claims": cmd_claims, "controlled-packets": cmd_controlled_packets,
            "controlled-check": cmd_controlled_check}
```

`lab/evidence/prompts/question_writer.md`:

```markdown
# 질문 작성 지시

당신은 한국 상장사 사업보고서로 리서치 질문을 만드는 작성자다. 아래 문단(회사의 개요, 사업의 내용)을 읽고 질문 6개를 만든다.

- 범주마다 정확히 1개: 사업 개요, 주요 제품·서비스, 매출 구성·수치, 원재료·생산설비, 위험·파생, 연구개발·주요계약
- 문단으로 답할 수 있는 질문만 만든다. 그 범주 내용이 문단에 없으면 가장 가까운 내용으로 묻는다.
- 질문 문장에 답(숫자·제품명·고객명)을 넣지 않는다.
- 투자자가 실제로 물을 법한 자연스러운 한국어 한 문장으로 쓴다.
- 출력은 "출력 파일" 경로에 JSONL 6줄로 쓴다. 각 줄: {"category": "<범주>", "question": "<질문>"}
- 파일 외의 곳을 수정하지 않는다.
```

`lab/evidence/prompts/controlled_writer.md`:

```markdown
# 통제 주장 작성 지시

질문마다 아래 [문단 1~8]을 보고 주장 두 개를 만든다.

1. true_text: 한 문단이 직접 말하는 사실 하나를 **다른 표현으로 의역**한 한 문장. 원문 문장을 그대로 옮기지 않는다. 문단에 없는 내용을 더하지 않는다.
2. variant_text: true_text를 배정된 변형 유형대로 바꾼 한 문장. 나머지는 그대로 둔다.
   - 숫자 변경: 숫자 하나를 문단 8개 어디에도 없는 값으로 바꾼다.
   - 기간 바꾸기: 연도·분기를 문단 8개 어디에도 없는 값으로 바꾼다.
   - 주체 교체: 회사·부문·제품 이름을 문단 8개 어디에도 없는 이름으로 바꾼다.
   - 부정: 사실을 부정한다(예: "생산한다" → "생산하지 않는다").
   - 다른 기업 사실: 이 회사와 무관한 다른 상장사의 일반적인 사업 사실로 바꾼다. 그 회사명은 문단 8개에 없어야 한다.
- 출력은 "출력 파일" 경로에 질문마다 JSONL 한 줄로 쓴다: {"qid": "<qid>", "true_text": "...", "variant_text": "...", "variant_type": "<배정 유형>", "source_passage": <번호>}
- 파일 외의 곳을 수정하지 않는다.
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence/test_cli_data.py tests/evidence/test_cli.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add lab/evidence/__main__.py lab/evidence/prompts tests/evidence/test_cli_data.py
git commit -m "feat: 질문·검색·생성·자연/통제 주장 명령과 작성 지시문"
```

### Task 11: 명령줄 — 라벨 꾸러미와 병합

**Files:**
- Modify: `lab/evidence/__main__.py`
- Create: `lab/evidence/prompts/labeler.md`
- Test: `tests/evidence/test_cli_labels.py`

**Interfaces:**
- Consumes: Task 8 `kappa`, Task 9·10
- Produces:
  - `LABELS = ("supported", "contradicted", "no_evidence", "non_claim")`
  - `lid(cid: str) -> str`(`sha256(cid)[:10]`, 라벨러에게 출처를 숨기는 불투명 ID)
  - `merge_labels(r1: dict[str, dict[str, dict]], r2: dict[str, dict[str, dict]]) -> list[dict]`
    - 입력: 라벨러 → {cid → {"label","passage","reason"}}
    - 출력: `{"cid","label"("disputed" 가능),"r1":{opus,codex},"r2":{opus,codex}|None}`
  - `disagreements(r1) -> list[str]`(cid 목록)
  - 명령: `label-packets --labeler opus|codex --round 1|2 --split dev`, `labels-merge --split dev`
  - 라벨러 출력 위치: `data/labels/r<round>_<labeler>/<corp>.jsonl`, 각 줄 `{"lid","label","passage","reason"}`
  - `labels.jsonl` 각 줄: 위 merge_labels 출력

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_cli_labels.py
from lab.evidence import __main__ as cli


def _l(label):
    return {"label": label, "passage": 1, "reason": "r"}


def test_lid_is_opaque_and_stable():
    assert cli.lid("x-q1-c2") == cli.lid("x-q1-c2") and len(cli.lid("a")) == 10 and "c2" not in cli.lid("x-q1-c2")


def test_merge_labels_round1_agree_round2_resolve_and_dispute():
    r1 = {"opus": {"a": _l("supported"), "b": _l("supported"), "c": _l("no_evidence")},
          "codex": {"a": _l("supported"), "b": _l("no_evidence"), "c": _l("supported")}}
    assert cli.disagreements(r1) == ["b", "c"]
    r2 = {"opus": {"b": _l("no_evidence"), "c": _l("no_evidence")},
          "codex": {"b": _l("no_evidence"), "c": _l("supported")}}
    out = {r["cid"]: r for r in cli.merge_labels(r1, r2)}
    assert out["a"]["label"] == "supported" and out["a"]["r2"] is None
    assert out["b"]["label"] == "no_evidence" and out["c"]["label"] == "disputed"
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_cli_labels.py -v`
Expected: FAIL. `AttributeError: ... 'lid'`

- [ ] **Step 3: 구현** — `COMMANDS` 위에 추가하고 `COMMANDS`에 두 명령을 넣는다.

```python
LABELS = ("supported", "contradicted", "no_evidence", "non_claim")


def lid(cid: str) -> str:
    """라벨러에게 보이는 불투명 ID(출처·변형 유형을 숨긴다)."""
    return hashlib.sha256(cid.encode()).hexdigest()[:10]


def disagreements(r1: dict) -> list[str]:
    """1차 라벨이 다른 cid(두 라벨러 모두 낸 것만)."""
    a, b = r1["opus"], r1["codex"]
    return sorted(c for c in a.keys() & b.keys() if a[c]["label"] != b[c]["label"])


def merge_labels(r1: dict, r2: dict) -> list[dict]:
    """1차 일치는 그대로, 불일치는 2차(조정 라운드) 일치로, 그래도 다르면 disputed."""
    out = []
    for cid in sorted(r1["opus"].keys() & r1["codex"].keys()):
        a, b = r1["opus"][cid], r1["codex"][cid]
        rec = {"cid": cid, "r1": {"opus": a, "codex": b}, "r2": None}
        if a["label"] == b["label"]:
            rec["label"] = a["label"]
        else:
            a2, b2 = r2.get("opus", {}).get(cid), r2.get("codex", {}).get(cid)
            rec["r2"] = {"opus": a2, "codex": b2}
            rec["label"] = a2["label"] if a2 and b2 and a2["label"] == b2["label"] else "disputed"
        out.append(rec)
    return out


def _read_labels(P: Paths, rnd: int, labeler: str, codes: set[str], cids: dict[str, str]) -> dict:
    """라벨러 출력(lid 기준)을 cid 기준으로 바꾼다. 허용되지 않은 라벨은 ValueError."""
    out = {}
    for corp in sorted(codes):
        for r in read_jsonl(P.data / "labels" / f"r{rnd}_{labeler}" / f"{corp}.jsonl"):
            if r["label"] not in LABELS:
                raise ValueError(f"bad label {r['label']} ({labeler} r{rnd} {corp})")
            out[cids[r["lid"]]] = {"label": r["label"], "passage": r.get("passage"), "reason": r.get("reason", "")}
    return out


def cmd_label_packets(P: Paths, args) -> None:
    """라벨 꾸러미: 질문별 문단 8개와 주장(불투명 ID, 섞은 순서). 2차는 불일치만, 상대 라벨·이유를 함께 보인다."""
    import random

    guide = (P.ev / "prompts/labeler.md").read_text()
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    qtext = {q["qid"]: q["question"] for q in read_jsonl(P.jsonl("questions.jsonl"))}
    codes = {c["corp_code"] for c in companies(P, args.split)}
    claims = [c for c in read_jsonl(P.jsonl("claims.jsonl")) if c["qid"].split("-q")[0] in codes]
    only, other = None, {}
    if args.round == 2:
        cids = {lid(c["cid"]): c["cid"] for c in claims}
        r1 = {lb: _read_labels(P, 1, lb, codes, cids) for lb in ("opus", "codex")}
        only = set(disagreements(r1))
        other = r1["codex" if args.labeler == "opus" else "opus"]
    for corp in sorted(codes):
        parts = []
        for qid in sorted({c["qid"] for c in claims if c["qid"].startswith(corp)}):
            cl = [c for c in claims if c["qid"] == qid and (only is None or c["cid"] in only)]
            if not cl:
                continue
            random.Random(f"{args.round}-{qid}").shuffle(cl)
            ps = "\n".join(f"[문단 {j}] {text[pid]}" for j, pid in enumerate(ret[qid], 1))
            lines = []
            for c in cl:
                line = f"- lid={lid(c['cid'])}: {c['text']}"
                if only is not None:
                    o = other[c["cid"]]
                    line += f"\n  (다른 라벨러: {o['label']}, 문단 {o['passage']}, 이유: {o['reason']})"
                lines.append(line)
            parts.append(f"## 질문: {qtext[qid]}\n{ps}\n\n주장:\n" + "\n".join(lines) + "\n")
        out = P.priv / "packets/labels" / f"r{args.round}_{args.labeler}" / f"{corp}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{guide}\n\n# 출력 파일: lab/evidence/data/labels/r{args.round}_{args.labeler}/{corp}.jsonl\n\n"
                       + "\n".join(parts))
    print("ok")


def cmd_labels_merge(P: Paths, args) -> None:
    """1·2차 라벨을 병합해 labels.jsonl을 쓰고 1차 κ(이진, 비주장 제외)를 출력한다."""
    from lab.evidence.metrics import kappa

    codes = {c["corp_code"] for c in companies(P, args.split)}
    claims = [c for c in read_jsonl(P.jsonl("claims.jsonl")) if c["qid"].split("-q")[0] in codes]
    cids = {lid(c["cid"]): c["cid"] for c in claims}
    r1 = {lb: _read_labels(P, 1, lb, codes, cids) for lb in ("opus", "codex")}
    r2 = {lb: _read_labels(P, 2, lb, codes, cids) for lb in ("opus", "codex")}
    rows = merge_labels(r1, r2)
    keep = [r for r in read_jsonl(P.jsonl("labels.jsonl")) if r["cid"].split("-q")[0] not in codes]
    write_jsonl(P.jsonl("labels.jsonl"), sorted(keep + rows, key=lambda r: r["cid"]))
    both = [c for c in r1["opus"].keys() & r1["codex"].keys()
            if "non_claim" not in (r1["opus"][c]["label"], r1["codex"][c]["label"])]
    k = kappa([r1["opus"][c]["label"] == "supported" for c in both], [r1["codex"][c]["label"] == "supported" for c in both])
    log_attempt(P, "labels-merge", split=args.split, labeled=len(rows),
                disputed=sum(r["label"] == "disputed" for r in rows), kappa_binary_r1=round(k, 4))
    print(f"{len(rows)} labels, kappa r1 {k:.3f}")
```

`COMMANDS`에 다음 두 항목을 추가한다.

```python
COMMANDS.update({"label-packets": cmd_label_packets, "labels-merge": cmd_labels_merge})
```

`lab/evidence/prompts/labeler.md`:

```markdown
# 근거 라벨 지시

각 질문 아래에 사업보고서 [문단 1~8]과 주장 목록이 있다. 주장마다 **보여 준 문단 8개만** 근거로 라벨을 하나 붙인다. 세상 지식은 쓰지 않는다.

- supported: 문장의 **모든** 사실 단위를 문단 **하나**가 지지한다(표현이 달라도 된다). 여러 문단을 합쳐야 하면 supported가 아니다.
- contradicted: 어떤 문단이 같은 주체·기간·지표에 대해 다른 사실을 말한다. 한 문단은 지지하고 다른 문단은 반박하면 contradicted.
- no_evidence: 위 둘이 아닌 사실 주장.
- non_claim: 사과, 질문, 면책, 의견, 절차 안내처럼 사실 주장이 아닌 문장.
- 숫자는 단위를 환산해 같은 값인지 확인한다(예: 1.2조 = 12,000억). 반올림한 근사는 같은 값으로 본다. 문단에 없는 계산값은 no_evidence다.

출력은 "출력 파일" 경로에 주장마다 JSONL 한 줄로 쓴다:
{"lid": "<lid>", "label": "supported|contradicted|no_evidence|non_claim", "passage": <근거 문단 번호 또는 null>, "reason": "<한 줄 이유>"}

다른 라벨러의 의견이 함께 적혀 있으면 그것을 참고해 다시 판단하되, 문단에 근거해 독립적으로 결정한다. 파일 외의 곳을 수정하지 않는다.
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence -v`
Expected: 전부 통과(앞 Task 포함)

- [ ] **Step 5: Commit**

```bash
git add lab/evidence/__main__.py lab/evidence/prompts/labeler.md tests/evidence/test_cli_labels.py
git commit -m "feat: AI 참조 라벨 꾸러미(불투명 ID·조정 라운드)와 병합·κ"
```

### Task 12: 명령줄 — JEV 판정 실행과 Stage 0 리포트

**Files:**
- Modify: `lab/evidence/__main__.py`
- Test: `tests/evidence/test_cli_stage0.py`

**Interfaces:**
- Consumes: Task 1·6·8·11
- Produces:
  - `TOKEN_CAP = 20_000_000`
  - `subset(cids: list[str], n: int) -> list[str]`(sha256 순 앞 n개, 결정적)
  - `judge --split tune|check [--single] [--no-cache] [--limit N] --tag T`: `priv/scores/<T>.jsonl`에 `{"cid","s","c","ok","requests"}`를 쓴다. 대상은 labels.jsonl에서 label이 disputed·non_claim이 아닌 주장이다.
  - `sessions(times: list[str], gap_hours: float = 1.0) -> list[list[str]]`
  - `stage0_gates(...)` → dict(항목별 값·통과 여부), `stage0-report`가 `lab/evidence/results/stage0.json`과 `docs/lab/evidence-stage0-report.md`를 쓴다.
  - 태그 규약: `tune`, `check`(묶음, 캐시 사용), `single`(check 50건 단건), `repeat1`·`repeat2`·`repeat3`(check 50건, 캐시 끔)

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/evidence/test_cli_stage0.py
from lab.evidence import __main__ as cli


def test_subset_is_deterministic():
    cids = [f"x-q1-n{i}" for i in range(20)]
    assert cli.subset(cids, 5) == cli.subset(list(reversed(cids)), 5) and len(cli.subset(cids, 5)) == 5


def test_sessions_split_on_gap():
    t = ["2026-10-02T01:00:00+00:00", "2026-10-02T01:10:00+00:00", "2026-10-02T03:00:00+00:00"]
    assert [len(s) for s in cli.sessions(t)] == [2, 1]


def test_stage0_gates_pass_and_fail():
    ok = cli.stage0_gates(corpus=(40, 40), kappa=0.7, controlled_agree=0.9, auc=(0.8, 0.7, 0.9),
                          batch_agree=0.95, p95_ms=900.0, fail=(0, 400), n_sessions=2, repeat_agree=0.95)
    assert ok["go"] is True
    bad = cli.stage0_gates(corpus=(40, 40), kappa=0.5, controlled_agree=0.9, auc=(0.8, 0.45, 0.9),
                           batch_agree=0.95, p95_ms=900.0, fail=(0, 400), n_sessions=1, repeat_agree=0.95)
    assert bad["go"] is False and not bad["kappa"]["pass"] and not bad["h_ko"]["pass"] and not bad["latency_fail"]["pass"]
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_cli_stage0.py -v`
Expected: FAIL. `AttributeError: ... 'subset'`

- [ ] **Step 3: 구현** — `COMMANDS` 정의들 아래(마지막 `COMMANDS.update` 뒤)에 추가한다.

```python
TOKEN_CAP = 20_000_000


def subset(cids: list[str], n: int) -> list[str]:
    """sha256 순으로 정렬한 앞 n개(결정적 표본)."""
    return sorted(cids, key=lambda c: hashlib.sha256(c.encode()).hexdigest())[:n]


def sessions(times: list[str], gap_hours: float = 1.0) -> list[list[str]]:
    """호출 시각을 gap_hours 이상 간격에서 세션으로 나눈다."""
    ts = sorted(datetime.fromisoformat(t) for t in times)
    out: list[list[str]] = []
    for t in ts:
        if not out or (t - datetime.fromisoformat(out[-1][-1])).total_seconds() >= gap_hours * 3600:
            out.append([])
        out[-1].append(t.isoformat())
    return out


def _judgeable(P: Paths, split_name: str) -> list[dict]:
    """분할의 주장 중 최종 라벨이 disputed·non_claim이 아닌 것(라벨 포함)."""
    codes = {c["corp_code"] for c in companies(P, split_name)}
    lab = {r["cid"]: r["label"] for r in read_jsonl(P.jsonl("labels.jsonl"))}
    return [dict(c, label=lab[c["cid"]]) for c in read_jsonl(P.jsonl("claims.jsonl"))
            if c["qid"].split("-q")[0] in codes and lab.get(c["cid"]) not in (None, "disputed", "non_claim")]


def cmd_judge(P: Paths, args) -> None:
    """JEV 판정을 실행해 비공개 점수 파일에 쓴다. 사전등록 파일이 없으면 거부."""
    from app.lib.jev import JevClient
    from app.services.evidence.judge import judge_claim

    if not P.prereg.exists():
        raise SystemExit("prereg.json must be committed before JEV calls")
    if not args.tag:
        raise SystemExit("--tag is required")
    claims = _judgeable(P, args.split)
    if args.limit:
        keep = set(subset([c["cid"] for c in claims if c["source"] == "natural"], args.limit))
        claims = [c for c in claims if c["cid"] in keep]
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    names = _corp_names(P)
    client = JevClient(P.calls, TOKEN_CAP)
    out = []
    for c in claims:
        j = judge_claim(client, names[c["qid"].split("-q")[0]], c["text"], [text[i] for i in ret[c["qid"]]],
                        use_cache=not args.no_cache, tag=args.tag, single=args.single)
        out.append({"cid": c["cid"], "s": j.s, "c": j.c, "ok": j.ok, "requests": j.requests})
    write_jsonl(P.priv / "scores" / f"{args.tag}.jsonl", out)
    log_attempt(P, "judge", tag=args.tag, split=args.split, claims=len(out), failed=sum(not r["ok"] for r in out),
                used_tokens=client.used_tokens)
    print(f"{len(out)} judged, used tokens {client.used_tokens}")


def stage0_gates(*, corpus, kappa, controlled_agree, auc, batch_agree, p95_ms, fail, n_sessions, repeat_agree) -> dict:
    """spec 7절 Stage 0 관문 판정."""
    from lab.evidence.metrics import cp_upper

    upper = cp_upper(*fail)
    g = {
        "corpus": {"value": corpus, "pass": corpus[0] >= 38},
        "kappa": {"value": kappa, "pass": kappa >= 0.6},
        "controlled": {"value": controlled_agree, "pass": controlled_agree >= 0.85},
        "h_ko": {"value": auc, "pass": auc[0] is not None and auc[0] >= 0.70 and auc[1] is not None and auc[1] > 0.5},
        "batching": {"value": batch_agree, "pass": batch_agree >= 0.90},
        "latency_fail": {"value": {"p95_ms": p95_ms, "first_requests": fail[1], "fail_upper": upper,
                                   "sessions": n_sessions},
                         "pass": fail[1] >= 300 and n_sessions >= 2 and p95_ms <= 1500 and upper <= 0.02},
        "repeat": {"value": repeat_agree, "pass": repeat_agree >= 0.90},
    }
    g["go"] = all(v["pass"] for v in g.values())
    return g


def _scores(P: Paths, tag: str) -> dict[str, dict]:
    return {r["cid"]: r for r in read_jsonl(P.priv / "scores" / f"{tag}.jsonl")}


def cmd_stage0_report(P: Paths, args) -> None:
    """Stage 0 관문을 계산해 집계 JSON과 리포트를 쓴다(달러 금액은 쓰지 않는다)."""
    from app.services.evidence.judge import Judgement, passage_labels
    from lab.evidence.metrics import auc, cluster_bootstrap, kappa, percentile

    split = load_split(P)
    cluster = {c["corp_code"]: c["cluster"] for c in split["companies"]}
    n_ok = sum(1 for c in split["companies"] if (P.priv / "docs" / f"{c['rcept_no']}.xml").exists())
    labels = {r["cid"]: r for r in read_jsonl(P.jsonl("labels.jsonl"))}
    claims = {c["cid"]: c for c in read_jsonl(P.jsonl("claims.jsonl"))}
    dev_codes = {c["corp_code"] for c in companies(P, "dev")}
    nat = [l for cid, l in labels.items() if claims[cid]["source"] == "natural"
           and cid.split("-q")[0] in dev_codes]
    both = [l for l in nat if "non_claim" not in (l["r1"]["opus"]["label"], l["r1"]["codex"]["label"])]
    k = kappa([l["r1"]["opus"]["label"] == "supported" for l in both],
              [l["r1"]["codex"]["label"] == "supported" for l in both])
    ctrl = [(l, claims[cid]) for cid, l in labels.items() if claims[cid]["source"] == "controlled"
            and cid.split("-q")[0] in dev_codes]
    agree = [(l["r1"][lb]["label"] == "supported") == (c["expected"] == "supported") for l, c in ctrl
             for lb in ("opus", "codex")]
    controlled_agree = sum(agree) / len(agree) if agree else 0.0
    chk = _scores(P, "check")
    rows = []
    for c in _judgeable(P, "check"):
        if c["source"] == "natural" and c["cid"] in chk:
            r = chk[c["cid"]]
            rows.append({"cluster": cluster[c["qid"].split("-q")[0]], "qid": c["qid"],
                         "y": int(c["label"] == "supported"), "score": max(r["s"]) if r["ok"] else 0.0})
    stat = lambda rs: auc([r["y"] for r in rs], [r["score"] for r in rs])
    auc_ci = cluster_bootstrap(rows, stat)
    single, batched = _scores(P, "single"), chk
    pairs = [(a, b) for cid in single for a, b in zip(passage_labels(Judgement(single[cid]["s"], single[cid]["c"], True, 0)),
                                                       passage_labels(Judgement(batched[cid]["s"], batched[cid]["c"], True, 0)))
             if single[cid]["ok"] and batched[cid]["ok"]]
    batch_agree = sum(a == b for a, b in pairs) / len(pairs) if pairs else 0.0
    reps = [_scores(P, f"repeat{i}") for i in (1, 2, 3)]

    def sig(r):
        j = Judgement(r["s"], r["c"], r["ok"], 0)
        top = max(range(len(j.s)), key=lambda i: j.s[i])
        return (top, passage_labels(j)[top])

    rep_cids = [cid for cid in reps[0] if all(cid in r and r[cid]["ok"] for r in reps)]
    repeat_agree = (sum(len({sig(r[cid]) for r in reps}) == 1 for cid in rep_cids) / len(rep_cids)) if rep_cids else 0.0
    first = [r for r in read_jsonl(P.calls) if r["attempt"] == 1]
    lat = [r["latency_ms"] for r in first]
    fails = sum(not r["ok"] for r in first)
    sess = sessions([r["called_at"] for r in first])
    gates = stage0_gates(corpus=(n_ok, len(split["companies"])), kappa=k, controlled_agree=controlled_agree,
                         auc=auc_ci, batch_agree=batch_agree, p95_ms=percentile(lat, 95) if lat else 1e9,
                         fail=(fails, len(first)), n_sessions=len(sess), repeat_agree=repeat_agree)
    summary = {"gates": gates, "check_rows": len(rows), "check_supported": sum(r["y"] for r in rows),
               "latency_ms": {q: percentile(lat, q) for q in (50, 95, 99)} if lat else {},
               "first_requests": len(first), "input_tokens": sum(r["input_tokens"] for r in read_jsonl(P.calls)),
               "session_sizes": [len(s) for s in sess], "disputed": sum(l["label"] == "disputed" for l in nat)}
    out = P.ev / "results/stage0.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str) + "\n")
    report = P.root / "docs/lab/evidence-stage0-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 근거 판정 엔진 Stage 0 리포트", "",
             "> 정답 라벨은 사람이 아니라 AI(Claude Opus·Codex)가 만든 **AI 참조 라벨**이다. 결과는 AI 참조 라벨과의 일치 성능이다.", "",
             f"**판정: {'GO' if gates['go'] else 'NO-GO'}**", "", "| 기준 | 값 | 통과 |", "|---|---|---|"]
    for name, g in gates.items():
        if name != "go":
            lines.append(f"| {name} | {json.dumps(g['value'], ensure_ascii=False, default=str)} | {'예' if g['pass'] else '아니오'} |")
    lines += ["", f"- 확인 세트 자연 주장 {summary['check_rows']}건(지지됨 {summary['check_supported']}건)",
              f"- JEV 첫 요청 {summary['first_requests']}회, 입력 토큰 {summary['input_tokens']:,}개, 세션 크기 {summary['session_sizes']}",
              f"- 라벨 조정 후에도 갈린 개발 자연 주장 {summary['disputed']}건(주결과에서 제외)"]
    report.write_text("\n".join(lines) + "\n")
    log_attempt(P, "stage0-report", go=gates["go"])
    print("GO" if gates["go"] else "NO-GO")


COMMANDS.update({"judge": cmd_judge, "stage0-report": cmd_stage0_report})
```

- [ ] **Step 4: 통과 확인**

Run: `/tmp/evt.sh tests/evidence -v`
Expected: 전부 통과

- [ ] **Step 5: Commit**

```bash
git add lab/evidence/__main__.py tests/evidence/test_cli_stage0.py
git commit -m "feat: JEV 판정 실행·세션 분리·Stage 0 관문 리포트"
```

### Task 13: 키 배치·추첨·코퍼스·사전등록 (실행)

**Files:**
- Create: `lab/evidence/prereg.json`
- 생성(커밋): `lab/evidence/split.json`, `lab/evidence/data/{draw_ledger,dart_ledger,passages_manifest}.jsonl`, `lab/evidence/attempts.jsonl`

- [ ] **Step 1: OpenDART 키 배치(값 출력 금지)**

```bash
mkdir -p ~/.config/opendart && umask 077 && grep -E '^DART_API_KEY=' /Users/noah/portfolios/stock-coin-trade/.env | cut -d= -f2- | tr -d '\n' > ~/.config/opendart/api_key && chmod 600 ~/.config/opendart/api_key && stat -f '%Lp %z' ~/.config/opendart/api_key
```
Expected: `600 40`

- [ ] **Step 2: 추첨·분할**

Run: `/tmp/ev.sh split`
Expected: `{"tune": ≤10, "check": …, "holdout": ≤20}` 형태 한 줄. 시드 15개사가 모두 `ok`여야 한다(아니면 SystemExit로 멈춘다. 원인을 원장에 남기고 spec 7절 대체 규칙을 따른다).

- [ ] **Step 3: 문단 분해**

Run: `/tmp/ev.sh passages`
Expected: `N passages`(N은 수천 개 수준). `lab/evidence/data/passages_manifest.jsonl`에 본문이 없는지 확인한다. `grep -c '"text"' lab/evidence/data/passages_manifest.jsonl`의 결과가 0이어야 한다.

- [ ] **Step 4: 사전등록 v1 작성**

`lab/evidence/prereg.json`(값은 아래 그대로 쓰고, `split_sha256`·`question_sha`는 실제 값으로 채운다):

```json
{
  "version": 1,
  "spec": "docs/superpowers/specs/2026-10-02-evidence-assistant-design.md@a46903f",
  "seed": 20261002,
  "split_sha256": "<sha256 of lab/evidence/split.json>",
  "k": 8,
  "jev_model": "jev-1.13.0",
  "judge_question_sha": "<app.services.evidence.judge.QUESTION_SHA>",
  "generator": {"model": "llama3.2:1b", "digest": "baf6a787fdff", "fallback": "qwen2.5:3b", "fallback_rule": "tune non_claim share > 0.5"},
  "embedder": "nomic-embed-text",
  "labels": {"prompt": "lab/evidence/prompts/labeler.md", "labelers": ["claude-opus subagent", "codex exec"], "reconcile": "one round, then disputed"},
  "stage0_gates": {"corpus_min": 38, "kappa_min": 0.6, "controlled_min": 0.85, "auc_min": 0.70, "auc_lo_gt": 0.5,
                   "batch_agree_min": 0.90, "first_requests_min": 300, "sessions_min": 2, "p95_ms_max": 1500,
                   "fail_upper_max": 0.02, "repeat_agree_min": 0.90},
  "bootstrap": {"levels": ["cluster", "question"], "n": 2000, "seed": 20261002},
  "token_cap": 20000000,
  "stop": "any gate fails -> NO-GO, publish, move to module B if h_ko fails"
}
```

```bash
shasum -a 256 lab/evidence/split.json
uv run -q --no-project --python 3.12 python -c "from app.services.evidence.judge import QUESTION_SHA; print(QUESTION_SHA)"
```

- [ ] **Step 5: Commit**

```bash
git add lab/evidence/prereg.json lab/evidence/split.json lab/evidence/data/draw_ledger.jsonl lab/evidence/data/dart_ledger.jsonl lab/evidence/data/passages_manifest.jsonl lab/evidence/attempts.jsonl
git commit -m "chore: 평가 기업 40개사 추첨·군집 분할·문단 매니페스트와 사전등록 v1"
```

### Task 14: 질문·검색·생성·주장 (실행)

- [ ] **Step 1: 질문 작성 꾸러미**

Run: `/tmp/ev.sh question-packets --split dev`

- [ ] **Step 2: 질문 작성 서브에이전트 실행**

개발 회사마다 Opus 서브에이전트 1개를 띄운다. 동시에 최대 10개, 회사마다 다른 파일에 쓴다. 지시문:

> 파일 `/Users/noah/portfolios/lumina-invest/lab/data/evidence/packets/questions/<corp>.md`를 끝까지 읽고 그 안의 지시를 따른다. 결과는 그 파일에 적힌 출력 파일 경로에만 쓴다. 다른 파일을 읽거나 수정하지 않는다. 끝나면 쓴 줄 수만 보고한다.

- [ ] **Step 3: 병합·임베딩·검색·생성·주장**

```bash
/tmp/ev.sh questions-merge --split dev
/tmp/evd.sh embed
/tmp/evd.sh retrieve --split dev
/tmp/evd.sh generate --split dev
/tmp/ev.sh claims --split dev
```
Expected: 질문 수 = 개발 기업 수 × 6, 검색 행 수와 답변 수도 같다.

- [ ] **Step 4: 생성기 대체 판단(spec 2절)**

조정 세트 자연 주장 중 비주장 비율은 라벨이 생긴 뒤에 알 수 있다. 그래서 이 판단은 Task 15 Step 4에서 한다.

- [ ] **Step 5: 통제 주장**

```bash
/tmp/ev.sh controlled-packets --split dev
```
개발 회사마다 Opus 서브에이전트 1개를 띄운다. 지시문은 Step 2와 같고 경로만 `packets/controlled/<corp>.md`로 바꾼다. 끝나면 다음을 실행한다.
```bash
/tmp/ev.sh controlled-check --split dev
```
Expected: `accepted`가 질문 수의 90% 이상이다. 거부된 질문은 같은 지시로 한 번 다시 쓰게 하고, 그래도 거부되면 원장에 사유를 남긴다.

- [ ] **Step 6: Commit**

```bash
git add lab/evidence/data/questions lab/evidence/data/questions.jsonl lab/evidence/data/retrieval.jsonl lab/evidence/data/answers.jsonl lab/evidence/data/claims.jsonl lab/evidence/data/controlled lab/evidence/data/controlled_rejected.jsonl lab/evidence/attempts.jsonl
git commit -m "chore: 개발 세트 질문·검색·동결 답변·자연/통제 주장"
```

### Task 15: AI 참조 라벨 (실행)

- [ ] **Step 1: 1차 라벨 — Opus**

Run: `/tmp/ev.sh label-packets --labeler opus --round 1 --split dev`
개발 회사마다 Opus 서브에이전트 1개를 띄운다(동시 최대 10). 지시문은 Task 14 Step 2와 같고 경로는 `packets/labels/r1_opus/<corp>.md`다.

- [ ] **Step 2: 1차 라벨 — Codex**

```bash
/tmp/ev.sh label-packets --labeler codex --round 1 --split dev
for f in lab/data/evidence/packets/labels/r1_codex/*.md; do
  corp=$(basename "$f" .md)
  codex exec -s workspace-write -C /Users/noah/portfolios/lumina-invest \
    "파일 $f 를 끝까지 읽고 그 안의 지시를 따르라. 결과는 lab/evidence/data/labels/r1_codex/$corp.jsonl 에만 써라. 다른 파일을 수정하지 마라." >/dev/null
done
```

- [ ] **Step 3: 2차(조정) 라벨과 병합**

```bash
/tmp/ev.sh label-packets --labeler opus --round 2 --split dev
/tmp/ev.sh label-packets --labeler codex --round 2 --split dev
```
2차 꾸러미가 있는 회사만 Step 1·2와 같은 방식으로 실행한다. 그다음 다음을 실행한다.
```bash
/tmp/ev.sh labels-merge --split dev
```
Expected: `N labels, kappa r1 x.xxx`.

- [ ] **Step 4: 생성기 대체 판단**

`labels.jsonl`에서 조정 세트 자연 주장 중 `non_claim` 비율을 계산한다. 0.5를 넘으면 다음 순서로 진행하고 원장에 남긴다.
1. `qwen2.5:3b`를 pull하고 `GEN_MODEL`을 바꾼다.
2. 개발 답변·주장·라벨을 다시 만든다. 기존 파일은 `lab/evidence/data/archive_llama/`로 옮긴다.

0.5 이하이면 그대로 진행한다.

- [ ] **Step 5: 타당도 표본 점검**

숫자가 든 개발 주장 50개를 `subset`으로 뽑는다. 작성자(Claude)가 원문 XML 문단과 대조해 라벨 오류 수를 `lab/evidence/data/label_audit.jsonl`에 기록한다(`{"cid","final","audited","note"}`). 기대 라벨과 다르게 붙은 통제 주장도 같은 파일에 확정 결과를 남긴다.

- [ ] **Step 6: Commit**

```bash
git add lab/evidence/data/labels lab/evidence/data/labels.jsonl lab/evidence/data/label_audit.jsonl lab/evidence/attempts.jsonl
git commit -m "chore: 개발 세트 AI 참조 라벨(1·2차)과 표본 감사"
```

### Task 16: JEV 측정과 Stage 0 판정 (실행)

- [ ] **Step 1: 세션 1(조정 세트, 묶음)**

Run: `/tmp/ev.sh judge --split tune --tag tune`

- [ ] **Step 2: 판정 질문 문구 조정(필요할 때만)**

조정 세트 자연 주장의 AUC를 본다. 문구를 바꾸면 다음 순서를 지킨다.
1. `judge.py`의 INSTRUCTIONS·CRITERIA를 바꾼다.
2. `prereg.json`의 `judge_question_sha`를 갱신한다.
3. 원장에 이유를 남기고 커밋한다.
4. 조정 세트를 다시 돌린다.

확인 세트는 이 단계에서 보지 않는다. 문구 조정은 최대 3회다.

- [ ] **Step 3: 묶기 검사(같은 세션)**

Run: `/tmp/ev.sh judge --split check --tag check`, 이어서 `/tmp/ev.sh judge --split check --single --limit 50 --tag single`

- [ ] **Step 4: 세션 2(1시간 이상 뒤): 반복 일관성**

```bash
/tmp/ev.sh judge --split check --limit 50 --no-cache --tag repeat1
/tmp/ev.sh judge --split check --limit 50 --no-cache --tag repeat2
/tmp/ev.sh judge --split check --limit 50 --no-cache --tag repeat3
```

- [ ] **Step 5: 리포트**

Run: `/tmp/ev.sh stage0-report`
Expected: `GO` 또는 `NO-GO`. `docs/lab/evidence-stage0-report.md`와 `lab/evidence/results/stage0.json`이 생긴다.

다음 두 grep의 결과가 모두 0이어야 한다. 달러 금액이 없어야 하고, JEV 입력·출력 짝이 커밋 대상에 섞이지 않아야 한다.

```bash
grep -c '\$' docs/lab/evidence-stage0-report.md
git status --porcelain | grep -c 'lab/data/'
```

- [ ] **Step 6: Commit과 PR**

```bash
git add docs/lab/evidence-stage0-report.md lab/evidence/results/stage0.json lab/evidence/attempts.jsonl lab/evidence/prereg.json
git commit -m "docs: 근거 판정 엔진 Stage 0 결과"
```
PR 본문에 다음을 적는다.
- 관문 표
- AI 참조 라벨이라는 한계
- 세션 시각
- 토큰 수
- NO-GO일 때의 다음 단계(spec 7절)
