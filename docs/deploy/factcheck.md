# 공시 팩트체커 배포 절차(T4)

> 이 문서는 AI(Claude Code)가 코드를 읽고 작성했다. 클라우드 세션에서는 `docker compose -f compose.factcheck.yml config`로
> 문법·필수 값만 확인했다. 2절의 실측 숫자는 리드가 로컬에서 이 compose로 실제 기동·스냅샷 복원·첫 검수를 한 결과다
> (2026-10-06). AWS 리소스 생성·외부 공개는 노아 승인 사항이다.

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

## 2. 서버 사양(로컬 실측 기준)

| 항목 | 권장 | 실측(리드 로컬, 2026-10-06) |
|---|---|---|
| CPU·GPU | 2 vCPU 이상, GPU 없음 | 답변 생성 없음. 임베딩(nomic-embed-text)만 CPU로. 첫 검수 3문장 11.2초(차가운 임베딩 포함) |
| 메모리 | **4GB 이상** | 대기 합 약 1.0GB: app 141MB · qdrant 485MB · ollama 336MB · postgres 31MB · caddy 24MB |
| 디스크 | **30GB(gp3) 이상** | 이미지 약 9GB(ollama 6.99GB · app 1.25GB · postgres 411MB · qdrant 291MB · caddy 89MB), 볼륨 약 1GB(qdrant 657MB · ollama 모델 274MB · postgres 50MB) |

- 데이터: 문단 8,577개(삼성전자 4,459 · SK하이닉스 4,118), XBRL 행 1,122개(603 · 519). 스냅샷 파일 약 80MB(80,344,064바이트),
  복원 12초.
- 디스크 여유: 스냅샷 두 벌(현재·롤백용)과 로그, 이미지 갱신 때 옛 이미지가 잠시 함께 남는 것을 감안했다.

## 3. 미리 준비(노아 확인·승인)

1. 서버(예: EC2 한 대, 메모리 4GB 이상·디스크 30GB 이상)와 보안 그룹: 인바운드 80·443만, SSH는 관리자 IP만.
2. 공개 주소: 기본은 sslip.io(아래 3-1). 자체 도메인을 쓰면 DNS A 레코드 → 서버 공인 IP.
3. **키 파일·데이터 폴더를 먼저 만든다.** 마운트할 경로가 없으면 Docker가 빈 **디렉터리**를 만들어 버려 앱이 `no_api_key`·
   `data_unavailable`로 뜬다.
   ```bash
   sudo install -d -m 700 /etc/factcheck && sudo install -m 600 /dev/null /etc/factcheck/typesafe_api_key   # 키 값을 넣는다
   sudo install -d -m 755 /srv/factcheck/data /srv/factcheck/snapshots
   ```
   판정 API 키 파일은 권한 0600이어야 앱이 읽는다. 키 값을 환경변수·`.env.factcheck`에 넣지 않는다.
4. **DB 비밀번호는 16진수로**(`openssl rand -hex 24`). `DATABASE_URL`에 그대로 들어가므로 `@ : / ?` 같은 문자를 피한다.
5. **Ollama 이미지·임베딩 모델 태그를 고정한다.** 문단을 적재한 로컬과 같은 임베딩이어야 검색이 맞는다. 로컬에서:
   `docker exec <ollama 컨테이너> ollama --version` → `.env.factcheck`의 `OLLAMA_TAG`(예: `0.x.y`),
   `docker exec <ollama 컨테이너> ollama list`의 `nomic-embed-text` ID를 적어 두고, 서버에서 받은 모델 ID가 같은지 본다
   (`OLLAMA_EMBED_MODEL`은 적재 때 쓴 이름 그대로 — 기본 `nomic-embed-text`).
6. 하루 상한: **전체 1천만 토큰/일(2026-10-06 노아 결정, `FACTCHECK_DAILY_GLOBAL_TOKENS=10000000`)**. 키별 2,500,000·하루 3회.
   30문장 검수 하나가 1,736,000 토큰을 예약하므로 상한은 '그날 실제 사용량 + 검수 하나의 예약'보다 커야 한다.

### 3-1. 공개 주소: sslip.io(기본안, 도메인 구매 없이)

`FACTCHECK_DOMAIN`에 서버 공인 IP의 점을 하이픈으로 바꾼 sslip.io 이름을 쓴다(예: 공인 IP 3.35.10.20 → `3-35-10-20.sslip.io`).
sslip.io는 이름 속 IP로 응답하는 공개 DNS라 따로 DNS를 설정하지 않아도 되고, Caddy가 이 이름으로 Let's Encrypt 인증서를
자동으로 받는다(80/443이 열려 있어야 한다). 공인 IP가 바뀌지 않게 탄력적 IP(Elastic IP)를 붙인다 — IP가 바뀌면 주소도 바뀐다.
나중에 자체 도메인이 생기면: DNS A 레코드를 같은 IP로 → `.env.factcheck`의 `FACTCHECK_DOMAIN`을 새 이름으로 →
`docker compose … up -d`(Caddy가 새 인증서를 받고, 앱의 `FACTCHECK_ALLOWED_ORIGINS`도 새 이름으로 바뀐다). 옛 sslip.io 주소는
그때부터 Origin이 달라 검수 요청이 403이 되므로, 공유한 링크는 새 주소로 바꾼다.

## 4. 처음 배포

### 4-1. 로컬(적재한 컴퓨터)에서 데이터 준비

```bash
# 문단 스냅샷(로컬 Qdrant의 factcheck_passages)
QDRANT_URL=http://127.0.0.1:6333 deploy/factcheck/snapshot_create.sh   # → lab/data/factcheck/snapshots/(git 무시)
# XBRL 행·상장사명 사전(T1 수집 산출)
ls -l lab/data/factcheck/xbrl_facts.json lab/data/factcheck/corp_names.json
```

### 4-2. 서버로 올리기

```bash
scp lab/data/factcheck/snapshots/factcheck_passages-YYYYMMDD.snapshot server:/srv/factcheck/snapshots/
scp lab/data/factcheck/xbrl_facts.json lab/data/factcheck/corp_names.json server:/srv/factcheck/data/
```

`/srv/factcheck/data`는 `.env.factcheck`의 `FACTCHECK_DATA_HOST_DIR`이고 앱에 읽기 전용(`/data/factcheck`)으로 붙는다.

### 4-3. 서버에서 설정·기동

```bash
git clone … && cd lumina-invest
cp .env.factcheck.example .env.factcheck && vi .env.factcheck   # 주소·메일·DB 비밀번호(hex)·키 파일·데이터 폴더·Ollama 태그
docker compose -f compose.factcheck.yml --env-file .env.factcheck up -d postgres qdrant ollama
docker compose -f compose.factcheck.yml --env-file .env.factcheck ps     # ollama가 healthy(모델 받기 끝)까지 몇 분
docker compose -f compose.factcheck.yml --env-file .env.factcheck up -d   # app·caddy
deploy/factcheck/snapshot_restore.sh /srv/factcheck/snapshots/factcheck_passages-YYYYMMDD.snapshot   # 앱을 멈추고 복원·다시 켬
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
다시 확인**해 회복하면 스스로 연다(재시작 불필요). 판정 키 거부로 닫힌 것은 10분마다 시험 호출로 다시 확인한다.

| `code` | 뜻 | 할 일 |
|---|---|---|
| `starting` | 아직 시작 중 | 잠시 기다린다 |
| `startup_failed` | 마이그레이션·PG 연결·미정산 예약 복구 중 하나 실패(`checks.migrations/db/recovery`) | `logs app`의 `[WARN] 시작 실패 — …` 단계를 본다. PG가 회복되면 30초 안에 다시 연다 |
| `no_api_key` | 키 파일이 없거나 비었다(`checks.api_key=false`), 또는 401이 3번 이어져 키가 막혔다(`checks.api_key_rejected=true`) | 키 파일 경로·권한(0600)·값을 고친다. 거부로 막힌 경우 키를 고치면 10분 안의 시험 호출로 다시 열린다(바로 열려면 `restart app`) |
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
4. `deploy/factcheck/snapshot_restore.sh <새 스냅샷>` — 스크립트가 `stop app` → 컬렉션 복원 → `start app` 순서로 한다(서비스
   중 컬렉션을 덮어쓰면 그 사이 검수가 빈·반쯤 찬 컬렉션을 본다). 앱이 다시 켜질 때 새 XBRL 행·상장사명도 읽는다(이미 준비된
   앱은 데이터를 다시 읽지 않으므로, 파일만 바꿨으면 `stop app` → 파일 교체 → `start app`).
5. `/api/health`의 `checks.data`(회사별 facts·passages)가 늘었는지, 첫 검수 1회.

## 6. 롤백

1. `FACTCHECK_DATA_HOST_DIR`의 `xbrl_facts.json`·`corp_names.json`을 이전 판으로 되돌린다.
2. 이전 스냅샷으로 `snapshot_restore.sh <이전 스냅샷>`(앱을 멈추고 복원한 뒤 다시 켠다).
3. `/api/health` 200 확인.
4. 코드 롤백은 이전 커밋을 체크아웃하고 `up -d --build app`. 한도·예약 표(마이그레이션 0012·0013)는 그대로 둔다.

## 7. 운영 메모

- `FACTCHECK_ALLOWED_ORIGINS`는 compose가 `https://${FACTCHECK_DOMAIN}`으로 채운다. 비우면 TLS 프록시 뒤에서 브라우저 POST가
  모두 403이 된다.
- 앱은 Caddy 고정 주소(172.30.1.10)만 신뢰 프록시로 보고 X-Forwarded-For의 사용자 주소로 익명 한도를 센다(리드 실측:
  X-Forwarded-For·X-Real-IP 위조 요청도 같은 익명 키로 셌다). uvicorn은 `--no-proxy-headers`로 프록시 헤더 처리를 끈다(기본은 켜짐).
- edge 네트워크(172.30.1.0/24)가 서버의 다른 네트워크와 겹치면 `subnet`·`ip_range`·Caddy `ipv4_address`·
  `FACTCHECK_TRUSTED_PROXIES`를 함께 바꾼다(한쪽만 바꾸면 모든 사용자가 한 익명 키로 묶인다 — 테스트가 이 일치를 고정한다).
- 판정 키 차단기: 401이 3번 이어지면 검수를 닫고(`no_api_key`, `checks.api_key_rejected`), 10분마다 한도 안에서 시험 호출
  1회로 다시 확인해 받아들여지면 연다(로그 `factcheck_auth_blocked`/`factcheck_auth_unblocked`). 403은 세지 않고 로그만 남긴다.
- 사용량은 판정 호출마다 원장으로 정산되고, 서버가 죽어 정산하지 못한 예약은 다음 시작(또는 30초 sweeper) 때 300초가 지난
  것부터 예약량으로 정산된다.
- 앱 재시작은 진행 중 검수를 취소한다(정산은 끝까지 한다). 사용자는 다시 검수해야 한다.

## 8. 남은 일(배포 뒤 개선)

- 예약 대기 Task 추적, 공백 없는 짧은 문장('네.네.네.')이 한 문장으로 세지는 것, 실패한 job 재검수를 첫 실행 전에 취소하면
  오류가 `cancelled`로 덮이는 것, 종료 중 sweep이 꺼낸 job의 정산 누락 가능성.
- 429 응답(사용량 미보고)은 시도당 요청 상한(28,000)으로 센다(현 정책).
- 판정 키 차단기의 상태 기록(`_note_status`)을 과금 정산 뒤로 옮기기, 정산에 계속 실패하는 오래된 예약을 30초마다 에러 로그로
  남기는 것의 빈도 조절과 컨테이너 로그 로테이션(json-file `max-size` 등).
- 워커를 늘리려면 job 저장소를 공유 저장소로 옮긴다.
