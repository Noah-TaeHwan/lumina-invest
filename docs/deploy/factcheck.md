# 공시 팩트체커 배포 절차(T4)

> 이 문서는 AI(Claude Code)가 코드를 읽고 작성했다. 클라우드 세션에서는 `docker compose -f compose.factcheck.yml config`로
> 문법·필수 값만 확인했고, 실제 기동·Ollama 모델 받기·스냅샷 복원·판정 호출은 하지 않았다. 아래 디스크·메모리 숫자는
> **추정**이며 로컬 실측으로 채운다. AWS 리소스 생성·외부 공개·도메인 구매는 노아 승인 사항이다.

## 1. 구성

```
인터넷 ──443/80──▶ caddy(HTTPS 자동 인증서) ──▶ app(uvicorn app.factcheck_main:app, 워커 1개)
                                                   ├─▶ postgres:16(한도·예약·솔트 표)
                                                   ├─▶ qdrant v1.13.4(factcheck_passages 문단)
                                                   └─▶ ollama(CPU, nomic-embed-text 임베딩만)
```

- 파일: `compose.factcheck.yml`, `deploy/factcheck/Caddyfile`, `.env.factcheck.example`, `deploy/factcheck/snapshot_*.sh`.
- 밖에 여는 포트는 Caddy 80/443뿐이다. PostgreSQL·Qdrant·Ollama·앱 포트는 열지 않는다.
- 모든 서비스는 `restart: unless-stopped`와 healthcheck를 갖는다. 앱은 PG·Qdrant·Ollama가 healthy가 된 뒤 뜬다.
  Caddy는 앱 healthy를 기다리지 않는다(준비 안 됨일 때도 `/api/health`를 밖에서 볼 수 있게).
- **앱 워커는 1개만 쓴다.** 검수 job이 프로세스 메모리에 있어 워커가 둘 이상이면 결과 폴링이 다른 워커로 가 404가 난다.

## 2. 서버 사양 가정(추정)

| 항목 | 가정 | 근거 |
|---|---|---|
| CPU·GPU | 2~4 vCPU, GPU 없음 | 답변 생성 없음. 임베딩(nomic-embed-text)만 CPU로 |
| 메모리 | 8GB(여유 있게 16GB) | 앱 ~1GB, Qdrant ~1GB(문단 수에 비례), Ollama 임베딩 ~1GB, PostgreSQL ~0.3GB, Caddy·OS |
| 디스크 | 30GB(gp3 등) | 아래 계산 + 이미지·로그·스냅샷 두 벌 여유 |

디스크 계산(추정):
- Qdrant: 문단 수 × (768차원 float32 3KB + payload 약 1.5KB) × 색인 여유 1.5 ≈ **문단 수 × 7KB**.
  예: 50,000 문단 ≈ 0.35GB. 스냅샷 파일도 비슷한 크기(서버에 이전 판 하나를 롤백용으로 둔다).
- Ollama 모델(nomic-embed-text) ≈ 0.3GB, 이미지(app·postgres·qdrant·ollama·caddy) ≈ 5~6GB.
- PostgreSQL: 한도·예약 행만이라 작다(수십 MB 이하).
- **로컬에서 채울 값:** 실제 문단 수 = `snapshot_create.sh` 출력의 `points=`(또는 `factcheck/store.py documents()` 합).
  문단 수: ____ → Qdrant 추정 ____ GB.

## 3. 미리 준비(노아 확인·승인)

1. 서버(예: EC2 한 대)와 보안 그룹: 인바운드 80·443만, SSH는 관리자 IP만.
2. 도메인 DNS A 레코드 → 서버 공인 IP(Caddy가 Let's Encrypt로 인증서를 받으려면 80/443이 열려 있어야 한다).
3. 판정 API 키 파일: 서버의 고정 경로(예 `/etc/factcheck/typesafe_api_key`)에 두고 `chmod 600`. 앱 컨테이너가 읽기
   전용으로 마운트한다(권한이 0600이 아니면 앱이 읽지 않는다). 키 값을 환경변수·`.env.factcheck`에 넣지 않는다.
4. 하루 상한 공개 값(`FACTCHECK_DAILY_*`)을 노아가 정한다. 30문장 검수 하나가 1,736,000 토큰을 예약하므로 키별·전체 상한은
   '그날 실제 사용량 + 검수 하나의 예약'보다 커야 한다(`app/services/factcheck/settings.py`).

## 4. 처음 배포

### 4-1. 로컬(적재한 컴퓨터)에서 데이터 준비

```bash
# 문단 스냅샷(로컬 Qdrant의 factcheck_passages)
QDRANT_URL=http://127.0.0.1:6333 deploy/factcheck/snapshot_create.sh ./factcheck_passages-$(date +%Y%m%d).snapshot
# XBRL 행·상장사명 사전(T1 수집 산출)
ls -l lab/data/factcheck/xbrl_facts.json lab/data/factcheck/corp_names.json
```

### 4-2. 서버로 올리기

```bash
scp ./factcheck_passages-YYYYMMDD.snapshot server:/srv/factcheck/snapshots/
scp lab/data/factcheck/xbrl_facts.json lab/data/factcheck/corp_names.json server:/srv/factcheck/data/
```

`/srv/factcheck/data`는 `.env.factcheck`의 `FACTCHECK_DATA_HOST_DIR`이고 앱에 읽기 전용(`/data/factcheck`)으로 붙는다.

### 4-3. 서버에서 설정·기동

```bash
git clone … && cd lumina-invest
cp .env.factcheck.example .env.factcheck && vi .env.factcheck   # 도메인·메일·DB 비밀번호·키 파일 경로·데이터 폴더
docker compose -f compose.factcheck.yml --env-file .env.factcheck up -d postgres qdrant ollama
docker compose -f compose.factcheck.yml --env-file .env.factcheck ps     # ollama가 healthy(모델 받기 끝)까지 몇 분
deploy/factcheck/snapshot_restore.sh /srv/factcheck/snapshots/factcheck_passages-YYYYMMDD.snapshot
docker compose -f compose.factcheck.yml --env-file .env.factcheck up -d   # app·caddy
docker compose -f compose.factcheck.yml --env-file .env.factcheck logs -f app
```

시작 로그에서 확인한다:
- `[factcheck] data dir=/data/factcheck facts=N names=M passages=P qdrant=ok missing=-`(N·M·P가 0이 아니고 missing이 `-`)
- `[factcheck] 서버 시작 완료`(뒤에 `검수 비활성`이 없어야 한다)

### 4-4. 준비 상태 확인

```bash
curl -s https://도메인/api/health | python3 -m json.tool
```

준비됐으면 200과 `"status": "ok"`. 준비가 안 됐으면 503과 `code`·`checks`가 이유를 말한다. 앱은 준비가 안 됐으면 **30초마다
다시 확인**해 회복하면 스스로 연다(재시작 불필요). 단, 판정 키 거부로 닫힌 것은 재시작해야 다시 연다.

| `code` | 뜻 | 할 일 |
|---|---|---|
| `starting` | 아직 시작 중 | 잠시 기다린다 |
| `startup_failed` | 마이그레이션·PG 연결·미정산 예약 복구 중 하나 실패(`checks.migrations/db/recovery`) | `logs app`의 `[WARN] 시작 실패 — …` 단계를 본다. PG가 회복되면 30초 안에 다시 연다 |
| `no_api_key` | 키 파일이 없거나 비었다(`checks.api_key=false`), 또는 401·403이 3번 이어져 키가 막혔다(`checks.api_key_rejected=true`) | 키 파일 경로·권한(0600)·값을 고친다. 거부로 막힌 경우 키를 바꾸고 `restart app` |
| `data_unavailable` | 데이터 파일이 없거나 비었다, Qdrant 연결 실패(`checks.data.qdrant=unreachable`), 컬렉션 없음(`no_collection`), 또는 데모 회사 중 하나의 XBRL 행·문단이 0(`checks.data.missing`에 corp_code) | 스냅샷 복원·데이터 업로드를 다시 한다. 복원 뒤 30초 안에 열린다 |

### 4-5. 첫 검수 1회

브라우저로 `https://도메인/`에서 5문장 정도의 분석글을 붙여 검수한다. 끝난 뒤 서버에서 정산이 맞는지 본다:

```bash
docker compose -f compose.factcheck.yml --env-file .env.factcheck exec postgres \
  psql -U factcheck -d factcheck -c "select est, actual, settled_at is not null from factcheck_reservations order by created_at desc limit 3;"
```

`actual`이 0보다 크고 `est` 이하(보통 훨씬 작다)이며 `settled_at`이 채워져 있어야 한다.

## 5. 분기 갱신(정기보고서·잠정실적 공시 뒤)

1. 로컬에서 재수집·적재: `python -m app.services.factcheck.collect …` → `python -m app.services.factcheck.store load …`(T1).
2. 새 스냅샷: `deploy/factcheck/snapshot_create.sh`.
3. 서버에 스냅샷·`xbrl_facts.json`·`corp_names.json`을 올린다(이전 판은 지우지 말고 이름을 바꿔 둔다 — 롤백용).
4. `deploy/factcheck/snapshot_restore.sh <새 스냅샷>`.
5. **앱을 다시 시작한다**: `docker compose … restart app`. XBRL 행·상장사명은 시작할 때 한 번 읽고, 이미 준비된 앱은 다시 확인하지
   않는다.
6. `/api/health`의 `checks.data`(회사별 facts·passages)가 늘었는지, 첫 검수 1회.

## 6. 롤백

1. 이전 스냅샷으로 `snapshot_restore.sh <이전 스냅샷>`.
2. `FACTCHECK_DATA_HOST_DIR`의 `xbrl_facts.json`·`corp_names.json`을 이전 판으로 되돌린다.
3. `restart app` → `/api/health` 200 확인.
4. 코드 롤백은 이전 커밋을 체크아웃하고 `up -d --build app`. 한도·예약 표(마이그레이션 0012·0013)는 그대로 둔다.

## 7. 운영 메모

- `FACTCHECK_ALLOWED_ORIGINS`는 compose가 `https://${FACTCHECK_DOMAIN}`으로 채운다. 비우면 TLS 프록시 뒤에서 브라우저 POST가
  모두 403이 된다.
- 앱은 Caddy 고정 주소(172.30.1.10)만 신뢰 프록시로 보고 X-Forwarded-For의 사용자 주소로 익명 한도를 센다.
  uvicorn `--proxy-headers`는 쓰지 않는다.
- 사용량은 판정 호출마다 원장으로 정산되고, 서버가 죽어 정산하지 못한 예약은 다음 시작(또는 30초 sweeper) 때 300초가 지난
  것부터 예약량으로 정산된다.
- 앱 재시작은 진행 중 검수를 취소한다(정산은 끝까지 한다). 사용자는 다시 검수해야 한다.

## 8. 남은 일(배포 뒤 개선)

- 예약 대기 Task 추적, 공백 없는 짧은 문장('네.네.네.')이 한 문장으로 세지는 것, 실패한 job 재검수를 첫 실행 전에 취소하면
  오류가 `cancelled`로 덮이는 것, 종료 중 sweep이 꺼낸 job의 정산 누락 가능성.
- 429 응답(사용량 미보고)은 시도당 요청 상한(28,000)으로 센다(현 정책).
- 워커를 늘리려면 job 저장소를 공유 저장소로 옮긴다.
