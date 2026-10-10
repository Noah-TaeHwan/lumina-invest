# 근거 판정 A-2 평가(P5) 실행 계획

> 작성: AI(Claude Code)가 작성한 계획 초안이다. 결정권자는 노아다. 코드·사전등록·단위 테스트는 클라우드에서 만들었고, 아래 실행 단계는 전부 **로컬**에서 한다(DART·Ollama·판정 모델·AI 라벨러, `lab/data/`).

**Goal:** 새 무작위 40개사와 서비스 생성기(`llama3.1:8b`) 답변으로 ✅ 임계값 τ_s와 1차 필터 구간 θ_low·θ_high를 조정 세트에서 고르고, 확인 세트에서 **한 번** 재서 정책 `a2-v1`을 확정한다. 결과가 부정이어도 리포트를 공개한다.

**Spec:** `docs/superpowers/specs/2026-10-02-evidence-chat-a2-design.md` 6절(가설·데이터·선택 규칙·사전등록·생성기 변경 절차), 7.1절(생성 지연), 11절 P5. 사전등록은 `lab/evidence/prereg_a2.json`이다. 이 계획과 사전등록이 다르면 사전등록이 이긴다.

**Architecture:**
- A-1 명령줄(`python -m lab.evidence`)을 그대로 쓰고 `--study a2`로 경로만 나눈다. A-1 파일·결과는 건드리지 않는다.

| 항목 | A-1 | A-2(`--study a2`) |
|---|---|---|
| 커밋 데이터 | `lab/evidence/data/` | `lab/evidence/study_a2/` |
| 비공개 데이터(gitignore) | `lab/data/evidence/` | `lab/data/evidence_a2/`(문서·문단·벡터·점수·판정 모델 호출 기록) |
| 분할 | `split.json` | `split_a2.json` |
| 사전등록 | `prereg.json` | `prereg_a2.json` |
| 동결 | `prereg_holdout.json` | `prereg_a2_check.json` |
| 봉인 분할 | holdout | check(확인 세트 = A-2의 홀드아웃) |
| 조정 결과 | `results/stage1-tune.json` | `results/a2-tune.json` |
| 확인 결과·리포트 | `results/stage1.json`, `docs/lab/evidence-stage1-report.md` | `results/a2-check.json`, `docs/lab/evidence-a2-report.md` |
| 원장 | `attempts.jsonl` | 같은 파일에 `"study": "a2"`로 덧붙임 |
| 입력 토큰 상한 | 20,000,000 | 6,000,000(A-2 호출 기록 파일 기준) |

- 확인 세트는 두 단계로 봉인한다. 데이터(질문~라벨)는 `results/a2-tune.json`이 생긴 뒤에만, 판정은 `prereg_a2_check.json`이 생기고 동결 해시가 그대로일 때 `--tag check`로 한 번만.
- 1차 필터는 제품 실행기와 **같은 함수**(`app/services/evidence/lexical.py`의 `lex_features`·`tier_route`, 회사명 조건 `names_in_passage`)로 경로를 정한다.
- A-2 전용 명령은 `a2-tune`(τ_s·θ 선택)과 `a2-report`(관문·정책·리포트)다. A-1 전용 명령(`stage0-report`, `stage1-*`, `baselines`, `claim-embed`)은 `--study a2`에서 거부된다.

## 로컬에서 확정한 값(사전등록에 적힘)

| 항목 | 값 |
|---|---|
| 서비스 생성기 | Ollama `llama3.1:8b`, digest `46e0c10c039e`(2026-10-03 pull), 옵션 `generate.OPTIONS` 그대로 |
| 임베딩 | `nomic-embed-text` `0a109f422b47` |
| 생성 지연(스모크) | 8B 답변 1개 약 10~20초(첫 호출 적재 포함 22.6초), 1B 4.8초 |
| 8B 생성 특성 | 문단을 그대로 옮기지 않고 요약·의역한다 → A-1에서 어휘 겹침이 유리했던 조건이 바뀌었다(사전등록 `motivation`) |
| 비주장 규칙 | 정책 `a2-provisional-2`(목록 머리말 규칙 포함). 로컬 확인: AI 라벨 주장 457개 중 0개 오제외, 비주장 178개 중 62개 거름 |

## 실행 환경(로컬)

```bash
REPO=/Users/noah/portfolios/lumina-invest
cat > /tmp/ev2.sh <<EOF
#!/bin/bash
cd $REPO
exec uv run -q --no-project --python 3.12 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 \
  --with scipy==1.18.1 --with pydantic-settings python -m lab.evidence --study a2 "\$@"
EOF
chmod +x /tmp/ev2.sh
# 임베딩·검색·생성은 호스트 Ollama(Metal). A-1 Stage 1과 같은 방식
alias ev2o='OLLAMA_BASE_URL=http://127.0.0.1:11434 /tmp/ev2.sh'
```

- 테스트: CLAUDE.md의 근거 판정 단위 테스트 명령(외부 호출 없음).
- 사전 확인: `ollama list`에 `llama3.1:8b`가 있고 digest 앞 12자리가 `46e0c10c039e`인지 본다. 다르면 `generate`가 거부한다(중지 조건 2).

## 단계와 소요

| # | 단계 | 어디서 | 소요(추정) | 커밋 |
|---|---|---|---|---|
| 0 | 사전등록·코드·테스트(이 PR) | 클라우드 | — | `prereg_a2.json`, `lab/evidence/a2.py` 등 |
| 1 | 추첨·분할 | 로컬(OpenDART) | 15~30분 | `split_a2.json`, `study_a2/draw_ledger.jsonl`, `mention_edges.jsonl` |
| 2 | 조정 세트 데이터: 문단·임베딩·질문·검색·생성·주장·통제 | 로컬(DART 원문·Ollama·Opus) | 약 1~1.5시간(8B 생성 120문항 약 30분) | `study_a2/*` |
| 3 | 조정 세트 라벨(Opus·Codex 1·2차) | 로컬 | 약 1~1.5시간 | `study_a2/labels*` |
| 4 | 조정 세트 판정·선택 | 로컬(판정 모델) | 판정 5~10분, 선택 1분 | `results/a2-tune.json` |
| 5 | 확인 세트 데이터·라벨 → 동결 | 로컬 | 약 2~3시간(8B 생성 120문항 약 30분) | `study_a2/*`, `prereg_a2_check.json` |
| 6 | 확인 세트 1회 판정·리포트 | 로컬(판정 모델) | 판정 5~10분, 리포트 2분 | `results/a2-check.json`, `docs/lab/evidence-a2-report.md` |
| 7 | 정책 `a2-v1` 반영 | 클라우드 가능(P6) | — | 별도 PR |

8B 생성은 질문 240개(조정 120 + 확인 120) × 10~20초 ≈ **약 1시간**이다. `generate`는 답변 하나마다 파일을 다시 쓰므로 중간에 멈춰도 다시 실행하면 남은 질문만 생성한다(이어서 시작한 첫 답변은 `cold`로 표시된다).

입력 토큰 예상: 조정 자연 주장 약 350~600개 + 확인 자연 약 350~600개 + 확인 통제 약 240개 ≈ 940~1,440회 × 약 4,600 ≈ 430만~660만. 상한 6,000,000을 넘을 수 있으므로 예산 규칙(아래 4·6단계)이 호출 전에 막는다.

---

### 1단계: 추첨·분할

```bash
/tmp/ev2.sh split
```

- 입력: OpenDART 키, A-1 비공개 문단 `lab/data/evidence/passages.jsonl`(A-1 40개사 교차 언급 계산용, 없으면 거부).
- 동작: `random.Random(20261103)` 순서로 상장사를 보며 A-1 40개사(`a1_company`)·시드 그룹 접두어·금융업·사업보고서 없음·지정 절 3,000자 미만·A-1과 교차 언급 10회 이상(`a1_mention:<A-1 코드>`)을 빼고 40개사를 채운다. 교차 언급 군집 단위로 확인 ≤ 20, 나머지 조정.
- 예상 출력: `{"tune": 20, "check": 20}` 근처(군집 크기에 따라 확인 15~20). 원장에 `split`(제외 사유별 개수)이 남는다.
- **중지:** 40개사를 못 채우거나 확인 < 15·조정 = 0이면 거부된다. 규칙을 바꾸려면 원장에 이유를 남기고 사전등록 개정(새 버전) 뒤에 다시 한다. 판정 모델 호출 뒤에는 `split`이 거부된다.

```bash
git add lab/evidence/split_a2.json lab/evidence/study_a2/draw_ledger.jsonl lab/evidence/study_a2/mention_edges.jsonl \
  lab/evidence/study_a2/dart_ledger.jsonl lab/evidence/attempts.jsonl
git commit -m "chore: A-2 추첨·분할(시드 20261103, A-1 제외)"
```

### 2단계: 조정 세트 데이터

```bash
/tmp/ev2.sh passages                         # 40개사 문단(비공개) + study_a2/passages_manifest.jsonl
ev2o embed --split tune
/tmp/ev2.sh question-packets --split tune    # lab/data/evidence_a2/packets/questions/<corp>.md
```

질문: 조정 회사마다 Opus 서브에이전트 1개(동시 최대 10). 지시문은 A-1과 같다.

> 파일 `$REPO/lab/data/evidence_a2/packets/questions/<corp>.md`를 끝까지 읽고 그 안의 지시를 따른다. 결과는 그 파일에 적힌 출력 파일 경로에만 쓴다. 다른 파일을 읽거나 수정하지 않는다. 끝나면 쓴 줄 수만 보고한다.

```bash
/tmp/ev2.sh questions-merge --split tune     # 예상: "120 questions"
ev2o retrieve --split tune                   # 예상: "120 retrievals"
ev2o generate --split tune                   # 예상: "120 answers", 약 30분
/tmp/ev2.sh claims --split tune              # 예상: "N natural claims"(규칙 표시 포함), 원장 claims(문장·규칙·상한 수)
/tmp/ev2.sh controlled-packets --split tune  # Opus 서브에이전트(경로만 packets/controlled/)
/tmp/ev2.sh controlled-check --split tune    # 예상: accepted ≥ 질문 수의 90%
```

- `generate` 행에는 `latency_ms`, `cold`가 남는다. 첫 몇 개의 지연이 스모크(10~20초)보다 크게 다르면 Ollama 설정(Metal 사용)을 확인한다.
- **중지:** `generate`가 digest·프롬프트 해시(`generator.prompt_sha`) 불일치로 거부되면 생성하지 않는다(6.5 절차: 새 사전등록). `split`·`generate`·`claims`·`judge`·`a2-tune`·`a2-report`는 사전등록 코드 해시(`code_sha256`: 1차 필터·비주장·숫자·SYS·생성·추첨·선택 코드)가 다르면 외부 호출 전에 거부된다.

```bash
git add lab/evidence/study_a2 lab/evidence/attempts.jsonl
git commit -m "chore: A-2 조정 세트 질문·검색·8B 답변·주장·통제 주장"
```

### 3단계: 조정 세트 AI 참조 라벨

A-1 Stage 0 Task 15와 같다. 경로만 A-2다.

```bash
/tmp/ev2.sh label-packets --labeler opus --round 1 --split tune     # Opus 서브에이전트
/tmp/ev2.sh label-packets --labeler codex --round 1 --split tune
for f in lab/data/evidence_a2/packets/labels/r1_codex/*.md; do
  corp=$(basename "$f" .md)
  codex exec -s workspace-write -C $REPO \
    "파일 $f 를 끝까지 읽고 그 안의 지시를 따르라. 결과는 lab/evidence/study_a2/labels/r1_codex/$corp.jsonl 에만 써라. 다른 파일을 수정하지 마라." >/dev/null
done
/tmp/ev2.sh label-packets --labeler opus --round 2 --split tune     # 불일치만
/tmp/ev2.sh label-packets --labeler codex --round 2 --split tune
/tmp/ev2.sh labels-merge --split tune        # 예상: "N labels, kappa r1 x.xxx"
```

- 라벨러에게는 주장 출처·변형 유형·판정 모델 출력·어휘 점수를 보이지 않는다(불투명 ID).
- 비주장 규칙에 걸린 문장도 라벨링한다(규칙 오분류율 보고용).
- **중지:** 1차 이진 κ < 0.6이면 `a2-tune`이 멈춘다. 라벨 절차를 고치고 원장에 남긴다(확인 세트는 열지 않는다).

```bash
git add lab/evidence/study_a2/labels lab/evidence/study_a2/labels.jsonl lab/evidence/attempts.jsonl
git commit -m "chore: A-2 조정 세트 AI 참조 라벨"
```

### 4단계: 조정 세트 판정과 τ_s·θ 선택

```bash
/tmp/ev2.sh judge --split tune --tag tune    # 예상: "N judged, used tokens …"
/tmp/ev2.sh a2-tune                          # 예상: {"tau_s": …, "tau_c": 0.35, "theta_low": …, "theta_high": …}
```

- `judge`(조정)는 **자연 주장만** 부른다(선택 규칙이 자연 주장만 쓴다). 호출 전에 `사용량 + 캐시에 없는 주장 × 5,070 + 확인 세트 몫(같은 수로 예약)`이 6,000,000을 넘으면 거부한다.
- `a2-tune`은 확인 세트 질문이 하나라도 생기면 거부한다. 선택 규칙(사전등록 `selection`):
  - τ_c 0.35 고정. τ_s 후보 0.55~0.95 중 ✅ 예측 30개 이상·정밀도 ≥ 0.93인 가장 작은 값, 없으면 0.85.
  - θ_low: 하단(lex < θ) 지지됨 비율 ≤ 0.03인 가장 큰 값(빈 구간 제외).
  - θ_high: 상단(lex ≥ θ, 숫자 확인·회사명 조건 통과) 30개 이상·정밀도 ≥ 0.97인 가장 작은 값.
  - θ는 라벨과 어휘 점수만 읽는다(MCA 2.3(b)).
- 결과 파일에 후보별 표, 경계 비율(τ_s ± 0.05), 운영점, 비주장 규칙 감사, 조정 세트 생성 지연이 남는다.

```bash
git add lab/evidence/results/a2-tune.json lab/evidence/attempts.jsonl
git commit -m "chore: A-2 조정 세트 τ_s·θ 선택"
```

### 5단계: 확인 세트 데이터 → 동결(판정 모델 호출 전)

2·3단계를 `--split check`로 반복한다(`embed`, `question-packets`, 서브에이전트, `questions-merge`, `retrieve`, `generate`, `claims`, `controlled-*`, `label-packets`, `labels-merge`).

```bash
git add lab/evidence/study_a2 lab/evidence/attempts.jsonl
git commit -m "chore: A-2 확인 세트 데이터와 AI 참조 라벨"
/tmp/ev2.sh freeze-holdout                   # 예상: "check frozen", prereg_a2_check.json 생성
git add lab/evidence/prereg_a2_check.json lab/evidence/attempts.jsonl
git commit -m "chore: A-2 확인 세트 동결"
```

- `freeze-holdout`은 `app/`·`lab/evidence/`에 커밋하지 않은 변경이 있거나 동결 파일이 이미 있으면 거부한다. 동결 묶음은 A-1 항목에 `tune_sha256`(조정 결과)을 더한다.
- **중지:** 동결 뒤에는 확인 세트 데이터 명령이 거부된다. 동결 뒤 라벨·주장·코드·조정 결과가 바뀌면 판정이 `mismatch`로 거부된다.

### 6단계: 확인 세트 1회 판정과 리포트

```bash
/tmp/ev2.sh judge --split check --tag check  # 한 번만. --limit/--single/--no-cache 금지
/tmp/ev2.sh a2-report                        # 예상: a2-v1 정책 JSON 한 줄
```

- 예산: 자연 + 통제 주장 추정이 상한을 넘으면 통제 주장을 빼고 원장에 `budget-drop-controlled`를 남긴다. 자연만으로도 넘으면 거부(중지).
- 두 번째 `judge --split check`는 출력 파일 또는 원장(같은 연구 줄)으로 거부된다. A-1의 `split: check` 원장 줄은 세지 않는다.
- `a2-report`는 추가 호출 없이 계산한다: 관문 H-prec·H-low·H-high·H-tier(2단 군집 부트스트랩 2,000회, 시드 20261103), 정책 `a2-v1`(사전등록 반영 표), SYS − 어휘 겹침 AUC, 운영점, 통제 변형별 정확도, 비주장 규칙 오분류율, 생성 지연 p50·p95(cold 제외, 조정·확인 분할별)와 타임아웃 재설정값(p95 × 2).
- 확인 세트 ✅ 예측이 150개 미만이면 H-prec는 기술 통계로만 보고하고 τ_s는 0.85다.
- 확인 세트 상단 구간 주장이 30개 미만이면 H-high는 판정 불가 → 미채택이다(2026-10-03 데이터 전 개정).
- 판정 가능한 자연 주장 중 점수가 없는 것이 있으면(잘못된 `--tag` 등) `a2-tune`·`a2-report`가 거부한다.
- 리포트는 결과가 부정이어도 그대로 공개한다. 확인 세트 결과를 보고 τ_s·θ를 다시 고르지 않는다.

```bash
git add lab/evidence/results/a2-check.json docs/lab/evidence-a2-report.md lab/evidence/attempts.jsonl
git commit -m "docs: A-2 평가 리포트와 정책 a2-v1"
```

### 7단계: 정책 반영(P6, 별도 PR)

리포트의 `policy_a2_v1` 값을 `app/services/evidence/runner.py`의 새 `Policy`로 넣고, 요약줄 "(시험 기준)"을 지운다. H-prec 실패면 배지 툴팁 문구를 덧붙이고, 비주장 규칙 감사에서 5%를 넘은 표현이 있으면 규칙에서 뺀다(정책 버전을 올린다). 답변 생성 타임아웃을 리포트 값으로 바꾼다.

## 비용 없이 확인하는 거부 경로(격리 루트)

verify 레시피(`.claude/skills/verify/SKILL.md`)처럼 코드와 `lab/data/evidence_a2`를 임시 루트에 복사해 `--root`로 돌린다.

| 명령 | 기대 |
|---|---|
| 동결 전 `judge --split check --tag check` | `check is frozen until prereg_a2_check.json` |
| `a2-tune` 전 `question-packets --split check` | `check data is sealed until results/a2-tune.json exists` |
| 확인 세트 질문이 생긴 뒤 `a2-tune` | `tune is closed: check data exists` |
| 동결 뒤 `labels-merge --split check` | `check data is frozen (prereg_a2_check.json exists)` |
| 판정 뒤 다시 `judge --split check --tag check` | `check judge check already ran` |
| `--study a2 stage1-report` | `stage1-report is an a1 command` |
| `lexical.py`를 고친 뒤 `claims`(또는 `split`·`generate`) | `code changed after prereg_a2.json: [...]` |
| `judge --split tune --limit 20` 뒤 `a2-tune` | `N tune claims lack JEV scores` |

## 테스트(이 PR)

- `tests/evidence/test_name_condition.py`: 회사명 후보(위치 무관·일반명사 제외)·토큰 경계 비교, `lex_features`·`tier_route`.
- `tests/evidence/test_a2_split.py`: 시드·A-1 제외·교차 언급(양방향, 10회 경계, 2자 이름 무시)·분할 크기.
- `tests/evidence/test_a2_select.py`: τ_s(정밀도 0.93 경계, 30개 경계, 폴백, 숫자 확인·반박 우선), 경계 비율, θ_low·θ_high(경계·빈 구간·조건 실패).
- `tests/evidence/test_a2_tier.py`: 경로 집계, 호출 감소율, 관문 통과·실패, 150개 미만, 정책 매핑, 예산, 생성 지연, 비주장 감사.
- `tests/evidence/test_a2_cli.py`: 경로 분리·원장 표시, 봉인·동결·mismatch, 같은 연구 원장만 세는 1회 실행, 조정 닫힘, 연구별 명령, 자연 주장, 코드 해시 가드, 커밋된 사전등록 대조.
- `tests/evidence/test_a2_flow.py`: 합성 데이터로 `a2-tune → freeze-holdout → a2-report` 전체, κ 중지, 예산 규칙.
