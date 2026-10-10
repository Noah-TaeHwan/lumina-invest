# 근거 판정 엔진 Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 숫자 존재 확인과 SYS 규칙을 붙이고, 개발 세트로 임계값·최고 기준선·검정력을 정해 사전등록에 동결한다. 그다음 홀드아웃 20개사 데이터를 만들고 해시를 동결한 뒤 판정기를 한 번 실행해 주결과(Δ = AUC(SYS) − AUC(B\*))를 낸다.

**Architecture:**
- 제품 코드는 `app/services/evidence/numbers.py`(숫자 존재 확인)와 `judge.py`(SYS 판정)에 추가한다.
- 평가 쪽은 `lab/evidence/`에 `thresholds.py`(임계값 선택), `baselines.py`(기준선 4종), 명령줄 명령을 더한다.
- 홀드아웃은 두 단계로 봉인한다.
  - 데이터 생성은 사전등록에 `stage1` 블록이 있어야 열린다.
  - 판정·기준선 실행은 `prereg_holdout.json`에 해시가 있어야 열리고, 한 번만 실행할 수 있다.

**Tech Stack:** Stage 0와 같다. 여기에 B-nli용 `torch`·`transformers`·`sentencepiece`·`protobuf`를 평가 하네스 전용 uv 환경으로 추가한다(앱 의존성에는 넣지 않음).

**Spec:** `docs/superpowers/specs/2026-10-02-evidence-assistant-design.md` 3·5·6·7절. 선행 계획은 `docs/superpowers/plans/2026-10-02-evidence-stage0.md`, Stage 0 리포트는 `docs/lab/evidence-stage0-report.md`다.

## Global Constraints

- Stage 0 Global Constraints를 모두 이어받는다(모델 고정, k=8, 시드 20261002, 키 경로, `lab/data/` 비공개, 금액 비공개, 판정 모델 출력으로 학습 금지).
- 임계값 후보는 0.05~0.95, 0.05 간격이다. 먼저 τ_c를 반박 F1 최대로 정하고, 그다음 τ_s를 지지됨 정밀도 0.90 이상 중 최솟값으로 정한다(없으면 F1 최대). 동률이면 큰 값을 고른다. 선택에는 조정 세트 자연 주장만 쓴다.
- B\*는 조정 세트 자연 주장 AUC가 가장 높은 기준선이다. 부트스트랩 반복마다 다시 고르지 않는다.
- MDE는 개발 자연 주장으로 홀드아웃 군집 수(12)만큼 2단 재표집해 Δ의 표준편차 SE를 구하고, 2.8 × SE로 둔다.
  - MDE > 0.10이면 홀드아웃 질문을 기업당 10개로 늘린다.
  - 그래도 0.10을 넘을 것으로 보이면 주결과를 탐색적 평가로 미리 낮춘다.
- 홀드아웃 판정·기준선은 출력 파일이 이미 있으면 거부한다(한 번만 실행).
- 임베딩: 주장 임베딩과 홀드아웃 문단 임베딩은 호스트 Ollama(Metal)에서 한다. 같은 모델 digest `0a109f422b47`이다.
- 생성: 호스트 Ollama `llama3.2:1b`(digest `baf6a787fdff`)다.

## Review Focus

| 순서 | 입력·조건 | 기대 동작 | 고정하는 테스트 |
|---|---|---|---|
| 1 | 표 단위가 "백만원"이고 주장은 "약 1조 67억원" | 단위를 환산하고 마지막 자리 범위 안이면 일치 | Task 1 `test_number_check_units_and_truncation` |
| 2 | 주장 "33.0%" 대 표 단위 "%"의 맨 숫자 33.04 | 퍼센트로 해석해 일치 | Task 1 `test_percent_table_unit` |
| 3 | 숫자 확인에 실패한 문단의 높은 지지 확률 | 반박을 막지 못하고 지지됨도 되지 않음 | Task 2 `test_sys_rule_invalid_support_does_not_block_contradiction` |
| 4 | 홀드아웃 판정을 두 번 실행 | 두 번째는 거부 | Task 5 `test_holdout_run_once` |
| 5 | 동결 전에 홀드아웃 판정, 사전등록 전에 홀드아웃 데이터 생성 | 각각 거부 | Task 5 `test_two_stage_holdout_seal` |

---

### Task 1: 숫자 존재 확인

**Files:**
- Create: `app/services/evidence/numbers.py`
- Test: `tests/evidence/test_numbers.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Num(value: Decimal, step: Decimal, kind: str)`. kind는 `"abs"|"pct"|"pctp"`다.
  - `parse(text: str, default_scale: int = 1, default_kind: str = "abs") -> list[Num]`
  - `passage_numbers(text: str) -> list[Num]`. 표 행 접두어 `[... 표, 단위 X]`를 조각마다 반영한다.
  - `number_check(claim: str, passage: str) -> bool`. 주장에 숫자가 없으면 True다.

- [ ] **Step 1: 실패하는 테스트**

```python
# tests/evidence/test_numbers.py
from decimal import Decimal

from app.services.evidence import numbers as nb


def test_parse_compound_units_and_kinds():
    assert nb.parse("1조 2,345억 원") == [nb.Num(Decimal("1234500000000"), Decimal("100000000"), "abs")]
    assert nb.parse("33.0%") == [nb.Num(Decimal("33.0"), Decimal("0.1"), "pct")]
    assert nb.parse("3.5%p") == [nb.Num(Decimal("3.5"), Decimal("0.1"), "pctp")]
    assert nb.parse("(1,234) 감소")[0].value == Decimal("-1234")


def test_number_check_units_and_truncation():
    row = "[가. 매출 표, 단위 백만원] 구분: 매출 | 2025: 1,006,771"
    assert nb.number_check("매출은 약 1조 67억원이다.", row)
    assert not nb.number_check("매출은 2조원이다.", row)
    assert nb.number_check("매출은 1.23조원이다.", "매출 12,345억원")
    assert nb.number_check("숫자가 없는 주장이다.", row)
    assert not nb.number_check("2025년에 설립했다.", "2024년 설립")


def test_percent_table_unit():
    row = "[나. 비중 표, 단위 %] 구분: DX | 비중: 33.04"
    assert nb.number_check("DX 비중은 33.0%다.", row)
    assert not nb.number_check("DX 비중은 34%다.", row)
    assert nb.number_check("비중은 33.0%다.", "비중 33.04%")


def test_segments_use_their_own_units():
    text = "[가 표, 단위 억원] 매출: 120 [나 표, 단위 백만원] 이익: 3,000"
    assert nb.number_check("매출 120억원, 이익 30억원", text)
```

- [ ] **Step 2: 실패 확인**

Run: `/tmp/evt.sh tests/evidence/test_numbers.py -v`
Expected: FAIL. `ImportError: cannot import name 'numbers'`

- [ ] **Step 3: 구현**

```python
# app/services/evidence/numbers.py
"""주장의 숫자가 문단에 같은 값으로 있는지 코드로 확인한다(spec 3절, 필요조건).

같은 값이란 문단 값을 주장 표기의 마지막 자리 단위(step)로 반올림하거나 버림했을 때 주장 값과 같아지는 것이다
(예: 1조 67억 ← 1,006,771백만은 버림으로 일치, 2조 ← 1,006,771백만은 불일치). 기간·지표가 맞는지는 판단하지 않는다(JEV 몫).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

_UNIT = {"조": 10**12, "십억": 10**9, "억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4, "천": 10**3}
_TOKEN = re.compile(r"(\()?(\d[\d,]*(?:\.\d+)?)\s*(조|십억|억|천만|백만|만|천)?\s*(%p|%포인트|%|퍼센트)?")
_SEGMENT = re.compile(r"(?=\[[^\]]*표[^\]]*\])")
_TABLE_UNIT = re.compile(r"\[[^\]]*표[^\]]*단위 ([^\]]+)\]")


@dataclass(frozen=True)
class Num:
    """정규화한 숫자: 값, 허용 범위(마지막 자리 단위), 종류(abs·pct·pctp)."""

    value: Decimal
    step: Decimal
    kind: str


def _scale(unit_text: str) -> tuple[int, str]:
    """표 단위 문자열을 배수와 기본 종류로 바꾼다(예: '백만원' → 10^6, '%' → 퍼센트)."""
    u = unit_text.strip()
    if u.startswith("%"):
        return 1, "pct"
    for k, v in _UNIT.items():
        if u.startswith(k):
            return v, "abs"
    return 1, "abs"


def parse(text: str, default_scale: int = 1, default_kind: str = "abs") -> list[Num]:
    """숫자 표현을 차례로 뽑는다. '1조 2,345억'처럼 큰 단위 뒤 작은 단위는 하나로 합친다."""
    out: list[Num] = []
    prev_mult, prev_end = 0, -1
    for m in _TOKEN.finditer(text):
        paren, digits, unit, pct = m.groups()
        raw = digits.replace(",", "").rstrip(".")
        if not raw or not raw.replace(".", "", 1).isdigit():
            continue
        val = Decimal(raw)
        dec = -val.as_tuple().exponent if "." in raw else 0
        if pct in ("%p", "%포인트"):
            kind, mult = "pctp", 1
        elif pct:
            kind, mult = "pct", 1
        elif unit:
            kind, mult = "abs", _UNIT[unit]
        else:
            kind, mult = default_kind, (default_scale if default_kind == "abs" else 1)
        v = val * mult
        step = Decimal(1).scaleb(-dec) * mult
        if paren and text[m.end(2):m.end(2) + 1] == ")":
            v = -v
        if unit and out and prev_mult > mult and kind == "abs" and text[prev_end:m.start()].strip() == "":
            v = out.pop().value + v
        out.append(Num(v, step, kind))
        prev_mult, prev_end = (_UNIT[unit] if unit else 0), m.end()
    return out


def passage_numbers(text: str) -> list[Num]:
    """문단을 표 행 접두어 단위로 나눠, 조각마다 표 단위를 적용해 숫자를 뽑는다."""
    nums: list[Num] = []
    for seg in _SEGMENT.split(text):
        m = _TABLE_UNIT.match(seg)
        scale, kind = _scale(m.group(1)) if m else (1, "abs")
        body = seg[m.end():] if m else seg
        nums += parse(body, scale, kind)
    return nums


def _same(h: Num, w: Num) -> bool:
    """문단 값 h를 주장 w의 마지막 자리 단위로 반올림 또는 버림했을 때 w와 같은가."""
    if h.kind != w.kind:
        return False
    q, target = h.value / w.step, w.value / w.step
    return q.to_integral_value(ROUND_HALF_UP) == target or q.to_integral_value(ROUND_DOWN) == target


def number_check(claim: str, passage: str) -> bool:
    """주장의 모든 숫자가 문단에 같은 값(반올림·버림 일치)으로 있으면 True."""
    wanted = parse(claim)
    if not wanted:
        return True
    have = passage_numbers(passage)
    return all(any(_same(h, w) for h in have) for w in wanted)
```

- [ ] **Step 4: 통과 확인** — Run: `/tmp/evt.sh tests/evidence/test_numbers.py -v` / Expected: 4 passed
- [ ] **Step 5: Commit** — `feat: 숫자 존재 확인(단위 환산·표 단위·허용 범위)`

### Task 2: SYS 판정 규칙과 임계값 선택

**Files:**
- Modify: `app/services/evidence/judge.py`(함수 추가)
- Create: `lab/evidence/thresholds.py`
- Test: `tests/evidence/test_sys_rules.py`

**Interfaces:**
- Produces:
  - `judge.sys_decision(j: Judgement, valid: list[bool], tau_s: float, tau_c: float) -> tuple[str, int | None, float]`. 판정은 `"supported"|"contradicted"|"no_evidence"|"unjudged"` 중 하나이고, 출처 문단 인덱스와 SYS 점수를 함께 돌려준다.
  - `thresholds.GRID: list[float]`
  - `thresholds.choose_tau_c(rows) -> float`, `thresholds.choose_tau_s(rows, tau_c) -> float`. rows 항목은 `{"s": list[float], "c": list[float], "valid": list[bool], "label": str}`이다.

- [ ] **Step 1: 실패하는 테스트**

```python
# tests/evidence/test_sys_rules.py
from app.services.evidence.judge import Judgement, sys_decision
from lab.evidence import thresholds as th


def J(s, c, ok=True):
    return Judgement(s, c, ok, 1)


def test_sys_rule_invalid_support_does_not_block_contradiction():
    j = J([0.95, 0.85, 0.0], [0.0, 0.0, 0.9])
    assert sys_decision(j, [False, True, True], tau_s=0.8, tau_c=0.8) == ("contradicted", 2, 0.0)
    assert sys_decision(j, [True, True, True], tau_s=0.8, tau_c=0.8) == ("supported", 0, 0.95)


def test_sys_rule_picks_valid_passage_and_scores():
    j = J([0.9, 0.7, 0.1], [0.05, 0.1, 0.1])
    assert sys_decision(j, [False, True, True], 0.6, 0.8) == ("supported", 1, 0.7)
    assert sys_decision(j, [False, False, False], 0.6, 0.8) == ("no_evidence", None, 0.0)
    assert sys_decision(J([0.0], [0.0], ok=False), [True], 0.5, 0.5) == ("unjudged", None, 0.0)


def test_choose_thresholds():
    rows = ([{"s": [0.9], "c": [0.05], "valid": [True], "label": "supported"}] * 9
            + [{"s": [0.6], "c": [0.1], "valid": [True], "label": "no_evidence"}] * 3
            + [{"s": [0.1], "c": [0.8], "valid": [True], "label": "contradicted"}] * 4
            + [{"s": [0.2], "c": [0.4], "valid": [True], "label": "no_evidence"}] * 2)
    tc = th.choose_tau_c(rows)
    assert tc == 0.8
    assert th.choose_tau_s(rows, tc) == 0.65
```

- [ ] **Step 2: 실패 확인** — Run: `/tmp/evt.sh tests/evidence/test_sys_rules.py -v` / Expected: FAIL `ImportError`

- [ ] **Step 3: 구현** — `judge.py` 끝에 추가:

```python
def sys_decision(j: Judgement, valid: list[bool], tau_s: float, tau_c: float) -> tuple[str, int | None, float]:
    """spec 3절 SYS 규칙. valid는 문단별 숫자 존재 확인 통과 여부다.

    반박: max c ≥ τ_c이고 max c > S_V(숫자 확인을 통과한 문단의 지지 최댓값)일 때. 점수 0.
    지지됨: S_V ≥ τ_s일 때, 그 문단이 출처. 점수 S_V. 나머지는 근거 없음(점수 S_V).
    """
    if not j.ok:
        return "unjudged", None, 0.0
    cand = [(s, i) for i, (s, v) in enumerate(zip(j.s, valid)) if v]
    s_v, idx = max(cand) if cand else (0.0, None)
    mc = max(j.c)
    if mc >= tau_c and mc > s_v:
        return "contradicted", j.c.index(mc), 0.0
    if idx is not None and s_v >= tau_s:
        return "supported", idx, s_v
    return "no_evidence", None, s_v
```

`max(cand)`는 지지 확률이 같으면 인덱스가 큰 문단을 고른다. 출처는 점수가 같은 문단 중 아무것이나 돼도 판정은 같다.

테스트 `("supported", 0, 0.95)`는 문단 0이 유일한 최댓값이라 맞다. `("supported", 1, 0.7)`도 유효 문단 중 최댓값이 문단 1이라 맞다.

```python
# lab/evidence/thresholds.py
"""조정 세트에서 SYS 임계값을 고른다(spec 3절 규칙 5): τ_c 먼저, 그다음 τ_s. 동률이면 큰 값."""
from __future__ import annotations

from app.services.evidence.judge import Judgement, sys_decision

GRID = [round(0.05 * i, 2) for i in range(1, 20)]


def _f1(tp: int, fp: int, fn: int) -> float:
    return 0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn)


def _decide(r: dict, tau_s: float, tau_c: float) -> str:
    return sys_decision(Judgement(r["s"], r["c"], True, 1), r["valid"], tau_s, tau_c)[0]


def choose_tau_c(rows: list[dict]) -> float:
    """반박 대 나머지 F1이 최대인 τ_c. 규칙 1은 τ_s와 무관하므로 τ_s=1.01로 둔다."""
    best = (-1.0, 0.0)
    for t in GRID:
        pred = [_decide(r, 1.01, t) == "contradicted" for r in rows]
        gold = [r["label"] == "contradicted" for r in rows]
        tp = sum(p and g for p, g in zip(pred, gold))
        fp = sum(p and not g for p, g in zip(pred, gold))
        fn = sum(g and not p for p, g in zip(pred, gold))
        best = max(best, (_f1(tp, fp, fn), t))
    return best[1]


def choose_tau_s(rows: list[dict], tau_c: float) -> float:
    """지지됨 정밀도 ≥ 0.90인 가장 작은 τ_s. 없으면 지지됨 F1 최대(동률이면 큰 값)."""
    stats = []
    for t in GRID:
        pred = [_decide(r, t, tau_c) == "supported" for r in rows]
        gold = [r["label"] == "supported" for r in rows]
        tp = sum(p and g for p, g in zip(pred, gold))
        fp = sum(p and not g for p, g in zip(pred, gold))
        fn = sum(g and not p for p, g in zip(pred, gold))
        stats.append((t, tp, fp, fn))
    for t, tp, fp, fn in stats:
        if tp + fp > 0 and tp / (tp + fp) >= 0.90:
            return t
    return max((_f1(tp, fp, fn), t) for t, tp, fp, fn in stats)[1]
```

테스트 데이터로 검산한다.
- **τ_c:** t ≤ 0.4이면 반박 6건(c=0.8 넷, c=0.4 둘)을 예측해 F1 = 8/10 = 0.8이다. 0.45~0.8 구간에서는 4건 모두 정답이라 F1 = 1.0이다. 동률이면 큰 값이므로 0.8을 고른다.
- **τ_s(τ_c=0.8):**
  - t ≤ 0.2: 지지됨 예측은 0.9 아홉, 0.6 셋, 0.2 둘이다(c=0.4는 τ_c 미만이라 반박 아님). 정밀도 9/14다.
  - t ≤ 0.6: 지지됨 예측은 0.9 아홉, 0.6 셋이다. 정밀도 9/12 = 0.75다.
  - t = 0.65: 지지됨 예측은 0.9 아홉뿐이다. 정밀도 1.0이므로 0.65를 고른다.

- [ ] **Step 4: 통과 확인** — Run: `/tmp/evt.sh tests/evidence/test_sys_rules.py -v` / Expected: 3 passed
- [ ] **Step 5: Commit** — `feat: SYS 판정 규칙과 조정 세트 임계값 선택`

### Task 3: 기준선 4종

**Files:**
- Create: `lab/evidence/baselines.py`
- Test: `tests/evidence/test_baselines.py`

**Interfaces:**
- Produces:
  - `char_bigrams(text: str) -> set[str]`
  - `lex_score(claim: str, passages: list[str]) -> float`. 숫자 확인을 통과한 문단에서 주장 글자 바이그램이 나타나는 비율을 계산하고, 문단별 최댓값을 쓴다.
  - `emb_score(claim_vec, passage_vecs) -> float`(코사인 최댓값)
  - `LLM_SYSTEM: str`, `LLM_OPTIONS: dict`, `llm_messages(claim, passages) -> list[dict]`, `parse_llm_score(text: str) -> float | None`(0~100 정수를 0~1로)
  - `NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"`, `nli_entailment(pairs: list[tuple[str, str]], revision: str) -> list[float]`(지연 import)

- [ ] **Step 1: 실패하는 테스트**

```python
# tests/evidence/test_baselines.py
from lab.evidence import baselines as bl


def test_lex_score_uses_number_check_and_bigrams():
    p_ok, p_bad = "삼성전자 DX 매출 비중 33.0% 기록", "삼성전자 DX 매출 비중 40% 기록"
    assert bl.lex_score("DX 매출 비중 33.0%", [p_bad]) == 0.0
    assert bl.lex_score("DX 매출 비중 33.0%", [p_bad, p_ok]) == 1.0
    assert 0 < bl.lex_score("반도체를 만든다", ["반도체 생산"]) < 1


def test_emb_score_and_llm_parse():
    assert abs(bl.emb_score([1, 0], [[0, 1], [1, 0]]) - 1.0) < 1e-9
    assert bl.parse_llm_score("점수: 85") == 0.85
    assert bl.parse_llm_score("150") is None and bl.parse_llm_score("모름") is None
    m = bl.llm_messages("주장", ["가", "나"])
    assert "[문단 2] 나" in m[1]["content"] and "[주장] 주장" in m[1]["content"]
```

- [ ] **Step 2: 실패 확인** — Run: `/tmp/evt.sh tests/evidence/test_baselines.py -v` / Expected: FAIL `ImportError`

- [ ] **Step 3: 구현**

```python
# lab/evidence/baselines.py
"""spec 5절 기준선. 어느 것도 JEV 출력으로 학습하지 않는다."""
from __future__ import annotations

import re

import numpy as np

from app.services.evidence.numbers import number_check

NLI_MODEL = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"
LLM_SYSTEM = ("당신은 근거 판정자입니다. [문단]들 중 하나가 [주장]의 모든 사실을 그대로 뒷받침하는 정도를 "
              "0~100 사이 정수 하나로만 답하세요. 뒷받침하지 않거나 어긋나면 0에 가깝게 답하세요.")
LLM_OPTIONS = {"temperature": 0, "seed": 20261002, "num_ctx": 8192, "num_predict": 8}
_WS = re.compile(r"\s+")


def char_bigrams(text: str) -> set[str]:
    """공백을 지운 글자 바이그램(한국어 조사 변화에 덜 민감한 어휘 단위)."""
    t = _WS.sub("", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def lex_score(claim: str, passages: list[str]) -> float:
    """B-lex: 숫자 확인을 통과한 문단에서 주장 바이그램 재현율의 최댓값."""
    cb = char_bigrams(claim)
    if not cb:
        return 0.0
    return max((len(cb & char_bigrams(p)) / len(cb) if number_check(claim, p) else 0.0) for p in passages)


def emb_score(claim_vec, passage_vecs) -> float:
    """B-emb: 코사인 유사도 최댓값."""
    q = np.asarray(claim_vec, dtype=float)
    m = np.asarray(passage_vecs, dtype=float)
    return float(np.max((m @ q) / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-12)))


def llm_messages(claim: str, passages: list[str]) -> list[dict]:
    """B-llm 프롬프트."""
    ctx = "\n".join(f"[문단 {i}] {t}" for i, t in enumerate(passages, 1))
    return [{"role": "system", "content": LLM_SYSTEM}, {"role": "user", "content": f"{ctx}\n\n[주장] {claim}\n점수:"}]


def parse_llm_score(text: str) -> float | None:
    """첫 정수(0~100)를 0~1로. 없거나 범위 밖이면 None."""
    m = re.search(r"\d+", text)
    if not m or int(m.group()) > 100:
        return None
    return int(m.group()) / 100


def nli_entailment(pairs: list[tuple[str, str]], revision: str) -> list[float]:
    """B-nli: (문단, 주장) 쌍의 entailment 확률. torch·transformers는 평가 환경에서만 import한다."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(NLI_MODEL, revision=revision)
    model = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL, revision=revision).eval()
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    model.to(dev)
    ent = [i for i, l in model.config.id2label.items() if l.lower().startswith("entail")][0]
    out: list[float] = []
    with torch.no_grad():
        for i in range(0, len(pairs), 16):
            batch = pairs[i:i + 16]
            enc = tok([p for p, _ in batch], [c for _, c in batch], truncation="only_first", max_length=512,
                      padding=True, return_tensors="pt").to(dev)
            out += torch.softmax(model(**enc).logits, dim=-1)[:, ent].tolist()
    return out
```

- [ ] **Step 4: 통과 확인** — Run: `/tmp/evt.sh tests/evidence/test_baselines.py -v` / Expected: 2 passed
- [ ] **Step 5: Commit** — `feat: 기준선 4종(어휘·임베딩·NLI·로컬 LLM)`

### Task 4: MDE와 Stage 1 집계 함수

**Files:**
- Modify: `lab/evidence/metrics.py`
- Test: `tests/evidence/test_stage1_metrics.py`

**Interfaces:**
- Produces:
  - `metrics.mde(rows: list[dict], n_clusters: int, stat, n: int = 2000, seed: int = SEED) -> float | None`. rows의 군집 가운데 n_clusters개를 복원추출하고 질문을 2단 재표집해, `2.8 × std(stat)`를 낸다.
  - `metrics.verdict(lo: float | None, hi: float | None, margin: float = 0.05) -> dict`. 반환은 `{"statistical": "superior"|"inferior"|"inconclusive", "practically_equivalent": bool}`이다.
  - `metrics.macro_f1(gold: list[str], pred: list[str], labels: tuple) -> float`

- [ ] **Step 1: 실패하는 테스트**

```python
# tests/evidence/test_stage1_metrics.py
from lab.evidence import metrics


def test_mde_shrinks_with_more_clusters():
    rows = [{"cluster": c, "qid": f"{c}-{q}", "d": (c % 3) * 0.1 + q * 0.01} for c in range(19) for q in range(6)]
    stat = lambda rs: sum(r["d"] for r in rs) / len(rs)
    small, big = metrics.mde(rows, 4, stat, n=400), metrics.mde(rows, 40, stat, n=400)
    assert small > big > 0


def test_verdict_branches():
    assert metrics.verdict(0.01, 0.03) == {"statistical": "superior", "practically_equivalent": True}
    assert metrics.verdict(-0.2, -0.1) == {"statistical": "inferior", "practically_equivalent": False}
    assert metrics.verdict(-0.02, 0.08) == {"statistical": "inconclusive", "practically_equivalent": False}
    assert metrics.verdict(None, None)["statistical"] == "inconclusive"


def test_macro_f1():
    assert metrics.macro_f1(["a", "b"], ["a", "b"], ("a", "b")) == 1.0
```

- [ ] **Step 2: 실패 확인** — Run: `/tmp/evt.sh tests/evidence/test_stage1_metrics.py -v` / Expected: FAIL `AttributeError`

- [ ] **Step 3: 구현** — `metrics.py`에 추가:

```python
def mde(rows: list[dict], n_clusters: int, stat, n: int = 2000, seed: int = SEED) -> float | None:
    """홀드아웃 군집 수만큼 군집을 복원추출하고 군집 안 질문을 재표집해 stat의 SE를 구하고, 2.8×SE를 돌려준다."""
    by_c: dict = {}
    for r in rows:
        by_c.setdefault(r["cluster"], {}).setdefault(r["qid"], []).append(r)
    keys = sorted(by_c)
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        sample: list[dict] = []
        for ci in rng.integers(0, len(keys), n_clusters):
            qs = by_c[keys[ci]]
            qk = sorted(qs)
            for qi in rng.integers(0, len(qk), len(qk)):
                sample.extend(qs[qk[qi]])
        v = stat(sample)
        if v is not None:
            vals.append(v)
    return None if len(vals) < 2 else float(2.8 * np.std(vals, ddof=1))


def verdict(lo: float | None, hi: float | None, margin: float = 0.05) -> dict:
    """통계 판정(우월·열등·판단 불가)과 실용적 동등(구간이 ±margin 안)을 따로 낸다."""
    if lo is None or hi is None:
        return {"statistical": "inconclusive", "practically_equivalent": False}
    stat = "superior" if lo > 0 else ("inferior" if hi < 0 else "inconclusive")
    return {"statistical": stat, "practically_equivalent": lo >= -margin and hi <= margin}


def macro_f1(gold: list[str], pred: list[str], labels: tuple) -> float:
    """다중 분류 macro-F1."""
    from sklearn.metrics import f1_score
    return float(f1_score(gold, pred, labels=list(labels), average="macro", zero_division=0))
```

- [ ] **Step 4: 통과 확인** — Run: `/tmp/evt.sh tests/evidence/test_stage1_metrics.py -v` / Expected: 3 passed
- [ ] **Step 5: Commit** — `feat: Stage 1 지표(MDE·판정 분기·macro-F1)`

### Task 5: 명령줄 — 2단 봉인, 기준선·주장 임베딩, 조정·동결, 1회 실행

**Files:**
- Modify: `lab/evidence/__main__.py`
- Test: `tests/evidence/test_stage1_cli.py`

**Interfaces:**
- Consumes: Task 1~4
- Produces:
  - **봉인 1단(데이터):** `holdout_data_open(P) -> bool`(prereg.json에 `stage1` 블록이 있으면 True). `companies(P, split, stage="judge")`가 `stage="data"`이면 이 조건으로 연다. 데이터 명령은 모두 `stage="data"`로 부른다: question-packets, questions-merge, embed, retrieve, generate, claims, controlled-packets, controlled-check, label-packets, labels-merge.
  - **봉인 2단(판정):** 기존 `holdout_frozen`. judge, baselines, claim-embed, stage1-report는 `stage="judge"`다.
  - **1회 실행:** `once(path: Path, split: str) -> None`. 홀드아웃이고 출력 파일이 이미 있으면 SystemExit를 낸다.
  - 명령:
    - `claim-embed --split S`: `priv/claim_vecs.jsonl`에 `{"cid","sha256","vec"}`를 쓴다. QUERY 접두어를 쓰고, 이미 있는 cid+sha는 건너뛴다.
    - `baselines --split S --tag lex|emb|nli|llm`: `priv/baselines/<tag>_<S>.jsonl`에 `{"cid","score","ok"}`를 쓴다.
    - `stage1-tune`: `lab/evidence/results/stage1-tune.json`에 τ_c, τ_s, 기준선별 조정 AUC, B\*, MDE, 홀드아웃 질문 수 결정, 확인 세트 운영점을 쓴다.
    - `stage1-freeze-config`: stage1-tune 결과를 prereg.json `stage1` 블록에 넣는다. 덮어쓰기는 거부한다.
    - `freeze-holdout`: `prereg_holdout.json`에 홀드아웃 행의 split·questions·retrieval·answers·claims·labels 해시와 prereg 해시, `git rev-parse HEAD`를 쓴다. 덮어쓰기는 거부한다.
    - `stage1-report`: `lab/evidence/results/stage1.json`과 `stage1-gates.md`를 쓴다.
  - `nli_revision()`: `huggingface_hub.HfApi().model_info(NLI_MODEL).sha`

- [ ] **Step 1: 실패하는 테스트**

```python
# tests/evidence/test_stage1_cli.py
import json

import pytest

from lab.evidence import __main__ as cli

COMPS = [{"corp_code": "a", "corp_name": "A", "split": "tune", "cluster": 0, "rcept_no": "1"},
         {"corp_code": "c", "corp_name": "C", "split": "holdout", "cluster": 2, "rcept_no": "3"}]


def _P(tmp_path):
    P = cli.Paths(tmp_path)
    P.split_json.parent.mkdir(parents=True, exist_ok=True)
    P.split_json.write_text(json.dumps({"companies": COMPS}))
    P.prereg.write_text(json.dumps({"version": 1}))
    return P


def test_two_stage_holdout_seal(tmp_path):
    P = _P(tmp_path)
    with pytest.raises(SystemExit):
        cli.companies(P, "holdout", stage="data")
    P.prereg.write_text(json.dumps({"version": 2, "stage1": {"tau_s": 0.7}}))
    assert [c["corp_code"] for c in cli.companies(P, "holdout", stage="data")] == ["c"]
    with pytest.raises(SystemExit, match="frozen"):
        cli.companies(P, "holdout")


def test_holdout_run_once(tmp_path):
    out = tmp_path / "x.jsonl"
    cli.once(out, "holdout")
    out.write_text("{}\n")
    with pytest.raises(SystemExit, match="already"):
        cli.once(out, "holdout")
    cli.once(out, "tune")


def test_freeze_config_refuses_overwrite(tmp_path):
    P = _P(tmp_path)
    (P.ev / "results").mkdir(parents=True)
    (P.ev / "results/stage1-tune.json").write_text(json.dumps({"tau_s": 0.7, "tau_c": 0.6, "b_star": "nli"}))
    cli.cmd_stage1_freeze_config(P, None)
    assert json.loads(P.prereg.read_text())["stage1"]["b_star"] == "nli"
    with pytest.raises(SystemExit, match="already"):
        cli.cmd_stage1_freeze_config(P, None)
```

- [ ] **Step 2: 실패 확인** — Run: `/tmp/evt.sh tests/evidence/test_stage1_cli.py -v` / Expected: FAIL(`companies()` got unexpected keyword `stage`)

- [ ] **Step 3: 구현**

1. `companies`를 바꾼다.

```python
def holdout_data_open(P: Paths) -> bool:
    """사전등록에 stage1 블록(임계값·B*·MDE·프롬프트)이 있어야 홀드아웃 데이터를 만들 수 있다."""
    return P.prereg.exists() and bool(json.loads(P.prereg.read_text()).get("stage1"))


def companies(P: Paths, split_name: str, stage: str = "judge") -> list[dict]:
    """분할의 회사 목록. 홀드아웃은 데이터 생성(stage1 사전등록 뒤)과 판정(동결 뒤) 두 단계로 연다."""
    names = {"dev": {"tune", "check"}, "all": {"tune", "check", "holdout"}}.get(split_name, {split_name})
    if "holdout" in names:
        if stage == "data" and not holdout_data_open(P):
            raise SystemExit("holdout data is sealed until prereg.json has a stage1 block")
        if stage != "data" and not holdout_frozen(P):
            raise SystemExit("holdout is frozen until prereg_holdout.json")
    return [c for c in load_split(P)["companies"] if c["split"] in names]


def once(path: Path, split_name: str) -> None:
    """홀드아웃 산출물은 한 번만 만든다."""
    if split_name == "holdout" and Path(path).exists():
        raise SystemExit(f"holdout output already exists: {path}")
```

2. 데이터 명령의 `companies(P, args.split)` 호출을 모두 `companies(P, args.split, stage="data")`로 바꾼다. 대상은 question-packets, questions-merge, embed, retrieve, generate, claims, controlled-packets, controlled-check, label-packets, labels-merge다.
3. `cmd_judge`의 시작부(태그 검사 직후)에 `once(P.priv / "scores" / f"{args.tag}.jsonl", args.split)`를 넣는다.
4. 새 명령을 추가한다.

```python
def _claims_for(P: Paths, split_name: str, stage: str = "judge") -> list[dict]:
    codes = {c["corp_code"] for c in companies(P, split_name, stage=stage)}
    return [c for c in read_jsonl(P.jsonl("claims.jsonl")) if c["qid"].split("-q")[0] in codes]


def cmd_claim_embed(P: Paths, args) -> None:
    """주장을 QUERY 접두어로 임베딩한다(cid+본문 해시가 같으면 건너뜀). 호스트 Ollama."""
    import asyncio

    from app.services.evidence.retrieve import QUERY_PREFIX

    out = P.priv / "claim_vecs.jsonl"
    done = {(r["cid"], r["sha256"]) for r in read_jsonl(out)}
    todo = [c for c in _claims_for(P, args.split) if (c["cid"], _sha(c["text"])) not in done]
    llm = _ollama()

    async def run():
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a") as f:
            for c in todo:
                f.write(json.dumps({"cid": c["cid"], "sha256": _sha(c["text"]),
                                    "vec": await llm.embed(EMBED_MODEL, QUERY_PREFIX + c["text"])}) + "\n")

    asyncio.run(run())
    print(f"{len(todo)} claim vectors")


def nli_revision() -> str:
    from huggingface_hub import HfApi

    from lab.evidence.baselines import NLI_MODEL
    return HfApi().model_info(NLI_MODEL).sha


def cmd_baselines(P: Paths, args) -> None:
    """기준선 하나를 분할 주장에 실행한다. 홀드아웃은 한 번만."""
    import asyncio

    from lab.evidence import baselines as bl

    out = P.priv / "baselines" / f"{args.tag}_{args.split}.jsonl"
    once(out, args.split)
    lab = {r["cid"]: r["label"] for r in read_jsonl(P.jsonl("labels.jsonl"))}
    claims = [c for c in _claims_for(P, args.split) if lab.get(c["cid"]) not in (None, "disputed", "non_claim")]
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    rows: list[dict] = []
    if args.tag == "lex":
        rows = [{"cid": c["cid"], "score": bl.lex_score(c["text"], [text[i] for i in ret[c["qid"]]]), "ok": True} for c in claims]
    elif args.tag == "emb":
        pv = load_vectors(P)
        cv = {r["cid"]: r["vec"] for r in read_jsonl(P.priv / "claim_vecs.jsonl")}
        rows = [{"cid": c["cid"], "score": bl.emb_score(cv[c["cid"]], [pv[i] for i in ret[c["qid"]]]), "ok": True} for c in claims]
    elif args.tag == "nli":
        rev = nli_revision()
        pairs = [(text[i], c["text"]) for c in claims for i in ret[c["qid"]]]
        probs = bl.nli_entailment(pairs, rev)
        rows = [{"cid": c["cid"], "score": max(probs[k * K:(k + 1) * K]), "ok": True, "revision": rev}
                for k, c in enumerate(claims)]
    elif args.tag == "llm":
        llm = _ollama()

        async def run():
            res = []
            for c in claims:
                txt = await llm.chat(GEN_MODEL, bl.llm_messages(c["text"], [text[i] for i in ret[c["qid"]]]), bl.LLM_OPTIONS)
                sc = bl.parse_llm_score(txt)
                res.append({"cid": c["cid"], "score": sc if sc is not None else 0.0, "ok": sc is not None})
            return res

        rows = asyncio.run(run())
    else:
        raise SystemExit("--tag must be lex|emb|nli|llm")
    write_jsonl(out, rows)
    log_attempt(P, "baselines", tag=args.tag, split=args.split, claims=len(rows), failed=sum(not r["ok"] for r in rows))
    print(f"{len(rows)} {args.tag} scores")
```

`stage1-tune`, `stage1-freeze-config`, `freeze-holdout`, `stage1-report`의 구현은 Task 6에서 한다. 이 Task에서는 `cmd_stage1_freeze_config`만 테스트에 맞춰 넣는다.

```python
def cmd_stage1_freeze_config(P: Paths, args) -> None:
    """stage1-tune 결과를 사전등록 stage1 블록으로 동결한다(홀드아웃 데이터 생성이 열린다). 덮어쓰기 금지."""
    pre = json.loads(P.prereg.read_text())
    if pre.get("stage1"):
        raise SystemExit("stage1 block already frozen")
    pre["stage1"] = json.loads((P.ev / "results/stage1-tune.json").read_text())
    pre["version"] = max(2, pre.get("version", 1))
    P.prereg.write_text(json.dumps(pre, ensure_ascii=False, indent=2) + "\n")
    log_attempt(P, "stage1-freeze-config")
    print("stage1 frozen")


COMMANDS.update({"claim-embed": cmd_claim_embed, "baselines": cmd_baselines,
                 "stage1-freeze-config": cmd_stage1_freeze_config})
```

`main()`의 `--tag`는 그대로 쓴다(기준선 이름을 겸한다).

- [ ] **Step 4: 통과 확인** — Run: `/tmp/evt.sh tests/evidence -v` / Expected: 전부 통과(기존 테스트 포함)
- [ ] **Step 5: Commit** — `feat: 홀드아웃 2단 봉인·1회 실행·기준선·주장 임베딩 명령`

### Task 6: 명령줄 — stage1-tune, freeze-holdout, stage1-report

**Files:**
- Modify: `lab/evidence/__main__.py`
- Test: `tests/evidence/test_stage1_report.py`

**Interfaces:**
- Produces:
  - `score_rows(P, split, tau_c) -> list[dict]`
    - 판정 가능한 자연 주장마다 `{"cid","qid","cluster","label","y","sys","jev","lex","emb","nli","llm","has_number","decision"}`를 만든다.
    - SYS는 `sys_decision`과 문단별 `number_check`로 계산한다. 판정 점수는 `scores/<split>.jsonl`(조정은 tune, 확인은 check, 홀드아웃은 holdout)에서 읽는다.
    - 기준선은 `baselines/<tag>_<split>.jsonl`에서 읽고, 없는 기준선은 None으로 둔다.
  - `delta_stat(b_star) -> callable`: rows로 `auc(sys) − auc(b_star)`를 계산한다.
  - `cmd_stage1_tune`, `cmd_freeze_holdout`, `cmd_stage1_report`

- [ ] **Step 1: 실패하는 테스트**

```python
# tests/evidence/test_stage1_report.py
from lab.evidence import __main__ as cli


def test_delta_stat_pairs_same_rows():
    rows = [{"y": 1, "sys": 0.9, "nli": 0.6}, {"y": 0, "sys": 0.1, "nli": 0.7},
            {"y": 1, "sys": 0.8, "nli": 0.9}, {"y": 0, "sys": 0.2, "nli": 0.1}]
    assert cli.delta_stat("nli")(rows) == 1.0 - 0.75
    assert cli.delta_stat("nli")([r for r in rows if r["y"] == 1]) is None
```

- [ ] **Step 2: 실패 확인** — Run: `/tmp/evt.sh tests/evidence/test_stage1_report.py -v` / Expected: FAIL `AttributeError`

- [ ] **Step 3: 구현**

```python
BASELINES = ("lex", "emb", "nli", "llm")


def delta_stat(b_star: str):
    """같은 표본에서 AUC(SYS) − AUC(B*). 한 종류뿐이면 None."""
    from lab.evidence.metrics import auc

    def stat(rows):
        a = auc([r["y"] for r in rows], [r["sys"] for r in rows])
        b = auc([r["y"] for r in rows], [r[b_star] for r in rows])
        return None if a is None or b is None else a - b
    return stat


def score_rows(P: Paths, split_name: str, tau_s: float, tau_c: float, natural_only: bool = True) -> list[dict]:
    """판정 가능한 주장마다 SYS·JEV·기준선 점수와 라벨을 모은다."""
    from app.services.evidence.judge import Judgement, sys_decision
    from app.services.evidence.numbers import number_check, parse

    cluster = {c["corp_code"]: c["cluster"] for c in load_split(P)["companies"]}
    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    jev = _scores(P, split_name)
    base = {t: {r["cid"]: r["score"] for r in read_jsonl(P.priv / "baselines" / f"{t}_{split_name}.jsonl")} for t in BASELINES}
    rows = []
    for c in _judgeable(P, split_name):
        if (natural_only and c["source"] != "natural") or c["cid"] not in jev:
            continue
        r = jev[c["cid"]]
        ps = [text[i] for i in ret[c["qid"]]]
        valid = [number_check(c["text"], p) for p in ps]
        dec, idx, sys_score = sys_decision(Judgement(r["s"], r["c"], r["ok"], 0), valid, tau_s, tau_c)
        rows.append({"cid": c["cid"], "qid": c["qid"], "cluster": cluster[c["qid"].split("-q")[0]],
                     "label": c["label"], "y": int(c["label"] == "supported"), "sys": sys_score,
                     "jev": max(r["s"]) if r["ok"] else 0.0, "has_number": bool(parse(c["text"])),
                     "decision": dec, "source": c["source"], "variant": c.get("variant"),
                     "expected": c.get("expected"), **{t: base[t].get(c["cid"]) for t in BASELINES}})
    return rows


def _judge_rows_for_tuning(P: Paths, split_name: str) -> list[dict]:
    from app.services.evidence.numbers import number_check

    text = _passage_text(P)
    ret = {r["qid"]: r["passage_ids"] for r in read_jsonl(P.jsonl("retrieval.jsonl"))}
    jev = _scores(P, split_name)
    return [{"s": jev[c["cid"]]["s"], "c": jev[c["cid"]]["c"], "label": c["label"],
             "valid": [number_check(c["text"], text[i]) for i in ret[c["qid"]]]}
            for c in _judgeable(P, split_name) if c["source"] == "natural" and c["cid"] in jev and jev[c["cid"]]["ok"]]


def _op_point(rows: list[dict]) -> dict:
    """지지됨 정밀도·재현율·커버리지와 반박 오탐률(정답이 반박 아님인데 반박 판정)."""
    sup = [r for r in rows if r["decision"] == "supported"]
    tp = sum(r["y"] for r in sup)
    pos = sum(r["y"] for r in rows)
    neg_c = [r for r in rows if r["label"] != "contradicted"]
    return {"supported_precision": tp / len(sup) if sup else None, "supported_recall": tp / pos if pos else None,
            "coverage": len(sup) / len(rows) if rows else None,
            "contradiction_false_positive_rate": (sum(r["decision"] == "contradicted" for r in neg_c) / len(neg_c)) if neg_c else None}


def cmd_stage1_tune(P: Paths, args) -> None:
    """τ_c→τ_s(조정 자연), B*(조정 자연 AUC), MDE(개발 자연, 홀드아웃 군집 수), 확인 세트 운영점."""
    from lab.evidence import thresholds as th
    from lab.evidence.metrics import auc, mde

    tune = _judge_rows_for_tuning(P, "tune")
    tau_c = th.choose_tau_c(tune)
    tau_s = th.choose_tau_s(tune, tau_c)
    t_rows = score_rows(P, "tune", tau_s, tau_c)
    base_auc = {t: auc([r["y"] for r in t_rows], [r[t] for r in t_rows]) for t in BASELINES
                if all(r[t] is not None for r in t_rows)}
    b_star = max(base_auc, key=base_auc.get)
    dev = t_rows + score_rows(P, "check", tau_s, tau_c)
    n_hold = len({c["cluster"] for c in load_split(P)["companies"] if c["split"] == "holdout"})
    m = mde(dev, n_hold, delta_stat(b_star))
    c_rows = score_rows(P, "check", tau_s, tau_c)
    result = {"tau_c": tau_c, "tau_s": tau_s, "baseline_auc_tune": base_auc, "b_star": b_star,
              "sys_auc_tune": auc([r["y"] for r in t_rows], [r["sys"] for r in t_rows]),
              "mde": m, "holdout_clusters": n_hold,
              "holdout_questions_per_company": 6 if (m is not None and m <= 0.10) else 10,
              "exploratory": False, "check_operating_point": _op_point(c_rows),
              "check_sys_auc": auc([r["y"] for r in c_rows], [r["sys"] for r in c_rows]),
              "nli_revision": next((r.get("revision") for r in read_jsonl(P.priv / "baselines/nli_tune.jsonl")), None),
              "llm_prompt": __import__("lab.evidence.baselines", fromlist=["LLM_SYSTEM"]).LLM_SYSTEM}
    out = P.ev / "results/stage1-tune.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    log_attempt(P, "stage1-tune", tau_c=tau_c, tau_s=tau_s, b_star=b_star, mde=m)
    print(json.dumps({k: result[k] for k in ("tau_c", "tau_s", "b_star", "mde", "holdout_questions_per_company")}))


def _sha_rows(rows: list[dict]) -> str:
    return hashlib.sha256("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows).encode()).hexdigest()


def cmd_freeze_holdout(P: Paths, args) -> None:
    """홀드아웃 데이터 해시를 동결한다(판정 봉인 해제). 덮어쓰기 금지."""
    import subprocess

    if P.prereg_holdout.exists():
        raise SystemExit("prereg_holdout.json already exists")
    codes = {c["corp_code"] for c in companies(P, "holdout", stage="data")}
    pick = lambda name, key: [r for r in read_jsonl(P.jsonl(name)) if r[key].split("-q")[0] in codes]
    labels = pick("labels.jsonl", "cid")
    if not labels:
        raise SystemExit("no holdout labels")
    data = {"split_sha256": hashlib.sha256(P.split_json.read_bytes()).hexdigest(),
            "questions_sha256": _sha_rows(pick("questions.jsonl", "qid")),
            "retrieval_sha256": _sha_rows(pick("retrieval.jsonl", "qid")),
            "answers_sha256": _sha_rows(pick("answers.jsonl", "qid")),
            "claims_sha256": _sha_rows(pick("claims.jsonl", "cid")),
            "labels_sha256": _sha_rows(labels),
            "prereg_sha256": hashlib.sha256(P.prereg.read_bytes()).hexdigest(),
            "code_commit": subprocess.run(["git", "-C", str(P.root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
            "frozen_at": datetime.now(timezone.utc).isoformat()}
    P.prereg_holdout.write_text(json.dumps(data, indent=1) + "\n")
    log_attempt(P, "freeze-holdout", claims=len(pick("claims.jsonl", "cid")))
    print("holdout frozen")


def cmd_stage1_report(P: Paths, args) -> None:
    """홀드아웃 주결과와 보조 지표를 계산한다(동결 뒤에만)."""
    from lab.evidence.metrics import auc, cluster_bootstrap, macro_f1, verdict

    cfg = json.loads(P.prereg.read_text())["stage1"]
    companies(P, "holdout")  # 동결 확인
    rows = score_rows(P, "holdout", cfg["tau_s"], cfg["tau_c"])
    b = cfg["b_star"]
    pos, neg = sum(r["y"] for r in rows), sum(1 - r["y"] for r in rows)
    point, lo, hi = cluster_bootstrap(rows, delta_stat(b))
    a = lambda key, rs=rows: auc([r["y"] for r in rs], [r[key] for r in rs])
    by_cluster = {}
    for r in rows:
        by_cluster.setdefault(r["cluster"], []).append(r)
    signs = [delta_stat(b)(rs) for rs in by_cluster.values()]
    nonum = [r for r in rows if not r["has_number"]]
    pred3 = [{"supported": "supported", "contradicted": "contradicted"}.get(r["decision"], "no_evidence") for r in rows]
    ctrl = score_rows(P, "holdout", cfg["tau_s"], cfg["tau_c"], natural_only=False)
    ctrl = [r for r in ctrl if r["source"] == "controlled"]
    by_var = {}
    for r in ctrl:
        by_var.setdefault(r["variant"], []).append((r["decision"] == "supported") == (r["expected"] == "supported"))
    res = {"n": len(rows), "supported": pos, "not_supported": neg, "descriptive_only": pos < 60 or neg < 60,
           "b_star": b, "auc": {k: a(k) for k in ("sys", "jev", *BASELINES) if all(r[k] is not None for r in rows)},
           "delta": {"point": point, "lo": lo, "hi": hi, **verdict(lo, hi)}, "mde": cfg.get("mde"),
           "exploratory": cfg.get("exploratory", False),
           "jev_minus_bstar": cluster_bootstrap(rows, lambda rs: (lambda x, y: None if x is None or y is None else x - y)(
               auc([r["y"] for r in rs], [r["jev"] for r in rs]), auc([r["y"] for r in rs], [r[b] for r in rs]))),
           "operating_point": _op_point(rows),
           "numberless_auc": {"sys": a("sys", nonum), b: a(b, nonum), "n": len(nonum)},
           "macro_f1_3class": macro_f1([r["label"] if r["label"] in ("supported", "contradicted") else "no_evidence" for r in rows],
                                       pred3, ("supported", "contradicted", "no_evidence")),
           "cluster_delta_signs": {"positive": sum(s is not None and s > 0 for s in signs),
                                   "negative": sum(s is not None and s < 0 for s in signs), "undefined": sum(s is None for s in signs)},
           "controlled_accuracy_by_variant": {k: sum(v) / len(v) for k, v in by_var.items()}}
    out = P.ev / "results/stage1.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str) + "\n")
    (P.ev / "results/stage1-gates.md").write_text(
        f"# Stage 1 주결과\n\nΔ = AUC(SYS) − AUC({b}) = {point} (95% {lo} ~ {hi}) → {res['delta']}\n\n"
        f"n={len(rows)} (지지됨 {pos}), descriptive_only={res['descriptive_only']}\n")
    log_attempt(P, "stage1-report", delta=point, lo=lo, hi=hi)
    print(json.dumps(res["delta"]))


COMMANDS.update({"stage1-tune": cmd_stage1_tune, "freeze-holdout": cmd_freeze_holdout,
                 "stage1-report": cmd_stage1_report})
```

`_scores(P, split_name)`는 Stage 0의 `_scores(P, tag)`를 그대로 쓴다. 판정 태그는 분할 이름과 같다(`tune`, `check`, `holdout`).

- [ ] **Step 4: 통과 확인** — Run: `/tmp/evt.sh tests/evidence -v` / Expected: 전부 통과
- [ ] **Step 5: Commit** — `feat: Stage 1 조정·동결·주결과 리포트 명령`

### Task 7: 개발 세트 기준선과 조정 (실행)

1. 호스트 래퍼(`OLLAMA_BASE_URL=http://127.0.0.1:11434 /tmp/ev.sh`)로 주장을 임베딩한다: `claim-embed --split tune`, `claim-embed --split check`.
2. `baselines --split tune|check --tag lex|emb|llm`을 실행한다.
3. NLI는 전용 래퍼로 실행한다. `/tmp/evnli.sh`는 ev.sh와 같고 `--with torch --with transformers --with sentencepiece --with protobuf --with huggingface_hub`를 더한다: `baselines --split tune|check --tag nli`
4. `stage1-tune`을 실행한 뒤 결과를 검토하고 원장에 기록한다.
5. MDE > 0.10이면 홀드아웃 질문을 기업당 10개로 한다. 질문 지시문의 개수를 바꾸고, `merge_questions`가 6개 범주 외 추가 질문을 받도록 바꾸는 것은 그 경우에만 Ruling으로 처리한다.
6. `stage1-freeze-config`를 실행하고 커밋한다.

### Task 8: 홀드아웃 데이터 생성과 동결 (실행)

Stage 0 Task 14·15와 같은 순서로 진행한다. `--split holdout`으로 실행하고, 임베딩·생성은 호스트에서 한다.
1. 질문(서브에이전트)
2. 문단 임베딩(호스트 `embed --split holdout`)
3. 검색, 생성, 주장
4. 통제 주장(서브에이전트)
5. Opus·Codex 1차 라벨, 조정, 병합
6. 숫자 주장 감사 표본 50건
7. `freeze-holdout` 실행 후 커밋

### Task 9: 홀드아웃 1회 평가와 리포트 (실행)

1. `judge --split holdout --tag holdout`
2. `claim-embed --split holdout`
3. `baselines --split holdout --tag lex|emb|nli|llm`(각 1회)
4. `stage1-report`
5. `docs/lab/evidence-stage1-report.md`를 작성한다. 사람이 읽는 리포트이고, 결과가 부정이어도 같은 형식으로 쓴다.
6. 브랜치 전체 리뷰를 받은 뒤 PR을 만든다.
