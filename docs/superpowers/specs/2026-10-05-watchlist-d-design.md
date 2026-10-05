# Lumina 관심종목과 지표 출처·시점 — 설계 (모듈 D)

- 작성일: 2026-10-05
- 상태: 초안 1판. 코드 변경 없음. 구현은 이 문서를 리드가 검토한 다음 계획 문서(`docs/superpowers/plans/`)에서 단계별로 한다.
- 작성: AI(Claude Code)가 작성한 설계 초안이다. 결정권자는 노아다. **1~7의 골격 결정(저장 방식·열·연결·매핑·출처 표시·범위·단계)은 리드(노아)가 내린 결정을 그대로 옮겼고**, 세부(검증 규칙·오류 코드·화면 위치 등)는 AI가 기존 코드 패턴에 맞춰 채웠다. 리드 결정과 코드 사실이 어긋나거나 반론 근거가 있는 곳은 결정을 바꾸지 않고 12절 열린 질문으로 남겼다.
- 선행 문서:
  - 판단 일지 설계 [`2026-10-04-judgment-journal-c-design.md`](2026-10-04-judgment-journal-c-design.md)(이하 "C spec") — 메모 비전송 원칙(결정 7-4), 메모 없는 422(결정 5-5의 7), 관리자 초기화(결정 5-7)
  - A-2 설계 [`2026-10-02-evidence-chat-a2-design.md`](2026-10-02-evidence-chat-a2-design.md) — 근거 모드 회사 선택
- 이 모듈은 TypeSafe의 JEV를 새로 부르지 않는다. 이 문서에는 TypeSafe 가격·금액을 쓰지 않는다.

각 결정은 **추천안 · 근거 · 틀렸을 때 비용** 순서로 적는다.

## 1. 목적과 범위

README의 제품 목표 흐름은 "관심종목 → 기업 자료(출처·시점) → AI 설명과 근거 링크 → 내 판단 노트 → 재방문"이다(`readme.md:123,206` "영속 관심종목, 자료 출처·시점"이 다음 개발로 남아 있다). 지금 상태:

| 칸 | 상태 |
|---|---|
| 관심종목 | **없음.** 기업 화면의 종목 선택은 새로 고치면 사라지는 메모리 목록이다(2.2) |
| 기업 자료(출처·시점) | 기업 지표 화면은 있으나 **출처·조회 시각 표시가 없다**(2.1) |
| AI 설명과 근거 링크 | 근거 모드(A-2) |
| 내 판단 노트 | 판단 일지(C) |
| 재방문 | 일지의 다시 볼 날짜·due 필터·배너(C) |

모듈 D는 앞 두 칸을 채우고, 관심종목 한 줄에서 뒤 칸(근거 모드·일지)으로 건너가는 연결을 만든다. 새 판정·새 모델 출력·새 외부 서비스는 없다.

## 2. 출발점: 확인한 사실 (코드에서 직접 확인)

### 2.1 기업 지표(`get_fundamentals`)

| 사실 | 위치 |
|---|---|
| `async def get_fundamentals(symbol: str) -> dict` — Yahoo quoteSummary v10(crumb·쿠키) 한 번 | `app/services/stock.py:126,139` |
| 캐시 키 `fundamentals:{symbol}`, `cache_get(..., max_age_hours=6)` / `cache_set` | `app/services/stock.py:133-136,210` |
| 캐시 저장소는 **PostgreSQL `data_cache` 표**(JSONB `data`, `updated_at`)다. Redis·메모리가 아니다. `cache_set`은 upsert로 `updated_at=now()`를 쓴다 | `app/services/data_cache.py:32-68`, `app/models/reference.py:90-97` |
| `cache_info(key)`가 `{updated_at, age_minutes}`를 주지만 지표 경로에서 부르지 않는다 | `app/services/data_cache.py:71-89` |
| 오류 응답은 캐시하지 않는다(조기 반환이 `cache_set` 앞) → 캐시 행의 `updated_at`은 곧 Yahoo에서 마지막으로 성공적으로 받은 시각이다 | `app/services/stock.py:155,158` |
| 응답 키: `symbol, name, price, chg, cap, per, pbr, eps, bps, roe, roa, debt, div, divYield, opMargin, revenue[], op[], net[], quarters[], assets, equity, liabilities, cash`. **출처·조회 시각 칸이 없다** | `app/services/stock.py:185-209` |
| **분기 실적은 분기 끝 날짜를 갖고 있지 않다.** Yahoo `endDate`를 읽어 `"24Q1"` 같은 라벨만 만들고 날짜는 버린다 | `app/services/stock.py:174-177` |
| 경로 `GET /api/stocks/fundamentals?symbol=` — **인증 의존성 없음**, Yahoo 오류는 502 | `app/routes/stocks.py:29,138-144` |

### 2.2 기업 화면(`public/js/company.js`)

| 사실 | 위치 |
|---|---|
| 종목 탭 목록 `dashboardStocks`는 목업 회사 5개(`COMPANIES`)에서 `NNNNNN.KS`로 만든 **모듈 변수**다. localStorage에도 저장하지 않는다 → 새로 고치면 추가한 종목이 사라진다 | `public/js/company.js:6-12,55-56` |
| `addAndSelectCompany(symbol, name)`은 목록에 없으면 넣고 고른다. **export·`window` 노출이 없다** | `public/js/company.js:82-87` |
| 브라우저 쪽 지표 캐시 `companyFundCache = {}`(세션 동안 유지) | `public/js/company.js:57` |
| 지표 카드는 `renderCompanyData(d)`가 `#co-overview`·`#co-valuation`·`#co-profitability`·`#co-quarterly`·`#co-balance`에 그린다 | `public/js/company.js:109-179`, `public/app.html:1117-1155` |
| 종목 검색 모달은 `/api/stocks/search?q=` 결과 `{symbol, name, exchange, type}`를 준다. 국내 종목 심볼은 `f"{code}.{suffix}"`(`.KS`/`.KQ`) | `public/js/company.js:226-314`, `app/services/krx_companies.py:59` |
| 기업 화면은 GNB `company`("투자 인디케이터")의 LNB `company-dashboard` 뷰다 | `public/js/core.js:68-80`, `public/app.html:1117` |
| 쓰는 localStorage 키는 안내 접힘, 비교 트레이, 로보 성향뿐이다 | `public/js/core.js:271,291,303,306`, `public/js/robo.js:156,165` |

### 2.3 근거 모드·일지 연결점

| 사실 | 위치 |
|---|---|
| 근거 모드 라우터 전체가 `EVIDENCE_CHAT_ENABLED`로 막힌다(꺼지면 404) | `app/routes/evidence.py:61-66` |
| `GET /api/evidence/companies?q=` → `{"companies": [{corp_code, corp_name, stock_code, rcept_no, passages}]}`. **적재된 회사만** 나온다(필드명은 `name`이 아니라 `corp_name`) | `app/routes/evidence.py:234-262`, `app/services/evidence/store.py:131-139` |
| 이 경로는 KRX 검색 결과의 심볼에서 접미사를 떼어 `stock_code`로 맞춘다(`h["symbol"].split(".")[0]`) | `app/routes/evidence.py:258-262` |
| 문단 저장소는 앱 시작 시 `EVIDENCE_CHAT_ENABLED`일 때만 연결된다. `get_company_list()`는 연결이 없으면 None | `app/main.py:57`, `app/routes/evidence.py:71-89` |
| `prefillEvidenceChat({corp_name, corp_code}, question)`: 근거 모드 가용성을 확인하고(아니면 false) 회사·질문을 채운다. 보내지 않는다. 고지 미확인이면 고지 대화상자 | `public/js/evidence.js:589-605` |
| 일지의 "같은 질문 다시 묻기"가 `navigate("agent-chat")` 뒤 `prefillEvidenceChat`을 부르는 선례 | `public/js/journal.js:721-727` |
| `GET /api/journal` 쿼리 `due, corp_code, decision, limit(1~200), offset` → `{items, total, due_count, limit, offset}`. **`total`은 거르기 뒤 개수, `due_count`는 거르기와 무관한 사용자 전체 due 수**다 | `app/routes/journal.py:233-269` |
| 일지 회사 거르기 상태 `jr.filters.corp`를 밖에서 정하는 export 함수가 없다 | `public/js/journal.js:179,412-425,739` |
| **기능 켜짐을 알려 주는 API(`/api/config` 등)는 없다.** 화면은 API를 찔러 404면 꺼짐으로 본다: 근거 모드 `/api/evidence/notice`, 일지 `/api/journal?limit=1` | `public/js/evidence.js:427-436`, `public/js/journal.js:192-219` |

### 2.4 저장·인증·운영

| 사실 | 위치 |
|---|---|
| 마이그레이션 이름은 `"000N"`, 마지막은 `revision="0010"`, `down_revision="0009"` | `alembic/versions/0010_judgment_journal.py:16-17` |
| 모델 믹스인 `Base`·`UUIDPkMixin`·`CreatedAtMixin` | `app/models/base.py` |
| 인증 `get_current_user_any`(Bearer JWT → `fin_session` 쿠키, 아니면 401) | `app/lib/jwt_auth.py:131-151` |
| 일지는 본문을 직접 읽어 검사하고 422에 `loc`·`msg`만 담는다(입력값 없음). 중복은 `409 {"detail", "entry_id"}` | `app/routes/journal.py:90-104,198-199` |
| 관리자 `USER_MODELS`(초기화·행 수 집계) | `app/routes/admin.py:18,32,47` |
| DB 테스트 `pg` 픽스처가 테스트마다 비우는 표 목록 `_TABLES` | `tests/evidence/conftest.py:69-120` |
| 실스택의 app 컨테이너는 시작할 때 자체 PostgreSQL migration을 실행한다 | `PORTFOLIO_LOCAL.md:74` |
| 요청 본문을 기록하는 미들웨어는 없다 | `app/main.py` 검색 결과 |
| 화면 고지 선례 "교육용 과거 검증이며 투자 권유가 아닙니다." | `public/app.html:710` |
| 관심종목·즐겨찾기 기능은 없다 | `app/`·`public/` 검색 결과(`watchlist`, `favorite`, `관심`, `bookmark`) 없음 |

## 3. 사용자 시나리오와 화면 상태

### 3.1 시나리오

1. 사용자가 **투자 인디케이터 → 기업 지표**(`company-dashboard`)를 연다. 맨 위에 **관심종목** 패널이 있다(비어 있으면 안내 문구).
2. `종목 검색`으로 "삼성전자"를 찾아 고르면 지표 카드가 뜬다. 개요 카드 머리의 `☆ 관심종목` 버튼을 누르면 `★ 관심종목`으로 바뀌고 패널에 한 줄이 생긴다.
3. 패널 한 줄: `삼성전자 · 005930.KS · KOSPI · (메모)` 와 연결 버튼들.
   - `지표 보기` — 같은 화면에서 이 종목을 고른다(a).
   - `근거 모드로 질문` — 근거 모드가 켜져 있고 `corp_code`가 있을 때만 보인다. 누르면 에이전트 채팅으로 가서 근거 모드 회사가 미리 선택된다. 질문 칸은 비어 있고 보내기는 사용자가 누른다(b).
   - `판단 기록 3 · 다시 볼 때 1` — 일지가 켜져 있고 `corp_code`가 있을 때만 보인다. 누르면 일지 탭이 이 회사로 걸러진 채 열린다(c).
   - `메모`(편집), `삭제`.
4. 지표 카드 아래에 `출처: Yahoo Finance · 조회 2026-10-05 14:32(KST) · 최대 6시간 지난 값일 수 있음`과 투자 권유 아님 문구가 보인다(결정 6-2).
5. 다음 날 다른 기기에서 로그인해도 같은 관심종목이 보인다(서버 저장).

### 3.2 화면 상태

| 화면 | 상태 | 표시 | 진입 조건 |
|---|---|---|---|
| 관심종목 패널 | 불러오는 중 | "관심종목을 불러오는 중…" | 뷰 열기 직후 |
| 관심종목 패널 | 비어 있음 | "관심종목이 없습니다. 종목을 검색해 ☆를 누르면 여기에 모입니다." | 0건 |
| 관심종목 패널 | 목록 | 3.1-3의 줄 | 1건 이상 |
| 관심종목 패널 | 불러오기 실패 | "관심종목을 불러오지 못했습니다" + `다시 시도`. 지표 화면 나머지는 그대로 동작 | 5xx·네트워크 |
| 관심종목 패널 | 로그인 안 됨 | 패널 숨김(별 버튼도 숨김) | 401 |
| 별 버튼 | 추가 전 / 추가됨 | `☆ 관심종목` / `★ 관심종목` | 현재 종목이 목록에 없음 / 있음 |
| 별 버튼 | 상한 | 토스트 "관심종목은 100개까지 담을 수 있습니다" | 409 `limit` |
| 별 버튼 | 이미 있음 | 조용히 `★`로 바꾼다(다른 탭에서 먼저 추가한 경우) | 409 `duplicate` |
| 연결 (b) | 숨김 | 버튼 없음 | 근거 모드 꺼짐(`/api/evidence/notice` 404) 또는 `corp_code` null |
| 연결 (c) | 숨김 | 버튼 없음 | 일지 꺼짐(`/api/journal?limit=1` 404) 또는 `corp_code` null |
| 연결 (c) | 기록 0건 | `판단 기록 0` (누르면 빈 거르기 목록) | 켜짐, corp_code 있음, 기록 없음 |
| 연결 (c) | 개수 조회 실패 | `판단 기록 보기`(숫자 없이) | 개수 요청 실패 |
| 지표 출처 줄 | 정상 | 결정 6-2 문구 | `fetched_at` 있음 |
| 지표 출처 줄 | 시각 모름 | `출처: Yahoo Finance · 조회 시각 확인 불가` | `fetched_at` null(결정 6-1의 예외 경로) |

## 4. 데이터 모델

### 결정 4-1. 관심종목은 서버에 사용자별로 저장한다 (리드 결정)

- **추천:** 새 표 `watchlist_items`, Alembic **`0011_watchlist`**(`revision="0011"`, `down_revision="0010"`). 모델은 `app/models/watchlist.py`의 `WatchlistItem(Base, UUIDPkMixin, CreatedAtMixin)`, `app/models/__init__.py`에서 다시 내보낸다.

| 열 | 형 | 설명 |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK → `users.id` | 소유자 |
| `symbol` | 문자열(20), **NOT NULL** | Yahoo 심볼(예: `005930.KS`, `AAPL`). 저장 전 대문자로 정규화 |
| `name` | 문자열(100), null | 표시 이름(검색 결과의 이름). 없으면 화면은 심볼을 보인다 |
| `corp_code` | 문자열(8), null | DART 고유번호. 국내 상장사이고 근거 모드 적재 회사와 맞을 때만(결정 4-3) |
| `market` | 문자열(16), null | `KOSPI`/`KOSDAQ`/기타(결정 5-2) |
| `note` | 문자열(200), null | 짧은 메모. 밖으로 나가지 않는다(결정 8-1) |
| `created_at` | 시각 | |

- 유일: `(user_id, symbol)` — 제약 이름 `uq_watchlist_user_symbol`.
- 색인: `(user_id, created_at)`(목록 정렬). 사용자당 100행 이하라 다른 색인은 두지 않는다.
- `updated_at`은 두지 않는다. 메모 수정 시각을 보여 줄 화면이 없다.
- **근거:** 목표 흐름의 "재방문"은 다른 날·다른 기기에서 같은 목록을 보는 것이다. 지금 목록은 새로 고치면 사라진다(2.2). localStorage는 기기마다 따로이고 판단 일지(서버 저장)와 짝이 맞지 않는다.
- **틀렸을 때 비용:** 표 하나와 마이그레이션 하나가 는다. `downgrade`는 이 표만 지운다. 기존 표는 바뀌지 않는다.

### 결정 4-2. 사용자당 100개 상한

- **추천:** 추가 시 그 사용자의 행 수가 100 이상이면 409 `limit`. 상한은 설정이 아니라 라우터 상수 `MAX_ITEMS = 100`.
- **근거:** 목록 API가 한 번에 전부 돌려주고(페이지 없음), 화면이 줄마다 일지 개수를 물을 수 있어(결정 7-3) 행 수에 상한이 있어야 비용이 묶인다. 개인 리서치 목록으로 100개면 넉넉하다.
- **틀렸을 때 비용:** 100개 넘게 쓰려는 사용자가 정리해야 한다. 상수 하나를 바꾸면 늘릴 수 있다. 동시 추가로 101개가 되는 경쟁 상태는 막지 않는다(행 수 확인과 삽입 사이) — 피해가 한두 행이라 잠금을 두지 않는다.

### 결정 4-3. corp_code 매핑: Yahoo → stock_code → 근거 모드 기업 목록 (리드 결정)

- **추천:**
  1. 심볼이 `^\d{6}\.(KS|KQ)$`이면 앞 6자리를 `stock_code`로 쓴다. 아니면(해외·지수·ETF 형식 밖) `corp_code = null`.
  2. 근거 모드 문단 저장소의 회사 목록(`get_company_list()` → `PassageStore.companies()`)에서 `stock_code`가 같은 항목의 `corp_code`를 쓴다. 정확히 하나가 맞을 때만 쓰고, 0개·2개 이상이면 null.
  3. 저장소가 없거나(근거 모드 꺼짐) 예외를 내면 null. 매핑 실패는 추가를 막지 않는다.
  4. **지어내지 않는다:** 이름 유사도, KRX 목록 추정, OpenDART 조회로 채우지 않는다. 클라이언트가 보낸 `corp_code`는 받지 않는다(요청 본문에 칸이 없다).
- **나중에 채우기(AI 세부):** 저장소는 근거 모드가 켜져 있을 때만 연결되므로(2.3), 근거 모드가 꺼진 동안 추가한 국내 종목은 null로 남는다. 목록 API는 `corp_code`가 null인 `.KS/.KQ` 행이 하나라도 있고 저장소가 연결돼 있으면 `companies()`를 **한 번** 불러 맞는 행을 채우고 DB에 쓴다. 한 번 채운 값은 지우지 않는다(적재 회사가 빠져도 그대로 — 그 경우 (b)를 누르면 근거 모드가 "적재되지 않은 회사"로 답한다).
- **근거:** 근거 모드와 일지는 모두 `corp_code`로 회사를 가리킨다. 근거 모드가 답할 수 있는 회사는 문단이 적재된 회사뿐이므로, "적재 목록에 있는가"가 곧 (b) 버튼을 보일 조건이다. 국내 상장사라도 적재되지 않았으면 null이 맞다. `companies()`는 Qdrant 전체 scroll이라(C spec 6-1) 부르는 때를 묶는다: 추가는 `.KS/.KQ` 심볼일 때만 요청당 1회(상한 100개로 묶임), 목록은 null인 국내 행이 있고 저장소가 연결돼 있을 때만 1회. 이 경우 `GET /api/watchlist`가 DB에 쓰는 부수 효과가 있다 — 의도한 것이다(채울 값이 결정적이고 지어낸 값이 아니며, 따로 "다시 매핑" 경로를 두는 것보다 단순하다).
- **틀렸을 때 비용:** 적재 회사가 늘어나도 이미 채워진 행은 그대로라 문제없고, null 행은 다음 목록 조회에서 채워진다. 우선주(`005935.KS`)처럼 같은 회사의 다른 종목은 `stock_code`가 달라 null이 된다 — 지어내지 않는 쪽을 택한다.

## 5. API 계약

라우터 `app/routes/watchlist.py`, `APIRouter(prefix="/api/watchlist", tags=["watchlist"])`. `main.py`의 지역 import 패턴으로 붙인다(`journal` 다음). 모든 경로 `get_current_user_any`. 기능 플래그는 없다(결정 5-5).

### 결정 5-1. 경로 네 개

| 메서드·경로 | 동작 |
|---|---|
| `GET /api/watchlist` | 내 관심종목 전부, `created_at` 오름차순(추가한 순서). 결정 4-3의 나중에 채우기를 여기서 한다 |
| `POST /api/watchlist` | 추가. 201과 항목 |
| `PATCH /api/watchlist/{id}` | **메모만** 고친다(AI 추가 — 12절 Q3) |
| `DELETE /api/watchlist/{id}` | 삭제. 204 |

- 남의 항목·없는 id·UUID 형식이 아닌 id는 모두 404 "관심종목을 찾을 수 없습니다."(존재 여부를 드러내지 않는다, `_uuid_or_404`·`_owned_entry`와 같은 방식).
- 목록 API는 Yahoo를 부르지 않는다(가격·등락을 담지 않는다 — 결정 9 범위 밖 "실시간 시세").

**`GET /api/watchlist` 응답**

```json
{
  "items": [
    {"id": "6f0c…", "symbol": "005930.KS", "name": "삼성전자", "corp_code": "00126380",
     "market": "KOSPI", "note": "HBM 고객 다변화 확인", "created_at": "2026-10-05T05:31:00Z"},
    {"id": "a12e…", "symbol": "AAPL", "name": "Apple Inc.", "corp_code": null,
     "market": "NASDAQ", "note": null, "created_at": "2026-10-05T05:40:12Z"}
  ],
  "limit": 100
}
```

**`POST /api/watchlist` 요청·응답**

```json
// 요청
{"symbol": "005930.ks", "name": "삼성전자", "exchange": "KSC", "note": ""}
// 201
{"id": "6f0c…", "symbol": "005930.KS", "name": "삼성전자", "corp_code": "00126380",
 "market": "KOSPI", "note": null, "created_at": "2026-10-05T05:31:00Z"}
// 409 중복
{"detail": "이미 관심종목에 있습니다.", "code": "duplicate", "item_id": "6f0c…"}
// 409 상한
{"detail": "관심종목은 100개까지 담을 수 있습니다.", "code": "limit", "limit": 100}
// 422 (입력값을 되돌려 주지 않는다)
{"detail": [{"loc": ["body", "note"], "msg": "메모는 200자 이하입니다."}]}
```

**`PATCH /api/watchlist/{id}`** 요청 `{"note": "…"}` → 200과 항목. 빈 문자열은 null로 저장.

### 결정 5-2. 입력 검증

- 본문은 일지의 `_parse` 방식으로 직접 읽어 pydantic `model_validate`로 검사하고, 실패하면 `loc`·`msg`만 담은 422를 돌려준다(`input` 없음). 메모를 담는 경로라서다(C spec 결정 5-5의 7). 공용 헬퍼로 옮길지는 P1 계획에서 정한다(옮기면 일지 응답이 바뀌지 않음을 기존 테스트로 확인).
- `symbol`: 앞뒤 공백 제거·대문자화 뒤 `^[A-Z0-9][A-Z0-9.\-^=]{0,19}$`. 존재 여부는 확인하지 않는다(추가 때 Yahoo를 부르지 않는다 — 별 버튼은 이미 지표가 뜬 종목에서 누르므로 실재하는 심볼이 들어온다).
- `name`: 선택, 100자 이하, 앞뒤 공백 제거, 빈 문자열은 null.
- `exchange`: 선택, 검색 결과의 거래소 문자열(32자 이하). 저장하지 않고 `market` 계산에만 쓴다.
- `market` 계산(서버): `.KS` → `KOSPI`, `.KQ` → `KOSDAQ`, 그 밖에는 `exchange`를 16자로 자른 값, 없으면 null. 클라이언트가 `market`을 직접 보내지 않는다.
- `note`: 선택, 200자 이하, 빈 문자열은 null. 줄바꿈은 그대로 둔다.
- 알 수 없는 칸은 무시한다(일지와 같음). `corp_code`를 보내도 쓰지 않는다(결정 4-3).

### 결정 5-3. 중복은 409(기존 id 포함), 멱등 처리는 화면이 한다

- **추천:** 같은 `(user_id, symbol)`이 있으면 `409 {"code": "duplicate", "item_id"}`. 삽입 경쟁으로 유일 제약 위반이 나도 제약 이름(`uq_watchlist_user_symbol`)을 보고 같은 409로 바꾼다(일지 `_constraint` 방식 — 예외 문자열은 메모 파라미터를 담을 수 있어 보지 않는다).
- **근거:** 일지의 "이미 기록 있음"(`409 {"detail", "entry_id"}`)과 같은 모양이다. 200 멱등으로 하면 기존 메모가 다를 때 어느 쪽이 남았는지 응답만으로 알 수 없다. 화면은 409 `duplicate`를 "이미 있음"으로 받아 별을 채우기만 하므로 사용자에게는 멱등으로 보인다.
- **틀렸을 때 비용:** 클라이언트가 409 한 갈래를 더 처리한다.

### 결정 5-4. 감사 로그는 남기지 않는다

- **추천:** 관심종목 추가·삭제·메모 수정에 `audit()`를 부르지 않는다.
- **근거:** 감사 로그 `payload`는 관리자에게 그대로 보인다(C spec 2.3). 종목 목록 자체가 개인의 투자 관심이고, 남길 운영상 이유(보안 사건)가 없다. 일지는 삭제·내보내기 같은 되돌릴 수 없는 동작이 있어 개수만 남겼지만, 관심종목은 그런 동작이 없다.
- **틀렸을 때 비용:** "누가 언제 관심종목을 지웠나"를 알 수 없다. 필요해지면 `{count}`만 남기는 사건을 더한다.

### 결정 5-5. 기능 플래그를 새로 만들지 않는다 (리드 결정)

- **추천:** 관심종목은 기본 기능이다. `WATCHLIST_ENABLED` 같은 설정을 두지 않는다. 라우터는 항상 붙는다.
- **근거(리드 결정에 AI가 더한 사실):** 일지가 플래그를 둔 이유 중 하나는 0010 미적용 DB에서 판정 API가 깨지지 않게 하는 것이었다(C spec 결정 5-6). 관심종목 표는 다른 API가 조회하지 않으므로 0011이 없어도 관심종목 경로만 실패하고, 실스택 app 컨테이너는 시작할 때 마이그레이션을 실행한다(`PORTFOLIO_LOCAL.md:74`). 근거 모드·일지처럼 외부 판정이나 개인 기록 삭제권 문제도 없다.
- **틀렸을 때 비용:** 0011을 적용하지 않은 환경(수동 실행)에서 패널이 "불러오지 못했습니다"를 띄운다(3.2) — 지표 화면 나머지는 동작한다. 반론은 12절 Q1.

### 결정 5-6. 관리자 초기화·테스트 표 목록에 넣는다

- **추천:** `app/routes/admin.py`의 `USER_MODELS`에 `WatchlistItem`을 더한다(부모 `users`보다 앞, 자식 없음). `tests/evidence/conftest.py`의 `_TABLES`에 `watchlist_items`를 더한다.
- **근거:** 관리자 초기화가 사용자 자료를 비우는데 관심종목 메모만 남으면 의미가 어긋난다(C spec 결정 5-7과 같은 이유). `/api/admin/stats`에는 행 수만 나온다. `_TABLES`에 없으면 DB 테스트끼리 행이 섞인다.
- **틀렸을 때 비용:** 시연 DB 초기화 때 노아의 관심종목도 지워진다(100개 이하라 다시 담으면 된다).

## 6. 지표 출처·시점

### 결정 6-1. `get_fundamentals` 응답에 `source`·`fetched_at`을 더한다 (리드 결정)

- **추천:**
  - Yahoo에서 받아 응답을 만든 직후, `cache_set` **전에** 응답 dict에 `"source": "Yahoo Finance"`, `"fetched_at": "<UTC ISO 8601>"`을 넣는다. 캐시에서 읽을 때는 저장된 값을 그대로 돌려주므로 처음 받은 시각이 유지된다.
  - **옛 캐시 행(필드 없음) 처리(AI 세부):** 캐시에서 읽은 dict에 `fetched_at`이 없으면 캐시 미스로 보고 Yahoo에서 다시 받는다. `cache_info()`의 `updated_at`으로 채우는 방법도 있지만(오류는 캐시하지 않으므로 그 값이 곧 받은 시각이다, 2.1), 배포 직후 최대 6시간 동안만 생기는 경우를 위해 조회 경로를 하나 더 두지 않는다.
  - 오류 응답(`{"symbol", "error"}`)에는 넣지 않는다(502로 바뀌므로 화면에 지표가 없다).
  - Yahoo 요청이 실패하면 지금처럼 오류다. 6시간 지난 캐시를 "옛 값"으로 대신 보여 주는 경로는 만들지 않는다(지금도 없다).
- **분기 끝 날짜(AI 세부, 리드 전제 정정):** 리드 전제는 "분기 실적은 이미 분기 끝 날짜가 있음"이었으나 실제로는 라벨(`"24Q1"`)만 있다(2.1). 추천은 같은 반복문에서 버리던 `endDate`를 `quarter_ends: ["2025-12-31", …]`(`quarters`와 같은 순서·길이, 없으면 `""`)로 함께 돌려주고, 분기 실적 표 머리 칸 툴팁에 `분기 끝 2025-12-31`을 보이는 것이다. 외부 호출이 늘지 않는다. 리드가 라벨만으로 충분하다고 보면 이 칸은 빼도 된다(12절 Q2).
- **근거:** 지표 숫자가 어디서 온 언제 값인지 모르면 사용자는 실시간 값으로 오해한다. 캐시가 6시간이라 "조회 시각"을 화면이 읽은 시각으로 쓰면 최대 6시간을 속인다 — 그래서 Yahoo에서 받은 시각을 데이터와 함께 저장한다.
- **틀렸을 때 비용:** 배포 직후 6시간 안에 열린 종목은 Yahoo를 한 번 더 부른다(종목당 1회). 응답 키가 두세 개 는다(기존 화면은 모르는 키를 무시한다).

### 결정 6-2. 화면 출처 줄과 고지

- **추천:** 지표 영역 맨 아래(재무 건전성 카드 뒤)에 한 줄:
  > 출처: Yahoo Finance · 조회 2026-10-05 14:32(KST) · 최대 6시간 지난 값일 수 있음

  바로 아래에 기존 고지 형식(`public/app.html:710`, `text-xs`·`--text-mute`)을 따른 문구:
  > 지표는 외부 시세 제공처의 값을 그대로 보여 주며 투자 권유가 아닙니다. 투자 판단과 그 결과는 본인에게 있습니다.

  - 시각은 화면에서 KST로 바꿔 `YYYY-MM-DD HH:mm`으로 쓴다(`journal.js`의 `kstDate`와 같은 방식으로 `Asia/Seoul` 변환, 분 단위). `fetched_at`이 없으면 "조회 시각 확인 불가"(3.2).
  - 관심종목 패널에도 고지 한 줄을 접지 않고 둔다: "관심종목은 내가 고른 목록입니다. 이 서비스는 종목을 추천하지 않습니다."
  - 브라우저 캐시 `companyFundCache`(2.2)는 그대로 두되, 출처 줄은 캐시된 응답의 `fetched_at`을 쓰므로 표시 시각은 실제 받은 시각과 어긋나지 않는다. 다만 탭을 오래 열어 두면 6시간보다 오래된 값이 보일 수 있다(12절 Q4).
- **근거:** 리드가 정한 문구 그대로다. "최대 6시간"은 서버 캐시 TTL(2.1)에서 나온 숫자라, TTL을 바꾸면 이 문구도 함께 바꾼다(P3 계획에 상수 하나로 묶는다).
- **틀렸을 때 비용:** 카드 아래 두 줄을 쓴다.

## 7. 화면 흐름과 연결

### 결정 7-1. 관심종목 패널은 기업 지표 화면 맨 위에 둔다

- **추천:** `company-dashboard` 뷰의 "종목 선택" 카드 위에 관심종목 카드를 새로 둔다. 별 버튼은 `#co-overview` 위(종목 이름 줄)에 둔다. 기존 목업 종목 탭(`COMPANIES` 5개)은 그대로 둔다(바꾸면 비교·섹터 화면의 목업 데이터와 어긋난다, 12절 Q5).
- **근거:** 관심종목의 첫 동작이 "지표 보기"이고, 별 버튼은 지표를 본 직후 누르는 자리라 같은 뷰에 있어야 왕복이 없다. 새 GNB 탭을 만들면 내비게이션 구조(`core.js:6-115`)를 넓혀야 한다.
- **틀렸을 때 비용:** 에이전트 채팅·일지에서 관심종목을 바로 볼 수 없다. 필요하면 같은 패널 컴포넌트를 다른 뷰에 한 번 더 붙인다.

### 결정 7-2. 연결 버튼 세 개와 숨김 규칙 (리드 결정)

| 버튼 | 보이는 조건 | 동작 |
|---|---|---|
| (a) `지표 보기` | 항상 | `addAndSelectCompany(symbol, name)` — P2에서 이 함수를 export한다(2.2) |
| (b) `근거 모드로 질문` | 근거 모드 켜짐 **그리고** `corp_code` 있음 | `navigate("agent-chat")` 뒤 `prefillEvidenceChat({corp_name: name, corp_code}, "")`. 회사만 선택되고 질문 칸은 비운다(입력 중이던 글은 덮어쓴다 — 회사가 바뀌므로 이전 질문을 남기지 않는다). 보내지 않는다(일지 `reask` 선례, 2.3). false가 돌아오면 토스트 "지금은 근거 모드를 쓸 수 없습니다" |
| (c) `판단 기록 N · 다시 볼 때 M` | 일지 켜짐 **그리고** `corp_code` 있음 | `navigate("journal")` 뒤 회사 거르기를 이 `corp_code`로 정한다. P2에서 `journal.js`에 `openJournalForCompany(corp_code, company)`를 export한다(거르기 선택 상자에 회사가 아직 없으면 넣는다) |

- **켜짐 판단:** 설정 API가 없으므로(2.3) 기존 탐지를 그대로 쓴다. 근거 모드는 `/api/evidence/notice`, 일지는 `/api/journal?limit=1`이 404면 꺼짐. 각 모듈의 기존 탐지 함수(`evidence.js` `probe`, `journal.js` `initJournal`·`evidenceAvailable`)를 재사용하고, 패널이 새 탐지 요청을 따로 만들지 않는다. 탐지 오류(5xx)는 꺼짐과 같이 숨긴다.
- **꺼진 기능의 버튼은 그리지 않는다**(비활성 회색 버튼도 두지 않는다).
- **(c)의 개수:** 줄마다 `GET /api/journal?corp_code=X&limit=1`의 `total`(기록 수)과 `GET /api/journal?corp_code=X&due=true&limit=1`의 `total`(다시 볼 때 된 수)을 쓴다. `due_count`는 사용자 전체 값이라 쓰지 않는다(2.3). corp_code가 있는 줄만 묻고, 동시 요청은 4개로 묶는다. 같은 `corp_code` 줄은 한 번만 묻는다.
- **근거:** 리드 결정. 꺼진 기능 버튼을 보여 주면 눌러서 404 오류를 보게 된다. 기존 일지 API를 그대로 쓰면 서버 변경이 없다.
- **틀렸을 때 비용:** corp_code가 있는 종목 수 × 2 요청이 패널을 열 때마다 난다. corp_code는 근거 모드 적재 회사(수십 개 규모)에만 붙으므로 실제로는 수 개~수십 개 요청이다. 늘어나면 요약 경로를 더한다(12절 Q6).

### 결정 7-3. 별 버튼 동작

- 현재 종목이 패널 목록에 있으면 `★`, 누르면 확인 없이 삭제(실수 시 다시 누르면 된다, 메모가 있으면 확인 대화상자 "메모도 함께 지워집니다").
- 없으면 `☆`, 누르면 `POST`(검색 결과의 `name`·`exchange`를 함께 보낸다. 목업 탭 종목은 `exchange` 없이 보낸다 — `.KS`로 `market`이 정해진다).
- 요청 중에는 버튼을 비활성화한다. 실패하면 원래 상태로 돌리고 토스트.

## 8. 보안·개인정보

### 결정 8-1. 관심종목 메모는 어디로도 나가지 않는다 (리드 결정)

- **추천:** 메모(`note`)와 관심종목 목록은 JEV, Ollama(LLM·임베딩), 에이전트 채팅 프롬프트, 알림 `dispatch`, 감사 로그 `payload`, 구조화 로그에 넣지 않는다. 422 응답에도 메모를 되돌려 주지 않는다(결정 5-2). 근거 모드로 질문(b)할 때도 메모를 질문 칸에 채우지 않는다.
- **근거:** C spec 결정 7-4와 같은 원칙이다. 메모에는 보유 여부·매수 계획 같은 사적인 내용이 들어가기 쉽다.
- **틀렸을 때 비용:** 메모를 근거 모드 질문으로 바로 쓰고 싶은 사용자는 복사해 붙인다.

### 8.1 그 밖의 보안 사항

- 소유자 확인: 모든 조회·수정·삭제는 `id`와 `user_id`를 함께 걸어 찾고, 없으면 404.
- 로그: 오류 로그는 기존 패턴대로 `{"event", "error": type(exc).__name__}`만 남긴다. 심볼·메모를 로그에 쓰지 않는다.
- 화면은 `name`·`note`를 `textContent`로만 넣는다(HTML 삽입 없음 — 검색 결과 이름도 사용자가 고른 외부 문자열이다).
- 지표 경로 `GET /api/stocks/fundamentals`는 지금처럼 인증 없이 둔다(2.1). 이 모듈은 그 경로에 사용자 정보를 더하지 않으므로 공개 범위가 바뀌지 않는다.
- 계정 삭제 API는 없다(C spec 2.3). 사용자는 관심종목을 하나씩 지울 수 있다. 전체 삭제 경로는 두지 않는다(100개 이하, 12절에 열지 않음).

## 9. 범위 밖 (리드 결정)

- 가격 알림, 실시간 시세(목록 API에 가격·등락을 넣지 않는다), 포트폴리오 수익률·보유 수량.
- 외부 알림(메일·푸시·텔레그램 등).
- JEV 새 호출. 이 모듈의 어떤 경로도 JEV·Ollama·OpenDART를 부르지 않는다. `Quota` 소모 0.
- 관심종목 그룹·정렬 바꾸기·공유, 목업 종목 탭 교체(12절 Q5).
- DART 매핑의 추정 보완(이름 유사도·KRX 목록·OpenDART 고유번호 조회).
- 지표의 다른 출처(KRX·DART 재무제표)와 출처 비교.

## 10. 테스트 계획

모든 단계는 실패하는 테스트를 먼저 쓰고(RED 출력 확인) 고친다(GREEN). 완료 보고에는 저장소 전체 테스트의 passed/failed/skipped 수와 DB 테스트 skip 수를 적는다. DB 테스트는 클라우드에서 docker `postgres:16-alpine`에 `EVIDENCE_TEST_DATABASE_URL`을 주고 0001→head를 적용해 돌린다(skip 0이 목표).

| 파일 | 내용 |
|---|---|
| `tests/evidence/test_watchlist_api.py` | `p2_support.make_app`에 관심종목 라우터를 붙여(하네스 확장) DB 테스트. 아래 수용 기준 1~7 |
| `tests/evidence/test_watchlist_mapping.py` | 매핑 순수 함수 단위 테스트(DB 없음): `.KS`/`.KQ`/해외/형식 밖/적재 목록에 없음/중복 `stock_code`/저장소 None·예외 |
| `tests/test_fundamentals_source.py`(위치는 P3 계획에서 기존 stock 테스트 옆으로) | `httpx` 가짜 응답·`cache_get`/`cache_set` 가짜로 `source`·`fetched_at`·`quarter_ends`, 캐시 적중 시 시각 유지, 옛 캐시 행 → 미스 |
| `tests/e2e/watchlist_views.py` | `journal_views.JournalApi`를 이어받은 가짜 API(`/api/watchlist`, `/api/stocks/fundamentals`, `/api/journal`, `/api/evidence/notice`)로 3.2 상태 전부. 근거 모드·일지 각각 404일 때 (b)·(c) 버튼이 DOM에 없음 |
| `tests/e2e/layout_views.py` | `HASHES`에 `company-dashboard`를 더하고 `LayoutApi`가 관심종목·지표 가짜 응답을 준다. 375/768/1280 폭에서 가로 넘침 없음, 375에서 줄의 연결 버튼이 줄바꿈되어 보임 |

### 10.1 수용 기준

1. 남의 관심종목 `PATCH`·`DELETE` → 404, 행이 바뀌지 않음. 목록에 남의 항목이 섞이지 않음. UUID 아닌 id → 404.
2. 같은 심볼 두 번(대소문자만 다른 경우 포함) → 두 번째 409 `duplicate`와 첫 항목 id, 행 1개.
3. 100개 있는 사용자의 추가 → 409 `limit`, 행 100개 유지. 다른 사용자의 행 수는 상한에 세지 않음.
4. 매핑: `005930.KS`(적재됨) → corp_code, 적재된 `.KQ` 심볼 → corp_code, `AAPL` → null, `000000.KS`(적재 안 됨) → null, 저장소 None → null(추가는 201). 근거 모드가 켜진 뒤 목록 조회 → null이던 국내 행이 채워지고 `companies()`는 1회만 불림. 요청 본문의 `corp_code`는 무시.
5. 메모 201자·잘못된 심볼·필수 `symbol` 누락 → 422, 응답 본문에 보낸 메모·심볼 문자열이 없음.
6. 관심종목 경로에서 JEV·LLM 클라이언트·`notification.dispatch`·`audit()`가 불리지 않음(가짜 호출 수 0). 로그 출력에 메모 문자열이 없음.
7. 관리자 초기화가 `watchlist_items`를 비우고 `/api/admin/stats`에 행 수만 나옴. 0011 `downgrade` → 표만 사라지고 0010 상태로 돌아감(`alembic_downgrade`).
8. 지표: Yahoo 응답으로 만든 dict에 `source == "Yahoo Finance"`, `fetched_at`(UTC ISO), `len(quarter_ends) == len(quarters)`. 캐시 적중 응답의 `fetched_at`은 처음 값과 같음. `fetched_at` 없는 캐시 dict → Yahoo 재요청. 오류 응답에는 두 칸이 없음.
9. 화면: 출처 줄이 `fetched_at`을 KST `YYYY-MM-DD HH:mm`으로 보임(UTC 자정 근처 값으로 날짜 넘어감 확인), `fetched_at` 없으면 "조회 시각 확인 불가". 고지 문구 두 개가 접히지 않고 보임.
10. 화면: 근거 모드 꺼짐 → (b) 없음, 일지 꺼짐 → (c) 없음, `corp_code` null → (b)·(c) 없음. (b) 누르면 `prefillEvidenceChat`이 회사와 빈 질문으로 불리고 채팅 전송 요청이 나가지 않음. (c)의 숫자가 `corp_code`별 `total` 두 값과 같음.
11. 화면: 프런트에서 `/api/` 밖(외부) 요청이 없음(기존 e2e의 CDN 차단 방식으로 확인). 이름·메모의 `<script>` 문자열이 텍스트로만 보임.
12. 기존 `tests/evidence`와 저장소 전체 테스트가 그대로 통과(**모든 단계**의 완료 기준).

## 11. 구현 단계 (리드 결정 — 각각 PR 하나)

| 단계 | 내용 | 어디서 | 완료 기준 |
|---|---|---|---|
| P1. 모델·마이그레이션·API | `0011_watchlist`, `WatchlistItem`, `app/routes/watchlist.py`(목록·추가·메모·삭제, 메모 없는 422, 409 두 종류), 매핑 함수, `USER_MODELS`·`_TABLES`, `p2_support` 하네스 확장 | 클라우드(docker postgres) | 10.1의 1~7·12, DB 테스트 skip 0, 전체 테스트 수치 보고 |
| P2. 화면 | 관심종목 패널·별 버튼·연결 버튼(3.2 전부), `addAndSelectCompany` export, `openJournalForCompany` export, 고지 | 클라우드(e2e 가짜 응답) + 로컬 실화면 1회 | 10.1의 10·11·12, `watchlist_views.py`·`layout_views.py` 통과 |
| P3. 지표 출처·시점 | `get_fundamentals`의 `source`·`fetched_at`(·`quarter_ends`, Q2 결과에 따라), 출처 줄·고지 | 클라우드 | 10.1의 8·9·12 |
| P4. 문서·실스택 종단 확인 | README 목표 흐름 표·체크박스 갱신(AI 작성 표시), `compose.portfolio.yml` 실스택에서 종목 검색 → ☆ → 새로 고침 뒤 남아 있음 → (b) 근거 모드 회사 선택 확인 → (c) 일지 거르기 → 출처 줄 시각 확인 한 바퀴 | 리드 로컬(Yahoo·근거 모드 적재 데이터 필요). 문서 수정은 클라우드 가능 | 한 바퀴 결과를 PR에 적고 10.1의 12 통과 |

- P1이 끝나야 P2를 시작한다. **P3은 P1·P2와 독립**이라 나란히 할 수 있다(관심종목 표를 쓰지 않는다).
- 어느 단계도 클라우드에서 Yahoo·JEV·OpenDART·Ollama를 부르지 않는다(지표 테스트는 가짜 httpx).

## 12. 열린 질문 (리드 결정을 바꾸지 않고 남긴 반론·확인 사항)

- **Q1. 플래그 없이 가도 되나?** 리드 결정은 "플래그 없음"이고 이 spec도 그대로 따랐다(결정 5-5). 반론 근거: 근거 모드·일지는 모두 기본 꺼짐 플래그로 들어왔고, 플래그가 있으면 0011 미적용 환경이나 P2 화면이 덜 된 상태에서 메뉴를 숨길 수 있다. 다만 관심종목 표는 다른 API가 조회하지 않고 실스택은 시작 시 마이그레이션을 하므로 위험이 작다고 본다. 리드가 그대로 가면 닫는다.
- **Q2. 분기 끝 날짜(`quarter_ends`)를 더할까?** 리드 전제("이미 있음")와 달리 지금은 `"24Q1"` 라벨만 있다(2.1). 추가 비용은 같은 반복문 한 줄이고 외부 호출이 없다. 라벨로 충분하면 빼고 출처 줄만 둔다.
- **Q3. 메모 수정 `PATCH`를 둘까?** 리드가 정한 API는 추가·삭제·목록이다. 별 버튼은 메모 없이 추가하므로 수정 경로가 없으면 메모 칸을 쓸 길이 "지우고 다시 추가"뿐이라 AI가 메모 전용 `PATCH`를 더했다. 빼면 메모는 패널의 추가 양식에서만 쓴다.
- **Q4. 오래 열어 둔 탭의 브라우저 캐시.** `companyFundCache`는 세션 동안 유지되어, 탭을 6시간 넘게 열어 두면 "최대 6시간 지난 값"보다 오래된 값이 보일 수 있다(시각 표시는 정확하다). 뷰를 다시 열 때 `fetched_at`이 6시간 지났으면 다시 받는 처리를 P3에 넣을지 정한다(추천: 넣는다, 코드 몇 줄).
- **Q5. 목업 종목 탭(`COMPANIES` 5개)을 관심종목으로 바꿀까?** 로그인 사용자에게는 관심종목이 더 쓸모 있지만, 비교·섹터 화면이 같은 목업을 쓴다(`company.js:14-50`). 이번에는 두고 패널을 따로 둔다.
- **Q6. 일지 개수 요약 경로.** (c)는 줄마다 요청 2개다(결정 7-2). 근거 모드 적재 회사가 많아지면 `GET /api/journal/summary?corp_code=…`(기록 수·due 수 묶음) 같은 일지 쪽 경로를 더할지 — 이번 범위에서는 만들지 않는다.

## 13. 결정 기록

| # | 결정 | 누가 |
|---|---|---|
| 1 | 관심종목 서버 저장, 0011 표, 열 구성, (user_id, symbol) 유일, 100개 상한, `get_current_user_any`, 플래그 없음 | 리드(노아) 결정을 AI가 옮김 |
| 2 | 연결 (a)(b)(c), 꺼진 기능 버튼 숨김 | 리드 결정을 AI가 옮김 |
| 3 | corp_code 매핑 규칙, 실패·해외는 null | 리드 결정을 AI가 옮김 |
| 4 | `source`·`fetched_at`, 출처 줄 문구 | 리드 결정을 AI가 옮김 |
| 5 | 범위 밖, 메모 비전송 | 리드 결정을 AI가 옮김 |
| 6 | P1~P4 단계, 테스트 계획 항목 | 리드 결정을 AI가 옮김 |
| 7 | 중복 409(`code`·`item_id`), 메모 없는 422, 감사 로그 없음, 입력 검증 규칙, `market` 서버 계산 | AI(기존 일지 패턴에 맞춤) |
| 8 | 목록 조회 때 null corp_code 나중에 채우기 | AI(근거 모드 꺼짐 동안 추가한 종목 대응) |
| 9 | 옛 캐시 행은 미스 처리, `quarter_ends` 추천 | AI(리드 전제 정정 포함, Q2) |
| 10 | 메모 `PATCH` 추가 | AI(Q3) |
| 11 | 패널 위치(기업 지표 화면 맨 위), 목업 탭 유지, 켜짐 판단은 기존 탐지 재사용, (c) 개수는 `total` 두 번 | AI(코드 사실에 맞춤, Q5·Q6) |

이 문서는 AI(Claude Code)가 작성했다. 결정 1~6은 리드가 내린 결정이고, 7~11은 AI가 채운 세부로 리드 검토 대상이다.
