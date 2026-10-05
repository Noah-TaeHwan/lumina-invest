# 포트폴리오 로컬 실행 가이드

[프로젝트 소개](readme.md) · [실행 설정](compose.portfolio.yml)

프로젝트 루트에서 Docker Compose v2로 실행한다. Docker 엔진, Git, Python 3와 이미지·모델 다운로드가 가능한 네트워크가 필요하다.
앱은 `http://127.0.0.1:8967`에서 열고, PostgreSQL·Redis·Neo4j·Qdrant·Ollama는 호스트 포트를 노출하지 않는다.
외부 시장 데이터 조회와 모델 다운로드에는 네트워크가 필요하며, 이 profile의 채팅은 로컬 Ollama를 사용한다.

## 1. 실행할 버전 준비

PR #2로 로컬 실행 설정이 main에 통합된 버전을 기준으로 한다. 기본 실행 경로는 main을 clone하는 것이다.

```sh
git clone https://github.com/Noah-TaeHwan/lumina-invest.git
cd lumina-invest
```

병합 전 변경을 검토할 때만 해당 PR의 head 브랜치를 `--branch`로 지정해 받는다.

이후 명령은 모두 `compose.portfolio.yml`이 있는 프로젝트 루트에서 실행한다.
기본 `docker-compose.yml`은 8966 포트와 ingest/Celery 등 원본의 백그라운드 실행 구성이므로 아래 명령과 혼용하지 않는다.

## 2. 비공개 설정 생성

기존 `.env.dev`·`.env.prod`·다른 프로젝트 설정이나 AWS 인증 파일을 복사하지 않는다.
아래 표준 라이브러리 명령은 네 개의 비밀값을 각각 생성하고 `.env.portfolio`를 0600으로 만든다. 실제 값은 출력하지 않는다.
`O_EXCL`로 기존 파일이 있으면 중단하므로, 이미 사용 중인 설정을 덮어쓰지 않는다.

```sh
python3 - <<'PYENV'
import os
import secrets

values = {
    name: secrets.token_urlsafe(32)
    for name in ("POSTGRES_PASSWORD", "NEO4J_PASSWORD", "SESSION_SECRET", "JWT_SECRET")
}
values.update(LLM_MODEL="llama3.2:1b", EMBED_MODEL="nomic-embed-text")
fd = os.open(".env.portfolio", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
print(".env.portfolio 생성 완료 (0600, 값 출력 없음)")
PYENV
```

`POSTGRES_USER`와 `POSTGRES_DB`의 기본값은 `lumina`다. 생성한 비밀번호는 연결 URL에서 사용할 수 있는 URL-safe 난수다.
비공개 파일은 Git과 Docker 이미지 빌드에서 제외한다. 파일 내용이나 비밀값이 포함된 Compose 전체 설정을 공개 로그에 출력하지 않는다.

Compose 변수 치환은 `--env-file .env.portfolio`, app의 환경 주입은 같은 파일 하나를 읽는 `env_file`을 사용한다.
app은 이 파일을 `/app/.env.portfolio`에 읽기 전용으로 마운트하며 `ENV_FILE=/app/.env.portfolio`로 Pydantic의 파일 경로를 고정한다.
Compose `environment`의 로컬 연결·provider·동기화 설정은 파일 값보다 우선한다. 같은 이름의 셸 환경 변수도 Compose 치환에 영향을 줄 수 있으므로 기존 운영 설정을 가진 셸과 혼용하지 않는다.

## 3. 프로젝트·포트 확인 후 기동

프로젝트 이름은 `lumina-portfolio`다. 먼저 이전 프로젝트의 컨테이너와 volume이 남아 있는지 확인한다.

```sh
docker ps -a --filter label=com.docker.compose.project=lumina-portfolio
docker volume ls --filter label=com.docker.compose.project=lumina-portfolio
```

기존 컨테이너 또는 volume이 있거나 8967 포트가 이미 사용 중이면 `up` 전에 멈추고, 해당 환경의 소유자와 관리 checkout을 확인한다.
컨테이너가 없어도 DB volume에는 이전 비밀번호 설정이 남아 있을 수 있다. 자신의 기존 환경이면 관리 checkout과 기존 `.env.portfolio`를 재사용하며, 새 비밀번호 파일로 같은 프로젝트를 덮어 기동하지 않는다. 다른 환경이면 담당자와 먼저 확인한다.
다른 checkout에서 같은 프로젝트의 `up`·`down`을 실행하거나 다른 서비스·volume을 종료·삭제하지 않는다.
별도 실행이 필요하면 담당자와 프로젝트 이름 및 Compose의 앱 host 포트를 함께 정한 뒤 진행한다.

```sh
docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio config --quiet
docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio up -d --build
docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio ps
```

app은 의존 서비스의 healthcheck가 통과한 뒤 시작하며, 자체 PostgreSQL migration과 Neo4j schema/seed를 실행한다.
컨테이너 이름을 고정하지 않고 프로젝트별 기본 네트워크와 named volume을 사용한다.

## 4. 모델 준비

Ollama의 `healthy`는 서버가 응답한다는 뜻이며 모델 설치를 보증하지 않는다. 채팅과 임베딩 전에 아래 모델을 준비한다.
다운로드에 필요한 시간·디스크·네트워크를 확보한 뒤 실행한다.

```sh
docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio exec ollama ollama pull llama3.2:1b
docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio exec ollama ollama pull nomic-embed-text
docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio exec ollama ollama list
docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio ps
```

모델과 데이터는 이 프로젝트의 전용 volume에 남는다. Qdrant 서버와 임베딩 모델이 준비돼도 검색할 문서가 자동으로 적재되는 것은 아니다.
로컬 확인에서 CPU 채팅 한 요청은 약 420초가 걸렸다(2026-10-01 단일 관측). 평균이나 다른 PC의 응답 시간 보장이 아니다.
macOS에서 이렇게 느리면 [6. 채팅이 느릴 때(macOS)](#6-채팅이-느릴-때macos)의 호스트 Ollama 구성을 참고한다.
화면에서 기다림이 길거나 요청이 끝나지 않으면 대화 이력과 해당 앱 상태를 확인한 뒤 재시도 여부를 판단한다.

## 5. 기능 확인

`http://127.0.0.1:8967`에서 다음을 확인한다.

1. 회원가입 → 로그인 → 사용자 화면 진입
2. AAPL 검색 → 기업 지표 표시
3. AI 질문 → 채팅 화면에 비어 있지 않은 답변 표시
4. 아래 읽기 전용 API로 대화 저장·시드 그래프·시작 동기화 상태를 확인

로그인한 **같은 브라우저**에서 다음 URL을 연다. 채팅 답변의 화면 표시와 API의 저장 결과를 각각 확인하며, 별도 대화 이력·그래프 UI가 검증됐다는 뜻으로 해석하지 않는다.

| 읽기 전용 URL | 확인할 결과 |
|---|---|
| `http://127.0.0.1:8967/api/conversations` | `items[].id`에서 해당 대화 ID 확인 |
| `http://127.0.0.1:8967/api/conversations/{id}` | 위 ID로 `{id}`를 바꾸고 `messages`의 질문·답변과 `message_count` 확인 |
| `http://127.0.0.1:8967/api/graph/related/005930.KS` | 삼성전자 시드 관계 조회 결과 `found=true` 확인 |
| `http://127.0.0.1:8967/api/system/sync-status` | 시작 후 5초를 넘겨 `scheduler.running=false`, `scheduler.syncing=false` 확인 |

`STARTUP_DATA_SYNC_ENABLED=false`로 시작 시 시장 동기화 스케줄러를 만들지 않는다. 일반 앱 실행의 기본값은 `true`다.
자동 수집 확인 중에는 수동 시장 동기화 버튼을 누르지 않는다. 사용자가 요청하는 시세 조회는 별도의 외부 요청이다.
Celery worker/Beat·ingest 서비스·Docker socket·공유 LEAN volume은 포함하지 않으며 `LEAN_MODE=local`이다.
Alpaca 주문 키와 AWS 자격 증명 환경 변수는 profile에서 비워 둔다.

`/api/health`만으로 인증·DB·모델 동작 성공을 판정하지 않는다. 시작 분기의 최소 검사는 다음처럼 실행할 수 있다.

```sh
python3 tests/test_startup_data_sync.py
```

이 검사는 실제 lifespan의 활성/비활성 분기와 종료 cleanup을 외부 서비스 없이 확인한다.
2026-10-01 별도 로컬 실행에서 위 기본 사용자 흐름을 확인했으며, 이 문서 편집 중 새 clone이나 앱을 다시 실행하지는 않았다.
일부 재무 필드 누락(예: Yahoo가 일부 국내 종목의 EPS·BPS를 주지 않음)과 일반 채팅(RAG)의 출처 링크 부재가 남아 있다. 해외 기업의 원화 표시 오류는 PR #39로 고쳤다(종목 통화별 단위). 전체 원본 기능·전체 CI·공개 배포·투자 성과는 검증하지 않았다.

## 6. 채팅이 느릴 때(macOS)

macOS의 Docker 컨테이너는 GPU(Metal)를 쓰지 못해 컨테이너 Ollama가 CPU로만 돈다. 4절의 420초 관측과 같은 현상이다.
이때는 호스트(macOS)에 Ollama를 설치해 Metal로 돌리고, app이 그 서버를 보게 하는 오버라이드 [compose.host-ollama.yml](compose.host-ollama.yml)을 겹쳐 쓴다.
오버라이드는 app의 `OLLAMA_BASE_URL`을 `http://host.docker.internal:11434`로 바꾸고, app의 `depends_on`에서 ollama만 뺀다. 기본 구성의 ollama 서비스 정의는 그대로 남는다.

**호스트 Ollama 준비.** 호스트에 Ollama를 설치하고 실행한 뒤 모델을 받는다. 컨테이너의 `ollama_models` volume에 받아 둔 모델은 호스트 Ollama가 재사용하지 않으므로 다시 받는다.

```sh
ollama pull llama3.2:1b        # 채팅(LLM_MODEL)
ollama pull nomic-embed-text   # 임베딩(EMBED_MODEL)
ollama pull llama3.1:8b        # 공시 근거 모드 생성기(EVIDENCE_LLM_MODEL 기본값), 근거 모드를 쓸 때만
ollama list
```

**오버라이드로 기동.** 대상 서비스를 `app`으로 지정하면 app과 나머지 의존 서비스(PostgreSQL·Redis·Neo4j·Qdrant)만 뜨고 컨테이너 ollama는 뜨지 않는다.

```sh
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -p lumina-portfolio config --quiet
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -p lumina-portfolio up -d --build app
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -p lumina-portfolio ps
```

- 기본 구성으로 이미 띄워 컨테이너 ollama가 돌고 있으면 `docker compose --env-file .env.portfolio -f compose.portfolio.yml -p lumina-portfolio stop ollama`로 멈춘다. 오버라이드 없이 `up -d`를 다시 하면 다시 뜬다.
- 오버라이드는 Compose 병합 태그 `!override`를 쓴다. 사용하는 Compose가 이 태그를 거부하면 오버라이드 파일의 `depends_on` 블록을 지우고 쓴 뒤, 위 `stop ollama`로 컨테이너 ollama를 멈춘다(app의 ollama 의존 때문에 함께 뜬다).
- 기본 구성으로 돌아가려면 오버라이드 없이 원래 3절 명령으로 기동한다.
- Linux에서는 `extra_hosts: host.docker.internal:host-gateway`로 이름은 풀리지만, 호스트 Ollama가 127.0.0.1에만 열려 있으면 컨테이너에서 닿지 않는다(`OLLAMA_HOST=0.0.0.0` 등으로 Docker 브리지에서 닿게 연다). macOS Docker Desktop은 별도 설정 없이 닿는다.

**근거 모드도 같은 서버를 쓴다(코드 확인).** 공시 근거 모드(`POST /api/evidence/chat`)의 생성기는 `get_llm_client`를 주입받고([evidence.py](app/routes/evidence.py)), `LLM_PROVIDER=ollama`이면 `OllamaClient(settings.OLLAMA_BASE_URL, …)`를 만든다([llm_client.py](app/lib/llm_client.py)). 모델만 `EVIDENCE_LLM_MODEL`(기본 `llama3.1:8b`)로 다르다. 문단 임베딩도 같은 `OLLAMA_BASE_URL`을 쓴다([store.py](app/services/evidence/store.py)). 따라서 이 오버라이드 하나로 채팅·근거 모드 생성·임베딩이 모두 호스트 Ollama로 간다. 모델이 없으면 근거 모드는 `ollama pull <모델>`을 안내하는 503을 돌려준다.

**로컬 실측(2026-10-05, 단일 세션).** Apple Silicon Mac, 같은 질문 3개, 모델 `llama3.2:1b`, 새 계정, `POST /api/chat`.

| 구성 | 결과 |
|---|---|
| 기본 `compose.portfolio.yml`(컨테이너 Ollama, CPU 전용) | 528.0초 504 시간 초과 / 95.7초(답 128자) / 252.3초(답 163자) — 가운데값 252초 |
| 호스트 Ollama(Metal) — app에 `OLLAMA_BASE_URL=http://host.docker.internal:11434`, 컨테이너 ollama 멈춤 | 50.9초 / 50.6초 / 41.9초, 답 3,582~4,392자 — 가운데값 51초, 실패 0 |

이 측정에서 가운데값은 252초 → 51초였다. 측정 중 호스트 부하가 높았다(load 11~130, 다른 앱 포함). 단일 세션 관측이며 평균이나 다른 PC의 응답 시간 보장이 아니다.
측정은 별도 로컬 실행에서 했고, 이 절과 오버라이드 파일 작성 중에는 스택을 기동하지 않았다(Compose 설정 병합만 확인). 이 절은 AI가 작성했다.

## 7. 공시 근거 모드·판단 일지 켜기

README '한눈에 보기'의 대표 화면(공시 근거 모드 답변, 문장마다 ✅·❔ 배지)을 재현하는 절차다. 두 기능은 기능 플래그 `EVIDENCE_CHAT_ENABLED`·`JOURNAL_ENABLED`(기본 꺼짐)로 숨겨져 있고, 꺼져 있으면 해당 API가 404라 화면에 토글·메뉴가 나타나지 않는다.
켜는 오버라이드는 [compose.features.yml](compose.features.yml)이다. 1~6절로 기본 스택을 띄워 본 뒤 진행한다.

### 7-1. 준비물

| 준비물 | 어디서 읽나(코드 확인) | 쓰는 곳 |
|---|---|---|
| TypeSafe JEV API 키 | `TYPESAFE_API_KEY` 환경 변수, 없으면 `~/.config/typesafe/api_key`(권한 0600이 아니면 거부) — [jev.py](app/lib/jev.py) `load_api_key()` | 앱의 답변 문장 판정(배지) |
| OpenDART API 키 | `DART_API_KEY` 환경 변수, 없으면 `~/.config/opendart/api_key`(권한 0600이 아니면 거부) — [dart.py](app/services/evidence/dart.py) `load_dart_key()` | 공시 문단 적재(7-2)만 |
| Ollama 모델 `llama3.1:8b` | `EVIDENCE_LLM_MODEL`(기본 `llama3.1:8b`, [config.py](app/config.py)) | 근거 모드 답변 생성 |
| Ollama 모델 `nomic-embed-text` | [store.py](app/services/evidence/store.py) `EMBED_MODEL`(앱의 `EMBED_MODEL` 설정과 별개로 고정) | 문단·질문 임베딩 |

- TypeSafe JEV 키는 TypeSafe에서 직접 발급받는다. 이 프로젝트는 TypeSafe와 제휴 관계가 아니며, 이 문서는 가격·요금을 다루지 않는다. 호출량에 따른 비용은 각자의 TypeSafe 계정 조건을 확인한다.
- 앱 컨테이너는 root로 돌아 `~`가 `/root`다. 오버라이드는 호스트의 키 **파일**을 `/root/.config/typesafe/api_key`에 읽기 전용으로 마운트한다. 키 값이 Compose 설정·이미지·Git에 들어가지 않도록 환경 변수(`TYPESAFE_API_KEY`)로 넘기지 않는다.
- 두 키 파일은 호스트에서 `chmod 600`으로 만든다. 바인드 마운트는 호스트 권한을 그대로 보이므로 0600이 아니면 컨테이너 안에서도 거부된다.
- Ollama는 6절의 호스트 Ollama(macOS면 Metal)를 권한다. `llama3.1:8b`는 컨테이너 CPU Ollama에서 매우 느릴 수 있다. 호스트에서 `ollama pull llama3.1:8b`, `ollama pull nomic-embed-text`로 받는다(6절 목록과 같다).
  - 컨테이너 Ollama를 쓰려면 아래 명령에서 `-f compose.host-ollama.yml`을 빼고, 4절처럼 `exec ollama ollama pull …`로 두 모델을 받는다.
- 판단 일지 표는 마이그레이션 `0010_judgment_journal`이다. 이 profile은 `RUN_MIGRATIONS_ON_STARTUP=true`라 app 시작 시 자동 적용된다. 판단 일지 플래그는 근거 모드 플래그와 묶이지 않지만, 기록은 근거 모드 답변에서만 만들 수 있으므로 두 플래그를 함께 켠다.

### 7-2. 기동

`TYPESAFE_KEY_FILE`은 호스트 키 파일의 절대 경로다. 셸에서 넘기거나, 자신의 `.env.portfolio`에 `TYPESAFE_KEY_FILE=/절대/경로` 한 줄을 더한다(경로만 들어가고 키 값은 들어가지 않는다).
값이 없으면 `config`·`up`이 `required variable TYPESAFE_KEY_FILE is missing a value: Set TYPESAFE_KEY_FILE to the host path of the TypeSafe JEV key file (mode 0600)`로 멈춘다.
경로에 파일이 없으면 `config`는 통과하지만 `up`이 바인드 원본 없음으로 실패한다(`create_host_path: false`, 빈 디렉터리를 대신 만들지 않는다).

```sh
export TYPESAFE_KEY_FILE="$HOME/.config/typesafe/api_key"
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -f compose.features.yml -p lumina-portfolio config --quiet
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -f compose.features.yml -p lumina-portfolio up -d --build app
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -f compose.features.yml -p lumina-portfolio ps
```

app 로그에 `근거 문단 저장소(evidence_passages) 연결 — 컬렉션이 없으면 검색은 503`이 보이면 플래그가 켜진 것이다.
이 시점에는 공시 문단이 없어 근거 모드 질문이 503(저장소 없음)이다. 앱은 컬렉션을 만들지 않는다.

### 7-3. 공시 문단 적재

적재 CLI는 [load_passages.py](app/services/evidence/load_passages.py)다. OpenDART에서 회사별 2025.12 사업보고서를 받아 문단으로 나누고, `nomic-embed-text`로 임베딩해 Qdrant `evidence_passages`에 넣는다.
앱 이미지 안에서 같은 설정(Qdrant·Ollama 주소)으로 돌리고, OpenDART 키 파일만 이 실행에 읽기 전용으로 붙인다. `--no-deps`로 이미 떠 있는 서비스를 그대로 쓴다. JEV 키는 쓰지 않으므로 `compose.features.yml`은 겹치지 않는다.

```sh
# 시드 15개사(삼성전자·SK하이닉스·NAVER 등, app/services/graph_service.py의 _COMPANIES) 전체
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -p lumina-portfolio \
  run --rm --no-deps -v "$HOME/.config/opendart/api_key:/root/.config/opendart/api_key:ro" \
  app python -m app.services.evidence.load_passages

# 일부만: 종목코드(6자리) 또는 DART 고유번호(8자리), 여러 번 줄 수 있다
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -p lumina-portfolio \
  run --rm --no-deps -v "$HOME/.config/opendart/api_key:/root/.config/opendart/api_key:ro" \
  app python -m app.services.evidence.load_passages --corp 005930

# 적재 확인(키 불필요, 임베딩만 씀): 삼성전자(고유번호 00126380) 질문 하나의 상위 8개 문단 id
docker compose --env-file .env.portfolio -f compose.portfolio.yml -f compose.host-ollama.yml -p lumina-portfolio \
  run --rm --no-deps app python -m app.services.evidence.load_passages --search 00126380 "주요 제품은?"
```

- 회사별 한 줄과 끝의 `합계 N개사: 적재 …, 변경 없음 …, 실패 …` 요약이 나온다. 한 회사가 실패해도 나머지를 계속하고, 실패가 있으면 종료 코드 1이다. 다시 돌리면 바뀌지 않은 문단은 임베딩을 건너뛴다.
- 원문 XML·고유번호 목록은 `--data-dir`(기본 `data/evidence_passages/`, 컨테이너의 `/app/data`는 `app_data` volume)에 남는다. 출력·오류에서 키 문자열은 지운다.
- 적재 뒤 app을 재시작할 필요는 없다(다음 요청부터 검색된다).

### 7-4. 화면에서 확인할 것

`http://127.0.0.1:8967`에서 로그인한 뒤:

1. AI 상담 화면 입력창 위에 **공시 근거 모드** 토글과 도움말이 보인다(플래그가 꺼져 있으면 숨겨진다).
2. 토글을 처음 켜면 **공시 근거 모드 — 외부 전송 안내** 창이 뜬다. 내용을 확인하고 **확인하고 켜기**.
3. **회사 선택 (예: 삼성전자)** 칸에서 적재한 회사를 고르고 질문한다(질문마다 문단을 새로 검색하며 이전 대화 문맥은 쓰지 않는다).
4. 답변 문장마다 배지가 붙는다: ✅ 확인됨 · ⚠️ 어긋남 · ❔ 확인되지 않음 · ⊘ 판정 불가. 배지를 누르면 근거 공시 문단이 열린다.
5. 답변 요약줄의 **판단 기록** 버튼으로 판단(관망·매수 검토·제외)·확신·메모·다시 볼 날짜를 기록한다(판정이 끝나기 전에는 비활성).
6. **판단 일지** 메뉴에서 기록 목록·상세(당시 답변·배지·근거 문단 스냅샷)·이후 공시 변화·내보내기(JSON)·삭제를 확인한다.

모델이 없으면 근거 모드는 `ollama pull <모델>`을 안내하는 503을 돌려준다. 회사 목록이 비어 있으면 7-3 적재부터 확인한다.

### 7-5. 비용·외부 전송 주의

- 근거 모드 질문 하나마다 TypeSafe JEV를 호출한다. 판정 요청(state)에는 **회사 이름, AI 답변 문장(주장), 검색된 공시 문단**이 들어간다([judge.py](app/services/evidence/judge.py) `build_state`). 질문 원문은 판정 요청에 넣지 않지만, 답변 문장이 질문 내용을 되풀이할 수 있으므로 개인정보를 질문에 넣지 않는다.
- 질문과 답변 생성·임베딩은 Ollama(이 구성에선 호스트 로컬)에서 하고, 공시 원문은 OpenDART에서 받는다.
- 일일 JEV 한도는 사용자별 호출 150회·입력 토큰 75만, 전체 입력 토큰 300만이다(`EVIDENCE_DAILY_*`, KST 자정에 풀림). 한도는 호출량을 제한할 뿐 비용을 보증하지 않는다.
- 판단 일지의 메모는 JEV·LLM·알림으로 나가지 않고 감사 로그에도 남지 않는다([journal.py](app/routes/journal.py)). 일지는 JEV를 새로 부르지 않고 당시 판정을 보여 준다.
- 이 기능의 배지는 검색된 문단 기준 AI 판정이며 사실 여부를 보증하지 않는다. 투자 권유가 아니다.

이 절은 AI(Claude Code)가 코드를 읽고 작성했다. 클라우드 세션에서 `docker compose … config`로 세 파일 병합과 `TYPESAFE_KEY_FILE` 누락 시 실패 메시지만 확인했고(더미 `.env.portfolio`·더미 키 파일 사용), 스택 기동·문단 적재·JEV 호출은 하지 않았다. 대표 화면은 별도 로컬 실행에서 위와 같은 구성(세 파일에 해당하는 설정)으로 얻었다.
