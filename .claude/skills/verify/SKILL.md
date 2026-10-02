---
name: verify
description: lumina-invest의 lab/jev_gate·lab/evidence 명령줄을 격리된 루트에서 실행해 변경을 확인하는 레시피
---

# lumina-invest 검증 레시피

## JEV Gate Lab 명령줄 (`lab/jev_gate`)

표면은 `python -m lab.jev_gate`다. 저장소 결과 파일을 건드리지 않게 항상 `--root <임시 디렉터리>`로 격리한다.

```bash
# zsh는 변수 안의 명령을 단어로 나누지 않는다 — 래퍼 스크립트를 쓴다
cat > /tmp/lg.sh <<'EOF'
#!/bin/bash
cd /Users/noah/portfolios/lumina-invest
exec uv run -q --no-project --python 3.12 --with pandas==3.0.6 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 python -m lab.jev_gate "$@"
EOF
chmod +x /tmp/lg.sh
T=$(mktemp -d)
mkdir -p $T/lab/data && cp -R lab/data/raw $T/lab/data/raw   # 원본 zip 재사용(체크섬만 다시 받음)
/tmp/lg.sh --root $T stage0-rule                             # 커밋된 rule_stats·sample과 같아야 한다
cp lab/results/stage0/jev_calls.jsonl $T/lab/results/stage0/ # 캐시를 복사하면 세션 1 재실행이 유료 호출 0건
/tmp/lg.sh --root $T stage0-call --session 1                 # 100/100 cached=True
/tmp/lg.sh --root $T stage0-report                           # $T/docs/lab/stage0-report.md
```

## 근거 판정 엔진 명령줄 (`lab/evidence`)

표면은 `python -m lab.evidence`다. 비공개 데이터(`lab/data/evidence/`)와 코드를 임시 루트에 복사해 `--root`로 격리한다. 홀드아웃 관련 명령은 동결 해시를 다시 계산하므로 코드 파일도 함께 복사해야 한다.

```bash
T=$(mktemp -d)
mkdir -p $T/app/lib $T/app/services $T/lab/data
cp app/__init__.py app/config.py $T/app/
cp app/lib/__init__.py app/lib/jev.py app/lib/ollama.py $T/app/lib/
cp -R app/services/__init__.py app/services/evidence $T/app/services/
cp -R lab/__init__.py lab/evidence $T/lab/
cp -R lab/data/evidence $T/lab/data/
cat > /tmp/evv.sh <<EOS
#!/bin/bash
cd $T
exec uv run -q --no-project --python 3.12 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 --with scipy==1.18.1 --with pydantic-settings python -m lab.evidence --root $T "\$@"
EOS
chmod +x /tmp/evv.sh
/tmp/evv.sh stage1-report                          # $T/.../stage1.json이 커밋본 lab/evidence/results/stage1.json과 같아야 한다
/tmp/evv.sh judge --split holdout --tag holdout    # "already ran"으로 거부(유료 호출 0건)
/tmp/evv.sh labels-merge --split holdout           # "holdout data is frozen"으로 거부
/tmp/evv.sh judge --split all --tag x              # "not all"로 거부
env -u TYPESAFE_API_KEY HOME=$(mktemp -d) /tmp/evv.sh judge --split tune --tag tune   # 캐시만 사용, jev_calls.jsonl 줄 수 불변
```

- 단위 테스트: `uv run ... --with pytest python -m pytest tests/evidence -p no:cacheprovider`(외부 호출 없음)
- **커밋별 재현:** `stage1-report`는 동결 묶음의 코드 해시(`app/lib/jev.py`, `app/services/evidence/*.py`, `lab/evidence/*.py`)를 다시 검증한다. 이 파일들이 바뀐 뒤에는 HEAD에서 거부되는 것이 정상이다. 그때는 홀드아웃 결과 커밋(`5f228fe`)을 체크아웃해 재현한다.
- **Stage 0 지연 관문은 Stage 0 태그(check·repeat1~3) 첫 시도만 읽는다.** `latency_rows`가 tune·holdout 등 Stage 1 호출을 빼므로 나중에 다시 돌려도 Stage 1 호출이 섞이지 않는다. 판정은 여전히 커밋된 `lab/evidence/results/stage0.json`이 기준이다.
- 생성·임베딩은 호스트 Ollama가 빠르다: `OLLAMA_BASE_URL=http://127.0.0.1:11434 /tmp/ev.sh ...`(Docker Ollama와 같은 모델 digest).

## 비용 없이 볼 수 있는 실패 경로

- 세션 간격: 직전 세션 마지막 호출 후 2시간 안에 `--session 2` → 경과 시간과 함께 거부(exit 1).
- 키 없음: `env -u TYPESAFE_API_KEY HOME=<빈 디렉터리>` → HTTP 전에 실패하고 호출 기록 파일이 생기지 않아야 한다.
- 0644 키 파일: PermissionError로 거부, 키 문자열이 어떤 출력에도 없어야 한다.

## 주의

- `stage0-call`·`stage0-repeat`를 캐시 없이 돌리면 TypeSafe 유료 호출이 나간다(호출당 약 $0.00003).
- `stage0-report`는 실행할 때마다 `lab/attempts.jsonl`에 한 줄을 추가한다.
- 저장소 전체 pytest는 macOS uv 환경에서 lightgbm(libomp)이 없어 실패한다 — 앱 이미지 `lumina-portfolio-app`에서 읽기 전용 마운트로 돌린다.
