# lumina-invest 작업 규칙

강사 레포(edumgt/lumina-invest)를 부품 상자로 삼아 조립하는 투자 리서치 포트폴리오다. 핵심은 TypeSafe JEV 활용이다. 이 레포는 public이다.

## Git
- `main`에 직접 commit·push하지 않는다. `<type>/<short>` 브랜치 → PR로 올린다. force push 금지.
- 커밋은 Conventional(feat/fix/docs/refactor/chore) + 한국어 본문, PR 본문도 한국어.
- `.env`·키·토큰을 커밋하지 않는다. `.env.dev`·`.env.prod`는 강사 원본 placeholder이니 건드리지 않는다.

## 실험(lab/) 신뢰 장치
- 사전등록·원장이 이 포트폴리오의 신뢰 장치다. `lab/evidence/prereg*.json`, `lab/evidence/attempts.jsonl`, `lab/evidence/results/`, `docs/lab/*-report.md`는 해시를 맞추거나 결과를 바꾸려고 고치지 않는다.
- 홀드아웃은 1회 실행으로 끝났다. 다시 열지 않는다. 코드가 바뀐 뒤 HEAD에서 `stage1-report`가 `mismatch`로 거부되는 것은 정상이다(재현은 커밋 `5f228fe`에서).
- JEV 출력으로 모델을 학습·증류하지 않는다. 공개 문서에 TypeSafe 가격·금액을 쓰지 않는다.
- AI가 만든 라벨·문서는 AI가 만들었다고 밝힌다.

## 어디서 무엇을 하나
- 실제 실행 스택은 compose.portfolio.yml(Celery 워커 없음). 실행 환경 전제는 PORTFOLIO_LOCAL.md를 먼저 확인한다.
- `lab/data/`(DART 원문·문단·벡터·JEV 호출 기록)는 gitignore라 클론에 없다. TypeSafe·OpenDART 키와 Ollama도 로컬에만 있다.
- 그래서 JEV 실호출, DART 수집, Ollama 생성·임베딩, 평가 재실행은 로컬에서만 한다. 클라우드 세션은 설치·호출을 시도하지 말고 코드·문서·단위 테스트까지만 한다.
- 근거 판정 단위 테스트(외부 호출 없음):
  `uv run -q --no-project --python 3.12 --with numpy==2.5.3 --with httpx==0.28.1 --with scikit-learn==1.9.1 --with scipy==1.18.1 --with pydantic-settings --with pytest --with fastapi==0.142.2 --with "sqlalchemy[asyncio]==2.1.2" --with asyncpg==0.31.0 --with alembic==1.20.0 --with "python-jose[cryptography]==3.5.0" --with redis==8.1.0 --with qdrant-client==1.19.1 python -m pytest tests/evidence -p no:cacheprovider`
- 저장·API 테스트(P2)는 빈 PostgreSQL이 필요하다. `EVIDENCE_TEST_DATABASE_URL=postgresql+asyncpg://…/빈DB`를 주면 마이그레이션 0001→head를 적용하고 돈다. 없으면 그 테스트만 건너뛴다(운영·compose DB를 가리키지 않는다 — 표를 비운다).
- DB 테스트는 EVIDENCE_TEST_DATABASE_URL 없으면 스킵된다. 완료 보고에는 skip 수를 함께 적고, 클라우드에서는 docker로 postgres:16-alpine을 띄워 돌린다.
- 완료 전 저장소 전체 테스트도 돌린다: `pip install -r requirements.txt -r requirements-dev.txt` 후 `python -m pytest -p no:cacheprovider -o addopts='' -q`(Linux/클라우드에서 가능, macOS uv에서는 lightgbm 때문에 앱 이미지 사용). 보고에 passed/failed/skipped 수를 적는다.
- 버그 수정·기능은 실패하는 테스트를 먼저 쓰고(RED 출력 확인) 고친다(GREEN).

## 참고
- 검증 레시피: `.claude/skills/verify/SKILL.md`
- 설계·계획: `docs/superpowers/specs/`, `docs/superpowers/plans/`, 결과 리포트: `docs/lab/`
